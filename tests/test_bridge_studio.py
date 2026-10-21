"""Offline Studio regressions; temporary private state and mocked local commands.

No deployed config, systemctl, project grants or project files are modified.
"""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/bridge_studio.py'
SPEC = importlib.util.spec_from_file_location('maintenance_studio', SOURCE)
studio = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(studio)


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        (self.root / 'alpha').mkdir()
        (self.root / 'beta').mkdir()
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'state_dir': str(self.state), 'projects': [
            {'id': 'alpha', 'name': '中文项目', 'root': str(self.root / 'alpha')},
            {'id': 'beta', 'root': str(self.root / 'beta')}]}), encoding='utf-8')
        self.config.chmod(0o600)
        self.app = studio.Studio(self.config)
        self.server = studio.StudioServer(self.app, 0)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.host = '127.0.0.1:' + str(self.server.server_port)
        self.cookie = ''
        self.csrf = ''

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, headers=None, authenticated=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        values = {'Host': self.host}
        if method != 'GET':
            values['Origin'] = 'http://' + self.host
        if authenticated:
            if self.cookie:
                values['Cookie'] = self.cookie
            if self.csrf:
                values['X-CSRF-Token'] = self.csrf
        if body is not None:
            body = json.dumps(body).encode('utf-8')
            values['Content-Type'] = 'application/json'
        values.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def login(self):
        status, headers, raw = self.request('POST', '/api/login', {'key': self.app.admin})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.csrf = json.loads(raw)['csrf']
        return headers

    def save(self, **changes):
        value = {'project_id': 'alpha', 'title': '设计记录', 'body': '本机知识',
                 'kind': 'decision', 'tags': ['local']}
        value.update(changes)
        return self.request('POST', '/api/notes/save', value)

    def test_login_auth_cookie_and_key_persistence(self):
        self.assertEqual(self.request('GET', '/')[0], 200)
        self.assertEqual(self.request('GET', '/api/bootstrap')[0], 401)
        wrong = self.app.admin[::-1]
        self.assertEqual(self.request('POST', '/api/login', {'key': wrong})[0], 401)
        headers = self.login()
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertTrue(self.app.created)
        again = studio.Studio(self.config)
        self.assertFalse(again.created)
        self.assertEqual(again.admin, self.app.admin)
        self.assertEqual((self.state / 'studio-ui.key').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        with patch.object(self.app, 'projects', return_value=[]), patch.object(self.app, 'connections', return_value={}):
            status, _, raw = self.request('GET', '/api/bootstrap')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['csrf'], self.csrf)
        self.assertNotIn(self.app.admin.encode(), raw)
        self.assertEqual(self.request('POST', '/api/logout', {})[0], 200)
        self.assertEqual(self.request('GET', '/api/bootstrap')[0], 401)

    def test_host_origin_and_csrf(self):
        for headers in ({'Host': 'evil.invalid'}, {'Host': '127.0.0.1:8767'},
                        {'Origin': 'https://evil.invalid'}, {'Origin': 'null'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('GET', '/', headers=headers)[0], 403)
        self.assertEqual(self.request('POST', '/api/login', {'key': self.app.admin},
                                      headers={'Origin': ''})[0], 403)
        self.login()
        self.assertEqual(self.request('POST', '/api/notes/save', {},
                                      headers={'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('POST', '/api/notes/save', {},
                                      headers={'Origin': 'http://localhost:' + str(self.server.server_port)})[0], 403)
        self.assertEqual(self.request('GET', '/api/notes?project_id=alpha')[0], 200)
        self.assertEqual(self.save()[0], 200)

    def test_memory_crud_conflicts_project_scope_and_separation(self):
        self.login()
        status, _, raw = self.save(origin='codex', source='ignored')
        self.assertEqual(status, 200)
        note = json.loads(raw)
        self.assertEqual(note['origin'], 'studio')
        self.assertEqual(note['source'], '')
        self.assertFalse((self.state / 'bridge.sqlite3').exists())
        self.assertEqual(self.app.memory.path, self.state / 'studio.sqlite3')
        status, _, raw = self.request('GET', '/api/notes?project_id=beta')
        self.assertEqual(json.loads(raw)['items'], [])
        self.assertEqual(self.save(project_id='beta', note_id=note['id'], expected_revision=1)[0], 400)
        self.assertEqual(self.save(note_id=note['id'], expected_revision=1, title='修订')[0], 200)
        self.assertEqual(self.save(note_id=note['id'], expected_revision=1)[0], 409)
        self.assertEqual(self.request('POST', '/api/notes/archive', {
            'project_id': 'alpha', 'note_id': note['id'], 'expected_revision': 1})[0], 409)
        self.assertEqual(self.request('POST', '/api/notes/archive', {
            'project_id': 'alpha', 'note_id': note['id'], 'expected_revision': 2})[0], 200)
        self.assertEqual(json.loads(self.request('GET', '/api/notes?project_id=alpha')[2])['items'], [])
        archived = json.loads(self.request('GET', '/api/notes?project_id=alpha&archived=1')[2])['items']
        self.assertEqual(archived[0]['revision'], 3)
        self.assertTrue(archived[0]['archived'])
        self.assertEqual(self.save(project_id='outside')[0], 400)
        self.assertEqual(self.save(title='x' * 17000)[0], 413)

    def test_session_expiry_and_login_throttling(self):
        self.login()
        for value in self.app.sessions.values():
            value['expires'] = time.monotonic() - 1
        self.assertEqual(self.request('GET', '/api/notes?project_id=alpha')[0], 401)
        self.app.login_attempts = [time.monotonic()] * 12
        self.assertEqual(self.request('POST', '/api/login', {'key': self.app.admin})[0], 429)

    def test_registration_preview_mocked_no_restart_or_apply(self):
        self.login()
        with patch.object(studio.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'DRY RUN'
            bad = {'project_id': 'new_project', 'folder': str(self.root / 'ordinary')}
            self.assertEqual(self.request('POST', '/api/projects/preview', bad)[0], 400)
            run.assert_not_called()
            status, _, raw = self.request('POST', '/api/projects/preview',
                dict(bad, confirm='PREVIEW new_project'))
            self.assertEqual(status, 200)
            job = json.loads(raw)['job_id']
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = json.loads(self.request('GET', '/api/job?id=' + job)[2])
                if result['state'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(result['state'], 'succeeded')
            self.assertIn('--yes', result['local_command'])
            argv = run.call_args.args[0]
            self.assertIn('--dry-run', argv)
            self.assertNotIn('--yes', argv)
            self.assertNotIn('systemctl', argv)
            self.assertFalse(run.call_args.kwargs.get('shell', False))
            self.assertEqual(run.call_args.kwargs['timeout'], 25)
            self.assertEqual(run.call_args.kwargs['env']['BRIDGE_CONFIG'], str(self.config))
        self.assertEqual(len(json.loads(self.config.read_text())['projects']), 2)
        self.assertEqual(self.request('POST', '/api/projects/preview', {
            'project_id': '-bad', 'folder': '/tmp', 'confirm': 'PREVIEW -bad'})[0], 400)

    def test_local_admin_registration_requires_confirmation_and_csrf(self):
        self.login()
        payload = {'project_id': 'new_project', 'folder': str(self.root / 'ordinary')}
        with patch.object(studio.subprocess, 'run') as execute:
            execute.return_value.returncode = 0
            execute.return_value.stdout = 'REGISTER OK'
            self.assertEqual(self.request('POST', '/api/projects/register', payload)[0], 400)
            execute.assert_not_called()
            wrong = dict(payload, confirm='REGISTER new_project')
            self.assertEqual(self.request('POST', '/api/projects/register', wrong,
                headers={'X-CSRF-Token': 'invalid'})[0], 403)
            execute.assert_not_called()
            status, _, raw = self.request('POST', '/api/projects/register', wrong)
            self.assertEqual(status, 200)
            job = json.loads(raw)['job_id']
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = json.loads(self.request('GET', '/api/job?id=' + job)[2])
                if result['state'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(result['state'], 'succeeded')
            self.assertIn('已注册', result['message'])
            self.assertIsNone(result['local_command'])
            argv = execute.call_args.args[0]
            self.assertIn('--yes', argv)
            self.assertNotIn('--dry-run', argv)
            self.assertFalse(execute.call_args.kwargs.get('shell', False))
            self.assertEqual(execute.call_args.kwargs['timeout'], 180)
        self.assertEqual(len(json.loads(self.config.read_text())['projects']), 2)

    def test_only_revoke_not_grant(self):
        self.login()
        with patch.object(self.app, 'command', return_value='{}') as command:
            self.assertEqual(self.request('POST', '/api/projects/revoke', {'project_id': 'alpha'})[0], 400)
            command.assert_not_called()
            self.assertEqual(self.request('POST', '/api/projects/grant', {'project_id': 'alpha'})[0], 404)
            self.assertEqual(self.request('POST', '/api/projects/revoke', {
                'project_id': 'alpha', 'confirm': 'REVOKE alpha'})[0], 200)
            self.assertEqual(command.call_args.args[0], [studio.CLI, 'revoke-project', 'alpha'])

    def test_tasks_are_readonly_and_private_fields_are_omitted(self):
        path = self.state / 'bridge.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE records (kind TEXT,id TEXT,data TEXT)')
            for number in range(55):
                connection.execute('INSERT INTO records VALUES (?,?,?)', ('task', str(number), json.dumps({
                    'id': str(number), 'project_id': 'alpha', 'state': 'succeeded',
                    'prompt': 'private prompt', 'error': 'private detail',
                    'thread_id': 'private thread', 'tests_passed': True})))
        before = path.read_bytes()
        result = self.app.tasks()
        self.assertTrue(result['available'])
        self.assertEqual(len(result['items']), 50)
        self.assertEqual(result['items'][0]['id'], '54')
        for task in result['items']:
            self.assertNotIn('prompt', task)
            self.assertNotIn('error', task)
            self.assertNotIn('thread_id', task)
        self.assertEqual(path.read_bytes(), before)

    def test_loopback_binding_no_file_serving_and_safe_dom(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        with self.assertRaises(ValueError):
            studio.StudioServer(self.app, 8767)
        self.login()
        for path in ('/etc/passwd', '/../config.json', '/scripts/bridge_studio.py', '/api/file?path=/etc/passwd'):
            self.assertEqual(self.request('GET', path)[0], 404)
        status, headers, raw = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('frame-ancestors', headers['Content-Security-Policy'])
        self.assertNotIn(b'innerHTML', raw)
        self.assertIn(b'textContent', raw)
        self.assertNotIn(b'https://cdn', raw)

    def test_unsafe_existing_key_is_rejected_without_chmod(self):
        key_path = self.state / 'studio-ui.key'
        key_path.chmod(0o644)
        with self.assertRaises(PermissionError):
            studio.Studio(self.config)
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o644)
        key_path.chmod(0o600)
        key_path.unlink()
        outside = self.root / 'outside'
        outside.write_text('not a key')
        key_path.symlink_to(outside)
        with self.assertRaises(OSError):
            studio.Studio(self.config)
        self.assertEqual(outside.read_text(), 'not a key')


if __name__ == '__main__':
    unittest.main()
