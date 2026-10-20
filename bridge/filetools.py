"""Add project-scoped text-file tools to an existing MCP server.

Zero additional cloud services. Linux/POSIX only; tested on Python 3.11.
Writes use explicit project grants or individual local approval.
The caller must provide trusted project policies resolved from the existing bridge.
"""
from __future__ import annotations

import json
import fnmatch
import difflib
import hashlib
import re
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Mapping
from .responses import FileReadResult, FileListResult, FileWriteResult, FileWriteStatus

MAX_BYTES = 128 * 1024
MAX_LIST = 200
TTL_SECONDS = 24 * 3600
BLOCKED_COMPONENTS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".ssh", ".aws", ".config", ".gnupg"}
BLOCKED_NAMES = {"id_rsa", "id_ed25519", "authorized_keys", "known_hosts", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml"}
BLOCKED_SUFFIXES = (".pem", ".p12", ".pfx", ".key", "_secrets.json", "_credentials.json")


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectPolicy:
    root: Path
    include: tuple[str, ...] = ("*.py", "*.md", "*.txt")
    allow_write: bool = False


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Defense in depth for common accidentally committed credentials. Not a complete
# DLP system; do not grant access to projects containing sensitive data.
_INLINE_KEYS = re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b")
_ASSIGNMENT_KEYS = re.compile(r"(?im)^([^\n]*(?:api_key|auth_token|access_token|password|client_secret|private_key)[^\n]*?\s*[:=]\s*)([^\r\n]+)")


def _redact(text: str) -> tuple[str, bool]:
    cleaned = _INLINE_KEYS.sub("[REDACTED_KEY]", text)
    cleaned = _ASSIGNMENT_KEYS.sub(lambda m: m.group(1) + "[REDACTED_VALUE]", cleaned)
    return cleaned, cleaned != text


def _check_path(relative_path: str, allow_root: bool = False) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or "\x00" in relative_path or "\\" in relative_path:
        raise WorkspaceError("Invalid path")
    if relative_path in ("", "."):
        if allow_root:
            return ()
        raise WorkspaceError("A file path is required")
    if relative_path.startswith("/") or relative_path.endswith("/"):
        raise WorkspaceError("Absolute paths and trailing slash are not permitted")
    bits = relative_path.split("/")
    if any(b in ("", ".", "..") for b in bits):
        raise WorkspaceError("Traversal or non-normalized path")
    for bit in bits:
        lower = bit.lower()
        if bit.startswith(".") or lower in BLOCKED_COMPONENTS or lower in BLOCKED_NAMES or lower.endswith(BLOCKED_SUFFIXES):
            raise WorkspaceError("Hidden/sensitive paths are not exposed")
    return tuple(bits)


def _safe_open_parent(root: Path, bits: tuple[str, ...]):
    """Open verified directory FDs without following any user-controlled symlinks."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for component in bits[:-1]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_from_parent(pfd: int, name: str) -> tuple[bytes | None, int | None]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=pfd)
    except FileNotFoundError:
        return None, None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode): raise WorkspaceError("Only regular files are allowed")
        if info.st_nlink != 1: raise WorkspaceError("Hard-linked files are not exposed")
        if info.st_size > MAX_BYTES: raise WorkspaceError("File exceeds the read limit")
        data=bytearray()
        while len(data)<=MAX_BYTES:
            chunk=os.read(fd,min(8192,MAX_BYTES+1-len(data)))
            if not chunk: break
            data.extend(chunk)
        if len(data)>MAX_BYTES: raise WorkspaceError("File exceeds the read limit")
        return bytes(data),stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def _read_at(root: Path, bits: tuple[str, ...]) -> tuple[bytes | None, int | None]:
    try: pfd=_safe_open_parent(root,bits)
    except FileNotFoundError: return None, None
    try: return _read_from_parent(pfd,bits[-1])
    finally: os.close(pfd)


class WorkspaceBridge:
    def __init__(self, projects: Mapping[str, ProjectPolicy], state_dir: Path, authorization=None):
        self.authorization=authorization
        if not projects:
            raise WorkspaceError("Explicit project allowlist required")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.projects: dict[str, ProjectPolicy] = {}
        for project_id, policy in projects.items():
            if not project_id or not isinstance(policy, ProjectPolicy):
                raise WorkspaceError("Invalid project policy")
            root = Path(policy.root).expanduser()
            if root.is_symlink() or not root.is_dir():
                raise WorkspaceError(f"Project root not a real directory: {project_id}")
            resolved=root.resolve(strict=True)
            if resolved in (Path('/'), Path.home()) or self.state_dir.is_relative_to(resolved):
                raise WorkspaceError('Authorize a narrow project outside bridge state')
            if not policy.include or not all(isinstance(x,str) and x for x in policy.include):
                raise WorkspaceError('Nonempty include patterns required')
            self.projects[project_id] = ProjectPolicy(root.resolve(strict=True), tuple(policy.include), policy.allow_write)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.backup_dir = self.state_dir / "backups"
        self.backup_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.backup_dir, 0o700)
        self.db_path = self.state_dir / "workspace.sqlite3"
        previous_umask = os.umask(0o077)
        try:
            with self._db() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS proposals (
                      id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                      path TEXT NOT NULL, expected_sha TEXT NOT NULL,
                      new_content BLOB NOT NULL, status TEXT NOT NULL,
                      created_at INTEGER NOT NULL, applied_at INTEGER,
                      backup_path TEXT);
                    CREATE TABLE IF NOT EXISTS audit (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      proposal_id TEXT NOT NULL, event TEXT NOT NULL,
                      ts INTEGER NOT NULL, detail TEXT NOT NULL);
                """)
        finally:
            os.umask(previous_umask)
        with self._db() as db:
            columns={r['name'] for r in db.execute('PRAGMA table_info(proposals)')}
            if 'policy_digest' not in columns:
                db.execute('ALTER TABLE proposals ADD COLUMN policy_digest TEXT')
        os.chmod(self.db_path, 0o600)

    def _policy_digest(self, project_id):
        p=self.projects.get(project_id)
        if p is None: raise WorkspaceError('Project not authorized')
        return _sha(json.dumps({'root':str(p.root),'include':p.include,'write':p.allow_write},sort_keys=True).encode())

    def _check_proposal(self, row):
        if row['status'] != 'pending': raise WorkspaceError('Proposal is not pending')
        if time.time()-row['created_at']>TTL_SECONDS: raise WorkspaceError('Proposal expired')
        if row['policy_digest'] != self._policy_digest(row['project_id']):
            raise WorkspaceError('Project policy changed; re-propose for the new scope')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def _policy(self, project_id: str, relative_path: str, write: bool = False):
        policy = self.projects.get(project_id)
        if policy is None:
            raise WorkspaceError("Project not authorized")
        if write and not policy.allow_write:
            raise WorkspaceError("Write is disabled for this project")
        bits = _check_path(relative_path)
        path = "/".join(bits)
        if not any(fnmatch.fnmatchcase(path, x) for x in policy.include):
            raise WorkspaceError("File does not match project allowlist")
        return policy, bits

    def list_files(self, project_id: str, relative_dir: str = ".") -> dict:
        policy = self.projects.get(project_id)
        if policy is None:
            raise WorkspaceError("Project not authorized")
        bits = _check_path(relative_dir, allow_root=True)
        # Directory traversal uses no-follow checks; only regular files are emitted.
        fd = _safe_open_parent(policy.root, bits + ("__placeholder__",))
        try:
            results = []
            for name in sorted(os.listdir(fd)):
                try:
                    child_bits = bits + (name,)
                    _check_path("/".join(child_bits))
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                        rel = "/".join(child_bits)
                        if any(fnmatch.fnmatchcase(rel, p) for p in policy.include):
                            results.append({"path": rel, "bytes": st.st_size})
                    elif stat.S_ISDIR(st.st_mode):
                        results.append({"directory": "/".join(child_bits)})
                except (WorkspaceError, OSError):
                    pass
                if len(results) >= MAX_LIST:
                    break
            return {"project_id": project_id, "directory": relative_dir, "entries": results, "limit": MAX_LIST}
        finally:
            os.close(fd)

    def read_file(self, project_id: str, relative_path: str) -> dict:
        policy, bits = self._policy(project_id, relative_path)
        data, _ = _read_at(policy.root, bits)
        if data is None:
            raise WorkspaceError("File does not exist")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("Only UTF-8 text files are supported") from exc
        if "\x00" in content:
            raise WorkspaceError("Binary files are not supported")
        visible, redacted = _redact(content)
        return {"project_id": project_id, "path": relative_path, "sha256": _sha(data), "bytes": len(data), "content": visible, "redacted": redacted}

    def _propose(self, project_id: str, relative_path: str, new_content: str, expected_sha256: str) -> dict:
        initial_grant=self.authorization.active(project_id) if self.authorization else None
        policy, bits = self._policy(project_id, relative_path, write=True)
        if not isinstance(new_content, str) or "\x00" in new_content:
            raise WorkspaceError("Only UTF-8 text is allowed")
        data = new_content.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise WorkspaceError("Proposed file is too large")
        old, _ = _read_at(policy.root, bits)
        old_sha = _sha(old) if old is not None else "ABSENT"
        if expected_sha256 != old_sha:
            raise WorkspaceError("File changed or expected SHA-256 incorrect; read again")
        if old == data:
            raise WorkspaceError("No change to propose")
        if old is not None and b'\x00' in old: raise WorkspaceError('Binary target is not supported')
        oldtext = "" if old is None else old.decode("utf-8")
        diff = "".join(difflib.unified_diff(oldtext.splitlines(keepends=True), new_content.splitlines(keepends=True),
                 fromfile=f"a/{relative_path}", tofile=f"b/{relative_path}"))
        visible_diff, redacted = _redact(diff)
        pid = secrets.token_hex(16)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT count(*) FROM proposals WHERE status='pending' AND created_at>?",(int(time.time())-TTL_SECONDS,)).fetchone()[0]>=100:
                raise WorkspaceError('Pending proposal limit reached')
            db.execute("INSERT INTO proposals (id, project_id, path, expected_sha, new_content, status, created_at) VALUES (?,?,?,?,?,?,?)",
                       (pid, project_id, relative_path, old_sha, data, "pending", int(time.time())))
            db.execute('UPDATE proposals SET policy_digest=? WHERE id=?',(self._policy_digest(project_id),pid))
            db.execute("INSERT INTO audit (proposal_id,event,ts,detail) VALUES (?,?,?,?)",
                       (pid, "proposed", int(time.time()), "Queued by MCP tool"))
        response = {"proposal_id": pid, "status": "pending_local_approval", "project_id": project_id,
                "path": relative_path, "old_sha256": old_sha, "new_sha256": _sha(data), "diff": visible_diff[:32000], "diff_redacted": redacted, "diff_truncated": len(visible_diff) > 32000,
                "next_step": "~/.local/bin/codex-local-bridge approve-file " + pid}
        if self.authorization:
            from .policy import ordinary_path
            with self.authorization.locked():
                grant=self.authorization.active(project_id,initial_grant['id']) if initial_grant else None
                if grant and ordinary_path(relative_path) and not _redact(new_content)[1] and not _redact(oldtext)[1]:
                    self.approve_and_apply(pid)
                    response.update(status='applied',authorization='project_grant',grant_id=grant['id'])
                    response.pop('next_step',None)
                    self.authorization.store.audit('file_applied_automatically',{'project':project_id,'proposal':pid,'grant_id':grant['id']})
        return response

    def request_write_file(self, project_id: str, relative_path: str, new_content: str, expected_sha256: str) -> dict:
        """Queue a full-text create/replace; does NOT write unless locally approved."""
        return self._propose(project_id, relative_path, new_content, expected_sha256)

    def request_edit_file(self, project_id: str, relative_path: str, old_text: str, new_text: str, expected_sha256: str) -> dict:
        """Queue replacement of exactly one occurrence; does NOT write yet."""
        policy, bits = self._policy(project_id, relative_path, write=True)
        raw, _ = _read_at(policy.root, bits)
        if raw is None:
            raise WorkspaceError("File does not exist")
        if expected_sha256 != _sha(raw):
            raise WorkspaceError("File changed; read again")
        try:
            original = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("Only UTF-8 text files are supported") from exc
        if not old_text or original.count(old_text) != 1:
            raise WorkspaceError("old_text must appear exactly once")
        content = original.replace(old_text, new_text, 1)
        return self._propose(project_id, relative_path, content, expected_sha256)

    def review_proposal_local(self, proposal_id: str) -> dict:
        """Read full proposed change for local terminal only. NEVER expose as MCP."""
        with self._db() as db:
            row = db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            raise WorkspaceError("Unknown proposal")
        self._check_proposal(row)
        policy, bits = self._policy(row["project_id"], row["path"], write=True)
        old, _ = _read_at(policy.root, bits)
        old_sha = _sha(old) if old is not None else "ABSENT"
        if old_sha != row["expected_sha"]:
            raise WorkspaceError("File changed since proposal; review and propose again")
        before = "" if old is None else old.decode("utf-8")
        after = bytes(row["new_content"]).decode("utf-8")
        diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                    fromfile=f"a/{row['path']}", tofile=f"b/{row['path']}"))
        return {"project_id": row["project_id"], "path": row["path"], "status": row["status"],
                "proposal_id": proposal_id, "new_sha256": _sha(bytes(row["new_content"])), "diff": diff}

    def get_write_status(self, proposal_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT id,project_id,path,status,created_at,applied_at,backup_path FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            raise WorkspaceError("Unknown proposal")
        self._policy(row['project_id'],row['path'])
        result=dict(row)
        result.pop('backup_path',None)
        if row['status']=='pending' and time.time()-row['created_at']>TTL_SECONDS:
            result['status']='expired'
        return result

    def _save_backup(self, proposal_id: str, data: bytes) -> str:
        path = self.backup_dir / (proposal_id + ".bak")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return str(path)

    def approve_and_apply(self, proposal_id: str) -> dict:
        """Local CLI ONLY. Do not register this method as an MCP tool."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise WorkspaceError("No pending proposal with that ID")
            if time.time() - row["created_at"] > TTL_SECONDS:
                db.execute("UPDATE proposals SET status='expired' WHERE id=?", (proposal_id,))
                db.commit()
                raise WorkspaceError("Proposal expired")
            self._check_proposal(row)
            policy, bits = self._policy(row["project_id"], row["path"], write=True)
            from .writeback import open_parent
            pfd = open_parent(policy.root, bits)
            tmp_name = ".bridge-write-" + secrets.token_hex(12)
            try:
                old, mode = _read_from_parent(pfd, bits[-1])
                current_sha = _sha(old) if old is not None else "ABSENT"
                if current_sha != row["expected_sha"]:
                    raise WorkspaceError("File changed since proposal; re-propose instead of overwriting")
                # Reject symlink and non-regular destination, including dangling symlinks.
                try:
                    info = os.stat(bits[-1], dir_fd=pfd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise WorkspaceError("Target must be a regular file")
                except FileNotFoundError:
                    pass
                backup_path = self._save_backup(proposal_id, old) if old is not None else None
                fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(bytes(row["new_content"]))
                        fh.flush()
                        os.fsync(fh.fileno())
                        os.fchmod(fh.fileno(), (mode & 0o755) if mode is not None else 0o600)
                    # no shell; destination validated under an already opened project dir FD
                    os.replace(tmp_name, bits[-1], src_dir_fd=pfd, dst_dir_fd=pfd)
                    os.fsync(pfd)
                finally:
                    try:
                        os.unlink(tmp_name, dir_fd=pfd)
                    except FileNotFoundError:
                        pass
                db.execute("UPDATE proposals SET status='applied', applied_at=?, backup_path=? WHERE id=?",
                           (int(time.time()), backup_path, proposal_id))
                db.execute("INSERT INTO audit (proposal_id,event,ts,detail) VALUES (?,?,?,?)",
                           (proposal_id, "applied", int(time.time()), "Applied through local approval or validated project grant"))
                db.commit()
                return {"status": "applied", "proposal_id": proposal_id, "backup": backup_path}
            finally:
                os.close(pfd)


def register_file_tools(mcp, bridge: WorkspaceBridge):
    """Register new tools with the *existing* FastMCP instance; preserves old tools.

    Never expose approve_and_apply to the network.
    Existing HTTP transport/OAuth middleware must remain configured.
    """
    from mcp.types import ToolAnnotations
    read_annotation = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write_annotation = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @mcp.tool(annotations=read_annotation, structured_output=True)
    def list_project_files(project_id: str, relative_dir: str = ".") -> FileListResult:
        """List authorized local files or directories (one level at a time)."""
        return FileListResult.model_validate(bridge.list_files(project_id, relative_dir))

    @mcp.tool(annotations=read_annotation, structured_output=True)
    def read_project_file(project_id: str, relative_path: str) -> FileReadResult:
        """Read authorized UTF-8 text and its SHA-256 for conflict-safe editing."""
        return FileReadResult.model_validate(bridge.read_file(project_id, relative_path))

    @mcp.tool(annotations=write_annotation, structured_output=True)
    def request_write_file(project_id: str, relative_path: str, new_content: str, expected_sha256: str) -> FileWriteResult:
        """Create/replace UTF-8 text with SHA conflict checks. Active project grants auto-apply ordinary code; risky changes need local approval. Use ABSENT to create."""
        return FileWriteResult.model_validate(bridge.request_write_file(project_id, relative_path, new_content, expected_sha256))

    @mcp.tool(annotations=write_annotation, structured_output=True)
    def request_edit_file(project_id: str, relative_path: str, old_text: str, new_text: str, expected_sha256: str) -> FileWriteResult:
        """Replace one exact text span with SHA conflict checks. Active project grants auto-apply ordinary code; risky changes need local approval."""
        return FileWriteResult.model_validate(bridge.request_edit_file(project_id, relative_path, old_text, new_text, expected_sha256))

    @mcp.tool(annotations=read_annotation, structured_output=True)
    def get_write_status(proposal_id: str) -> FileWriteStatus:
        """Get the status of a local file change proposal."""
        return FileWriteStatus.model_validate(bridge.get_write_status(proposal_id))

    return bridge


def create_file_bridge(existing_bridge):
    """Use the existing Bridge project's authoritative root/include settings."""
    if any(type(p.get('file_write_enabled', False)) is not bool for p in existing_bridge.projects.values()):
        raise WorkspaceError('file_write_enabled must be a JSON boolean')
    projects={p['id']: ProjectPolicy(Path(p['root']),tuple(p['include']),
              bool(p.get('file_write_enabled', False))) for p in existing_bridge.projects.values()}
    state_dir=existing_bridge.store.directory / 'filetools'
    return WorkspaceBridge(projects, state_dir, existing_bridge.authorization)
