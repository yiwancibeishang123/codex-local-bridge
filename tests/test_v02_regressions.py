import asyncio
import json
import hashlib
import time
from pathlib import Path
import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull
from bridge.server import build_server
from bridge.filetools import WorkspaceBridge, ProjectPolicy, WorkspaceError
from tests.test_bridge import config


def test_policy_change_cannot_apply_to_different_root(tmp_path):
    a=tmp_path/'a'; b=tmp_path/'b'; a.mkdir(); b.mkdir()
    for p in (a,b): (p/'same.py').write_text('before')
    state=tmp_path/'state'
    first=WorkspaceBridge({'demo':ProjectPolicy(a,('*.py',),True)},state)
    prop=first.request_write_file('demo','same.py','after',hashlib.sha256(b'before').hexdigest())
    second=WorkspaceBridge({'demo':ProjectPolicy(b,('*.py',),True)},state)
    with pytest.raises(WorkspaceError,match='policy changed'):second.approve_and_apply(prop['proposal_id'])
    assert (a/'same.py').read_text()==(b/'same.py').read_text()=='before'


def test_file_scope_expiry_replay_and_binary(tmp_path):
    root=tmp_path/'project';root.mkdir()
    bridge=WorkspaceBridge({'demo':ProjectPolicy(root,('src/*.py',),True)},tmp_path/'state')
    (root/'other').mkdir();(root/'other/x.py').write_text('x')
    with pytest.raises(WorkspaceError):bridge.read_file('demo','other/x.py')
    (root/'src').mkdir();(root/'src/x.py').write_bytes(b'\x00abc')
    with pytest.raises(WorkspaceError,match='Binary'):bridge.request_write_file('demo','src/x.py','text',hashlib.sha256(b'\x00abc').hexdigest())
    p=bridge.request_write_file('demo','src/new.py','created','ABSENT')
    with bridge._db() as db: db.execute('update proposals set created_at=? where id=?',(int(time.time())-90000,p['proposal_id']))
    assert bridge.get_write_status(p['proposal_id'])['status']=='expired'
    with pytest.raises(WorkspaceError,match='expired'):bridge.approve_and_apply(p['proposal_id'])
    p=bridge.request_write_file('demo','src/new.py','created','ABSENT')
    bridge.approve_and_apply(p['proposal_id'])
    with pytest.raises(WorkspaceError):bridge.approve_and_apply(p['proposal_id'])


def test_http_worker_survives_disconnected_requests(config,monkeypatch):
    # Real SDK/ASGI, deterministic engine execution. This checks lifecycle, not inference.
    async def delayed_execute(self,task_id):
        self.store.update('task',task_id,state='running')
        await asyncio.sleep(.15)
        self.store.update('task',task_id,state='succeeded',execution_succeeded=True)
    monkeypatch.setattr('bridge.core.Bridge.execute',delayed_execute)
    async def run():
        app,b,auth=build_server(config)
        client=OAuthClientInformationFull(client_id='lifecycle-test',redirect_uris=config['redirect_uris'],token_endpoint_auth_method='none')
        await auth.register_client(client);token=auth.issue(client,['bridge'],auth.resource)
        headers={'Authorization':'Bearer '+token.access_token,'Accept':'application/json, text/event-stream'}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=config['public_url'],headers=headers) as c:
                r=await c.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'delegate_task','arguments':{'project_id':'demo','prompt':'read'}}})
                assert r.status_code==200
                result=r.json()['result'];task_id=(result.get('structuredContent') or json.loads(result['content'][0]['text']))['id']
            # The client/request is gone, but the application worker must keep running.
            deadline=time.monotonic()+3
            while b.status(task_id)['state']!='succeeded':
                assert time.monotonic()<deadline
                await asyncio.sleep(.05)
            assert b.status(task_id)['execution_succeeded']
    asyncio.run(run())


def test_authenticated_file_tools_and_local_approval(config):
    config['projects'][0]['file_write_enabled']=True
    async def run():
        app,b,auth=build_server(config,False)
        from bridge.filetools import create_file_bridge
        files=create_file_bridge(b)
        client=OAuthClientInformationFull(client_id='files-test',redirect_uris=config['redirect_uris'],token_endpoint_auth_method='none')
        await auth.register_client(client);token=auth.issue(client,['bridge'],auth.resource)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=config['public_url'],headers={'Authorization':'Bearer '+token.access_token,'Accept':'application/json, text/event-stream'}) as c:
                async def call(name,args):
                    r=await c.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}})
                    assert r.status_code==200
                    result=r.json()['result'];assert not result.get('isError'),result
                    return result.get('structuredContent') or json.loads(result['content'][0]['text'])
                p=await call('request_write_file',{'project_id':'demo','relative_path':'created.py','new_content':'hello\n','expected_sha256':'ABSENT'})
                root=Path(config['projects'][0]['root']);assert not (root/'created.py').exists()
                assert (await call('get_write_status',{'proposal_id':p['proposal_id']}))['status']=='pending'
                files.approve_and_apply(p['proposal_id']) # Trusted fixture's LOCAL approval, never remote tool.
                assert (await call('read_project_file',{'project_id':'demo','relative_path':'created.py'}))['content']=='hello\n'
                names={x.name for x in (await app.bridge_fastmcp.list_tools())}
                assert 'approve_and_apply' not in names and 'approve_file' not in names
    asyncio.run(run())


