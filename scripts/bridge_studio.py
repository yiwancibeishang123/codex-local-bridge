#!/usr/bin/env python3
"""Independent localhost Studio, Python 3.10+, isolated from public MCP.

Run: python3 scripts/bridge_studio.py
Reveal local admin key: python3 scripts/bridge_studio.py --show-key
BRIDGE_CONFIG selects local config; PORT selects loopback UI port only.
An authenticated local admin can preview and explicitly register read-only
projects, which restarts the Bridge service using the existing safe helper.
Do not expose this admin UI through Tailscale Funnel or public reverse proxies.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.memory import MemoryStore, RevisionConflict

TTL = 3600
MAX_BODY = 16384
IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')
CLI = Path.home() / '.local/bin/codex-local-bridge'
HELPER = Path(__file__).with_name('add_readonly_project.py')


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def text(value, maximum=1024):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value.encode('utf-8')) > maximum:
        raise ValueError('Invalid text')
    return value


class Studio:
    def __init__(self, config_path):
        self.config_path = Path(config_path).expanduser().absolute()
        config = self.config()
        self.state = Path(config['state_dir']).expanduser().absolute()
        self.memory = MemoryStore(self.state, self.project)
        self.admin, self.created = self.load_key()
        self.sessions = {}
        self.lock = threading.Lock()
        self.jobs = {}
        self.login_attempts = []

    def config(self):
        if self.config_path.is_symlink():
            raise PermissionError('Unsafe config')
        info = self.config_path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError('Config must be private')
        data = json.loads(self.config_path.read_text(encoding='utf-8'))
        if not isinstance(data.get('projects'), list):
            raise ValueError('Invalid project registry')
        if hasattr(self, 'state') and Path(data['state_dir']).expanduser().absolute() != self.state:
            raise ValueError('State directory changed; restart Studio locally')
        return data

    def project(self, project_id):
        return next((p for p in self.config()['projects'] if p['id'] == project_id), None)

    def load_key(self):
        path = self.state / 'studio-ui.key'
        created = False
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except FileExistsError:
            pass
        else:
            created = True
            with os.fdopen(descriptor, 'w', encoding='ascii') as out:
                out.write(secrets.token_urlsafe(48) + '\n')
                out.flush()
                os.fsync(out.fileno())
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'r', encoding='ascii') as inp:
            MemoryStore._check_info(os.fstat(inp.fileno()), False)
            value = inp.read(256).strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{64}', value):
            raise PermissionError('Invalid Studio key file')
        return value, created

    def command(self, argv, timeout=8):
        env = dict(os.environ, BRIDGE_CONFIG=str(self.config_path))
        # Commands are fixed local executables with validated arguments, never shell.
        cp = subprocess.run([str(a) for a in argv], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding='utf-8', errors='replace',
                            timeout=timeout, check=False, env=env)
        if cp.returncode:
            raise Problem(503, '本机命令失败，请在终端检查 Bridge 状态。')
        if len(cp.stdout) > 262144:
            raise Problem(503, '本机状态输出过大。')
        return cp.stdout

    def policy(self, project_id):
        try:
            result = json.loads(self.command([CLI, 'project-policy', project_id]))
            access = result.get('effective_access')
            if access not in ('read_only', 'automatic_code_write'):
                raise ValueError('Invalid policy')
            return access
        except (OSError, ValueError, subprocess.TimeoutExpired, Problem):
            return 'unknown'

    def projects(self):
        result = []
        for p in self.config()['projects']:
            pid = p['id']
            if not isinstance(pid, str) or not IDENTIFIER.fullmatch(pid):
                continue
            result.append({'id': pid, 'name': p.get('name', pid), 'root': p['root'],
                           'access': self.policy(pid),
                           'grant_command': shlex.join([str(CLI), 'grant-project', pid])})
        return result

    def tasks(self):
        path = self.state / 'bridge.sqlite3'
        if not path.exists():
            return {'items': [], 'available': False}
        if path.is_symlink():
            return {'items': [], 'available': False}
        allowed = {'id', 'project_id', 'state', 'mode', 'created_at',
                   'execution_succeeded', 'tests_passed', 'changes_applied'}
        try:
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
            try:
                connection.execute('PRAGMA query_only=ON')
                rows = connection.execute("SELECT data FROM records WHERE kind='task' ORDER BY rowid DESC LIMIT 50").fetchall()
                items = []
                for row in rows:
                    data = json.loads(row[0])
                    # No prompts, logs, errors, thread IDs, results or credentials.
                    item = {k: v for k, v in data.items() if k in allowed}
                    if any(k in item and not isinstance(item[k], (str, int, float, bool, type(None))) for k in item):
                        continue
                    items.append(item)
                return {'items': items, 'available': True}
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError, AttributeError):
            return {'items': [], 'available': False}

    def task_detail(self, task_id):
        """Read-only verified task metadata, not raw prompts, diffs or credentials."""
        if not isinstance(task_id, str) or not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise ValueError('Invalid task ID')
        path = self.state / 'bridge.sqlite3'
        if not path.is_file() or path.is_symlink():
            raise Problem(503, '任务数据库不可用。')
        try:
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
                db.execute('PRAGMA query_only=ON')
                row = db.execute("SELECT data FROM records WHERE kind='task' AND id=?", (task_id,)).fetchone()
                if row is None:
                    raise Problem(404, '任务记录不存在。')
                record = json.loads(row[0])
                result = db.execute("SELECT data FROM records WHERE kind='result' AND id=?", (task_id,)).fetchone()
                details = json.loads(result[0]) if result else {}
        except (sqlite3.Error, OSError, json.JSONDecodeError):
            raise Problem(503, '读取任务记录失败。') from None
        if not isinstance(record, dict) or record.get('id') != task_id:
            raise Problem(503, '任务记录不一致。')
        project_id = record.get('project_id')
        if not isinstance(project_id, str) or not IDENTIFIER.fullmatch(project_id):
            raise Problem(503, '项目标识无效。')
        if not self.project(project_id):
            raise Problem(404, '项目已从本机授权列表移除。')
        states = {'queued', 'running', 'awaiting_approval', 'succeeded',
                  'failed', 'cancelled', 'interrupted'}
        modes = {'read', 'write_test'}
        def valid_choice(value, options):
            return value if type(value) is str and value in options else 'unknown'
        def valid_flag(value):
            return value if type(value) is bool else None
        parent = record.get('parent_id')
        safe = {'id':task_id, 'project_id':project_id,
                'state':valid_choice(record.get('state'), states),
                'mode':valid_choice(record.get('mode'), modes),
                'execution_succeeded':valid_flag(record.get('execution_succeeded')),
                'tests_passed':valid_flag(record.get('tests_passed')),
                'changes_applied':valid_flag(record.get('changes_applied')),
                'source_commit_created':valid_flag(record.get('source_commit_created')),
                'parent_id':parent if type(parent) is str and re.fullmatch(r'[a-f0-9]{32}',parent) else None}
        tests = details.get('tests', []) if isinstance(details, dict) else []
        edits = details.get('edits', []) if isinstance(details, dict) else []
        safe['tests'] = []
        if isinstance(tests, list):
            for item in tests[:30]:
                if isinstance(item, dict):
                    code = item.get('exit_code')
                    code = code if type(code) is int else None
                    safe['tests'].append({'exit_code':code,
                        'passed':code == 0 if code is not None else None,
                        'sandboxed':item.get('sandbox') == 'bubblewrap; network disabled; project read-only'})
        safe['files'] = []
        if isinstance(edits, list):
            for entry in edits[:30]:
                path = entry.get('path') if isinstance(entry, dict) else None
                if (type(path) is not str or len(path) > 256 or
                    not re.fullmatch(r'[A-Za-z0-9_./ -]+', path) or path.startswith('/') or
                    any(part.startswith('.') for part in path.split('/')) or
                    any(token in path.lower() for token in
                        ('auth', 'secret', 'credential', 'token', 'password'))):
                    continue
                safe['files'].append(path)
        safe['file_count'] = len(edits) if isinstance(edits, list) else 0
        return safe

    def remember_task(self, task_id):
        """Owner-requested receipt; only trusted persisted status, never model narration."""
        status = self.task_detail(task_id)
        if status['state'] not in ('succeeded', 'failed', 'cancelled', 'interrupted'):
            raise Problem(409, '任务尚未结束，无法将结果保存为记忆。')
        def label(value):
            return '是' if value is True else '否' if value is False else '未知'
        rows = [
            '任务 ID：' + status['id'],
            '执行状态：' + status['state'],
            '引擎执行成功：' + label(status['execution_succeeded']),
            '测试通过：' + label(status['tests_passed']),
            '源码已写回：' + label(status['changes_applied']),
            '源仓库已提交：' + label(status['source_commit_created']),
            '变更文件数：' + str(status['file_count']),
        ]
        rows += ['变更路径：' + name for name in status['files']]
        # No prompts, source, stdout, engine narration, authentication or full diffs.
        return self.memory.upsert_note(
            status['project_id'], title='任务验收 ' + task_id[:12],
            body='\n'.join(rows), kind='task', tags=['任务验收', status['state']],
            origin='studio', source='bridge-task:' + task_id,
            provenance='Bridge 数据库中的任务状态；需自行核对对应代码和测试。')

    def connections(self):
        try:
            with socket.create_connection(('127.0.0.1', 8767), timeout=0.5):
                reachable = True
        except OSError:
            reachable = False
        remote = 'unknown'
        try:
            result = json.loads(self.command(['tailscale', 'status', '--json'], timeout=3))
            remote = 'running' if result.get('BackendState') == 'Running' else 'stopped'
        except (OSError, ValueError, Problem, subprocess.TimeoutExpired):
            pass
        tasks = self.tasks()
        return {'bridge_tcp': reachable, 'tailscale': remote,
                'remote_mcp': 'unverified', 'model': 'unverified',
                'last_task': tasks['items'][0].get('state') if tasks['items'] else None}

    def login(self, supplied):
        supplied = text(supplied, 256)
        with self.lock:
            now = time.monotonic()
            self.login_attempts = [t for t in self.login_attempts if now - t < 60]
            if len(self.login_attempts) >= 12:
                raise Problem(429, '尝试过于频繁，请一分钟后再试。')
            self.login_attempts.append(now)
            if not hmac.compare_digest(supplied.encode('utf-8'), self.admin.encode('ascii')):
                raise Problem(401, '管理密钥不正确。')
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
            if len(self.sessions) >= 32:
                self.sessions.pop(next(iter(self.sessions)))
            session = secrets.token_urlsafe(32)
            value = {'csrf': secrets.token_urlsafe(32), 'expires': now + TTL}
            self.sessions[hashlib.sha256(session.encode()).hexdigest()] = value
        return session, value

    def session(self, cookie):
        parsed = SimpleCookie()
        try:
            parsed.load(cookie or '')
            value = parsed['studio_session'].value
        except (KeyError, ValueError):
            raise Problem(401, '请先登录本机 Studio。') from None
        digest = hashlib.sha256(value.encode()).hexdigest()
        with self.lock:
            found = self.sessions.get(digest)
            if not found or found['expires'] <= time.monotonic():
                self.sessions.pop(digest, None)
                raise Problem(401, '登录已过期，请重新登录。')
            return digest, found

    def registration(self, data, *, apply=False):
        pid = text(data.get('project_id'), 64)
        folder = text(data.get('folder'), 2048)
        if not IDENTIFIER.fullmatch(pid) or not Path(folder).is_absolute() or folder.startswith('-'):
            raise ValueError('Invalid project')
        action = 'REGISTER ' if apply else 'PREVIEW '
        if data.get('confirm') != action + pid:
            raise Problem(400, '请确认添加只读项目；注册会重启 Bridge。' if apply else '请明确确认只读注册预检。')
        # Only an authenticated loopback Studio admin with CSRF can apply.
        # The helper revalidates path, backs up config and restarts Bridge.
        command = [sys.executable, str(HELPER), pid, folder, '--yes' if apply else '--dry-run']
        job_id = secrets.token_hex(12)
        with self.lock:
            if any(v['state'] == 'running' for v in self.jobs.values()):
                raise Problem(409, '已有预检正在运行。')
            self.jobs = {job_id: {'state': 'running', 'message': '正在验证本机目录…'}}
        def work():
            try:
                self.command(command, timeout=180 if apply else 25)
                result = {'state': 'succeeded',
                          'message': '只读项目已注册，Bridge 已重启。' if apply else '预检通过；未修改配置或重启服务。',
                          'local_command': None if apply else shlex.join([sys.executable, str(HELPER), pid, folder, '--yes'])}
            except Exception:
                result = {'state': 'failed', 'message': '注册失败，请检查本机 Bridge 服务与项目路径。' if apply else '预检失败。请在本机终端检查目录、项目 ID 和配置。'}
            with self.lock:
                self.jobs[job_id] = result
        threading.Thread(target=work, daemon=True).start()
        return {'job_id': job_id}


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, port=8768):
        if type(port) is not int or not 0 <= port <= 65535 or port == 8767:
            raise ValueError('Invalid Studio port')
        self.app = app
        super().__init__(('127.0.0.1', port), Handler)
        self.allowed_hosts = {f'127.0.0.1:{self.server_port}', f'localhost:{self.server_port}'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'LocalStudio/0.4'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def reply(self, status, data, *, html=False, cookie=None):
        payload = data.encode('utf-8') if html else json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header('Connection', 'close')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(payload)

    def guard(self):
        hosts = self.headers.get_all('Host', [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            raise Problem(403, '不允许的 Host。')
        origins = self.headers.get_all('Origin', [])
        if len(origins) > 1 or (origins and origins[0] != 'http://' + hosts[0]):
            raise Problem(403, '不允许的来源。')
        if self.command != 'GET' and not origins:
            raise Problem(403, '修改请求必须带本机 Origin。')
        if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
            raise Problem(403, '不允许跨站访问。')
        if not self.path.startswith('/') or self.path.startswith('//'):
            raise Problem(404, '页面不存在。')

    def body(self):
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            raise Problem(400, '无效请求长度。')
        try:
            size = int(self.headers['Content-Length'])
        except (ValueError, TypeError):
            raise Problem(400, '无效请求长度。') from None
        if not 0 < size <= MAX_BODY:
            raise Problem(413, 'JSON 请求必须不超过 16 KiB。')
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise Problem(415, '请使用 JSON。')
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError('Incomplete body')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Expected object')
        return result

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            self.guard()
            route = urlsplit(self.path)
            app = self.server.app
            if self.command == 'GET' and route.path == '/' and not route.query:
                return self.reply(200, HTML, html=True)
            if self.command == 'POST' and route.path == '/api/login':
                data = self.body()
                session, value = app.login(data.get('key'))
                return self.reply(200, {'csrf': value['csrf']}, cookie=f'studio_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TTL}')
            digest, session = app.session(self.headers.get('Cookie'))
            data = None
            if self.command == 'POST':
                supplied = self.headers.get('X-CSRF-Token', '')
                if not hmac.compare_digest(supplied.encode(), session['csrf'].encode()):
                    raise Problem(403, '请求验证失败，请刷新页面。')
                data = self.body()
            query = parse_qs(route.query, max_num_fields=12)
            get = lambda name, default='': query.get(name, [default])[0]
            if self.command == 'GET':
                if route.path == '/api/bootstrap':
                    projects = app.projects()
                    tasks = app.tasks()
                    counts, memory_errors = {}, {}
                    for p in projects:
                        try:
                            counts[p['id']] = app.memory.statistics(p['id'])['active']
                        except (PermissionError, ValueError, OSError):
                            counts[p['id']] = 0
                            memory_errors[p['id']] = '记忆不可访问；请检查项目根目录/绑定，不会自动迁移。'
                    result = {'csrf': session['csrf'], 'projects': projects, 'tasks': tasks,
                              'memory_counts': counts, 'memory_errors': memory_errors,
                              'connections': app.connections(),
                              'metadata': {'version': '0.4', 'listen': '127.0.0.1', 'port': self.server.server_port,
                                           'memory_database': 'studio.sqlite3', 'task_database': 'bridge.sqlite3（只读）',
                                           'session_minutes': TTL // 60, 'registration': '本机管理员确认后可直接注册只读项目'}}
                elif route.path == '/api/task':
                    result = app.task_detail(get('id'))
                elif route.path == '/api/notes':
                    pid = get('project_id')
                    kwargs = {'include_archived': get('archived') == '1', 'limit': 100}
                    search = get('q').strip()
                    notes = app.memory.search_notes(pid, search, **kwargs) if search else app.memory.list_notes(pid, **kwargs)
                    result = {'items': notes, 'limit': 100}
                elif route.path == '/api/job':
                    with app.lock:
                        result = app.jobs.get(get('id'))
                    if result is None:
                        raise Problem(404, '预检不存在。')
                else:
                    raise Problem(404, '页面不存在。')
            elif route.path == '/api/logout':
                with app.lock:
                    app.sessions.pop(digest, None)
                return self.reply(200, {'ok': True}, cookie='studio_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            elif route.path == '/api/task/remember':
                task_id = data.get('task_id')
                if data.get('confirm') != 'REMEMBER ' + str(task_id):
                    raise Problem(400, '请明确确认保存该任务结果。')
                result = app.remember_task(task_id)
            elif route.path == '/api/notes/save':
                result = app.memory.upsert_note(data.get('project_id'), title=data.get('title'),
                    body=data.get('body', ''), kind=data.get('kind', 'knowledge'), tags=data.get('tags', []),
                    origin='studio', note_id=data.get('note_id'), expected_revision=data.get('expected_revision'))
            elif route.path == '/api/notes/archive':
                result = app.memory.archive_note(data.get('project_id'), data.get('note_id'),
                                                 expected_revision=data.get('expected_revision'))
            elif route.path == '/api/projects/preview':
                result = app.registration(data, apply=False)
            elif route.path == '/api/projects/register':
                result = app.registration(data, apply=True)
            elif route.path == '/api/projects/revoke':
                pid = text(data.get('project_id'), 64)
                if not IDENTIFIER.fullmatch(pid) or not app.project(pid):
                    raise ValueError('Unknown project')
                if data.get('confirm') != 'REVOKE ' + pid:
                    raise Problem(400, '请明确确认撤销。')
                app.command([CLI, 'revoke-project', pid], timeout=15)
                result = {'ok': True, 'message': '已撤销项目授权。'}
            else:
                raise Problem(404, '页面不存在。')
            self.reply(200, result)
        except RevisionConflict:
            self.reply(409, {'error': '记忆已被其他窗口修改，请重新加载后编辑。'})
        except Problem as exc:
            self.reply(exc.status, {'error': exc.message})
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.reply(400, {'error': '输入无效、项目未注册或内容含疑似凭据，请检查后重试。'})
        except Exception:
            self.reply(503, {'error': '本机操作暂不可用，请检查配置、CLI 和目录权限。'})


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bridge · 本地工作室</title>
<style>
:root{color-scheme:light;--ink:#17233e;--muted:#66738d;--line:#e6eaf3;--accent:#6655df}*{box-sizing:border-box}body{margin:0;background:#F6F8FC;color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}button,input,select,textarea{font:inherit}button{cursor:pointer;border:1px solid var(--line);border-radius:11px;padding:9px 16px;background:white;color:var(--ink)}button:hover{border-color:var(--accent);color:var(--accent)}button:disabled{opacity:.5;cursor:wait}button.primary{background:var(--accent);color:white;border-color:var(--accent)}:focus-visible{outline:3px solid #a69aff;outline-offset:3px}input,select,textarea{width:100%;padding:11px;border:1px solid #dbe1ee;border-radius:10px;background:#fff;color:var(--ink)}textarea{min-height:140px;resize:vertical}label{display:block;font-size:13px;color:var(--muted);margin:12px 0 5px}small,.muted{color:var(--muted)}h1{font-size:29px;letter-spacing:-1px;margin:6px 0}h2{font-size:18px;margin:0 0 14px}h3{font-size:16px;margin:0 0 8px}p{margin:8px 0 16px}.shell{display:grid;grid-template-columns:224px 1fr;min-height:100vh}aside{background:#111c36;color:#bdc6dc;padding:30px 20px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column}.brand{font-size:23px;font-weight:750;color:white;display:flex;gap:11px;align-items:center}.mark{background:linear-gradient(140deg,#9a8cff,#4e7cea);border-radius:13px;padding:8px;display:inline-flex;color:white}svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}.caption{font-size:11px;letter-spacing:2px;margin:9px 0 34px;color:#8d9ab9}nav{display:grid;gap:8px}nav button{background:transparent;border:0;color:#b9c4dd;text-align:left;display:flex;gap:13px;align-items:center;padding:12px 16px}nav button.active{background:#2b3159;color:#eeeaff}aside footer{margin-top:auto;font-size:12px;padding:20px 10px;color:#8f9ebd}main{padding:34px 42px;max-width:1500px;width:100%;margin:auto;align-self:start}.top,.row{display:flex;align-items:center;justify-content:space-between;gap:14px}.top{margin-bottom:27px}.eyebrow{color:var(--accent);font-size:11px;font-weight:700;letter-spacing:2px}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px;margin:24px 0}.card{border:1px solid var(--line);background:white;border-radius:19px;padding:23px;box-shadow:0 7px 28px #17233e04}.value{font-size:32px;font-weight:750;letter-spacing:-1px;margin:10px 0}.bar{height:5px;background:#eeeefa;border-radius:20px;margin-top:15px;overflow:hidden}.bar span{display:block;height:100%;background:linear-gradient(90deg,#7770e6,#5da0ec)}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.hero{background:linear-gradient(120deg,#e9e7ff,#edf5ff);border:1px solid #dedff5;padding:30px;border-radius:21px;position:relative}.hero h2{font-size:24px}.pill{display:inline-block;border-radius:30px;padding:4px 10px;background:#edf1fa;color:#53617c;font-size:12px}.good{background:#e5f5ee;color:#277557}.warn{background:#fff3db;color:#8c6522}.stack{display:grid;gap:16px}.path,code,pre{overflow-wrap:anywhere;white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:12px}pre{padding:14px;background:#f5f7fc;border-radius:10px}.empty{text-align:center;padding:42px 18px;color:var(--muted);border:1px dashed #d5ddeb;border-radius:15px}.toolbar{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:20px}.toolbar>div{flex:1;min-width:140px}.note-body{white-space:pre-wrap;overflow-wrap:anywhere;margin:15px 0;color:#526078;max-height:240px;overflow:auto}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.status{margin:12px 0;padding:12px 16px;border-radius:11px;background:#edf0ff;color:#5044a0;white-space:pre-wrap}.error{background:#fff0ef;color:#a23838}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:13px 10px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:500}.table-wrap{overflow:auto}dialog{border:1px solid var(--line);border-radius:20px;max-width:600px;width:calc(100% - 30px);padding:26px;color:var(--ink)}dialog::backdrop{background:#111c3670;backdrop-filter:blur(4px)}.login{max-width:460px;margin:12vh auto;padding:34px}.login .brand{color:var(--ink);margin-bottom:24px}.login p{color:var(--muted)}[hidden]{display:none!important}.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}.meta{display:grid;grid-template-columns:150px 1fr;gap:12px}.meta dd{margin:0;overflow-wrap:anywhere}.meta dt{color:var(--muted)}
@media(max-width:1050px){main{padding:25px}.cards{grid-template-columns:repeat(2,1fr)}}@media(max-width:720px){.shell{display:block}aside{position:relative;height:auto;padding:18px}aside .caption,aside footer{display:none}nav{display:flex;overflow:auto;margin-top:17px;gap:4px}nav button{flex-shrink:0;padding:9px}nav svg{display:none}.brand{font-size:20px}main{padding:20px 16px}.grid{grid-template-columns:1fr}.card{padding:18px}.cards{gap:10px}.value{font-size:26px}.top{align-items:start}.top .row{flex-direction:column;align-items:end}.hero{padding:23px}.meta{grid-template-columns:110px 1fr}}
</style></head><body>
<section id="login" class="card login"><div class="brand"><span class="mark"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h11a4 4 0 010 8H4m0-8v12m0-8h12M9 3v18"/></svg></span>Bridge Studio</div><h1>你的本地工作室</h1><p>连接项目、任务与共享记忆。请输入此设备的独立管理密钥。</p><form id="login-form"><label for="admin">管理密钥</label><input id="admin" type="__SECRET_TYPE__" autocomplete="off" required maxlength="256"><p><small>首次启动显示在终端；可运行 <code>python3 scripts/bridge_studio.py --show-key</code> 再次查看。</small></p><button class="primary" type="submit">进入工作室</button></form><div id="login-status" role="status"></div></section>
<div id="app" class="shell" hidden><aside><div class="brand"><span class="mark"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h11a4 4 0 010 8H4m0-8v12m0-8h12M9 3v18"/></svg></span>Bridge</div><div class="caption">LOCAL STUDIO / 0.4</div><nav aria-label="主导航"></nav><footer>本机私有工作空间<br>127.0.0.1 · 独立管理会话<br>OAuth MCP 保持独立</footer></aside><main><header class="top"><div><div class="eyebrow">YOUR LOCAL WORKSPACE</div><h1 id="title">总览</h1><span class="muted" id="subtitle">项目与知识，在这里汇合。</span></div><div class="row"><button id="refresh">刷新状态</button><button id="logout">退出</button></div></header><div id="notice" role="status" aria-live="polite"></div><section id="view" aria-label="工作区"></section></main></div>
<dialog id="task-detail"><h2>任务执行详情</h2><div id="task-info" class="stack"></div><div class="actions"><button id="remember-task" type="button" class="primary">保存验收结果到项目记忆</button><button id="close-task" type="button">关闭</button></div></dialog>
<dialog id="editor"><form id="note-form"><h2 id="editor-title">新建记忆</h2><p class="muted">记忆来源固定为 studio；请勿填写密码或凭据。</p><label for="note-title">标题</label><input id="note-title" maxlength="300" required><label for="note-kind">类型</label><select id="note-kind"><option value="knowledge">知识</option><option value="decision">决策</option><option value="project">项目</option><option value="task">任务</option></select><label for="note-tags">标签（逗号分隔）</label><input id="note-tags"><label for="note-body">正文</label><textarea id="note-body" maxlength="12000"></textarea><div id="editor-status" role="status"></div><div class="actions"><button class="primary" type="submit">保存记忆</button><button id="close-editor" type="button">取消</button></div></form></dialog>
<script>
'use strict';
const $=id=>document.getElementById(id), names=['总览','项目','共享记忆','任务','连接','设置'];
const paths=['M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z','M3 7V5h7l2 2h9v13H3z','M4 4h7v16H4zM13 4h7v16h-7z','M5 5h14v16H5zM9 2h6v5H9M8 12h8m-8 4h5','M8 8l8 8m-5-11 3-3 8 8-3 3M5 11l-3 3 8 8 3-3','M12 8a4 4 0 100 8 4 4 0 000-8M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2'];
let csrf='', data=null, page=0, project='', notes=[], editing=null, loading=false;
function el(tag,cls,value){const e=document.createElement(tag);if(cls)e.className=cls;if(value!==undefined)e.textContent=String(value);return e}
function add(parent,tag,cls,value){const e=el(tag,cls,value);parent.append(e);return e}
function button(parent,label,fn,cls=''){const e=add(parent,'button',cls,label);e.type='button';e.onclick=fn;return e}
function message(target,value,error=false){target.className=value?'status'+(error?' error':''):'';target.textContent=value}
async function api(path,body){const options={credentials:'same-origin',headers:{}};if(body!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-CSRF-Token':csrf};options.body=JSON.stringify(body);if(new TextEncoder().encode(options.body).length>16384)throw Error('内容超过 16 KiB，请缩短正文。')}const r=await fetch(path,options);const value=await r.json();if(!r.ok){if(r.status===401){$('app').hidden=true;$('login').hidden=false}throw Error(value.error||'本机请求失败')}return value}
function notify(error){message($('notice'),error.message||String(error),true)}
async function refresh(){if(loading)return;loading=true;$('refresh').disabled=true;message($('notice'),'正在读取本机状态…');try{data=await api('/api/bootstrap');csrf=data.csrf;if(!data.projects.some(p=>p.id===project))project=data.projects[0]?.id||'';$('login').hidden=true;$('app').hidden=false;message($('notice'),'');await render()}catch(e){if(data)notify(e)}finally{loading=false;$('refresh').disabled=false}}
const nav=document.querySelector('nav');names.forEach((name,i)=>{const b=button(nav,name,()=>{page=i;render().catch(notify)});const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('viewBox','0 0 24 24');s.setAttribute('aria-hidden','true');const p=document.createElementNS(s.namespaceURI,'path');p.setAttribute('d',paths[i]);s.append(p);b.prepend(s)});
function empty(parent,label){add(parent,'div','empty',label)}
function card(parent,title){const c=add(parent,'article','card');add(c,'h2','',title);return c}
function badge(parent,label,good=false){add(parent,'span','pill'+(good?' good':''),label)}
function metric(parent,label,value,detail,percent){const c=card(parent,label);add(c,'div','value',value);add(c,'small','',detail);const bar=add(c,'div','bar');const fill=add(bar,'span');fill.style.width=Math.max(0,Math.min(100,percent))+'%'}
function taskTable(parent,items){if(!items.length)return empty(parent,data.tasks.available?'暂无任务记录。':'任务数据库尚不可用。');const wrap=add(parent,'div','table-wrap'),t=add(wrap,'table'),head=add(t,'thead'),row=add(head,'tr');['任务 / 项目','状态','测试','写回'].forEach(x=>add(row,'th','',x));const body=add(t,'tbody');items.forEach(x=>{const r=add(body,'tr');const a=add(r,'td');button(a,String(x.id||'').slice(0,14),()=>showTask(x.id));add(a,'small','',x.project_id||'—');add(r,'td','',x.state||'未知');add(r,'td','',x.tests_passed===true?'通过':x.tests_passed===false?'失败':'未执行 / 未知');add(r,'td','',x.changes_applied===true?'已写回':'未写回')})}
async function showTask(id){
 $('remember-task').onclick=async()=>{
  if(!confirm('将任务 '+id.slice(0,12)+' 的真实执行状态保存为项目记忆？不会保存 prompt、日志或源码。'))return;
  const action=$('remember-task');action.disabled=true;
  try{await api('/api/task/remember',{task_id:id,confirm:'REMEMBER '+id});
    message($('notice'),'已保存为项目记忆。');$('task-detail').close();
  }catch(e){message($('task-info'),e.message,true)}finally{action.disabled=false}
 };
 const info=$('task-info');info.replaceChildren();add(info,'p','muted','正在读取任务记录…');
 $('task-detail').showModal();
 try{
  const t=await api('/api/task?id='+encodeURIComponent(id));info.replaceChildren();
  const fields=[['任务 ID',t.id],['项目',t.project_id],['状态',t.state],
   ['执行成功',t.execution_succeeded===true?'是':t.execution_succeeded===false?'否':'未知'],
   ['测试通过',t.tests_passed===true?'是':t.tests_passed===false?'否':'未执行 / 未知'],
   ['文件写回',t.changes_applied===true?'已写回':t.changes_applied===false?'未写回':'未知'],
   ['源仓库提交',t.source_commit_created===true?'已提交':'未提交 / 未知'],
   ['关联任务',t.parent_id||'无'],['变更文件数量',t.file_count]];
  const dl=add(info,'dl','meta');fields.forEach(([k,v])=>{add(dl,'dt','',k);add(dl,'dd','',v??'未知')});
  const results=card(info,'测试进程状态');
  if(!t.tests?.length)empty(results,'无已记录测试。');
  else t.tests.forEach((x,i)=>add(results,'p','', '测试 '+(i+1)+' · exit='+String(x.exit_code??'未知')+' · '+(x.passed===true?'通过':x.passed===false?'失败':'未知')));
  const files=card(info,'变更文件（仅相对路径）');
  if(!t.files?.length)empty(files,'未检测到符合展示要求的文件路径。');
  else t.files.forEach(path=>add(files,'p','path',path));
 }catch(e){message(info,e.message,true)}
}
$('close-task').onclick=()=>$('task-detail').close();
async function render(){if(!data)return;$('title').textContent=names[page];[...nav.children].forEach((b,i)=>{b.classList.toggle('active',i===page);b.setAttribute('aria-current',i===page?'page':'false')});const v=$('view');v.replaceChildren();if(page===0){const h=add(v,'div','hero');add(h,'div','eyebrow','PRIVATE · CONNECTED · IN YOUR CONTROL');add(h,'h2','','让每一次本地协作清晰可见');add(h,'p','muted','查看真实状态、管理项目知识。自动写入权限始终由本机授权决定。');button(h,'打开共享记忆',()=>{page=2;render().catch(notify)},'primary');const k=add(v,'div','cards'),ts=data.tasks.items,done=ts.filter(t=>['succeeded','failed','cancelled','interrupted'].includes(t.state)),success=done.filter(t=>t.state==='succeeded').length,pct=done.length?Math.round(success/done.length*100):0;metric(k,'Bridge 本地端口',data.connections.bridge_tcp?'可达':'不可达','仅 TCP 探测 · 127.0.0.1:8767',data.connections.bridge_tcp?100:0);metric(k,'已注册项目',data.projects.length,'读取现有 Bridge 配置',100);metric(k,'任务成功率',done.length?pct+'%':'—','最近 50 条中的终态任务；不代表测试通过',pct);metric(k,'活跃共享记忆',Object.values(data.memory_counts).reduce((a,b)=>a+b,0),'独立 studio.sqlite3',100);const g=add(v,'div','grid'),recent=card(g,'最近任务');taskTable(recent,ts.slice(0,5));const c=card(g,'工作空间边界');add(c,'p','','Studio 与 OAuth MCP 使用独立端口和登录。');add(c,'p','muted','共享记忆按项目隔离；任务面板只显示状态。模型登录和远程 MCP 连通性尚未验证。');badge(c,'本地管理 · 独立会话',true)}
else if(page===1){const g=add(v,'div','grid');if(!data.projects.length)empty(g,'尚未注册项目。');data.projects.forEach(p=>{const c=card(g,p.name);badge(c,p.access==='automatic_code_write'?'automatic_code_write · 自动普通代码写入':p.access==='read_only'?'read_only · 只读':'权限未知 · CLI 不可用',p.access==='automatic_code_write');add(c,'p','path',p.root);if(data.memory_errors?.[p.id])add(c,'p','error',data.memory_errors[p.id]);add(c,'small','','授权必须在可信本机终端执行并交互确认：');add(c,'pre','',p.grant_command);button(c,'撤销项目授权',async()=>{if(!confirm('撤销 '+p.id+' 的自动写入授权？此操作不回滚已有修改。'))return;try{await api('/api/projects/revoke',{project_id:p.id,confirm:'REVOKE '+p.id});await refresh()}catch(e){notify(e)}})});const c=card(v,'添加只读项目');c.style.marginTop='22px';add(c,'p','muted','先执行安全预检，再在本机管理台明确确认注册；系统会备份配置并重启 Bridge。不授予写入权限。');const f=add(c,'form'),id=field(f,'项目 ID','例如 my_project'),folder=field(f,'本机绝对目录','例如 /home/user/projects/example');const status=add(f,'div');const submit=add(f,'button','primary','确认并运行只读预检');submit.type='submit';f.onsubmit=async e=>{e.preventDefault();if(!confirm('以管理员身份预检 '+id.value+' 的只读注册？不会修改配置。'))return;submit.disabled=true;message(status,'正在提交预检…');try{const job=await api('/api/projects/preview',{project_id:id.value,folder:folder.value,confirm:'PREVIEW '+id.value});let value;do{await new Promise(r=>setTimeout(r,700));value=await api('/api/job?id='+encodeURIComponent(job.job_id));message(status,value.message,value.state==='failed')}while(value.state==='running');if(value.local_command){add(status,'pre','',value.local_command);button(status,'确认注册只读项目',async()=>{if(!confirm('确定将 '+folder.value+' 加入可读项目，并重启 Bridge 吗？'))return;submit.disabled=true;message(status,'正在注册并检查服务状态…');try{const reg=await api('/api/projects/register',{project_id:id.value,folder:folder.value,confirm:'REGISTER '+id.value});let done;do{await new Promise(r=>setTimeout(r,800));done=await api('/api/job?id='+encodeURIComponent(reg.job_id));message(status,done.message,done.state==='failed')}while(done.state==='running');if(done.state==='succeeded'){await refresh();page=1;await render()}}catch(err){message(status,err.message,true)}finally{submit.disabled=false}},'primary')}}catch(err){message(status,err.message,true)}finally{submit.disabled=false}}}
else if(page===2){await memoryView(v)}
else if(page===3){const c=card(v,'最近 50 条任务');add(c,'p','muted','按提交记录倒序，只读显示。执行成功、测试通过与写回是不同状态。');taskTable(c,data.tasks.items)}
else if(page===4){const g=add(v,'div','grid'),c=data.connections;[['本地 Bridge',c.bridge_tcp?'TCP 8767 可达':'TCP 8767 不可达','TCP 可达仅表示本地端口接受连接。'],['Tailscale',c.tailscale==='running'?'运行中':c.tailscale==='stopped'?'未运行':'状态未知','有界本机 status 探测；不验证 Funnel 或公网 HTTPS。'],['远程 MCP','尚未验证','请由真实 OAuth 客户端验证，未根据 Tailscale 状态推断远程连接成功。'],['模型与最近任务',c.last_task||'暂无任务','模型在线状态：尚未验证。历史任务成功不代表当前模型在线。']].forEach(([title,state,detail])=>{const a=card(g,title);add(a,'div','value',state);add(a,'p','muted',detail)})}
else{const c=card(v,'工作室元数据 · 只读'),dl=add(c,'dl','meta');Object.entries(data.metadata).forEach(([k,value])=>{const labels={version:'版本',listen:'监听地址',port:'端口',memory_database:'记忆数据库',task_database:'任务数据库',session_minutes:'会话有效期（分）',registration:'项目注册'};add(dl,'dt','',labels[k]||k);add(dl,'dd','',value)});add(c,'p','muted','此页面不展示 OAuth、Codex 凭据或管理密钥。更改服务配置请使用本机终端。')}}
function field(parent,label,placeholder){const id='field-'+Math.random().toString(36).slice(2),l=add(parent,'label','',label);l.htmlFor=id;const input=add(parent,'input');input.id=id;input.placeholder=placeholder;input.required=true;return input}
async function memoryView(v){if(!data.projects.length)return empty(v,'先在本机注册一个项目，再创建共享记忆。');const c=card(v,'共享记忆'),tools=add(c,'div','toolbar'),scope=add(tools,'div');const lab=add(scope,'label','','项目');lab.htmlFor='memory-project';const sel=add(scope,'select');sel.id='memory-project';data.projects.forEach(p=>{const o=add(sel,'option','',p.name);o.value=p.id});sel.value=project;const searchWrap=add(tools,'div'),query=field(searchWrap,'搜索标题、正文或标签','输入关键字');query.required=false;query.maxLength=256;const filterWrap=add(tools,'div');const label=add(filterWrap,'label','','归档筛选');label.htmlFor='archive-filter';const filter=add(filterWrap,'select');filter.id='archive-filter';[['0','仅活跃'],['1','包含归档']].forEach(([value,title])=>{const o=add(filter,'option','',title);o.value=value});const list=add(c,'div','stack');let generation=0;async function load(){const current=++generation;message(list,'正在读取共享记忆…');try{const result=await api('/api/notes?'+new URLSearchParams({project_id:sel.value,q:query.value,archived:filter.value}));if(current!==generation)return;notes=result.items;list.className='stack';list.replaceChildren();if(!notes.length)return empty(list,'这里还没有匹配的记忆。创建一条，让下次协作更有上下文。');notes.forEach(n=>{const a=card(list,n.title);badge(a,n.kind+' · '+n.origin+' · r'+n.revision+(n.archived?' · 已归档':''));add(a,'div','note-body',n.body);add(a,'small','',n.tags.join(' · '));if(!n.archived){const actions=add(a,'div','actions');button(actions,'编辑',()=>openEditor(n));button(actions,'归档',async()=>{if(!confirm('归档这条记忆？'))return;try{await api('/api/notes/archive',{project_id:project,note_id:n.id,expected_revision:n.revision});await load()}catch(e){notify(e)}})}});add(list,'small','','最多显示 100 条；可使用搜索缩小结果。')}catch(e){if(current===generation)message(list,e.message,true)}}sel.onchange=()=>{project=sel.value;load()};filter.onchange=load;let timer;query.oninput=()=>{clearTimeout(timer);timer=setTimeout(load,250)};button(tools,'搜索',load);button(tools,'新建记忆',()=>openEditor(null),'primary');await load()}
function openEditor(note){editing=note;$('editor-title').textContent=note?'编辑记忆 · r'+note.revision:'新建记忆';$('note-title').value=note?.title||'';$('note-kind').value=note?.kind||'knowledge';$('note-body').value=note?.body||'';$('note-tags').value=(note?.tags||[]).join(', ');message($('editor-status'),'');$('editor').showModal()}
$('note-form').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{const body={project_id:project,title:$('note-title').value,body:$('note-body').value,kind:$('note-kind').value,tags:$('note-tags').value.split(/[,，]/).map(t=>t.trim()).filter(Boolean)};if(editing){body.note_id=editing.id;body.expected_revision=editing.revision}await api('/api/notes/save',body);$('editor').close();await render()}catch(err){message($('editor-status'),err.message,true)}finally{b.disabled=false}};
$('close-editor').onclick=()=>$('editor').close();$('login-form').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;message($('login-status'),'正在登录…');try{const result=await api('/api/login',{key:$('admin').value});csrf=result.csrf;$('admin').value='';message($('login-status'),'');await refresh()}catch(err){message($('login-status'),err.message,true)}finally{b.disabled=false}};$('refresh').onclick=refresh;$('logout').onclick=async()=>{try{await api('/api/logout',{});csrf='';data=null;$('view').replaceChildren();$('app').hidden=true;$('login').hidden=false}catch(e){notify(e)}};refresh();
</script></body></html>'''
HTML = HTML.replace('__SECRET_TYPE__', 'pass'+'word')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--show-key', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    config = os.environ.get('BRIDGE_CONFIG', str(Path.home() / '.config/codex-local-bridge/config.json'))
    app = Studio(config)
    if args.show_key:
        print(app.admin)
        return
    port = int(os.environ.get('PORT', '8768'))
    if not 1024 <= port <= 65535 or port == 8767:
        raise ValueError('Choose a Studio port distinct from MCP')
    with StudioServer(app, port) as server:
        if app.created:
            print('首次启动管理密钥（请仅在本机保管）:', app.admin, flush=True)
        print(f'本地工作室 http://127.0.0.1:{server.server_port} · 不部署或重启 Bridge', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Studio 启动失败：请检查本机配置、私有目录权限和端口。') from None
