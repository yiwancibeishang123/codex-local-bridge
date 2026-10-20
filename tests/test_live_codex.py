"""Opt-in real Codex + MCP + bubblewrap test on a disposable fixture project.
No model mocks, sandbox bypasses or production task approvals.
Run: BRIDGE_LIVE_CODEX=1 .venv/bin/python -m pytest tests/test_live_codex.py -q -s
"""
import asyncio
import json
import os
import time
from pathlib import Path
import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull
from bridge.appserver import AppServer as RealAppServer
from bridge.cli import load_config
from bridge.server import build_server
from bridge.security import git

@pytest.mark.skipif(os.environ.get('BRIDGE_LIVE_CODEX')!='1',reason='Requires explicit opt-in and existing ChatGPT Codex login')
def test_real_model_two_rounds_with_sandbox(tmp_path,monkeypatch):
    installed=load_config()
    real_home=Path(installed['state_dir'])/'codex-home'
    assert real_home.is_dir()
    # Dependency injection points at the real authenticated engine; output/events
    # and filesystem/test execution are all real and are not monkeypatched.
    monkeypatch.setattr('bridge.core.AppServer',lambda command,home,timeout:RealAppServer(command,real_home,timeout))
    root=tmp_path/'fixture';root.mkdir()
    baseline='def add(a, b):\n    return a - b\n'
    (root/'calculator.py').write_text(baseline)
    (root/'test_calculator.py').write_text('import unittest\nfrom calculator import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n')
    git(root,'init','--quiet');git(root,'add','--all');git(root,'commit','--quiet','-m','Disposable live fixture')
    config={**installed,'public_url':'http://127.0.0.1:8767','state_dir':str(tmp_path/'state'),'task_timeout':180,
            'projects':[{'id':'live-fixture','root':str(root),'include':['*.py'],'project_authorization':True,'file_write_enabled':True,'allowed_modes':['read','write_test'],'tests':[['/usr/bin/python3','-m','unittest','discover','-v']]}]}
    report={'kind':'real-model-project-grant-auto-writeback-on-disposable-fixture','chatgpt_web_write_verified':False,'complete':False,'tasks':[]}
    async def run():
        app,b,auth=build_server(config)
        b.authorization.grant('live-fixture')
        client=OAuthClientInformationFull(client_id='real-model-test',redirect_uris=config['redirect_uris'],token_endpoint_auth_method='none')
        await auth.register_client(client);token=auth.issue(client,['bridge'],auth.resource)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=config['public_url'],headers={'Authorization':'Bearer '+token.access_token,'Accept':'application/json, text/event-stream'}) as c:
                r=await c.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'live-integration-test','version':'0.2.0'}}})
                assert r.status_code==200
                async def call(name,args):
                    r=await c.post('/mcp',json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':name,'arguments':args}})
                    assert r.status_code==200
                    result=r.json()['result'];assert not result.get('isError'),result
                    return result.get('structuredContent') or json.loads(result['content'][0]['text'])
                previous=None
                for prompt in ('Fix calculator.py add to return a+b. Keep test_calculator.py unchanged.',
                               'Keep the previous add fix. Add multiply(a,b) and a unittest for multiply(3,4)==12. Keep the existing add test. Return both updated files.'):
                    args={'task_id':previous} if previous else {'project_id':'live-fixture'}
                    t=await call('continue_task' if previous else 'delegate_task',{**args,'prompt':prompt,'mode':'write_test'})
                    assert t['state']=='queued'
                    assert t['project_grant_id']
                    # No per-task or per-diff approvals: one disposable project grant.
                    deadline=time.monotonic()+200
                    while True:
                        status=await call('get_task_status',{'task_id':t['id']})
                        if status['state'] in ('failed','succeeded','cancelled','interrupted'):break
                        assert time.monotonic()<deadline
                        await asyncio.sleep(.3)
                    result=await call('get_task_result',{'task_id':t['id'],'limit':1})
                    report['tasks'].append({k:v for k,v in result.items() if k not in ('logs','next_offset')})
                    print(json.dumps({'state':status['state'],'thread':status['thread_id'],'tests':result['tests']},ensure_ascii=False),flush=True)
                    assert status['state']=='succeeded',status
                    assert status['tests_passed'] is True,result
                    assert status['changes_applied'] is True,status
                    assert result['tests'][0]['exit_code']==0
                    assert result['diff']
                    previous=t['id']
                a,bresult=report['tasks']
                assert a['status']['thread_id']==bresult['status']['thread_id']
                assert 'Ran 1 test' in a['tests'][0]['output']
                assert 'Ran 2 tests' in bresult['tests'][0]['output']
                assert 'multiply' in bresult['diff']
                assert 'return a + b' in (root/'calculator.py').read_text()
                assert 'multiply' in (root/'calculator.py').read_text()
                b.authorization.revoke('live-fixture')
                assert not b.authorization.active('live-fixture')
                report['same_thread_verified']=True
                report['production_sources_unchanged']=True
                report['complete']=True
    try:asyncio.run(run())
    finally:
        out=Path(installed['state_dir'])/'real-model-fixture-report-v03.json'
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2));out.chmod(0o600)
        print('Evidence:',out,flush=True)
