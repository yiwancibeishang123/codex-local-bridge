import json
from pathlib import Path
import pytest
from bridge.core import Bridge
from bridge.maintenance import register_maintenance, save_registration, PROJECT_ID
from tests.test_bridge import config


def test_maintenance_is_separate_no_grant_and_preserves_projects(config,tmp_path):
    source=tmp_path/'installed';(source/'bridge').mkdir(parents=True)
    (source/'bridge/filetools.py').write_text('# file tools\n')
    (source/'bridge/secret-link.py').symlink_to(tmp_path/'outside.py')
    (tmp_path/'outside.py').write_text('do not expose\n')
    (source/'.venv').mkdir();(source/'.venv/auth.json').write_text('private runtime\n')
    (source/'config.json').write_text('private config\n')
    config['projects'].append({'id':'readonly_project','root':str(tmp_path/'readonly-project'),
                               'include':['*.py'],'allowed_modes':['read'],'file_write_enabled':False,'tests':[]})
    Path(config['projects'][-1]['root']).mkdir()
    prior=json.loads(json.dumps(config['projects']))
    p=register_maintenance(config,source)
    root=Path(p['root']);assert root!=source and (root/'bridge/filetools.py').read_text()=='# file tools\n'
    assert not (root/'config.json').exists() and not (root/'.venv').exists()
    assert not (root/'bridge/secret-link.py').exists()
    assert (root/'maintenance_tests/test_source_smoke.py').exists()
    assert config['projects'][:-1]==prior
    b=Bridge(config);assert b.authorization.describe(PROJECT_ID)['effective_access']=='read_only'
    assert b.submit(PROJECT_ID,'Fix ordinary file','write_test')['state']=='awaiting_approval'
    (root/'bridge/filetools.py').write_text('# maintainer changes\n')
    assert register_maintenance(config,source)==p
    assert (root/'bridge/filetools.py').read_text()=='# maintainer changes\n'
    assert len(config['projects'])==3


def test_registration_never_overwrites_existing_directory(config,tmp_path):
    root=Path(config['state_dir'])/'maintenance-workspace';root.mkdir(parents=True)
    (root/'keep').write_text('keep')
    with pytest.raises(ValueError,match='refusing to overwrite'):register_maintenance(config,tmp_path)
    assert (root/'keep').read_text()=='keep'


def test_registration_config_atomic_backup_and_stale_check(config,tmp_path):
    path=tmp_path/'config.json';original=json.dumps(config).encode();path.write_bytes(original);path.chmod(0o600)
    Path(config['state_dir']).mkdir()
    save_registration(config,path,original)
    backups=list(Path(config['state_dir']).glob('config-before-maintenance-*.json'))
    assert len(backups)==1 and backups[0].read_bytes()==original
    assert path.stat().st_mode & 0o077==0 and backups[0].stat().st_mode & 0o077==0
    with pytest.raises(ValueError,match='Config changed'):save_registration(config,path,b'stale')
