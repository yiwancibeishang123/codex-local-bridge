"""Real local engine + public HTTPS MCP acceptance; never substitutes a fake engine."""
import asyncio
import json
from pathlib import Path
import time
import secrets
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull
from .auth import LocalOAuth
from .core import Bridge,TERMINAL

async def run(config,project):
    bridge=Bridge(config)
    if input('This runs two real Codex turns and approved sandboxed tests on the demo. Type RUN: ')!='RUN':
        raise ValueError('Not authorized')
    auth=LocalOAuth(bridge.store,config['public_url'],config['redirect_uris'])
    # Locally authorized diagnostic token; full external OAuth flow is covered by HTTP tests.
    client=OAuthClientInformationFull(client_id='local-acceptance-'+secrets.token_hex(8),redirect_uris=config['redirect_uris'],token_endpoint_auth_method='none')
    await auth.register_client(client)
    token=auth.issue(client,['bridge'],auth.resource)
    report={'kind':'real-engine-and-https-mcp','started_at':time.time(),'chatgpt_web_call_verified':False,
            'oauth_browser_link_verified':False,'tasks':[],'complete':False}
    output=bridge.store.directory/'acceptance-report.json'
    try:
        async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token.access_token},timeout=30) as http:
            async with streamable_http_client(auth.resource,http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    async def call(name,arguments):
                        result=await session.call_tool(name,arguments)
                        if result.isError: raise RuntimeError(str(result.content))
                        if result.structuredContent is not None: return result.structuredContent
                        return json.loads(result.content[0].text)
                    projects=await call('list_projects',{})
                    report['https_tool_discovery_verified']=True
                    previous=None
                    for prompt in ('Fix calculator.py add(a,b) to return a+b. Keep tests unchanged.',
                                   'Continue from the previous fix. Add multiply(a,b), and add a unittest for multiply(3,4)==12. Keep add correct.'):
                        name='continue_task' if previous else 'delegate_task'
                        args={'task_id':previous} if previous else {'project_id':project}
                        submitted=await call(name,{**args,'prompt':prompt,'mode':'write_test','request_id':secrets.token_hex(16)})
                        id=submitted['id'];bridge.authorize_task(id)
                        deadline=time.monotonic()+config.get('task_timeout',900)+30
                        while True:
                            status=await call('get_task_status',{'task_id':id})
                            if status['state'] in TERMINAL:break
                            if time.monotonic()>deadline:raise TimeoutError('Acceptance task timed out')
                            await asyncio.sleep(1)
                        result=await call('get_task_result',{'task_id':id})
                        report['tasks'].append(result)
                        if status['state']!='succeeded' or status['tests_passed'] is not True:
                            raise RuntimeError('Real task or sandbox test failed; inspect acceptance report')
                        if not result.get('diff'):raise RuntimeError('No code diff returned')
                        previous=id
                    threads=[r['status']['thread_id'] for r in report['tasks']]
                    if threads[0]!=threads[1]:raise RuntimeError('Second turn did not resume same thread')
                    report['same_thread_verified']=True
                    report['complete']=True
                    print('Real Codex + HTTPS MCP two-turn check passed. ChatGPT web invocation is still a separate checkpoint.')
    except Exception as exc:
        from .security import redact
        report['error']=redact(exc);raise
    finally:
        access=await auth.load_access_token(token.access_token)
        if access:await auth.revoke_token(access)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print('Evidence report:',output)
