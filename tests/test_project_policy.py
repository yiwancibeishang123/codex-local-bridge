"""Project authorization regressions; engine fixture tests are not live inference."""
import asyncio
import hashlib
import json
from pathlib import Path
import pytest
from bridge.core import Bridge
from bridge.filetools import create_file_bridge, WorkspaceError
from bridge.policy import ordinary_path, ordinary_tests
from tests.test_bridge import config

def enabled(config):
    config['projects'][0].update(project_authorization=True,file_write_enabled=True,
                               allowed_modes=['read','write_test'],
                               tests=[['/usr/bin/python3','-m','unittest','discover','-v']])
    return Bridge(config)

def test_grant_not_implicit_and_revoke_persistent(config):
    b=enabled(config); files=create_file_bridge(b)
    assert b.authorization.describe('demo')['effective_access']=='read_only'
    proposal=files.request_write_file('demo','new.py','x=1\n','ABSENT')
    assert proposal['status']=='pending_local_approval'
    assert not (Path(b.project('demo')['root'])/'new.py').exists()
    grant=b.authorization.grant('demo')
    result=files.request_write_file('demo','auto.py','x=2\n','ABSENT')
    assert result['status']=='applied'
    assert result['grant_id']==grant['grant_id']
    assert files.request_write_file('demo','new/nested.py','x=4\n','ABSENT')['status']=='applied'
    b.authorization.revoke('demo')
    assert not Bridge(config).authorization.active('demo')
    result=files.request_write_file('demo','after.py','x=3\n','ABSENT')
    assert result['status']=='pending_local_approval'
    assert not (Path(b.project('demo')['root'])/'after.py').exists()

def test_auto_file_backup_sha_diff_risk_and_conflict(config):
    b=enabled(config);b.project('demo')['include']=['*.py','*.sh','*.json']
    b.authorization.grant('demo'); f=create_file_bridge(b)
    before=f.read_file('demo','calculator.py')
    p=f.request_edit_file('demo','calculator.py','a - b','a + b',before['sha256'])
    assert p['status']=='applied' and 'return a + b' in p['diff']
    assert (f.backup_dir/(p['proposal_id']+'.bak')).read_text()==before['content']
    with pytest.raises(WorkspaceError): f.request_write_file('demo','calculator.py','bad',before['sha256'])
    for name,content in [('run.sh','sudo reboot\n'),('auth.py','x=1\n'),
                         ('secret.py','x=1\n'),('ordinary.py','password = "sensitive"\n')]:
        assert f.request_write_file('demo',name,content,'ABSENT')['status']=='pending_local_approval'
        assert not (Path(b.project('demo')['root'])/name).exists()
    for path in ('../outside.py','.env','key.pem'):
        with pytest.raises(ValueError):f.request_write_file('demo',path,'bad','ABSENT')

def test_grant_scope_expiry_generation_and_readonly(config,monkeypatch):
    b=enabled(config);b.authorization.grant('demo',1);old=b.authorization.active('demo')['id']
    b.project('demo')['tests']=[['sudo','reboot']]
    assert not b.authorization.active('demo')
    b.project('demo')['tests']=[];b.authorization.grant('demo')
    with pytest.raises(PermissionError):b.authorization.require('demo',old)
    b.store.update('project_grant','demo',expires_at=1)
    assert not b.authorization.active('demo')
    b.project('demo')['allowed_modes']=['read']
    with pytest.raises(PermissionError):b.authorization.grant('demo')
    with pytest.raises(PermissionError):b.submit('demo','write','write_test')

def test_auto_task_real_sandbox_apply_resume_backup(config):
    b=enabled(config);root=Path(b.project('demo')['root']); original=(root/'calculator.py').read_text()
    b.authorization.grant('demo')
    first=b.submit('demo','Fix add','write_test');assert first['state']=='queued'
    asyncio.run(b.execute(first['id']))
    status=b.status(first['id']);assert status['state']=='succeeded',status
    assert status['tests_passed'] is True and status['changes_applied'] is True
    assert (b.store.directory/'backups'/first['id']/'calculator.py').read_text()==original
    second=b.submit('demo','Add multiply','write_test',first['id'])
    asyncio.run(b.execute(second['id']))
    assert b.status(second['id'])['changes_applied'] is True,b.status(second['id'])
    assert 'multiply' in (root/'calculator.py').read_text()
    assert 'return a - b' not in b.result(second['id'])['diff']
    assert b.status(second['id'])['thread_id']==status['thread_id']

