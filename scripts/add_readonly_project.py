#!/usr/bin/env python3
"""Register one read-only project in the existing Codex Local Bridge.

Run locally on Ubuntu; not exposed as an MCP tool. Existing projects, grants,
OAuth configuration and SQLite data are preserved.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import stat
import fnmatch

TEXT_PATTERNS = [
    "*.py", "*.pyi", "*.md", "*.rst", "*.txt", "*.yaml", "*.yml",
    "*.json", "*.toml", "*.ini", "*.cfg", "*.cpp", "*.hpp", "*.cc",
    "*.cxx", "*.c", "*.h", "*.cmake", "*.sh", "*.bash", "*.xml",
    "*.urdf", "*.xacro", "*.launch", "*.rviz", "*.tex", "CMakeLists.txt", "LICENSE"
]
DENIED_PARTS = {".ssh", ".aws", ".gnupg", ".codex", ".agents", ".venv", ".git"}
AUTOCONTEXT = ("README.md", "README.rst", "README.txt", "pyproject.toml", "CMakeLists.txt")


def error(message):
    raise RuntimeError(message)


def execute(*command):
    proc = subprocess.run(command, check=False, timeout=40)
    if proc.returncode != 0:
        error(f"Command failed ({proc.returncode}): {' '.join(map(str, command))}")


def replace_atomic(path, data, mode=0o600):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".bridge-config-", delete=False) as f:
            temp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temp.chmod(mode)
        os.replace(temp, path)
        # Durably persist the renamed file, not just its contents.
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def git_root(path):
    try:
        cp = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, timeout=10, check=False,
        )
        return cp.returncode == 0 and Path(cp.stdout.strip()).resolve() == path
    except (OSError, subprocess.TimeoutExpired):
        return False


def allowed_context(root, name):
    p = Path(name)
    if (p.is_absolute() or not p.parts or ".." in p.parts or
            any(part.startswith(".") or part in DENIED_PARTS for part in p.parts)):
        error(f"Protected or invalid context file: {name}")
    file = root / p
    if any((root / Path(*p.parts[:i])).is_symlink() for i in range(1, len(p.parts) + 1)):
        error(f"Symlink context file rejected: {name}")
    if not file.is_file() or file.stat().st_size > 128 * 1024:
        error(f"Context must be an existing UTF-8 text file <=128 KiB: {name}")
    try:
        file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        error(f"Non-UTF-8 context file: {name}")
    # Do not allow arbitrary files to enter model context.
    if not any(fnmatch.fnmatchcase(p.as_posix(), pat) for pat in TEXT_PATTERNS):
        error(f"Context outside text extension allowlist: {name}")
    return p.as_posix()


def main(argv=None, *, config_file=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", help="Short stable id (letters, digits, underscore, dash)")
    parser.add_argument("folder", help="Existing local Ubuntu project directory")
    parser.add_argument("--context", action="append", default=[], metavar="RELATIVE_FILE",
                        help="Limit Codex's initial context to these files; repeat as needed")
    parser.add_argument("--name", help="Optional display name")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing or restarting")
    parser.add_argument("--yes", action="store_true", help="Explicitly approve the local read-only registration")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", args.project_id):
        error("project_id must be 1-64 ASCII letters/digits/underscore/dash, start with a letter")
    entered = Path(args.folder).expanduser()
    if entered.is_symlink():
        error("Do not register a symlink as project root")
    root = entered.resolve(strict=True)
    if not root.is_dir():
        error(f"Not a directory: {root}")
    home = Path.home().resolve()
    config_path = Path(config_file or os.environ.get(
        "BRIDGE_CONFIG", home/".config/codex-local-bridge/config.json")).expanduser()
    if not config_path.is_file() or config_path.is_symlink():
        error("Bridge config not found or is a symlink")
    if config_path.stat().st_mode & 0o077:
        error("Bridge config permissions must be 0600")
    lock_path = config_path.with_name(config_path.name + ".registration.lock")
    if lock_path.is_symlink():
        error("Registration lock must not be a symlink")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    lock_info = os.fstat(fd)
    if lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077 or not stat.S_ISREG(lock_info.st_mode):
        os.close(fd)
        error("Registration lock must be a private regular file owned by the current user")
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            error('Another project registration is active; retry locally')
        old = config_path.read_bytes()
        config = json.loads(old.decode("utf-8"))
        state = Path(config["state_dir"]).expanduser().resolve()
        if (root in (Path("/"), home, state) or state.is_relative_to(root) or
                root.is_relative_to(state) or
                any(part in DENIED_PARTS for part in root.parts) or
                any(root.is_relative_to(Path(p)) for p in ("/etc", "/proc", "/sys", "/dev", "/run"))):
            error("Refusing home/system/credential/state directory; choose a specific ordinary project")
        if any(p["id"] == args.project_id for p in config["projects"]):
            error(f"Project ID already registered: {args.project_id}")
        if any(Path(p["root"]).expanduser().resolve() == root for p in config["projects"]):
            error("This root is already registered under another project ID")

        context = [allowed_context(root, file) for file in args.context]
        if not context and not git_root(root):
            # Non-Git projects require explicit or small automatically chosen source context
            for n in AUTOCONTEXT:
                candidate = root / n
                if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size <= 128 * 1024:
                    try:
                        context.append(allowed_context(root, n))
                    except (RuntimeError, OSError):
                        pass
                if len(context) >= 2:
                    break
            if not context:
                # Try a few top-level plain source files without walking a large tree.
                for candidate in sorted(root.iterdir()):
                    if not candidate.is_file() or candidate.is_symlink() or candidate.name.startswith("."):
                        continue
                    if candidate.stat().st_size > 128 * 1024:
                        continue
                    try:
                        context.append(allowed_context(root, candidate.name))
                    except (RuntimeError, OSError):
                        continue
                    if len(context) >= 2:
                        break
            if not context:
                print("NOTE: No suitable initial source context in this non-Git folder.")
                print("      Direct text file read/list works; Codex delegation needs --context RELATIVE_FILE.")
        project = {
            "id": args.project_id, "name": args.name or args.project_id,
            "root": str(root), "include": list(TEXT_PATTERNS),
            "allowed_modes": ["read"], "file_write_enabled": False,
            "project_authorization": False, "tests": []
        }
        if context:
            project["context_files"] = list(dict.fromkeys(context))
        print("\nNew READ-ONLY project (no write permissions):")
        print(json.dumps(project, ensure_ascii=False, indent=2))
        if args.dry_run:
            print("DRY RUN: no config changes, no restart.")
            return
        if not args.yes:
            if not sys.stdin.isatty():
                error("Interactive terminal required, or pass --yes locally")
            response = input(f"\nType REGISTER {args.project_id} to allow the web Bridge to read matching files: ")
            if response != f"REGISTER {args.project_id}":
                error("Registration not confirmed")

        config["projects"].append(project)
        updated = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if config_path.read_bytes() != old:
            error("Config changed concurrently; abort")
        backups = state/"registration-backups"
        if backups.is_symlink():
            error("Registration backup directory must not be a symlink")
        created_backup_dir = not backups.exists()
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created_backup_dir:
            state_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(state_fd)
            finally:
                os.close(state_fd)
        info = backups.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            error("Registration backup directory must be owner-only (0700)")
        now = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = backups/f"config-before-add-{args.project_id}-{now}.json"
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "wb") as f:
            f.write(old)
            f.flush()
            os.fsync(f.fileno())
        backup_dirfd = os.open(backups, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(backup_dirfd)
        finally:
            os.close(backup_dirfd)
        mode = config_path.stat().st_mode & 0o777
        try:
            replace_atomic(config_path, updated, mode)
            cli = home/".local/bin/codex-local-bridge"
            if not cli.is_file():
                error(f"Bridge CLI not found: {cli}")
            execute(str(cli), "project-policy", args.project_id)
            execute("systemctl", "--user", "restart", "codex-local-bridge")
            execute("systemctl", "--user", "is-active", "--quiet", "codex-local-bridge")
        except BaseException:
            print("Registration failed: checking whether it is safe to roll back", file=sys.stderr)
            # Do not erase a change written by a different config manager.
            if config_path.read_bytes() != updated:
                print("WARNING: config has changed concurrently; rollback not performed.", file=sys.stderr)
                print("Restore manually from:", backup, file=sys.stderr)
                raise
            replace_atomic(config_path, old, mode)
            try:
                execute("systemctl", "--user", "restart", "codex-local-bridge")
                execute("systemctl", "--user", "is-active", "--quiet", "codex-local-bridge")
            except Exception as exc:
                print("WARNING: Bridge unhealthy even after rollback:", exc, file=sys.stderr)
            raise
        print(f"\nSUCCESS: registered {args.project_id} ({root}) in read-only mode")
        print("Backup:", backup)
        print("Now use ChatGPT: list_projects / list_project_files / read_project_file")
        print("Writing remains disabled until separately enabled and locally authorized.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
