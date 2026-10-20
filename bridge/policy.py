"""Local-owner project grants. No grant/revoke operation is exposed over MCP."""
import fcntl
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .security import digest

# Deliberately small: executable/configuration/dependency files require review.
CODE_SUFFIXES = {'.py', '.pyi', '.js', '.jsx', '.ts', '.tsx', '.c', '.h', '.cpp',
                 '.hpp', '.cc', '.cxx', '.rs', '.go', '.java', '.css', '.html', '.md', '.txt'}
REVIEW_NAMES = {'agents.md', 'claude.md', 'gemini.md', 'conftest.py', 'sitecustomize.py',
                'usercustomize.py', 'setup.py', 'requirements.txt', 'cargo.toml',
                'package.json', 'makefile', 'cmakelists.txt'}

def ordinary_path(path):
    from .filetools import _check_path
    try: bits = _check_path(path)
    except ValueError: return False
    return (Path(path).suffix.lower() in CODE_SUFFIXES
            and not any(b.lower() in REVIEW_NAMES or any(s in b.lower() for s in
                        ('secret', 'credential', 'password', 'token', 'security', 'auth')) for b in bits))

def ordinary_tests(commands):
    # No shell, -c, plugins, alternate executables, installs or elevated commands.
    return all(isinstance(a,list) and len(a)>=3 and a[:3]==['/usr/bin/python3','-m','unittest']
               and all(isinstance(v,str) and v not in ('-c','--locals') and not v.startswith('/')
                       and '..' not in Path(v).parts for v in a[3:]) for a in commands)

class ProjectAuthorization:
    def __init__(self, bridge): self.bridge=bridge; self.store=bridge.store

    @contextmanager
    def locked(self):
        # Shared by local grant/revoke and all writes. Revocation returns only
        # after an in-progress atomic write finishes; no later write can pass.
        with (self.store.directory/'project-authorization.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            yield

    def scope(self, project_id):
        p=self.bridge.project(project_id); st=Path(p['root']).stat()
        return digest({'policy_version':1,'project':p,'root_device':st.st_dev,'root_inode':st.st_ino})

    def active(self, project_id, grant_id=None):
        p=self.bridge.project(project_id)
        if p.get('project_authorization') is not True: return None
        g=self.store.get('project_grant',project_id)
        if (not g or g.get('revoked_at') or g['scope']!=self.scope(project_id)
            or (g.get('expires_at') and g['expires_at']<=time.time())
            or (grant_id and grant_id!=g['id'])): return None
        return g

    def grant(self, project_id, hours=None):
        p=self.bridge.project(project_id)
        if p.get('project_authorization') is not True:
            raise PermissionError('Set project_authorization=true locally before granting this project')
        if p.get('file_write_enabled') is not True or 'write_test' not in p.get('allowed_modes',[]):
            raise PermissionError('Explicit file_write_enabled=true and allowed_modes write_test required')
        if hours is not None and (hours<=0 or hours>8760): raise ValueError('hours must be 1..8760')
        with self.locked():
            g={'id':uuid.uuid4().hex,'scope':self.scope(project_id),'created_at':time.time(),
               'expires_at':time.time()+hours*3600 if hours else None,'revoked_at':None}
            self.store.put('project_grant',project_id,g)
            self.store.audit('project_authorized_locally',{'project':project_id,**g})
        return self.describe(project_id)

    def revoke(self, project_id):
        self.bridge.project(project_id)
        with self.locked():
            g=self.store.get('project_grant',project_id)
            if g: self.store.update('project_grant',project_id,revoked_at=time.time())
            self.store.audit('project_revoked_locally',{'project':project_id})
        return self.describe(project_id)

    def require(self, project_id, grant_id):
        if not self.active(project_id,grant_id):
            raise PermissionError('Project authorization revoked, expired or changed; explicit local approval required')

    def describe(self, project_id):
        g=self.active(project_id)
        return {'effective_access':'automatic_code_write' if g else 'read_only',
                'grant_id':g['id'] if g else None,'expires_at':g['expires_at'] if g else None,
                'ordinary_files_auto_apply':bool(g),
                'sandboxed_tests_auto_run':bool(g) and ordinary_tests(self.bridge.project(project_id).get('tests',[])),
                'dangerous_operations':'separate_local_approval_or_denied',
                'local_cli':'codex-local-bridge grant-project / revoke-project'}
