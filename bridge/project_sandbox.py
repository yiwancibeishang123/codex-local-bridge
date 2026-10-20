"""Managed systemd write allowlist for native, owner-confirmed project grants.

Never widen the service to all of /home or execute commands through a shell.
Only a private, distinct managed drop-in is touched; existing administrator
drop-ins are left alone. Removing it returns to the service's base sandbox.
"""
import os
from pathlib import Path
import stat
import subprocess
import tempfile

UNIT = "codex-local-bridge.service"
HEADER = "# Managed by Bridge Studio project permissions. Local owner only."

def safe_root(path, home=None):
    home = Path(home or Path.home()).resolve()
    actual = Path(path).expanduser().resolve(strict=True)
    if not actual.is_dir() or actual == home or actual == Path("/") or not actual.is_relative_to(home):
        raise ValueError("Write project must be a specific directory inside the home folder")
    text = str(actual)
    if any(c in text for c in ('\n', '\r', '\0', '"', '\\', '%')):
        raise ValueError("Unsafe characters in project path")
    return text

def render_sandbox(projects, home=None):
    paths = sorted({safe_root(p["root"], home) for p in projects
                    if p.get("enabled") is not False
                    and p.get("file_write_enabled") is True
                    and p.get("project_authorization") is True
                    and "write_test" in p.get("allowed_modes", [])})
    if not paths:
        return None
    return HEADER + "\n[Service]\nReadWritePaths=" + " ".join('"' + p + '"' for p in paths) + "\n"

class SystemdProjectSandbox:
    def __init__(self, unit_dir=None, home=None):
        self.home = Path(home or Path.home()).resolve()
        self.unit_dir = Path(unit_dir or self.home / ".config/systemd/user")
        self.dropin = self.unit_dir / (UNIT + ".d/60-bridge-studio-projects.conf")

    def snapshot(self):
        if self.dropin.is_symlink():
            raise PermissionError("Unsafe systemd drop-in symlink")
        if not self.dropin.exists():
            return None
        info = self.dropin.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Systemd project drop-in not private")
        contents = self.dropin.read_text(encoding="utf-8")
        if not contents.startswith(HEADER + "\n"):
            raise PermissionError("Refusing to replace unmanaged systemd drop-in")
        return contents

    def apply(self, projects):
        content = render_sandbox(projects, self.home)
        old = self.snapshot()
        if old == content:
            return old
        dest = self.dropin
        if content is None:
            dest.unlink(missing_ok=True)
            return old
        if dest.parent.is_symlink():
            raise PermissionError("Unsafe systemd drop-in directory")
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=dest.parent, prefix=".bridge-sandbox-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, dest)
        finally:
            Path(temp).unlink(missing_ok=True)
        return old

    def restore(self, old):
        # Expected to be called under local config.lock after we applied
        # our own generated content. Never overwrite unknown drop-ins.
        self.snapshot()
        if old is None:
            self.dropin.unlink(missing_ok=True)
        else:
            if not old.startswith(HEADER + "\n"):
                raise PermissionError("Unsafe rollback source")
            dest = self.dropin
            fd, temp = tempfile.mkstemp(dir=dest.parent, prefix=".bridge-restore-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(old)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temp, 0o600)
                os.replace(temp, dest)
            finally:
                Path(temp).unlink(missing_ok=True)

    @staticmethod
    def reload():
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=20)
