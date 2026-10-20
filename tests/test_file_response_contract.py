"""Real SDK/HTTP/stdio wire contracts, not a mock ChatGPT connector."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import httpx
import jsonschema
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.auth import OAuthClientInformationFull
from bridge.core import Bridge
from bridge.filetools import create_file_bridge, WorkspaceBridge
from bridge.server import build_server
from tests.test_bridge import config

FILE_TOOLS={'list_project_files','read_project_file','request_write_file','request_edit_file','get_write_status'}


def test_discovery_declares_file_fields_instead_of_text_only(config):
    async def run():
        app,_,_=build_server(config,False)
        tools={t.name:t for t in await app.bridge_fastmcp.list_tools()}
        for name in FILE_TOOLS:
            schema=tools[name].outputSchema
            assert schema and schema['type']=='object' and schema['properties'],name
            assert schema['additionalProperties'] is False
        schema=tools['read_project_file'].outputSchema
        assert set(schema['required'])=={'project_id','path','sha256','bytes','content','redacted'}
        assert schema['properties']['content']['type']=='string'
        assert schema['properties']['sha256']['pattern']=='^[0-9a-f]{64}$'
        assert 'result' not in schema['properties'] # no extra connector-dependent envelope
    asyncio.run(run())


@pytest.mark.parametrize('content',['', '你好，Codex！\n第二行 🐍\r\n', '\ufeff# UTF-8 BOM\nvalue = 42\n'])
def test_utf8_file_survives_http_structured_and_text_serialization(config,content):
    root=Path(config['projects'][0]['root']);raw=content.encode('utf-8')
    (root/'unicode.py').write_bytes(raw)
    async def run():
        app,b,auth=build_server(config,False)
        client=OAuthClientInformationFull(client_id='file-contract',redirect_uris=config['redirect_uris'],token_endpoint_auth_method='none')
        await auth.register_client(client);token=auth.issue(client,['bridge'],auth.resource)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=config['public_url'],headers={'Authorization':'Bearer '+token.access_token,'Accept':'application/json, text/event-stream'}) as http:
                r=await http.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'read_project_file','arguments':{'project_id':'demo','relative_path':'unicode.py'}}})
                assert r.status_code==200
                wire=r.json()['result'];assert not wire.get('isError')
                # Do NOT fall back to parsing content when structuredContent is missing.
                data=wire['structuredContent'];assert data!={}
                assert data==json.loads(wire['content'][0]['text'])
                assert data==create_file_bridge(b).read_file('demo','unicode.py')
                assert data['path']=='unicode.py' and data['content']==content
                assert data['sha256']==hashlib.sha256(raw).hexdigest() and data['bytes']==len(raw)
                assert data['redacted'] is False
                tool=next(t for t in await app.bridge_fastmcp.list_tools() if t.name=='read_project_file')
                jsonschema.validate(data,tool.outputSchema)
    asyncio.run(run())


@pytest.mark.parametrize('name,raw',[('missing.py',None),('binary.py',b'abc\x00def'),('invalid.py',b'\xff')])
def test_read_errors_are_mcp_errors_never_empty_success(config,name,raw):
    if raw is not None:(Path(config['projects'][0]['root'])/name).write_bytes(raw)
    async def run():
        app,_,_=build_server(config,False)
        # Real SDK raises ToolError, serialized to isError=true by the protocol.
        from mcp.server.fastmcp.exceptions import ToolError
        with pytest.raises(ToolError):
            await app.bridge_fastmcp.call_tool('read_project_file',{'project_id':'demo','relative_path':name})
    asyncio.run(run())


def test_invalid_read_payload_fails_closed(config,monkeypatch):
    monkeypatch.setattr(WorkspaceBridge,'read_file',lambda *a:{})
    async def run():
        from mcp.server.fastmcp.exceptions import ToolError
        app,_,_=build_server(config,False)
        with pytest.raises(ToolError,match='validation'):
            await app.bridge_fastmcp.call_tool('read_project_file',{'project_id':'demo','relative_path':'calculator.py'})
    asyncio.run(run())


def test_stdio_structured_read_and_redaction_sha(config,tmp_path):
    root=Path(config['projects'][0]['root']);raw=b'password = "fixture-private-value"\n'
    (root/'credential_example.py').write_bytes(raw)
    path=tmp_path/'config.json';path.write_text(json.dumps(config));path.chmod(0o600)
    async def run():
        env={**os.environ,'BRIDGE_CONFIG':str(path),'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
        async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m','bridge.stdio'],env=env)) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                result=await session.call_tool('read_project_file',{'project_id':'demo','relative_path':'credential_example.py'})
                assert not result.isError and result.structuredContent
                data=result.structuredContent
                assert data==json.loads(result.content[0].text)
                assert data['redacted'] and 'fixture-private-value' not in data['content']
                assert data['sha256']==hashlib.sha256(raw).hexdigest()
    asyncio.run(run())


def test_persistent_grant_reused_across_bridge_instances_and_revoked(config):
    p=config['projects'][0];p.update(project_authorization=True,file_write_enabled=True,allowed_modes=['read','write_test'])
    first=Bridge(config);first.authorization.grant('demo')
    second=Bridge(config);files=create_file_bridge(second)
    created=files.request_write_file('demo','persistent.py','value = 1\n','ABSENT')
    assert created['status']=='applied'
    read=files.read_file('demo','persistent.py')
    third=Bridge(config);thirdfiles=create_file_bridge(third)
    assert thirdfiles.request_edit_file('demo','persistent.py','1','2',read['sha256'])['status']=='applied'
    assert third.authorization.active('demo')['expires_at'] is None
    assert second.authorization.active('demo')['id']==first.authorization.active('demo')['id']
    first.authorization.revoke('demo')
    read=thirdfiles.read_file('demo','persistent.py')
    assert thirdfiles.request_edit_file('demo','persistent.py','2','3',read['sha256'])['status']=='pending_local_approval'
    assert thirdfiles.read_file('demo','persistent.py')['content']=='value = 2\n'


def test_listings_keep_directory_and_file_entry_shapes(config):
    root=Path(config['projects'][0]['root']);(root/'subdir').mkdir()
    async def run():
        app,_,_=build_server(config,False)
        text,data=await app.bridge_fastmcp.call_tool('list_project_files',{'project_id':'demo'})
        assert data==json.loads(text[0].text)
        assert {'directory':'subdir'} in data['entries']
        assert all(e.get('path') for e in data['entries'] if 'path' in e)
    asyncio.run(run())
