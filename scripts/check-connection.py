#!/usr/bin/env python3
"""Real public HTTPS diagnostic with a short-lived, locally issued credential.
Does not impersonate ChatGPT; does not approve tasks or write project files.
"""
import asyncio
import json
import secrets
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from bridge.cli import load_config
from bridge.store import Store
from bridge.auth import LocalOAuth
from mcp.shared.auth import OAuthClientInformationFull

async def main():
    config=load_config();store=Store(config['state_dir'])
    auth=LocalOAuth(store,config['public_url'],config['redirect_uris'])
    client=OAuthClientInformationFull(client_id='diagnostic-'+secrets.token_hex(8),redirect_uris=config['redirect_uris'],token_endpoint_auth_method='none')
    await auth.register_client(client);issued=auth.issue(client,['bridge'],auth.resource)
    token=issued.access_token
    report={'kind':'real-https-file-tools-diagnostic','chatgpt_host_verified':False,'project_writes_performed':False,'checks':{}}
    try:
        async with httpx.AsyncClient(timeout=30,trust_env=False) as http:
            response=await http.post(config['public_url']+'/token',data={'grant_type':'refresh_token','client_id':client.client_id,'refresh_token':issued.refresh_token,'scope':'bridge','resource':auth.resource})
            response.raise_for_status();token=response.json()['access_token'];report['checks']['refresh_http']=response.status_code
            headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'}
            async def rpc(method,params):
                r=await http.post(auth.resource,headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
                r.raise_for_status();data=r.json()
                if 'error' in data:raise RuntimeError(str(data['error']))
                return data['result']
            await rpc('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'local-diagnostic','version':'0.2.0'}})
            declarations=(await rpc('tools/list',{}))['tools']
            names=sorted(t['name'] for t in declarations);report['tools']=names
            assert len(names)==10 and 'approve_and_apply' not in names
            file_tools={'list_project_files','read_project_file','request_write_file','request_edit_file','get_write_status'}
            schemas={t['name']:t.get('outputSchema') for t in declarations if t['name'] in file_tools}
            assert all(schema and schema.get('properties') for schema in schemas.values())
            assert {'path','content','sha256'} <= set(schemas['read_project_file']['properties'])
            report['checks']['file_output_schemas']=True
            async def call(name,args):
                r=await rpc('tools/call',{'name':name,'arguments':args})
                if r.get('isError'):raise RuntimeError(str(r['content']))
                if name in file_tools:
                    # A text fallback would hide the missing-structured-content bug.
                    assert r.get('structuredContent'),r
                    data=r['structuredContent'];assert data==json.loads(r['content'][0]['text'])
                    __import__('jsonschema').validate(data,schemas[name])
                    return data
                return r.get('structuredContent') or json.loads(r['content'][0]['text'])
            projects=await call('list_projects',{});projects=projects.get('result',projects) if isinstance(projects,dict) else projects
            project=projects[0]['id'];listing=await call('list_project_files',{'project_id':project})
            entry=next(e['path'] for e in listing['entries'] if 'path' in e)
            content=await call('read_project_file',{'project_id':project,'relative_path':entry})
            assert content['path']==entry and isinstance(content['content'],str) and len(content['sha256'])==64
            report['checks']['read_file']=entry;report['checks']['read_sha256']=content['sha256']
            rejected=await rpc('tools/call',{'name':'read_project_file','arguments':{'project_id':project,'relative_path':'../outside.py'}})
            assert rejected.get('isError');report['checks']['traversal_rejected']=True
            unauth=await http.post(auth.resource,json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
            assert unauth.status_code==401;report['checks']['unauthenticated_http']=401
            report['complete']=True
    finally:
        access=await auth.load_access_token(token)
        if access:await auth.revoke_token(access)
        with store.connect() as db:db.execute('DELETE FROM records WHERE kind=? AND id=?',('oauth_client',client.client_id))
        report_path=store.directory/'connection-report-v02.json';report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2));report_path.chmod(0o600)
        print(json.dumps(report,ensure_ascii=False,indent=2));print('Report:',report_path)

if __name__=='__main__':asyncio.run(main())
