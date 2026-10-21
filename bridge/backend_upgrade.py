"""Locally approved staged backend switch with backup and rollback.

This does not download code, install untrusted packages, grant root, alter
OAuth secrets or auto-start Tailscale. Candidate must be an already-installed
clean checkout of the user's GitHub project with an owner-local virtualenv.
"""
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time
import fcntl

from .snapshots import StateSnapshots, _private, _replace

SERVICE = "codex-local-bridge.service"
OFFICIAL = ("https://github.com/yiwancibeishang123/codex-local-bridge.git",
            "https://github.com/yiwancibeishang123/codex-local-bridge",
            "git@github.com:yiwancibeishang123/codex-local-bridge.git")
VERSION = re.compile(r'^version\s*=\s*["\x27]([0-9]+\.[0-9]+\.[0-9]+(?:[abrc]+[0-9]+)?)["\x27]$', re.M)


def _unit_path(home=None):
    return Path(home or Path.home()).resolve() / ".config/systemd/user" / SERVICE


def _unit_fields(unit):
    if "Description=Private Codex Local Bridge (OAuth MCP)" not in unit:
        raise PermissionError("Unknown service unit; refusing to modify it")
    lines = unit.splitlines()
    executable = [x[10:] for x in lines if x.startswith("ExecStart=")]
    directories = [x[17:] for x in lines if x.startswith("WorkingDirectory=")]
    if len(executable) != 1 or len(directories) != 1:
        raise ValueError("Expected exactly one ExecStart and WorkingDirectory")
    if not executable[0].endswith(' serve'):
        raise PermissionError("Not a recognized Bridge service")
    return executable[0], directories[0]


def _quote_unit(p):
    path = str(p)
    if any(c in path for c in ('\n', '\r', '\0', '"', '\\', '%', '$')):
        raise ValueError("Candidate path contains unsupported systemd syntax")
    return '"' + path + '"'


class UserService:
    def __init__(self, port=8767, run=None, probe=None):
        self.port = port
        self.run = run or subprocess.run
        self.probe = probe or self._probe

    def _command(self, *args):
        result = self.run(["systemctl", "--user", *args], check=False,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=30)
        if result.returncode:
            raise RuntimeError("Bridge user service operation failed")

    def _probe(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                return True
        except OSError:
            return False

    def stop(self):
        self._command("stop", SERVICE)

    def restart_verify(self):
        self._command("daemon-reload")
        self._command("restart", SERVICE)
        self._command("is-active", "--quiet", SERVICE)
        for _ in range(15):
            if self.probe():
                return
            time.sleep(0.3)
        raise RuntimeError("Bridge service restarted but local listener is unavailable")


class BackendUpgrade:
    def __init__(self, config_path, home=None, control=None):
        self.home = Path(home or Path.home()).resolve()
        self.config = Path(config_path).expanduser().absolute()
        self.snapshots = StateSnapshots(self.config, _unit_path(self.home))
        self.unit = _unit_path(self.home)
        configuration = json.loads(self.config.read_text(encoding="utf-8"))
        self.control = control or UserService(int(configuration.get("port", 8767)))

    def current(self):
        _private(self.unit)
        unit = self.unit.read_text(encoding="utf-8")
        exec_line, working = _unit_fields(unit)
        return {"working_directory": working.strip('"'),
                "exec_start": exec_line, "service_exists": True}

    def _candidate(self, path):
        p = Path(path).expanduser().resolve(strict=True)
        if not p.is_dir() or p == self.home or not p.is_relative_to(self.home):
            raise PermissionError("Candidate must be a specific owner-local checkout")
        if p.stat().st_uid != os.getuid():
            raise PermissionError("Candidate directory is not owned by the current user")
        if any((p / part).is_symlink() for part in (".git", "pyproject.toml", ".venv")):
            raise PermissionError("Candidate contains unsafe symlinks")
        command = p / ".venv/bin/codex-local-bridge"
        python = p / ".venv/bin/python"
        for executable in (command, python):
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise FileNotFoundError("Stage a tested candidate virtual environment first")
        if not (p / "bridge/server.py").is_file():
            raise FileNotFoundError("Bridge server module is missing")
        version = VERSION.search((p / "pyproject.toml").read_text(encoding="utf-8"))
        if not version:
            raise ValueError("Candidate version missing from pyproject.toml")
        def git(*args):
            proc = subprocess.run(["git", "-C", str(p), *args], check=False,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, timeout=10)
            if proc.returncode:
                raise RuntimeError("Candidate Git checkout validation failed")
            return proc.stdout.strip()
        if git("remote", "get-url", "origin") not in OFFICIAL:
            raise PermissionError("Only the configured upstream Bridge repository is eligible")
        if git("status", "--porcelain", "--untracked-files=no"):
            raise PermissionError("Commit or discard local candidate changes before upgrading")
        # Validates installed environment without using pip/network or a shell.
        chk = subprocess.run([str(python), "-c",
                              "from bridge.server import build_server; import importlib.metadata as m; "
                              "print(m.version('codex-local-bridge'))"],
                             cwd=p, capture_output=True, text=True, timeout=12)
        if chk.returncode or chk.stdout.strip() != version.group(1):
            raise RuntimeError("Candidate virtualenv does not match source version")
        return {"root": str(p), "bin": str(command),
                "version": version.group(1), "revision": git("rev-parse", "--short=12", "HEAD")}

    def preflight(self, path):
        candidate = self._candidate(path)
        current = self.current()
        if Path(current["working_directory"]).resolve() == Path(candidate["root"]):
            raise ValueError("Candidate is already the active backend checkout")
        return {"candidate": {k:candidate[k] for k in ("root", "version", "revision")},
                "current": {"working_directory":current["working_directory"]},
                "checks": ["owner local", "clean upstream git checkout", "venv imports backend",
                           "service managed", "private SQLite snapshot before change"]}

    def install(self, path, *, confirmed=False):
        if confirmed is not True:
            raise PermissionError("Explicit local upgrade confirmation required")
        plan = self.preflight(path)
        cand = self._candidate(path)
        with (self.snapshots.state / "upgrade.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            before = self.unit.read_bytes()
            snapshot = self.snapshots.create()
            text = before.decode("utf-8")
            lines = text.splitlines()
            updated = []
            for line in lines:
                if line.startswith("WorkingDirectory="):
                    updated.append("WorkingDirectory=" + _quote_unit(cand["root"]))
                elif line.startswith("ExecStart="):
                    updated.append("ExecStart=" + _quote_unit(cand["bin"]) + " serve")
                else:
                    updated.append(line)
            if self.unit.read_bytes() != before:
                raise RuntimeError("Service unit changed concurrently")
            _replace(self.unit, ("\n".join(updated) + "\n").encode("utf-8"))
            try:
                self.control.restart_verify()
            except Exception as problem:
                # Old unit + all private DBs + config come back via verified snapshot.
                try:
                    self.snapshots.restore(snapshot["id"], confirmed=True, service=self.control)
                except Exception as failed_restore:
                    raise RuntimeError("Upgrade failed; recovery needs manual inspection. Snapshot " +
                                       snapshot["id"]) from failed_restore
                raise RuntimeError("Backend upgrade failed and previous release was restored") from problem
        return {"previous":plan["current"]["working_directory"],
                "active": cand["root"], "version": cand["version"],
                "snapshot": snapshot["id"], "verified": True}

    def rollback(self, snapshot_id, *, confirmed=False):
        return self.snapshots.restore(snapshot_id, confirmed=confirmed, service=self.control)
