"""Owner-local, audited project access mode changes.

Never expose this class through OAuth MCP or the public localhost HTTP API.
Only the native Qt application invokes it after an explicit local confirmation.
The current bridge is restarted after the private config update, otherwise the
old in-memory project registry would continue to authorize the former scope.
"""
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

MODES = ("off", "read", "write")
SERVICE = "codex-local-bridge.service"


def project_mode(p):
    if p.get("enabled") is False:
        return "off"
    if (p.get("file_write_enabled") is True
            and p.get("project_authorization") is True
            and "write_test" in p.get("allowed_modes", [])):
        return "write"
    return "read"


def private_replace(path, data):
    fd, temp = tempfile.mkstemp(prefix=".bridge-project-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class ProjectModes:
    def __init__(self, config_path, restart=None, sandbox=None):
        self.path = Path(config_path).expanduser().absolute()
        self.restart = restart or self._restart
        # An injected restart is used by isolated unit tests, never the real host.
        from .project_sandbox import SystemdProjectSandbox
        self.sandbox = sandbox if sandbox is not None else (SystemdProjectSandbox() if restart is None else None)

    def _restart(self):
        if self.sandbox is not None:
            self.sandbox.reload()
        for argv in (["systemctl", "--user", "restart", SERVICE],
                     ["systemctl", "--user", "is-active", "--quiet", SERVICE]):
            subprocess.run(argv, check=True, timeout=25)

    def read(self):
        if self.path.is_symlink():
            raise PermissionError("Bridge config must not be a symlink")
        info = self.path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError("Bridge config must be owner-only 0600")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def change(self, project_id, mode, *, confirmed=False):
        if not isinstance(project_id, str) or not project_id or len(project_id) > 64:
            raise ValueError("Invalid project id")
        if mode not in MODES or not confirmed:
            raise PermissionError("Explicit owner confirmation required")
        lock_path = self.path.with_name(self.path.name + ".mode.lock")
        if lock_path.is_symlink():
            raise PermissionError("Unsafe lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise PermissionError("Unsafe lock owner")
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = self.path.read_bytes()
            data = self.read()
            found = next((p for p in data.get("projects", []) if p.get("id") == project_id), None)
            if found is None:
                raise ValueError("Unknown project")
            old = project_mode(found)
            if old == mode:
                return {"project_id": project_id, "mode": old, "changed": False}
            if mode == "write" and not found.get("tests"):
                raise ValueError("写入权限需要先在配置中设定沙箱测试命令")
            # Failing closed: read or off MUST drop both per-project write switches.
            found["enabled"] = mode != "off"
            found["allowed_modes"] = ["read", "write_test"] if mode == "write" else (["read"] if mode == "read" else [])
            found["file_write_enabled"] = mode == "write"
            found["project_authorization"] = mode == "write"
            changed = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            if self.path.read_bytes() != raw:
                raise RuntimeError("Concurrent config change, retry")
            # A private pre-change backup, kept out of the source project/repository.
            base = Path(data["state_dir"]).expanduser().resolve()
            backups = base / "permission-backups"
            if not backups.exists():
                backups.mkdir(parents=True, mode=0o700)
            if backups.is_symlink() or backups.stat().st_mode & 0o077:
                raise PermissionError("Unsafe backup directory")
            import uuid
            backup = backups / ("before-" + uuid.uuid4().hex + ".json")
            bfd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(bfd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            # Managed, project-specific systemd ReadWritePaths allowlist.
            # It must be updated atomically alongside config and rolled back on failure.
            old_sandbox = self.sandbox.snapshot() if self.sandbox else None
            try:
                if self.sandbox:
                    self.sandbox.apply(data["projects"])
                private_replace(self.path, changed)
                self.restart()
            except Exception:
                # Do not claim the change took effect if the old service remains running.
                if self.path.read_bytes() == changed:
                    private_replace(self.path, raw)
                if self.sandbox:
                    self.sandbox.restore(old_sandbox)
                try:
                    self.restart()
                except Exception:
                    pass
                raise
            # Existing grants MUST be revoked immediately on demotion/off;
            # otherwise re-enabling with the same scope could resurrect access.
            from .core import Bridge
            try:
                if mode != "write":
                    # The new effective settings disable this project; construct
                    # against the previous config so revocation can still locate it.
                    Bridge(json.loads(raw)).authorization.revoke(project_id)
                else:
                    Bridge(data).authorization.grant(project_id)
            except Exception:
                # Never retain a silently elevated config after a grant error.
                if self.path.read_bytes() == changed:
                    private_replace(self.path, raw)
                if self.sandbox:
                    self.sandbox.restore(old_sandbox)
                try:
                    self.restart()
                except Exception:
                    pass
                raise
            return {"project_id": project_id, "mode": mode, "previous": old,
                    "changed": True, "backup": str(backup)}
        finally:
            os.close(fd)
