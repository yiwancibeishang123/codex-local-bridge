"""Explicit one-click local Codex MCP registration (never exposed remotely).

Only uses the installed Codex CLI's built-in MCP manager. Refuses to overwrite
an existing provider, never edits config TOML by string concatenation, and
makes an owner-private backup before the authorized CLI operation.
"""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

SERVER = "bridge_memory"

def autodetect_backend_python(home=None, root=None):
    home = Path(home or Path.home()).resolve()
    candidates = []
    if root:
        candidates.append(Path(root) / ".venv/bin/python")
    launcher = home / ".local/bin/codex-local-bridge"
    if launcher.exists():
        target = launcher.resolve(strict=True)
        try:
            first = target.open("rb").readline(512).decode("utf-8", errors="strict").strip()
            if first.startswith("#!") and first.endswith("/python"):
                candidates.append(Path(first[2:]))
            elif first.startswith("#!") and re.search(r"/python3(\.\d+)?$", first):
                candidates.append(Path(first[2:]))
        except (OSError, UnicodeDecodeError):
            pass
    for p in candidates:
        try:
            original = p.absolute()
            resolved = p.resolve(strict=True)
            if (original.is_relative_to(home) and resolved.is_file()
                    and os.access(original, os.X_OK)):
                # Keep the venv entry path: resolving a venv python symlink
                # to /usr/bin/python would lose the installed Bridge package.
                return str(original)
        except (OSError, RuntimeError):
            continue
    return None

def register_codex_memory(config, config_path, backend_python, *, confirmed=False,
                          codex_binary=None, home=None, run=None):
    if not confirmed:
        raise PermissionError("Only the local user can opt in to Codex MCP setup")
    home = Path(home or Path.home()).resolve()
    tool = Path(codex_binary or config["codex_command"][0]).expanduser()
    python = Path(backend_python).expanduser()
    for p in (tool, python):
        if not p.is_file() or not os.access(p, os.X_OK):
            raise FileNotFoundError("Required executable not found: " + str(p))
    config_file = Path(config_path).expanduser()
    if not config_file.is_file() or config_file.is_symlink():
        raise PermissionError("Unsafe Bridge config path")
    codex_dir = home / ".codex"
    codex_file = codex_dir / "config.toml"
    if codex_file.is_symlink() or (codex_file.exists() and codex_file.stat().st_uid != os.getuid()):
        raise PermissionError("Unsafe Codex config")
    if codex_dir.exists() and codex_dir.is_symlink():
        raise PermissionError("Unsafe Codex settings directory")
    if run is None:
        run = subprocess.run
    def invoke(argv):
        return run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=22)
    # Refuse to silently replace an MCP provider chosen by the user.
    check = invoke([str(tool), "mcp", "get", SERVER, "--json"])
    if check.returncode == 0:
        raise FileExistsError("Codex already has bridge_memory configured; do not overwrite")
    backup = None
    if codex_file.exists():
        base = home / ".local/share/codex-local-bridge/codex-config-backups"
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if base.is_symlink() or base.stat().st_mode & 0o077:
            raise PermissionError("Backup folder is not private")
        backup = base / ("config-" + str(time.time_ns()) + ".toml")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(codex_file.read_bytes())
    argv = [str(tool), "mcp", "add", SERVER,
            "--env", "BRIDGE_CONFIG=" + str(config_file),
            "--", str(python), "-m", "bridge.codex_memory_stdio"]
    changed = invoke(argv)
    if changed.returncode:
        raise RuntimeError("Codex refused MCP registration; old config backup preserved")
    verified = invoke([str(tool), "mcp", "get", SERVER, "--json"])
    if verified.returncode:
        raise RuntimeError("Codex MCP entry installed but could not be verified; check Codex settings")
    return {"connected":True,"provider":SERVER,
            "config_file":str(codex_file),
            "backup_available":bool(backup)}