def test_revoked_queued_task_never_executes(config):
    b=enabled(config);b.authorization.grant('demo')
    t=b.submit('demo','Fix add','write_test');b.authorization.revoke('demo')
    asyncio.run(b.execute(t['id']))
    assert b.status(t['id'])['state']=='failed'
    assert not (b.store.directory/'snapshots'/t['id']).exists()

def test_revoke_during_model_blocks_tests_and_write(config,monkeypatch):
    b=enabled(config);b.authorization.grant('demo')
    from bridge.appserver import AppServer
    real=AppServer.run
    async def revoke(server,*args):
        answer=await real(server,*args);b.authorization.revoke('demo');return answer
    monkeypatch.setattr(AppServer,'run',revoke)
    t=b.submit('demo','Fix add','write_test');asyncio.run(b.execute(t['id']))
    assert b.status(t['id'])['state']=='failed'
    assert 'return a - b' in (Path(b.project('demo')['root'])/'calculator.py').read_text()

def test_risky_argv_needs_single_task_approval(config):
    b=enabled(config);b.project('demo')['tests']=[['sudo','reboot']];b.authorization.grant('demo')
    assert b.submit('demo','Fix','write_test')['state']=='awaiting_approval'
    assert not ordinary_tests([['/bin/sh','-c','id']])
    assert not ordinary_tests([['/usr/bin/python3','-c','print(1)']])
    assert ordinary_tests([['/usr/bin/python3','-m','unittest','discover','-v']])
    assert not ordinary_path('AGENTS.md') and not ordinary_path('setup.py')

def test_auto_apply_failure_never_reports_applied(config,monkeypatch):
    b=enabled(config);b.authorization.grant('demo')
    def fail(*args):raise PermissionError('Read-only file system')
    monkeypatch.setattr('bridge.writeback.apply_batch',fail)
    t=b.submit('demo','Fix add','write_test');asyncio.run(b.execute(t['id']))
    s=b.status(t['id']);assert s['state']=='succeeded' and s['execution_succeeded']
    assert s['changes_applied'] is False and s['apply_requires_approval'] is True
    assert 'Read-only' in s['apply_error']


def test_failed_tests_and_risky_model_edits_never_auto_apply(config,monkeypatch):
    b=enabled(config);b.project('demo')['include']=['*.py','*.sh'];b.authorization.grant('demo')
    from bridge.appserver import AppServer
    async def failed(server,task,context,on_thread,on_event):
        await on_thread('fixture')
        return {'summary':'Fixture failing code','edits':[{'path':'calculator.py','content':'def add(a,b): return 0\n'}]}
    monkeypatch.setattr(AppServer,'run',failed)
    t=b.submit('demo','Fixture','write_test');asyncio.run(b.execute(t['id']))
    assert b.status(t['id'])['tests_passed'] is False
    assert b.status(t['id'])['changes_applied'] is False
    assert b.status(t['id'])['apply_requires_approval'] is True
    async def risky(server,task,context,on_thread,on_event):
        await on_thread('fixture')
        return {'summary':'Fixture risky edit','edits':[{'path':'run.sh','content':'sudo reboot\n'}]}
    monkeypatch.setattr(AppServer,'run',risky)
    t=b.submit('demo','Fixture','write_test');asyncio.run(b.execute(t['id']))
    assert b.status(t['id'])['tests_passed'] is None
    assert b.result(t['id'])['tests']==[]
    assert b.status(t['id'])['apply_requires_approval'] is True
    assert not (Path(b.project('demo')['root'])/'run.sh').exists()


