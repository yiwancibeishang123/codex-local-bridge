import asyncio
import json
import os
from pathlib import Path
from .security import redact

OUTPUT_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['summary', 'edits'],
 'properties': {'summary': {'type': 'string'}, 'edits': {'type': 'array', 'items': {
 'type': 'object', 'additionalProperties': False, 'required': ['path', 'content'],
 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}}}}}}

class AppServer:
    """One stdio connection per turn; saved thread IDs resume across processes."""
    def __init__(self, command, home, timeout=900):
        self.command, self.home, self.timeout = command, Path(home), timeout
        self.work = self.home.parent / "engine-empty"
        self.seq = 0
        self.pending = {}
        self.events = asyncio.Queue()
        self.logs = []
        self.proc = None

    async def start(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {k: v for k, v in os.environ.items() if k in
               ('PATH', 'HOME', 'LANG', 'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY',
                'https_proxy', 'http_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE')}
        env['CODEX_HOME'] = str(self.home)
        command=list(self.command)
        # Configure the named profile at startup, so workspace requirement reloads
        # use the same validated configuration as the initial handshake.
        if 'app-server' in command:
            filesystem='{'+','.join(json.dumps(k)+'="read"' for k in (':minimal',str(self.work)))+'}'
            command += ['-c','permissions.bridge.filesystem='+filesystem,
                        '-c','permissions.bridge.network.enabled=false',
                        '-c','default_permissions="bridge"']
        self.proc = await asyncio.create_subprocess_exec(*command, cwd=self.work,
                    env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=4*1024*1024)
        self.reader = asyncio.create_task(self.read())
        self.stderr = asyncio.create_task(self.read_stderr())
        await self.request('initialize', {'clientInfo': {'name': 'codex_local_bridge',
                            'title': 'Codex Local Bridge', 'version': '0.3.2'},
                            'capabilities': {'experimentalApi': True}})
        await self.send({'method': 'initialized', 'params': {}})
        account = await self.request('account/read', {'refreshToken': False})
        if (account.get('account') or {}).get('type') != 'chatgpt':
            raise PermissionError('Codex ChatGPT login required; API-key accounts are not allowed')

    async def read_stderr(self):
        while line := await self.proc.stderr.readline():
            self.logs.append(redact(line.decode(errors='replace'))[:8192])
            self.logs = self.logs[-2000:]

    async def send(self, data):
        self.proc.stdin.write((json.dumps(data) + '\n').encode())
        await self.proc.stdin.drain()

    async def request(self, method, params):
        self.seq += 1
        request_id = self.seq
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.send({'id': request_id, 'method': method, 'params': params})
        try:
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(request_id, None)

    async def read(self):
        try:
            while line := await self.proc.stdout.readline():
                msg = json.loads(line)
                if 'method' in msg and 'id' in msg:
                    # This Bridge never grants native shell, filesystem or network escalation.
                    if msg['method'] in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                        answer = {'decision': 'decline'}
                    elif msg['method'] == 'item/permissions/requestApproval':
                        answer = {'permissions': {}, 'scope': 'turn'}
                    else:
                        await self.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'Unsupported server request'}})
                        continue
                    await self.send({'id': msg['id'], 'result': answer})
                elif 'id' in msg:
                    f = self.pending.get(msg['id'])
                    if f and not f.done():
                        if 'error' in msg:
                            f.set_exception(RuntimeError(redact(msg['error'])))
                        else:
                            f.set_result(msg.get('result', {}))
                else:
                    await self.events.put(msg)
            raise ConnectionError('App Server disconnected; turn outcome may be unknown')
        except Exception as exc:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(exc)
            await self.events.put({'method': '_disconnect', 'params': {'error': redact(exc)}})

    async def run(self, task, context, on_thread, on_event):
        await self.start()
        # Current App Server rejects legacy readOnly.access. Use a named profile
        # scoped to the empty engine directory; never broaden access on failure.
        common = {'cwd': str(self.work), 'approvalPolicy': 'never',
                  'permissions': 'bridge'}
        if task.get('thread_id'):
            result = await self.request('thread/resume', {**common, 'threadId': task['thread_id']})
        else:
            result = await self.request('thread/start', common)
        thread = result['thread']['id']
        await on_thread(thread)
        prompt = ('You author changes ONLY through the final JSON response. Native shell and file operations '
                  'are disabled. Read the supplied authorized source context. Return full replacement UTF-8 '
                  'files in edits, no deletions. Do not claim tests ran; Bridge runs configured tests. '
                  'For read-only tasks return edits=[].\nMODE: ' + task['mode'] + '\nTASK:\n' + task['prompt'] +
                  '\nAUTHORIZED CONTEXT:\n' + json.dumps(context, ensure_ascii=False))
        result = await self.request('turn/start', {'threadId': thread,
                   'input': [{'type': 'text', 'text': prompt}], 'approvalPolicy': 'never',
                   'cwd': str(self.work), 'permissions': 'bridge',
                   'outputSchema': OUTPUT_SCHEMA})
        turn_id = result['turn']['id']
        final_messages = {}
        async with asyncio.timeout(self.timeout) if hasattr(asyncio, 'timeout') else _timeout(self.timeout):
            while True:
                event = await self.events.get()
                method, params = event.get('method'), event.get('params', {})
                if method == '_disconnect':
                    raise ConnectionError(params['error'])
                if params.get('threadId', thread) != thread or params.get('turnId', turn_id) != turn_id:
                    continue
                await on_event(method, params)
                if method == 'item/completed':
                    item = params.get('item', {})
                    if item.get('type') == 'agentMessage':
                        final_messages[item['id']] = item.get('text', '')
                if method == 'turn/completed':
                    turn = params['turn']
                    if turn['status'] != 'completed':
                        raise RuntimeError(redact(turn))
                    if not final_messages:
                        raise RuntimeError('No structured final agent message')
                    return json.loads(list(final_messages.values())[-1])

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        for name in ('reader', 'stderr'):
            if hasattr(self, name):
                getattr(self, name).cancel()

# Python 3.10 compatibility: wrap run() externally with wait_for as well.
class _timeout:
    def __init__(self, seconds): self.seconds = seconds
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