def test_sensitive_context_not_duplicated_in_generic_logs(config):
    from bridge.core import Bridge
    b=Bridge(config);t=b.submit('demo','analyze')
    async def run():
        await b.log(t['id'],'item/started',{'item':{'id':'u','type':'userMessage','content':'PRIVATE_SOURCE'}})
        await b.log(t['id'],'item/completed',{'item':{'id':'r','type':'reasoning','text':'HIDDEN_REASONING'}})
        await b.log(t['id'],'turn/completed',{'turn':{'id':'x','status':'completed','items':[{'text':'STRUCTURED_SOURCE'}]}})
    asyncio.run(run())
    logs=b.result(t['id'])['logs']
    assert all(x not in logs for x in ('PRIVATE_SOURCE','HIDDEN_REASONING','STRUCTURED_SOURCE'))


def test_no_sensitive_file_lists_and_output_limits(tmp_path):
    from bridge.filetools import MAX_BYTES
    root=tmp_path/'project';root.mkdir()
    b=WorkspaceBridge({'demo':ProjectPolicy(root,('*.py',),True)},tmp_path/'state')
    (root/'outside.py').write_text('sensitive')
    import os
    os.link(root/'outside.py',root/'linked.py')
    assert not b.list_files('demo')['entries']
    (root/'huge.py').write_bytes(b'a'*(MAX_BYTES+1))
    with pytest.raises(WorkspaceError):b.read_file('demo','huge.py')
    with pytest.raises(WorkspaceError):b.request_write_file('demo','new.py','a'*(MAX_BYTES+1),'ABSENT')
    with pytest.raises(WorkspaceError):b.request_write_file('demo','new.py','a','wrongsha')


def test_file_write_setting_requires_actual_boolean(config):
    from bridge.core import Bridge
    from bridge.filetools import create_file_bridge
    config['projects'][0]['file_write_enabled']='false'
    with pytest.raises(WorkspaceError,match='boolean'):create_file_bridge(Bridge(config))


def test_real_cli_terminal_file_approval(config,tmp_path):
    # A dedicated fixture project only; verifies the actual CLI terminal flow.
    import os,pty,select,subprocess,sys
    from bridge.core import Bridge
    from bridge.filetools import create_file_bridge
    config['projects'][0]['file_write_enabled']=True
    config_path=tmp_path/'config.json';config_path.write_text(json.dumps(config));config_path.chmod(0o600)
    files=create_file_bridge(Bridge(config))
    proposal=files.request_write_file('demo','from_terminal.py','answer = 42\n','ABSENT')
    command=[sys.executable,'-m','bridge.cli','approve-file',proposal['proposal_id']]
    env={**os.environ,'BRIDGE_CONFIG':str(config_path)}
    denied=subprocess.run(command,env=env,input='',text=True,capture_output=True,timeout=10)
    assert denied.returncode!=0 and 'Interactive local terminal required' in denied.stderr
    assert not (Path(config['projects'][0]['root'])/'from_terminal.py').exists()
    master,slave=pty.openpty();out=b''
    process=subprocess.Popen(command,env=env,stdin=slave,stdout=slave,stderr=slave);os.close(slave)
    try:
        deadline=time.monotonic()+10
        while b'to apply:' not in out:
            assert time.monotonic()<deadline
            ready,_,_=select.select([master],[],[],.1)
            if ready:out+=os.read(master,8192)
        assert b'+answer = 42' in out
        os.write(master,('APPROVE '+proposal['proposal_id'][-6:]+'\n').encode())
        assert process.wait(timeout=10)==0
    finally:
        if process.poll() is None:process.kill();process.wait()
        os.close(master)
    assert files.get_write_status(proposal['proposal_id'])['status']=='applied'
    assert (Path(config['projects'][0]['root'])/'from_terminal.py').read_text()=='answer = 42\n'


def test_explicit_context_for_non_git_read_only_project(config):
    from bridge.core import Bridge
    root=Path(config['projects'][0]['root'])
    (root/'notes.md').write_text('authorized workspace note')
    config['projects'][0].update(include=['*.md'],context_files=['notes.md'],allowed_modes=['read'])
    # The explicitly authorized context is independent of Git tracking.
    b=Bridge(config)
    assert b.sources(b.project('demo'))=={'notes.md':'authorized workspace note'}
    assert b.submit('demo','analyze')['state']=='queued'
    with pytest.raises(PermissionError,match='not authorized'):b.submit('demo','modify','write_test')
    config['projects'][0]['context_files']=['../outside.md']
    with pytest.raises(ValueError,match='No authorized'):b.sources(b.project('demo'))