def test_batch_conflict_and_failure_rollback(config,monkeypatch):
    from bridge.writeback import apply_batch,replace_at
    b=enabled(config);b.authorization.grant('demo');root=Path(b.project('demo')['root'])
    t=b.submit('demo','Fixture','write_test');before=(root/'calculator.py').read_text()
    task=b.store.update('task',t['id'],baseline={'calculator.py':before})
    result={'review_digest':'fixture','edits':[{'path':'calculator.py','content':'first\n'}, {'path':'new.py','content':'second\n'}]}
    calls=0
    def fail_second(fd,name,data,mode):
        nonlocal calls
        calls+=1
        if calls==2:raise OSError('fixture disk failure')
        return replace_at(fd,name,data,mode)
    monkeypatch.setattr('bridge.writeback.replace_at',fail_second)
    with pytest.raises(OSError):apply_batch(b,task,result)
    assert (root/'calculator.py').read_text()==before and not (root/'new.py').exists()
    assert (b.store.directory/'backups'/t['id']/'calculator.py').read_text()==before
    t=b.submit('demo','Fixture','write_test');task=b.store.update('task',t['id'],baseline={'calculator.py':before})
    (root/'new.py').write_text('local concurrent addition\n')
    with pytest.raises(ValueError,match='Original changed'):apply_batch(b,task,result)
    assert (root/'calculator.py').read_text()==before


def test_new_untracked_files_visible_to_next_task(config):
    b=enabled(config);b.authorization.grant('demo');f=create_file_bridge(b)
    f.request_write_file('demo','created.py','value=42\n','ABSENT')
    assert b.sources(b.project('demo'))['created.py']=='value=42\n'


def test_oauth_mcp_project_files_and_immediate_revoke(config):
    import httpx
    from mcp.shared.auth import OAuthClientInformationFull
    from bridge.server import build_server
    enabled(config)
    async def run():
        app,b,auth=build_server(config,False);b.authorization.grant('demo')
        client=OAuthClientInformationFull(client_id='policy-fixture',redirect_uris=config['redirect_uris'],token_endpoint_auth_method='none')
        await auth.register_client(client);token=auth.issue(client,['bridge'],auth.resource)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=config['public_url'],headers={'Authorization':'Bearer '+token.access_token,'Accept':'application/json, text/event-stream'}) as http:
                async def call(name,args):
                    r=await http.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}})
                    assert r.status_code==200
                    data=r.json()['result'];assert not data.get('isError'),data
                    return data.get('structuredContent') or json.loads(data['content'][0]['text'])
                p=await call('request_write_file',{'project_id':'demo','relative_path':'new/through_mcp.py','new_content':'value=1\n','expected_sha256':'ABSENT'})
                assert p['status']=='applied'
                r=await call('read_project_file',{'project_id':'demo','relative_path':'new/through_mcp.py'})
                assert r['content']=='value=1\n'
                b.authorization.revoke('demo')
                p=await call('request_edit_file',{'project_id':'demo','relative_path':'new/through_mcp.py','old_text':'1','new_text':'2','expected_sha256':r['sha256']})
                assert p['status']=='pending_local_approval'
                assert (Path(config['projects'][0]['root'])/'new/through_mcp.py').read_text()=='value=1\n'
    asyncio.run(run())


def test_single_diff_approval_cannot_follow_changed_project_policy(config):
    b=enabled(config);b.project('demo')['tests']=[]
    t=b.submit('demo','Fix add','write_test');b.authorize_task(t['id'])
    asyncio.run(b.execute(t['id']))
    r=b.review(t['id']);b.authorize_apply(t['id'],r['review_digest'])
    b.project('demo')['include'].append('*.md')
    with pytest.raises(PermissionError,match='scope changed'):b.apply(t['id'])
    assert not b.status(t['id'])['changes_applied']


def test_legacy_v02_task_can_still_apply_after_exact_local_approval(config):
    b=enabled(config);b.project('demo')['tests']=[]
    t=b.submit('demo','Fix add','write_test');b.authorize_task(t['id']);asyncio.run(b.execute(t['id']))
    old=b.task(t['id']);old.pop('project_grant_id');b.store.put('task',t['id'],old)
    r=b.review(t['id']);b.authorize_apply(t['id'],r['review_digest']);b.apply(t['id'])
    assert b.status(t['id'])['changes_applied'] is True
