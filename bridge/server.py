import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import HTMLResponse, RedirectResponse
from .auth import LocalOAuth
from .core import Bridge


def build_server(config,with_worker=True):
    bridge=Bridge(config)
    issuer=config['public_url'].rstrip('/')
    if not issuer.startswith('https://') and not issuer.startswith('http://127.0.0.1:'):
        raise ValueError('Public issuer requires HTTPS')
    auth=LocalOAuth(bridge.store,issuer,config.get('redirect_uris',[]))
    @asynccontextmanager
    async def lifespan(_):
        worker=asyncio.create_task(bridge.worker()) if with_worker else None
        if worker:
            await asyncio.sleep(0)
            if worker.done(): await worker
        try: yield {}
        finally:
            if worker:
                worker.cancel()
                try: await worker
                except asyncio.CancelledError: pass
    server=FastMCP('Codex Local Bridge',host='127.0.0.1',port=config.get('port',8767),
        auth_server_provider=auth,
        auth=AuthSettings(issuer_url=issuer,resource_server_url=issuer+'/mcp',required_scopes=['bridge'],
                         validate_token_resource=True,
                         client_registration_options=ClientRegistrationOptions(enabled=True,valid_scopes=['bridge'],default_scopes=['bridge']),
                         revocation_options=RevocationOptions(enabled=True)),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                    allowed_hosts=[urlparse(issuer).netloc,'127.0.0.1:*','localhost:*'],
                    allowed_origins=[issuer,'https://chatgpt.com']),
        stateless_http=True,json_response=True,log_level='WARNING')
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
    write=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=False)
    @server.tool(annotations=read)
    def list_projects() -> list[dict]:
        """List only locally authorized projects and their file/test scopes."""
        return bridge.list_projects()
    @server.tool(annotations=write)
    def delegate_task(project_id:str,prompt:str,mode:str='read',request_id:str|None=None,
                      context_paths:list[str]|None=None) -> dict:
        """Queue a task. write_test uses an active project grant or separate local CLI approval; submission is not completion."""
        return bridge.submit(project_id,prompt,mode,request_id=request_id,context_paths=context_paths)
    @server.tool(annotations=read)
    def get_task_status(task_id:str) -> dict:
        """Poll state; execution success, tests passed, applied changes and commits are separate."""
        return bridge.status(task_id)
    @server.tool(annotations=read)
    def get_task_result(task_id:str,offset:int=0,limit:int=20000) -> dict:
        """Return proposed diff, test output, paginated logs and exact review digest."""
        return bridge.result(task_id,offset,limit)
    @server.tool(annotations=write)
    def continue_task(task_id:str,prompt:str,mode:str='read',request_id:str|None=None,
                      context_paths:list[str]|None=None) -> dict:
        """Continue the same saved Codex thread from a terminal task; active project authorization or separate approval for write_test."""
        t=bridge.task(task_id)
        return bridge.submit(t['project_id'],prompt,mode,task_id,request_id,context_paths)
    # BEGIN LOCAL FILE TOOLS 0.2 -- project-scoped, locally approved edits
    from .filetools import create_file_bridge, register_file_tools
    register_file_tools(server, create_file_bridge(bridge))
    from .memory_tools import register_memory_tools
    register_memory_tools(server, bridge)
    from .data_access import register_data_tools
    register_data_tools(server, bridge)
    from .trusted_workspace import register_trusted_workspace_tools
    register_trusted_workspace_tools(server, bridge)
    # END LOCAL FILE TOOLS 0.2
    @server.custom_route('/consent',methods=['GET'])
    async def consent(request):
        id=request.query_params.get('request','')
        try: target=auth.finish(id)
        except ValueError: return HTMLResponse('Consent expired or unknown',status_code=400)
        if target: return RedirectResponse(target,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
        import html
        safe=html.escape(id)
        return HTMLResponse('<!doctype html><html><meta name="referrer" content="no-referrer"><title>Codex Local Bridge</title>'
            '<h1>Authorize your private bridge</h1><p>Approve this request on your Ubuntu computer. '
            'This grants ChatGPT access to configured project sources and task history. '
            'Code changes and tests follow your local project permissions. '
            'Active project grants allow ordinary code writes and sandboxed tests; '
            'other changes require separate local approval.</p><pre>'
            'codex-local-bridge approve-auth '+safe+'</pre><p>Then reload this page.</p></html>',
            headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"})
    app=server.streamable_http_app()
    # Stateless HTTP creates a protocol session per request. Background work must
    # belong to the ASGI application, never to an individual protocol session.
    original_lifespan=app.router.lifespan_context
    @asynccontextmanager
    async def application_lifespan(application):
        async with original_lifespan(application):
            async with lifespan(application):
                yield {}
    app.router.lifespan_context=application_lifespan
    # Stable ChatGPT callback requires issuer identification (RFC 9207).
    from mcp.server.auth.routes import build_metadata
    from pydantic import AnyHttpUrl
    from starlette.routing import Route
    from starlette.responses import JSONResponse
    discovery=build_metadata(AnyHttpUrl(auth.issuer), None,
        ClientRegistrationOptions(enabled=True,valid_scopes=['bridge'],default_scopes=['bridge']),
        RevocationOptions(enabled=True)).model_dump(mode='json',exclude_none=True)
    discovery['authorization_response_iss_parameter_supported']=True
    async def metadata(request): return JSONResponse(discovery)
    app.routes[:]=[Route('/.well-known/oauth-authorization-server',metadata,methods=['GET'])
        if getattr(route,'path',None)=='/.well-known/oauth-authorization-server' else route for route in app.routes]
    app.bridge_fastmcp=server
    return app,bridge,auth
