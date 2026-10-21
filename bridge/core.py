import asyncio
import json
import os
from pathlib import Path
import shutil
import time
import uuid
import fcntl
from .store import Store
from .security import checked, digest, git, redact, sandbox_command
from .appserver import AppServer
from .policy import ProjectAuthorization, ordinary_path, ordinary_tests

TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}

class Bridge:
    def __init__(self, config):
        self.config = config
        self.store = Store(config['state_dir'])
        self.projects = {p['id']: p for p in config['projects']}
        for p in self.projects.values():
            root = Path(p['root']).expanduser().resolve()
            if not root.is_dir(): raise ValueError('Project root does not exist')
            if root == Path('/') or root == Path.home() or self.store.directory.is_relative_to(root):
                raise ValueError('Authorize a specific project, outside bridge state')
            p['root'] = str(root)
        self.authorization = ProjectAuthorization(self)
        self.active = {}

    def project(self, id):
        if id not in self.projects or self.projects[id].get('enabled') is False:
            raise ValueError('Project is not authorized')
        return self.projects[id]

    def sources(self, p, root=None, extra_paths=None, include_tests=False):
        root = Path(root or p['root'])
        # A local owner can name a small context explicitly for a non-Git or
        # large workspace. Remote callers cannot supply or broaden these paths.
        paths = p.get('context_files')
        if paths is None:
            paths = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
        elif not isinstance(paths,list) or not all(isinstance(x,str) for x in paths):
            raise ValueError('context_files must be a local list of relative file paths')
        # ChatGPT may select exact authorized text inputs for one Codex task.
        # Do not force the owner to edit context_files on every handoff.
        explicit = extra_paths or []
        if not isinstance(explicit,list) or len(explicit)>40 or any(
                not isinstance(x,str) for x in explicit):
            raise ValueError('context_paths must list <=40 project-relative files')
        paths = list(dict.fromkeys(list(paths)+explicit))
        # A unittest suite is not usable if its source files are absent from
        # the isolated snapshot. Copy only requested test_*.py, never imports
        # from outside the authorized project or arbitrary executable files.
        if include_tests:
            for command in p.get('tests',[]):
                if isinstance(command,list) and "-s" in command:
                    pos=command.index("-s")
                    if pos+1<len(command):
                        rel_dir=command[pos+1]
                        from .filetools import _check_path
                        try:
                            bits=_check_path(rel_dir)
                            test_dir=root.joinpath(*bits)
                            if test_dir.is_dir() and not test_dir.is_symlink():
                                paths.extend(str(pth.relative_to(root)) for pth in
                                             sorted(test_dir.glob("test_*.py"))[:32]
                                             if pth.is_file() and not pth.is_symlink())
                        except (ValueError,OSError):
                            continue
        paths = list(dict.fromkeys(paths))
        result = {}; total = 0
        for rel in paths:
            if not rel: continue
            try: path = checked(root, rel, p['include'])
            except ValueError: continue
            if not path.is_file(): continue
            from .filetools import _check_path, _read_at, _redact
            try:
                content,_ = _read_at(root,_check_path(rel))
            except (ValueError,OSError): continue
            if content is None: continue
            total += len(content)
            if len(content) > 200_000 or total > 1_000_000:
                raise ValueError('Source context exceeds 1MB / 200KB per file; narrow includes')
            try:
                text=content.decode('utf-8')
                if _redact(text)[1]: continue
                result[rel] = text
            except UnicodeDecodeError: continue
        if not result: raise ValueError('No authorized tracked UTF-8 sources')
        return result

    def list_projects(self):
        return [{'id': p['id'], 'name': p.get('name',p['id']), 'include': p['include'],
                 'default_mode': 'read', 'allowed_modes':p.get('allowed_modes',['read','write_test']),
                 'context_files':p.get('context_files'), 'github_url':p.get('github_url'),
                 'authorization':self.authorization.describe(p['id']), 'test_commands': p.get('tests',[])} for p in self.projects.values() if p.get('enabled') is not False]

    def scope(self, task):
        p = self.project(task['project_id'])
        return digest({'task': task['id'], 'prompt': task['prompt'], 'mode': task['mode'],
                       'project': p, 'parent': task.get('parent_id'),
                       'context_paths':task.get('context_paths',[])})

    def submit(self, project_id, prompt, mode='read', parent_id=None, request_id=None, context_paths=None):
        with (self.store.directory/'submission.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return self._submit(project_id,prompt,mode,parent_id,request_id,context_paths)

    def _submit(self, project_id, prompt, mode='read', parent_id=None, request_id=None, context_paths=None):
        p = self.project(project_id)
        if len([t for t in self.store.all('task') if t['state'] not in TERMINAL]) >= 100:
            raise ValueError('Task queue limit reached')
        if mode not in ('read','write_test'): raise ValueError('Invalid mode')
        if mode not in p.get('allowed_modes',['read','write_test']):
            raise PermissionError('Task mode is not authorized for this project')
        if not prompt.strip() or len(prompt) > 24000: raise ValueError('Prompt must be 1..24000 chars')
        if context_paths is None: context_paths=[]
        if not isinstance(context_paths,list) or len(context_paths)>40 or not all(
                isinstance(x,str) and x for x in context_paths):
            raise ValueError('context_paths must list <=40 project text files')
        from .security import checked
        for rel in context_paths:
            path=checked(p['root'],rel,p['include'])
            if not path.is_file() or path.stat().st_size>128*1024:
                raise ValueError('Selected context path missing or too large')
        if request_id:
            existing = self.store.get('idempotency', request_id)
            if existing:
                task = self.task(existing['task_id'])
                if (task['project_id'],task['prompt'],task['mode'],task.get('parent_id'),task.get('context_paths',[])) != (project_id,prompt,mode,parent_id,context_paths):
                    raise ValueError('Idempotency key reused with different request')
                return self.status(task['id'])
        parent = self.task(parent_id) if parent_id else None
        if parent and (parent['project_id'] != project_id or parent['state'] not in TERMINAL):
            raise ValueError('Parent must be terminal and belong to same project')
        if parent and not parent.get('snapshot'):
            raise ValueError('Parent has no snapshot to resume')
        project_grant=self.authorization.active(project_id) if mode=='write_test' and ordinary_tests(p.get('tests',[])) else None
        task = {'id':uuid.uuid4().hex, 'project_id':project_id,'prompt':prompt,'mode':mode,
                'context_paths':context_paths,
                'parent_id':parent_id,'thread_id':parent.get('thread_id') if parent else None,
                'state':'awaiting_approval' if mode=='write_test' and not project_grant else 'queued',
                'project_grant_id':project_grant['id'] if project_grant else None,
                'submitted':True,'execution_succeeded':False,'tests_passed':None,
                'changes_applied':False,'source_commit_created':False,
                'created_at':time.time(),'attempt':0}
        self.store.put('task',task['id'],task)
        if request_id: self.store.put('idempotency',request_id,{'task_id':task['id']})
        self.store.audit('task_submitted',{'id':task['id'],'project':project_id,'mode':mode})
        return self.status(task['id'])

    def task(self,id):
        task = self.store.get('task',id)
        if not task: raise ValueError('Unknown task')
        self.project(task['project_id'])
        return task

    def status(self,id):
        t=self.task(id)
        return {k:t.get(k) for k in ('id','project_id','state','submitted','execution_succeeded',
                'tests_passed','changes_applied','source_commit_created','snapshot_commit',
                'thread_id','parent_id','attempt','error','project_grant_id','apply_requires_approval','apply_error')}

    def result(self,id,offset=0,limit=20000):
        t=self.task(id)
        result=self.store.get('result',id) or {}
        logs=(self.store.directory/'logs'/f'{id}.jsonl')
        text=logs.read_text() if logs.exists() else ''
        offset=max(0,offset); limit=max(1,min(limit,50000))
        return {'status':self.status(id),'summary':result.get('summary'),
                'diff':result.get('diff','')[:100000], 'diff_truncated':len(result.get('diff',''))>100000,
                'review_digest':result.get('review_digest'),'tests':result.get('tests',[]),
                'logs':text[offset:offset+limit], 'next_offset':offset+limit if len(text)>offset+limit else None}

    def authorize_task(self,id):
        t=self.task(id)
        if t['state'] != 'awaiting_approval': raise ValueError('Task is not awaiting approval')
        self.store.put('grant',id,{'digest':self.scope(t),'expires_at':time.time()+3600})
        self.store.update('task',id,state='queued')
        self.store.audit('task_authorized_locally',{'id':id,'digest':self.scope(t)})

    def cancel(self,id):
        t=self.task(id)
        if t['state'] not in TERMINAL:
            self.store.update('task',id,state='cancelled',error='Cancelled locally')
        self.store.audit('task_cancelled_locally',{'id':id})

    def retry(self,id):
        t=self.task(id)
        if t['state'] not in ('failed','interrupted'): raise ValueError('Only failed/interrupted tasks can retry')
        # No blind replay: new child task, new local grant, saved context/thread.
        return self.submit(t['project_id'], t['prompt'], t['mode'], id,
                           context_paths=t.get('context_paths',[]))

    async def log(self,id,method,params):
        directory=self.store.directory/'logs'; directory.mkdir(mode=0o700,exist_ok=True)
        # Do not store hidden reasoning or structured source output in generic logs.
        if 'reasoning' in str(method) or method == 'item/agentMessage/delta': return
        if method == 'item/completed' and params.get('item',{}).get('type') == 'agentMessage':
            params={'item': {'id':params['item']['id'],'type':'agentMessage','structured_output_received':True}}
        if method in ('item/started','item/completed') and params.get('item',{}).get('type') in ('userMessage','reasoning'):
            params={'item':{'id':params['item'].get('id'),'type':params['item']['type']}}
        if method=='turn/completed':
            turn=params.get('turn',{})
            params={**params,'turn':{k:v for k,v in turn.items() if k!='items'}}
        with (directory/f'{id}.jsonl').open('a') as out:
            out.write(redact(json.dumps({'at':time.time(),'event':method,'data':params}))[:16000]+'\n')

    async def execute(self,id):
        t=self.task(id); p=self.project(t['project_id']); server=None
        try:
            if t['mode']=='write_test' and t.get('project_grant_id'):
                self.authorization.require(t['project_id'],t['project_grant_id'])
            elif t['mode']=='write_test':
                grant=self.store.consume('grant',id)
                if not grant or grant['expires_at']<time.time() or grant['digest']!=self.scope(t):
                    raise PermissionError('Missing, expired or stale local task grant')
            self.store.update('task',id,state='running',attempt=t['attempt']+1)
            destination=self.store.directory/'snapshots'/id
            destination.mkdir(parents=True,mode=0o700,exist_ok=False)
            parent=self.task(t['parent_id']) if t.get('parent_id') else None
            context=self.sources(p,parent['snapshot'] if parent and not parent.get('changes_applied') else None,
                                 extra_paths=t.get('context_paths',[]),include_tests=t['mode']=='write_test')
            if parent:
                prior=self.store.get('result',parent['id']) or {}
                t={**t,'prompt':t['prompt']+'\nPrevious verified Bridge test results:\n'+json.dumps(prior.get('tests',[]))}
            # Local, project-isolated memory is DATA, not executable instructions.
            from .auto_memory import load_for_codex
            memory_context = load_for_codex(self, t['project_id'])
            if memory_context:
                t={**t,'prompt':t['prompt']+memory_context}
            baseline=parent['baseline'] if parent and not parent.get('changes_applied') else context.copy()
            for rel,text in context.items():
                path=checked(destination,rel,p['include']); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(text)
            git(destination,'init','--quiet')
            git(destination,'add','--all')
            git(destination,'commit','--quiet','-m','Task input snapshot')
            # Diff must be cumulative against original baseline, not just this turn.
            self.store.update('task',id,snapshot=str(destination),baseline=baseline)
            server=AppServer(self.config['codex_command'],self.store.directory/'codex-home',self.config.get('task_timeout',900))
            async def on_thread(thread): self.store.update('task',id,thread_id=thread)
            async def on_event(method,params): await self.log(id,method,params)
            answer=await asyncio.wait_for(server.run(t,context,on_thread,on_event),self.config.get('task_timeout',900))
            if self.task(id)['state']=='cancelled': return
            import jsonschema
            from .appserver import OUTPUT_SCHEMA
            jsonschema.validate(answer,OUTPUT_SCHEMA)
            if t['mode']=='read' and answer['edits']: raise PermissionError('Read-only task returned edits')
            if len(answer['edits'])>100: raise ValueError('Too many edits')
            seen=set()
            # Validate entire batch before writing any proposed file.
            for edit in answer['edits']:
                checked(destination,edit['path'],p['include'])
                if edit['path'] in seen or len(edit['content'].encode())>128*1024:
                    raise ValueError('Duplicate or oversized edit')
                seen.add(edit['path'])
            for edit in answer['edits']:
                path=checked(destination,edit['path'],p['include']); path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(edit['content'])
            from .filetools import _redact
            safe_answer=all(ordinary_path(e['path']) and not _redact(e['content'])[1]
                            and not _redact(context.get(e['path'],''))[1] for e in answer['edits'])
            tests=[]
            if t['mode']=='write_test' and (not t.get('project_grant_id') or safe_answer):
                for argv in p.get('tests',[]):
                    if t.get('project_grant_id'): self.authorization.require(t['project_id'],t['project_grant_id'])
                    tests.append(await self.run_test(destination,argv,id))
            output=self.sources_untracked(p,destination,context.keys()|seen)
            edits=[{'path':rel,'content':text} for rel,text in output.items() if baseline.get(rel)!=text]
            # Generate complete diff including new files in an isolated baseline repo.
            git(destination,'add','--all')
            git(destination,'commit','--quiet','--allow-empty','-m','Codex proposed task '+id)
            snapshot_commit=git(destination,'rev-parse','HEAD').strip()
            import difflib
            diff=''.join(''.join(difflib.unified_diff(baseline.get(e['path'],'').splitlines(True),e['content'].splitlines(True),
                         fromfile='a/'+e['path'],tofile='b/'+e['path'])) for e in edits)
            result={'summary':redact(answer['summary']),'edits':edits,'diff':redact(diff),'tests':tests}
            result['review_digest']=digest({'edits':edits,'baseline':baseline,'tests':tests})
            self.store.put('result',id,result)
            self.store.update('task',id,state='running',execution_succeeded=True,
                              tests_passed=all(x['exit_code']==0 for x in tests) if tests else None,
                              snapshot_commit=snapshot_commit)
            if t.get('project_grant_id'):
                from .filetools import _redact
                safe=all(ordinary_path(e['path']) and not _redact(e['content'])[1]
                         and not _redact(baseline.get(e['path'],''))[1] for e in edits)
                if safe and tests and all(x['exit_code']==0 for x in tests):
                    try:
                        with self.authorization.locked():
                            self.authorization.require(t['project_id'],t['project_grant_id'])
                            if self.task(id)['state']=='cancelled': return
                            from .writeback import apply_batch
                            apply_batch(self,self.task(id),result)
                    except Exception as exc:
                        self.store.update('task',id,apply_requires_approval=True,apply_error=redact(exc))
                        self.store.audit('automatic_apply_blocked',{'id':id,'reason':redact(exc)})
                else:
                    self.store.update('task',id,apply_requires_approval=True,apply_error='Sensitive/risky edit or failed tests; local diff approval required')
                    self.store.audit('automatic_apply_blocked',{'id':id,'reason':'risk_or_failed_tests'})
            self.store.update('task',id,state='succeeded')
            self.store.audit('task_finished',{'id':id,'tests_passed':self.task(id)['tests_passed']})
        except asyncio.CancelledError:
            if self.task(id)['state']!='cancelled': self.store.update('task',id,state='interrupted',error='Worker stopped; resume explicitly')
            raise
        except Exception as exc:
            if self.task(id)['state']!='cancelled': self.store.update('task',id,state='failed',error=redact(exc))
            await self.log(id,'bridge/error',{'message':redact(exc)})
            if server and server.logs:
                await self.log(id,'bridge/engine_stderr',{'output':''.join(server.logs[-20:])})
        finally:
            if server: await server.close()
            # Capture verified terminal status even on failure/cancel; never block tasks.
            try:
                from .auto_memory import capture_finished
                capture_finished(self, id)
            except Exception:
                self.store.audit('project_memory_capture_skipped', {'task_id': id})

    def sources_untracked(self,p,root,paths):
        return {rel:checked(root,rel,p['include']).read_text() for rel in sorted(paths)}

    async def run_test(self,root,argv,id):
        command=sandbox_command(root,argv)
        proc=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,
                                                start_new_session=True)
        chunks=[]; length=0
        async def drain():
            nonlocal length
            while chunk := await proc.stdout.read(8192):
                length+=len(chunk)
                if length>1_000_000: raise RuntimeError('Test output limit exceeded')
                chunks.append(chunk)
        try:
            await asyncio.wait_for(drain(),120)
            await proc.wait()
        finally:
            import signal
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            await proc.wait()
        result={'argv':argv,'exit_code':proc.returncode,'output':redact(b''.join(chunks).decode(errors='replace')),
                'sandbox':'bubblewrap; network disabled; project read-only'}
        await self.log(id,'bridge/test',result)
        return result

    async def worker(self):
        lock=(self.store.directory/'worker.lock').open('w')
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('Another worker already owns this state directory')
        for t in self.store.all('task'):
            if t['state']=='running': self.store.update('task',t['id'],state='interrupted',error='Service restarted; explicit retry required')
        try:
            while True:
                queued=[t for t in self.store.all('task') if t['state']=='queued']
                if not queued: await asyncio.sleep(0.3); continue
                t=queued[0]; job=asyncio.create_task(self.execute(t['id']))
                try:
                    while not job.done():
                        await asyncio.sleep(0.2)
                        if self.task(t['id'])['state']=='cancelled': job.cancel()
                    try: await job
                    except asyncio.CancelledError: pass
                finally:
                    if not job.done():
                        job.cancel()
                        try: await job
                        except asyncio.CancelledError: pass
        finally: lock.close()

    def review(self,id):
        t=self.task(id); r=self.store.get('result',id)
        if t['state']!='succeeded' or not r: raise ValueError('Only completed proposals can be reviewed')
        return r

    def authorize_apply(self,id,review_digest):
        r=self.review(id)
        if r['review_digest']!=review_digest: raise ValueError('Review digest mismatch')
        self.store.put('apply_grant',id,{'digest':review_digest,'scope':self.scope(self.task(id)),'expires_at':time.time()+600})
        self.store.audit('apply_authorized_locally',{'id':id,'digest':review_digest})

    def apply(self,id):
        """Local-only CLI; backup and stale-base checks; never deletes or commits original."""
        t=self.task(id); r=self.review(id); p=self.project(t['project_id'])
        if t.get('changes_applied'): raise ValueError('Already applied')
        grant=self.store.consume('apply_grant',id)
        if not grant or grant['expires_at']<time.time() or grant['digest']!=r['review_digest']:
            raise PermissionError('Exact local diff approval required')
        if grant.get('scope')!=self.scope(t):
            raise PermissionError('Project scope changed after diff approval')
        from .writeback import apply_batch
        with self.authorization.locked():
            apply_batch(self,t,r,automatic=False)
        return self.status(id)
