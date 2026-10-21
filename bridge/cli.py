import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from .core import Bridge
from .security import git, redact, sandbox_command

DEFAULT_FLAGS = ['app-server','--strict-config','-c','features.shell_tool=false',
 '-c','features.unified_exec=false','-c','features.hooks=false', '-c','features.multi_agent=false',
 '-c','features.apps=false','-c','features.remote_plugin=false','-c','web_search="disabled"']

def config_path():
    return Path(os.environ.get('BRIDGE_CONFIG',str(Path.home()/'.config/codex-local-bridge/config.json')))

def load_config():
    p=config_path()
    config=json.loads(p.read_text())
    if p.stat().st_mode & 0o077: raise PermissionError('Config must be chmod 600')
    return config

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description='Private Codex bridge; approvals are local-only')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('serve','worker','doctor','login','history','auth-requests','init-demo','register-maintenance'): sub.add_parser(name)
    for name in ('inspect-task','approve-task','cancel','retry','review','apply'):
        p=sub.add_parser(name); p.add_argument('task_id')
    p=sub.add_parser('approve-auth'); p.add_argument('request_id')
    p=sub.add_parser('approve-apply'); p.add_argument('task_id'); p.add_argument('digest')
    p=sub.add_parser('submit'); p.add_argument('project_id'); p.add_argument('prompt'); p.add_argument('--mode',default='read')
    p=sub.add_parser('acceptance'); p.add_argument('--project',default='demo')
    # BEGIN LOCAL FILE TOOLS 0.2
    for name in ('approve-file','status-file'):
        p=sub.add_parser(name); p.add_argument('proposal_id')
    # END LOCAL FILE TOOLS 0.2

    p=sub.add_parser('grant-project'); p.add_argument('project_id'); p.add_argument('--hours',type=int)
    p=sub.add_parser('approve-data-scope'); p.add_argument('request_id')
    p=sub.add_parser('approve-data-write'); p.add_argument('proposal_id')
    p=sub.add_parser('revoke-data-scope'); p.add_argument('project_id')
    for name in ('revoke-project','project-policy','project-systemd'):
        p=sub.add_parser(name); p.add_argument('project_id')

    args=parser.parse_args()
    config=load_config(); bridge=Bridge(config)
    if args.command in ('approve-data-scope','approve-data-write','revoke-data-scope'):
        from .data_access import DataAccess
        api=DataAccess(bridge)
        if not os.isatty(0):
            raise PermissionError('Local interactive approval required')
        if args.command=='approve-data-scope':
            print(json.dumps(api.request_status(args.request_id), ensure_ascii=False, indent=2))
            token='AUTHORIZE DATA '+args.request_id
            if input('Type '+token+' to approve: ') != token:
                raise SystemExit('Not approved')
            result=api.approve_local(args.request_id)
        elif args.command=='revoke-data-scope':
            token='REVOKE DATA '+args.project_id
            if input('Type '+token+' to revoke: ') != token:
                raise SystemExit('Not approved')
            result=api.revoke_local(args.project_id)
        else:
            token='APPLY DATA '+args.proposal_id
            if input('Type '+token+' to apply this change: ') != token:
                raise SystemExit('Not approved')
            result=api.approve_text_write_local(args.proposal_id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command=='register-maintenance':
        from .maintenance import register_maintenance, save_registration, PROJECT_ID
        original=config_path().read_bytes()
        with bridge.authorization.locked():
            project=register_maintenance(config,Path(__file__).resolve().parents[1])
            save_registration(config,config_path(),original)
            bridge.store.audit('maintenance_project_registered',{'project':PROJECT_ID,'root':project['root']})
        print(json.dumps(project,ensure_ascii=False,indent=2))
        print('Isolated source copy registered. No automatic write grant issued. Restart service; grant-project bridge_maintenance once if desired. Changes do not deploy themselves. Tests are syntax smoke checks, not the full pytest suite.')
    elif args.command in ('grant-project','revoke-project','project-policy','project-systemd'):
        p=bridge.project(args.project_id)
        if args.command=='grant-project':
            print(json.dumps(p,ensure_ascii=False,indent=2))
            print('Authorize ordinary code writes and configured sandboxed tests. Backups and audit remain enabled. No source Git commits. Revoke at any time.')
            if not os.isatty(0): raise ValueError('Interactive local terminal required')
            expected='AUTHORIZE '+args.project_id
            if input('Type '+expected+' to grant this project: ')!=expected: raise SystemExit('Not authorized')
            result=bridge.authorization.grant(args.project_id,args.hours)
        elif args.command=='revoke-project': result=bridge.authorization.revoke(args.project_id)
        elif args.command=='project-systemd':
            # Print only; never weaken the running service automatically.
            root=p['root']
            if any(c in root for c in ('\n','\r','%','"','\\')): raise ValueError('Unsupported unit path')
            print('[Service]\nReadWritePaths="'+root+'"')
            return
        else: result=bridge.authorization.describe(args.project_id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command=='serve':
        import uvicorn
        from .server import build_server
        app,_,_=build_server(config)
        uvicorn.run(app,host='127.0.0.1',port=config.get('port',8767),access_log=False,log_level='warning',proxy_headers=False)
    elif args.command=='worker': asyncio.run(bridge.worker())
    elif args.command=='login':
        home=bridge.store.directory/'codex-home'; home.mkdir(mode=0o700,exist_ok=True)
        env={k:v for k,v in os.environ.items() if 'KEY' not in k.upper() and 'TOKEN' not in k.upper()}
        env['CODEX_HOME']=str(home)
        subprocess.run([config['codex_command'][0],'login'],env=env,check=True)
    elif args.command=='doctor':
        results={'python':__import__('sys').version.split()[0],'git':bool(shutil.which('git')),
                 'bubblewrap':bool(shutil.which('bwrap')),'tailscale':bool(shutil.which('tailscale')),
                 'codex':bool(shutil.which(config['codex_command'][0])), 'public_url':config['public_url']}
        try:
            import tempfile
            with tempfile.TemporaryDirectory(dir=bridge.store.directory) as probe:
                (Path(probe)/'.git').mkdir()
                p=subprocess.run(sandbox_command(probe,['/usr/bin/python3','--version']),capture_output=True,timeout=10)
            results['sandbox_ok']=p.returncode==0; results['sandbox_error']=redact(p.stderr.decode())
        except Exception as e: results['sandbox_ok']=False; results['sandbox_error']=redact(e)
        async def engine():
            from .appserver import AppServer
            server=AppServer(config['codex_command'],bridge.store.directory/'codex-home')
            try:
                await server.start(); results['codex_chatgpt_auth']=True
            except Exception as e: results['codex_chatgpt_auth']=False; results['codex_error']=redact(e)
            finally: await server.close()
        asyncio.run(engine()); print(json.dumps(results,ensure_ascii=False,indent=2))
        if not results.get('sandbox_ok') or not results.get('codex_chatgpt_auth'): raise SystemExit(1)
    elif args.command=='init-demo':
        p=bridge.project('demo'); root=Path(p['root'])
        if any(root.iterdir()): raise ValueError('Demo directory must be empty; will not overwrite')
        (root/'calculator.py').write_text('def add(a, b):\n    return a - b\n')
        (root/'test_calculator.py').write_text('import unittest\nfrom calculator import add\n\nclass TestCalculator(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n')
        git(root,'init','--quiet'); git(root,'add','--all'); git(root,'commit','--quiet','-m','Demo baseline')
        print('Demo initialized:',root)
    elif args.command=='history': print(json.dumps([bridge.status(t['id']) for t in bridge.store.all('task')],indent=2))
    elif args.command=='auth-requests':
        import time
        print(json.dumps([r for r in bridge.store.all('oauth_consent') if r['expires_at']>time.time()],indent=2))
    elif args.command=='approve-auth':
        from .auth import LocalOAuth
        auth=LocalOAuth(bridge.store,config['public_url'],config['redirect_uris'])
        r=bridge.store.get('oauth_consent',args.request_id)
        if not r: raise ValueError('Unknown consent request: use the actual ID shown on the consent page, not REQUEST_ID')
        print('Authorize ChatGPT for configured projects and task history, including writes/tests within active local project grants. Redirect:',r['params']['redirect_uri'])
        if r['expires_at']<__import__('time').time(): raise ValueError('Request expired: reconnect from ChatGPT to generate a new request')
        if input('Type AUTHORIZE to confirm: ')!='AUTHORIZE': raise SystemExit('Not approved')
        auth.approve(args.request_id); print('Approved. Reload the consent page in your browser.')
    elif args.command=='inspect-task':
        t=bridge.task(args.task_id); print(json.dumps(t,ensure_ascii=False,indent=2))
        print('Configured test argv:',bridge.project(t['project_id']).get('tests',[]))
    elif args.command=='approve-task':
        t=bridge.task(args.task_id); print('Task:',t['prompt']); print('Project:',t['project_id'])
        print('Create isolated proposals and run these sandboxed tests:',bridge.project(t['project_id']).get('tests',[]))
        if input('Type APPROVE to authorize this task: ')!='APPROVE': raise SystemExit('Not approved')
        bridge.authorize_task(args.task_id); print('Task queued')
    elif args.command=='cancel': bridge.cancel(args.task_id); print('Cancelled')
    elif args.command=='retry': print(json.dumps(bridge.retry(args.task_id),indent=2))
    elif args.command=='review':
        r=bridge.review(args.task_id); print(r['diff']); print('Review digest:',r['review_digest']); print(json.dumps(r['tests'],indent=2))
    elif args.command=='approve-apply':
        r=bridge.review(args.task_id); print(r['diff']); print('Original files will be overwritten; backups are kept.')
        if input('Type APPLY to approve this exact diff: ')!='APPLY': raise SystemExit('Not approved')
        bridge.authorize_apply(args.task_id,args.digest); print('Approved for ten minutes. Run apply TASK_ID.')
    elif args.command=='apply': print(json.dumps(bridge.apply(args.task_id),indent=2))
    elif args.command=='submit': print(json.dumps(bridge.submit(args.project_id,args.prompt,args.mode),indent=2))
    # BEGIN LOCAL FILE TOOLS 0.2
    elif args.command in ('approve-file','status-file'):
        from .filetools import create_file_bridge
        file_bridge=create_file_bridge(bridge)
        status=file_bridge.get_write_status(args.proposal_id)
        print(json.dumps(status,ensure_ascii=False,indent=2))
        if args.command=='approve-file':
            if not os.isatty(0): raise ValueError('Interactive local terminal required')
            review=file_bridge.review_proposal_local(args.proposal_id)
            print('\n--- FULL LOCAL DIFF ---\n'+review['diff']+'\n--- END DIFF ---')
            expected='APPROVE '+args.proposal_id[-6:]
            if input('Type '+expected+' to apply: ')!=expected: raise SystemExit('Not approved')
            print(json.dumps(file_bridge.approve_and_apply(args.proposal_id),ensure_ascii=False,indent=2))
    # END LOCAL FILE TOOLS 0.2
    elif args.command=='acceptance':
        from .acceptance import run
        asyncio.run(run(config,args.project))

def entrypoint():
    try: main()
    except Exception as e: raise SystemExit(redact(e))

if __name__=='__main__': entrypoint()
