"""Owner-controlled project automatic memory preferences.

A toggle affects future Bridge task capture/context, never collects complete
ChatGPT conversations or VS Code transcripts. It is local only, requires
confirmation, makes an owner-private config backup and restarts Bridge so the
long-running worker uses the new configuration.
"""
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

from .project_modes import private_replace


class AutoMemoryPreferences:
    def __init__(self, config_path, restart=None):
        self.path = Path(config_path).expanduser().absolute()
        self.restart = restart or self._restart

    @staticmethod
    def _restart():
        for argv in (
            ["systemctl", "--user", "restart", "codex-local-bridge.service"],
            ["systemctl", "--user", "is-active", "--quiet", "codex-local-bridge.service"],
        ):
            subprocess.run(argv, check=True, timeout=25, stdin=subprocess.DEVNULL,
                           capture_output=True)

    def read(self, project_id):
        data = self._config()
        item = next((p for p in data["projects"] if p.get("id") == project_id), None)
        if item is None:
            raise ValueError("Unknown project")
        return item.get("auto_memory", True) is not False

    def _config(self):
        if self.path.is_symlink():
            raise PermissionError("Unsafe Bridge config")
        st = self.path.stat()
        if (st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode)
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise PermissionError("Config must be current-user 0600")
        obj = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(obj.get("projects"), list):
            raise ValueError("Invalid Bridge config")
        return obj

    def set(self, project_id, enabled, *, confirmed=False):
        if confirmed is not True or type(enabled) is not bool:
            raise PermissionError("Local confirmation and a boolean preference are required")
        if not isinstance(project_id, str) or not project_id or len(project_id) > 64:
            raise ValueError("Invalid project ID")
        lockpath = self.path.with_name(self.path.name + ".mode.lock")
        if lockpath.is_symlink():
            raise PermissionError("Unsafe preferences lock")
        fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise PermissionError("Preferences lock must be private")
            fcntl.flock(fd, fcntl.LOCK_EX)
            old = self.path.read_bytes()
            config = self._config()
            project = next((p for p in config["projects"] if p.get("id") == project_id), None)
            if project is None:
                raise ValueError("Unknown project")
            previous = project.get("auto_memory", True) is not False
            if previous == enabled:
                return {"project_id": project_id, "enabled": enabled, "changed": False}
            project["auto_memory"] = enabled
            updated = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            state = Path(config["state_dir"]).expanduser().absolute()
            backups = state / "memory-preference-backups"
            if backups.is_symlink():
                raise PermissionError("Unsafe backup directory")
            backups.mkdir(parents=True, mode=0o700, exist_ok=True)
            si = backups.stat()
            if (si.st_uid != os.getuid() or not stat.S_ISDIR(si.st_mode)
                    or stat.S_IMODE(si.st_mode) != 0o700):
                raise PermissionError("Backup folder is not owner-private")
            backup = backups / ("config-" + uuid.uuid4().hex + ".json")
            bd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(bd, "wb") as target:
                target.write(old)
                target.flush()
                os.fsync(target.fileno())
            if self.path.read_bytes() != old:
                raise RuntimeError("Config changed concurrently")
            try:
                private_replace(self.path, updated)
                self.restart()
            except Exception:
                if self.path.read_bytes() == updated:
                    private_replace(self.path, old)
                try:
                    self.restart()
                except Exception:
                    pass
                raise
            return {"project_id": project_id, "enabled": enabled,
                    "changed": True, "backup": str(backup)}
        finally:
            os.close(fd)
