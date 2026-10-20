"""Owner-private consistent backups for Bridge Studio and Codex MCP.

Backups are inside the existing private state directory and never contain
Git repositories or user project files. SQLite online backup API correctly
handles active WAL databases. Neither OAuth tokens nor note bodies are
returned in metadata or diagnostic logs.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import uuid

NAMES = ("config.json", "bridge.sqlite3", "studio.sqlite3", "service.unit")
ID = re.compile(r"^[a-f0-9]{32}$")


def _private(path, *, directory=False):
    p = Path(path)
    if p.is_symlink():
        raise PermissionError("Refusing a symlink in private backup paths")
    info = p.stat()
    if (info.st_uid != os.getuid() or
        (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)) or
        stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600) or
        (not directory and info.st_nlink != 1)):
        raise PermissionError("Bridge private file ownership or mode is unsafe")
    return p


def _new_file(path, data):
    p = Path(path)
    if p.exists() or p.is_symlink():
        raise FileExistsError("Backup file already exists")
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    _private(p)


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as inp:
        for chunk in iter(lambda: inp.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _db_backup(source, destination):
    source = _private(source)
    p = Path(destination)
    if p.exists() or p.is_symlink():
        raise FileExistsError("DB snapshot already exists")
    # Mode 0600 from the first creation; sqlite backup includes current WAL.
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10) as src:
            with sqlite3.connect(str(p), timeout=10) as dst:
                src.backup(dst, pages=128, sleep=0.05)
        _private(p)
        with sqlite3.connect(str(p)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite snapshot integrity check failed")
    except Exception:
        p.unlink(missing_ok=True)
        raise


def _replace(path, data):
    p = Path(path)
    if p.is_symlink():
        raise PermissionError("Refusing to overwrite a symlink")
    if p.exists():
        _private(p)
    fd, temp = tempfile.mkstemp(prefix=".bridge-restore-", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, p)
        d = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        Path(temp).unlink(missing_ok=True)


class StateSnapshots:
    def __init__(self, config_path, unit_path=None):
        self.config = Path(config_path).expanduser().absolute()
        _private(self.config)
        settings = json.loads(self.config.read_text(encoding="utf-8"))
        self.state = Path(settings["state_dir"]).expanduser().absolute()
        _private(self.state, directory=True)
        self.root = self.state / "studio-snapshots"
        self.unit = Path(unit_path or
            Path.home() / ".config/systemd/user/codex-local-bridge.service").expanduser().absolute()

    def _root(self, create=False):
        if create and not self.root.exists():
            self.root.mkdir(mode=0o700)
        if self.root.exists():
            _private(self.root, directory=True)

    def create(self):
        self._root(create=True)
        token = uuid.uuid4().hex
        directory = self.root / token
        directory.mkdir(mode=0o700)
        files = {}
        try:
            candidates = [(self.config, "config.json", False),
                          (self.state / "bridge.sqlite3", "bridge.sqlite3", True),
                          (self.state / "studio.sqlite3", "studio.sqlite3", True),
                          (self.unit, "service.unit", False)]
            for source, name, is_db in candidates:
                if not source.exists():
                    if source.is_symlink():
                        raise PermissionError("Unexpected symlink in backup inputs")
                    continue
                _private(source)
                out = directory / name
                if is_db:
                    _db_backup(source, out)
                else:
                    _new_file(out, source.read_bytes())
                files[name] = {"sha256": _digest(out), "size": out.stat().st_size}
            manifest = {"format": 1, "id": token, "created_at": int(time.time()),
                        "files": files}
            _new_file(directory / "manifest.json",
                      (json.dumps(manifest, sort_keys=True) + "\n").encode())
            return {"id": token, "created_at": manifest["created_at"],
                    "files": list(files), "verified": True}
        except Exception:
            # Do not leave half-created snapshots marked as successful.
            for child in directory.iterdir():
                if child.is_file() and not child.is_symlink():
                    child.unlink()
            directory.rmdir()
            raise

    def verify(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not ID.fullmatch(snapshot_id):
            raise ValueError("Invalid snapshot ID")
        self._root()
        directory = self.root / snapshot_id
        _private(directory, directory=True)
        meta = directory / "manifest.json"
        _private(meta)
        manifest = json.loads(meta.read_text(encoding="utf-8"))
        files = manifest.get("files")
        if manifest.get("format") != 1 or manifest.get("id") != snapshot_id or not isinstance(files, dict):
            raise ValueError("Unsupported or corrupted snapshot manifest")
        if "config.json" not in files or set(files).difference(NAMES):
            raise ValueError("Incomplete snapshot")
        for name, info in files.items():
            path = _private(directory / name)
            if (not isinstance(info, dict) or _digest(path) != info.get("sha256")
                    or path.stat().st_size != info.get("size")):
                raise ValueError("Backup digest mismatch")
            if name.endswith(".sqlite3"):
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Backup SQLite integrity mismatch")
        return manifest

    def list(self):
        self._root()
        if not self.root.exists():
            return []
        result = []
        for folder in self.root.iterdir():
            if folder.is_dir() and ID.fullmatch(folder.name):
                try:
                    v = self.verify(folder.name)
                    result.append({"id": v["id"], "created_at": v["created_at"],
                                   "files": list(v["files"]), "verified": True})
                except (ValueError, PermissionError, OSError, sqlite3.Error):
                    result.append({"id": folder.name, "verified": False})
        return sorted(result, key=lambda x: x.get("created_at", 0), reverse=True)

    def restore(self, snapshot_id, *, confirmed=False, service=None):
        if confirmed is not True:
            raise PermissionError("Local owner confirmation required to restore data")
        manifest = self.verify(snapshot_id)
        if service is None:
            raise ValueError("A controlled stopped-service context is required")
        # Emergency snapshot is kept even if restoration fails.
        rescue = self.create()
        directory = self.root / snapshot_id
        try:
            service.stop()
            for name in ("bridge.sqlite3", "studio.sqlite3"):
                if name not in manifest["files"]:
                    continue
                dst = self.state / name
                if dst.exists():
                    _private(dst)
                else:
                    _new_file(dst, b"")
                with sqlite3.connect(str(directory / name)) as source:
                    with sqlite3.connect(str(dst)) as target:
                        source.backup(target, pages=128)
            _replace(self.config, (directory / "config.json").read_bytes())
            if "service.unit" in manifest["files"]:
                _replace(self.unit, (directory / "service.unit").read_bytes())
            service.restart_verify()
        except Exception:
            # Restore the immediately previous state if the desired restore fails.
            try:
                service.stop()
                emergency = self.root / rescue["id"]
                for name in ("bridge.sqlite3", "studio.sqlite3"):
                    if (emergency / name).exists():
                        with sqlite3.connect(str(emergency / name)) as src:
                            with sqlite3.connect(str(self.state / name)) as dst:
                                src.backup(dst)
                _replace(self.config, (emergency / "config.json").read_bytes())
                if (emergency / "service.unit").exists():
                    _replace(self.unit, (emergency / "service.unit").read_bytes())
                service.restart_verify()
            except Exception as recovery:
                raise RuntimeError("Restore failed AND automatic recovery failed; use rescue snapshot " + rescue["id"]) from recovery
            raise
        return {"restored": snapshot_id, "rescue": rescue["id"], "verified": True}
