"""Register an isolated source workspace; never edit the running installation."""
import fnmatch
import json
import os
from pathlib import Path
import uuid
from .security import git

PROJECT_ID='bridge_maintenance'
INCLUDE=['bridge/*.py','tests/*.py','scripts/*.py','scripts/*.sh',
         'maintenance_tests/*.py','docs/*.md','skills/*/SKILL.md',
         'README.md','pyproject.toml','plugin.json','mcp.json','config.example.json']

SMOKE_TEST='''"""Syntax smoke checks only; full pytest is required before deployment."""
import ast
import json
from pathlib import Path
import unittest

class SourceSmoke(unittest.TestCase):
    def test_python_source_parses(self):
        root=Path(__file__).resolve().parents[1]
        paths=[p for directory in ('bridge','tests','scripts','maintenance_tests')
               for p in (root/directory).rglob('*.py')]
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=str(path.relative_to(root))):
                ast.parse(path.read_text(encoding='utf-8'),filename=str(path))

    def test_plugin_manifests_parse(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('plugin.json','mcp.json','config.example.json'):
            with self.subTest(name=name):
                self.assertIsInstance(json.loads((root/name).read_text(encoding='utf-8')),dict)
'''

def register_maintenance(config, source):
    existing=next((p for p in config['projects'] if p['id']==PROJECT_ID),None)
    if existing: return existing
    source=Path(source).resolve()
    root=Path(config['state_dir']).expanduser().resolve()/'maintenance-workspace'
    if root.exists(): raise ValueError('Maintenance directory already exists; refusing to overwrite')
    files=[]
    # Never recurse into the private runtime, installation venv or arbitrary files.
    candidates=[p for directory in ('bridge','tests','scripts','docs','skills')
                for p in (source/directory).rglob('*')]
    candidates += [source/name for name in ('README.md','pyproject.toml','plugin.json','mcp.json','config.example.json')]
    for path in candidates:
        rel=path.relative_to(source).as_posix()
        if not path.is_file() or any((source/Path(*path.relative_to(source).parts[:i])).is_symlink()
                                     for i in range(1,len(path.relative_to(source).parts)+1)): continue
        if any(part.startswith('.') or part=='__pycache__' for part in path.relative_to(source).parts): continue
        if any(fnmatch.fnmatchcase(rel,pattern) for pattern in INCLUDE):
            files.append((rel,path.read_text(encoding='utf-8')))
    if not any(rel=='bridge/filetools.py' for rel,_ in files):
        raise ValueError('Source must contain Codex Local Bridge code')
    root.mkdir(parents=True,mode=0o700)
    for rel,content in files:
        target=root/rel;target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
        target.write_text(content,encoding='utf-8');target.chmod(0o600)
    smoke=root/'maintenance_tests/test_source_smoke.py';smoke.parent.mkdir(mode=0o700)
    smoke.write_text(SMOKE_TEST,encoding='utf-8');smoke.chmod(0o600)
    git(root,'init','--quiet');git(root,'add','--all');git(root,'commit','--quiet','-m','Isolated Bridge maintenance baseline')
    project={'id':PROJECT_ID,'name':'Codex Local Bridge maintenance (isolated source copy)',
             'root':str(root),'include':INCLUDE.copy(),'allowed_modes':['read','write_test'],
             'file_write_enabled':True,'project_authorization':True,
             'tests':[['/usr/bin/python3','-m','unittest','discover','-s','maintenance_tests','-v']]}
    config['projects'].append(project)
    return project

def save_registration(config, path, original):
    """Owner-only config update, backup first; detect concurrent config changes."""
    path=Path(path)
    if path.read_bytes()!=original: raise ValueError('Config changed; inspect before retrying registration')
    backup=Path(config['state_dir']).expanduser() / ('config-before-maintenance-'+uuid.uuid4().hex+'.json')
    fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as out: out.write(original)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex)
    try:
        fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(config,out,ensure_ascii=False,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally: temp.unlink(missing_ok=True)
