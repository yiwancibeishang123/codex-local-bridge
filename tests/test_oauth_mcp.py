import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import urlparse,parse_qs
import httpx
import pytest
from bridge.server import build_server
from tests.test_bridge import config


def test_actual_http_oauth_pkce_and_mcp(config):
    async def run():
        app,b,auth=build_server(config,False)
        transport=httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url=config['public_url']) as c:
            discovery=await c.get('/.well-known/oauth-authorization-server');assert discovery.status_code==200
            metadata=discovery.json();assert metadata['authorization_response_iss_parameter_supported'] is True
            r=await c.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
            assert r.status_code==401
            assert 'resource_metadata' in r.headers['www-authenticate']
            r=await c.post('/register',json={'redirect_uris':config['redirect_uris'],'token_endpoint_auth_method':'none'})
            assert r.status_code==201,r.text
            client_id=r.json()['client_id']
            verifier='v'*64;challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            r=await c.get('/authorize',params={'client_id':client_id,'redirect_uri':config['redirect_uris'][0],
                'response_type':'code','scope':'bridge','state':'test-state','code_challenge':challenge,
                'code_challenge_method':'S256','resource':auth.resource})
            assert r.status_code==302,r.text
            consent=r.headers['location']; request_id=parse_qs(urlparse(consent).query)['request'][0]
            r=await c.get(consent);assert r.status_code==200 and 'Approve' in r.text
            auth.approve(request_id)
            r=await c.get(consent);assert r.status_code==307
            query=parse_qs(urlparse(r.headers['location']).query)
            assert query['iss'][0]==metadata['issuer']
            code=query['code'][0]
            form={'grant_type':'authorization_code','client_id':client_id,'code':code,
                  'redirect_uri':config['redirect_uris'][0],'code_verifier':'wrong','resource':auth.resource}
            r=await c.post('/token',data=form);assert r.status_code==400
            form['code_verifier']=verifier
            r=await c.post('/token',data=form);assert r.status_code==200,r.text
            token=r.json();headers={'Authorization':'Bearer '+token['access_token'],
                'Accept':'application/json, text/event-stream'}
            # Real MCP SDK lifecycle is required even in stateless HTTP mode.
            async with app.router.lifespan_context(app):
                r=await c.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize',
                    'params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
                assert r.status_code==200,r.text
                r=await c.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list'})
                assert r.status_code==200,r.text
                assert {t['name'] for t in r.json()['result']['tools']}=={'list_projects','delegate_task','get_task_status','get_task_result','continue_task','list_project_files','read_project_file','request_write_file','request_edit_file','get_write_status'}
                r=await c.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call',
                    'params':{'name':'delegate_task','arguments':{'project_id':'demo','prompt':'Fix add','mode':'write_test'}}})
                assert r.status_code==200 and 'awaiting_approval' in r.text
            r=await c.post('/token',data=form);assert r.status_code==400 # Code replay rejected.
            r=await c.post('/token',data={'grant_type':'refresh_token','client_id':client_id,
                'refresh_token':token['refresh_token'],'scope':'bridge','resource':auth.resource})
            assert r.status_code==200,r.text
            assert await auth.load_access_token(token['access_token']) is None
            refreshed=r.json()
            access=await auth.load_access_token(refreshed['access_token']);assert access
            await auth.revoke_token(access);assert await auth.load_access_token(refreshed['access_token']) is None
    asyncio.run(run())

def test_bad_redirect_and_resource(config):
    async def run():
        app,b,auth=build_server(config,False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=config['public_url']) as c:
            r=await c.post('/register',json={'redirect_uris':['https://evil.invalid/callback'],'token_endpoint_auth_method':'none'})
            assert r.status_code==400
    asyncio.run(run())

def test_real_tcp_service_authentication(config,tmp_path):
    import os,socket,subprocess,sys
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    config={**config,'port':port,'public_url':f'http://127.0.0.1:{port}'}
    path=tmp_path/'service.json';path.write_text(json.dumps(config));path.chmod(0o600)
    env={**os.environ,'BRIDGE_CONFIG':str(path),'PYTHONPATH':str(__import__('pathlib').Path(__file__).resolve().parents[1])}
    process=subprocess.Popen([sys.executable,'-m','bridge.cli','serve'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        with httpx.Client(base_url=config['public_url'],timeout=2,trust_env=False) as c:
            deadline=time.monotonic()+10
            while True:
                try:r=c.get('/.well-known/oauth-authorization-server');break
                except httpx.ConnectError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.1)
            assert r.status_code==200
            r=c.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
            assert r.status_code==401
    finally:
        process.terminate();process.wait(timeout=5)
