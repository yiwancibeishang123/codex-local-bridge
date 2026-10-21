"""Owner-private native UI profile for first-run selection of an EXISTING Bridge.

Never creates OAuth credentials, copies config contents or silently downloads
software; stores only a path to an already installed, validated Bridge config.
"""
import json
import os
from pathlib import Path
import stat
import tempfile


def profile_file(home=None):
    return Path(home or Path.home()).expanduser().resolve() / ".config/bridge-studio/profile.json"


def config_location(*, home=None, environ=None):
    home = Path(home or Path.home()).expanduser().resolve()
    env = os.environ if environ is None else environ
    if env.get("BRIDGE_CONFIG"):
        return Path(env["BRIDGE_CONFIG"]).expanduser().absolute()
    path = profile_file(home)
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise PermissionError("Studio profile cannot be a symlink")
        st = path.stat()
        if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600:
            raise PermissionError("Studio profile must be current-user 0600")
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("bridge_config")
        if data.get("version") != 1 or not isinstance(value, str):
            raise ValueError("Studio profile schema unsupported")
        selected = Path(value)
        if not selected.is_absolute():
            raise ValueError("Studio profile must hold an absolute config path")
        return selected
    return home / ".config/codex-local-bridge/config.json"


def remember_config(source, *, home=None):
    home = Path(home or Path.home()).expanduser().resolve()
    selected = Path(source).expanduser().absolute()
    # Don't accept arbitrary system paths as an authority for owner config.
    if not selected.is_relative_to(home) or selected.is_symlink() or not selected.is_file():
        raise PermissionError("Bridge config must be a private file inside your user home")
    st = selected.stat()
    if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600:
        raise PermissionError("Bridge config file must be current-user 0600")
    raw = json.loads(selected.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("projects"), list) or not isinstance(raw.get("state_dir"), str):
        raise ValueError("Selected file is not a Bridge config")
    profile = profile_file(home)
    if profile.is_symlink():
        raise PermissionError("Studio profile path is a symlink")
    parent = profile.parent
    if parent.is_symlink():
        raise PermissionError("Studio config directory is a symlink")
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    if parent.stat().st_uid != os.getuid() or stat.S_IMODE(parent.stat().st_mode) != 0o700:
        raise PermissionError("Studio config directory must be owner-only 0700")
    if profile.exists() and (profile.stat().st_uid != os.getuid() or
                             stat.S_IMODE(profile.stat().st_mode) != 0o600):
        raise PermissionError("Unsafe existing Studio profile")
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "bridge_config": str(selected)}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(0o600)
        os.replace(temp, profile)
    finally:
        temp.unlink(missing_ok=True)
    return selected
