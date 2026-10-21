import asyncio
import json
from pathlib import Path
import sys
import time
import pytest
from bridge.core import Bridge
from bridge.security import checked, git, redact

@pytest.fixture
def config(tmp_path):
    root=tmp_path/'project';root.mkdir()
    (root/'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    (root/'test_calculator.py').write_text('import unittest\nfrom calculator import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n')
    git(root,'init','--quiet');git(root,'add','--all');git(root,'commit','--quiet','-m','fixture')
    return {'state_dir':str(tmp_path/'state'),'public_url':'https://bridge.example.ts.net',
       'redirect_uris':['https://chatgpt.com/connector_platform_oauth_redirect'],
       'codex_command':[sys.executable,str(Path(__file__).with_name('fake_appserver.py'))],
       'projects':[{'id':'demo','root':str(root),'include':['*.py'],'tests':[[sys.executable,'-m','unittest','discover','-v']]}]}

@pytest.mark.parametrize('relative',['../outside.py','/etc/passwd','.git/config','.env','key.pem','.codex/config.toml'])
def test_paths(config,relative):
    with pytest.raises(ValueError): checked(config['projects'][0]['root'],relative,['*'])

def test_symlink(config,tmp_path):
    root=Path(config['projects'][0]['root']);(root/'escape.py').symlink_to(tmp_path/'secret')
    with pytest.raises(ValueError): checked(root,'escape.py',['*.py'])

def test_read_only_and_unknown_project(config):
    b=Bridge(config)
    with pytest.raises(ValueError):b.submit('unknown','x')
    t=b.submit('demo','Analyze')
    asyncio.run(b.execute(t['id']))
    assert b.status(t['id'])['state']=='succeeded'
    assert b.result(t['id'])['diff']==''
    assert 'a - b' in (Path(config['projects'][0]['root'])/'calculator.py').read_text()

def test_approval_is_required_and_bound_to_config(config):
    b=Bridge(config);t=b.submit('demo','Fix add','write_test')
    assert t['state']=='awaiting_approval'
    asyncio.run(b.execute(t['id']));assert b.status(t['id'])['state']=='failed'
    t=b.submit('demo','Fix add','write_test');b.authorize_task(t['id'])
    b.projects['demo']['tests']=[['dangerous-command']]
    asyncio.run(b.execute(t['id']));assert 'stale' in b.status(t['id'])['error']

def test_fixture_two_turns_real_test_subprocess_and_review(config,monkeypatch):
    # Only this trusted fixture bypasses bubblewrap to test Bridge orchestration.
    # The actual production sandbox is tested separately; this is not E2E.
    monkeypatch.setattr('bridge.core.sandbox_command',lambda root,argv:argv)
    original=__import__('bridge.core',fromlist=['Bridge']).asyncio.create_subprocess_exec
    async def launch(*args,**kwargs):
        if args[0]==sys.executable and '-m' in args:
            snapshot=next(Path(config['state_dir']).glob('snapshots/*'))
            # Use latest task snapshot for the real unit-test subprocess.
            snapshot=max(Path(config['state_dir']).glob('snapshots/*'),key=lambda p:p.stat().st_mtime_ns)
            kwargs['cwd']=snapshot
        return await original(*args,**kwargs)
    monkeypatch.setattr('bridge.core.asyncio.create_subprocess_exec',launch)
    b=Bridge(config);first=b.submit('demo','Fix add','write_test');b.authorize_task(first['id'])
    asyncio.run(b.execute(first['id']))
    assert b.status(first['id'])['execution_succeeded']
    assert b.status(first['id'])['tests_passed'] is True
    assert 'Ran 1 test' in b.result(first['id'])['tests'][0]['output']
    second=b.submit('demo','Add multiply','write_test',first['id']);b.authorize_task(second['id'])
    asyncio.run(b.execute(second['id']))
    assert b.status(second['id'])['thread_id']==b.status(first['id'])['thread_id']
    r=b.review(second['id']);assert 'multiply' in r['diff'] and 'return a + b' in r['diff']
    with pytest.raises(PermissionError):b.apply(second['id'])
    b.authorize_apply(second['id'],r['review_digest']);b.apply(second['id'])
    assert b.status(second['id'])['changes_applied'] is True
    assert b.status(second['id'])['source_commit_created'] is False
    assert 'multiply' in (Path(config['projects'][0]['root'])/'calculator.py').read_text()

def test_stale_original_rejected(config,monkeypatch):
    config['projects'][0]['tests']=[]
    b=Bridge(config);t=b.submit('demo','Fix add','write_test');b.authorize_task(t['id']);asyncio.run(b.execute(t['id']))
    r=b.review(t['id']);b.authorize_apply(t['id'],r['review_digest'])
    (Path(config['projects'][0]['root'])/'calculator.py').write_text('local edit')
    with pytest.raises(ValueError,match='Original changed'):b.apply(t['id'])

def test_idempotency(config):
    b=Bridge(config);t=b.submit('demo','x',request_id='abc')
    assert b.submit('demo','x',request_id='abc')['id']==t['id']
    with pytest.raises(ValueError):b.submit('demo','different',request_id='abc')

def test_redaction(monkeypatch):
    monkeypatch.setenv('SOME_API_KEY','supersecret1234')
    assert 'supersecret1234' not in redact('supersecret1234 sk-abcdefghi Bearer sometoken')
    assert 'sometoken' not in redact('Bearer sometoken')

def test_cancel_queued(config):
    b=Bridge(config);t=b.submit('demo','x');b.cancel(t['id']);assert b.status(t['id'])['state']=='cancelled'

def test_native_approvals_declined_and_api_accounts_rejected(tmp_path):
    from bridge.appserver import AppServer
    script=tmp_path/'engine.py'
    script.write_text('''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize':r={}
 elif m.get('method')=='account/read':r={'account':{'type':'apiKey'}}
 else:continue
 print(json.dumps({'id':m['id'],'result':r}),flush=True)
''')
    async def run():
        server=AppServer([sys.executable,str(script)],tmp_path/'home')
        try:
            with pytest.raises(PermissionError,match='API-key'):await server.start()
        finally:await server.close()
    asyncio.run(run())

def test_stdio_sdk_discovery_and_harmless_call(config,tmp_path):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    import os
    path=tmp_path/'config.json';path.write_text(json.dumps(config));path.chmod(0o600)
    async def run():
        env={**os.environ,'BRIDGE_CONFIG':str(path),'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
        async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m','bridge.stdio'],env=env)) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tools=await session.list_tools()
                names={item.name for item in tools.tools}
                assert len(names)==32
                assert {"list_projects","read_project_file","request_write_file",
                        "get_project_data_access","request_data_access",
                        "inspect_project_data","read_project_data_chunk",
                        "request_data_text_write", "workspace_info", "browse_workspace",
                        "read_workspace_file", "write_workspace_file", "recent_workspace_changes"}.issubset(names)
                result=await session.call_tool('list_projects',{});assert not result.isError
                assert 'demo' in result.content[0].text
    asyncio.run(run())

def test_server_initiated_native_escalation_is_declined(tmp_path):
    from bridge.appserver import AppServer
    script=tmp_path/'approval_engine.py'
    script.write_text('''import json,sys
thread='fixture';turn='turn'
for line in sys.stdin:
 m=json.loads(line)
 def emit(x): print(json.dumps(x),flush=True)
 if m.get('method')=='initialize':emit({'id':m['id'],'result':{}})
 elif m.get('method')=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
 elif m.get('method')=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':thread}}})
 elif m.get('method')=='turn/start':
  emit({'id':m['id'],'result':{'turn':{'id':turn}}})
  emit({'id':999,'method':'item/commandExecution/requestApproval','params':{'threadId':thread,'turnId':turn,'command':'rm -rf /'}})
 elif m.get('id')==999:
  emit({'method':'item/completed','params':{'threadId':thread,'turnId':turn,'item':{'id':'answer','type':'agentMessage','text':json.dumps({'summary':m['result']['decision'],'edits':[]})}}})
  emit({'method':'turn/completed','params':{'threadId':thread,'turn':{'id':turn,'status':'completed'}}})
''')
    async def run():
        server=AppServer([sys.executable,str(script)],tmp_path/'home')
        async def noop(*args):pass
        try:
            result=await server.run({'mode':'read','prompt':'x'}, {},noop,noop)
            assert result['summary']=='decline'
        finally:await server.close()
    asyncio.run(run())

def test_worker_recovers_interrupted_without_automatic_replay(config):
    b=Bridge(config);t=b.submit('demo','x');b.store.update('task',t['id'],state='running',thread_id='saved-thread')
    async def run():
        w=asyncio.create_task(b.worker());await asyncio.sleep(.01);w.cancel()
        try:await w
        except asyncio.CancelledError:pass
    asyncio.run(run())
    assert b.status(t['id'])['state']=='interrupted'
    assert b.status(t['id'])['thread_id']=='saved-thread'
