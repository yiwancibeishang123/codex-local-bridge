import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

DENIED = {'.git', '.codex', '.agents', '.ssh', '.aws', '.gnupg', 'node_modules', '__pycache__', '.venv'}

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def redact(value):
    text = str(value)
    for key, secret in os.environ.items():
        if any(s in key.upper() for s in ('TOKEN', 'SECRET', 'API_KEY', 'PASSWORD')) and len(secret) >= 8:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[^\s"\x27]+', r'\1[REDACTED]', text)
    text = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'sk-[A-Za-z0-9_-]{8,}', '[REDACTED]', text)
    return text

def checked(root, relative, includes):
    root = Path(root).resolve()
    from .filetools import _check_path
    _check_path(relative)
    path = Path(relative)
    if path.is_absolute() or not path.parts or '..' in path.parts:
        raise ValueError('Only contained relative paths are allowed')
    if any(p in DENIED or p.startswith('.env') or p.lower().endswith(('.pem', '.key')) for p in path.parts):
        raise ValueError('Protected path')
    if not any(fnmatch.fnmatch(path.as_posix(), pattern) for pattern in includes):
        raise ValueError('Path outside configured include patterns')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlinks are not authorized')
    if not current.resolve().is_relative_to(root):
        raise ValueError('Path escape')
    return current

def git(root, *args):
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': '/nonexistent',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_AUTHOR_NAME': 'Codex Local Bridge',
           'GIT_AUTHOR_EMAIL': 'bridge@localhost', 'GIT_COMMITTER_NAME': 'Codex Local Bridge',
           'GIT_COMMITTER_EMAIL': 'bridge@localhost'}
    p = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                        '-c', 'core.untrackedCache=false', '-C', str(root), *args],
                       env=env, capture_output=True, timeout=30)
    if p.returncode:
        raise RuntimeError(redact(p.stderr.decode(errors='replace')))
    return p.stdout.decode(errors='replace')

def sandbox_command(root, argv):
    """No shell, network, host home, secrets, source repo or Git metadata."""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError('Invalid argv')
    args = ['bwrap', '--die-with-parent', '--new-session', '--unshare-all', '--clearenv']
    for directory in ('/usr', '/bin', '/lib', '/lib64'):
        if Path(directory).exists():
            args += ['--ro-bind', directory, directory]
    args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/home',
             '--dir', '/etc', '--ro-bind', str(root), '/project', '--tmpfs', '/project/.git',
             '--chdir', '/project', '--setenv', 'HOME', '/tmp', '--setenv', 'PATH', '/usr/bin:/bin',
             '--setenv', 'PYTHONDONTWRITEBYTECODE', '1', '--', *argv]
    return args
