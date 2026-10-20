"""Bridge Studio task receipt: authenticated, scoped, no raw source or prompts."""
import json
import sqlite3
import unittest
from tests.test_bridge_studio import StudioTests


class TestTaskDetail(StudioTests):
    def seed_task(self, tid='a'*32, *, project='alpha', extra=None, tests=None, edits=None):
        db=sqlite3.connect(self.state/'bridge.sqlite3')
        try:
            db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id))')
            record={'id':tid,'project_id':project,'state':'succeeded','mode':'write_test',
                'execution_succeeded':True,'tests_passed':True,'changes_applied':False,
                'prompt':'PRIVATE PROMPT','error':'SECRET_ERROR','thread_id':'SENSITIVE_THREAD'}
            record.update(extra or {})
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?)',
                       ('task',tid,json.dumps(record)))
            result={'edits':edits if edits is not None else [{'path':'src/main.py','content':'PRIVATE CODE'}],
                    'tests':tests if tests is not None else [{'exit_code':0,'output':'PRIVATE LOG',
                      'sandbox':'bubblewrap; network disabled; project read-only'}],
                    'diff':'PRIVATE DIFF'}
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?)',
                       ('result',tid,json.dumps(result)))
            db.commit()
        finally:
            db.close()

    def test_requires_login_and_strips_private_fields(self):
        self.seed_task()
        self.assertEqual(self.request('GET','/api/task?id='+'a'*32)[0],401)
        self.login()
        status,_,payload=self.request('GET','/api/task?id='+'a'*32)
        self.assertEqual(status,200)
        result=json.loads(payload)
        self.assertEqual(result['files'],['src/main.py'])
        self.assertTrue(result['tests'][0]['passed'])
        self.assertFalse(result['changes_applied'])
        for word in (b'PRIVATE',b'SENSITIVE',b'prompt',b'output',b'content',b'diff'):
            self.assertNotIn(word,payload)
        self.assertEqual(self.request('GET','/api/task?id=../../etc')[0],400)

    def test_rejects_stale_projects_and_record_id_mismatch(self):
        self.login()
        self.seed_task('b'*32,project='removed')
        self.assertEqual(self.request('GET','/api/task?id='+'b'*32)[0],404)
        self.seed_task('c'*32,extra={'id':'different'})
        self.assertEqual(self.request('GET','/api/task?id='+'c'*32)[0],503)

    def test_values_are_strictly_typed_and_paths_filtered(self):
        self.login()
        self.seed_task('d'*32,extra={
            'state':{'nested':'SECRET_IN_NESTED'},'mode':'SECRET_MODE',
            'execution_succeeded':{'prompt':'SECRET'},
            'tests_passed':'true','changes_applied':'true',
            'parent_id':'SENSITIVE'},tests=[
                {'exit_code':False,'sandbox':'please bubblewrap any sandbox'},
                {'exit_code':0.0},
                {'exit_code':0,'sandbox':'bubblewrap; network disabled; project read-only'}],
            edits=[{'path':'../../outside.py','content':'SENSITIVE'},
                   {'path':'src/token_file.py','content':'SENSITIVE'},
                   {'path':'/etc/passwd','content':'SENSITIVE'},
                   {'path':'safe/main.py','content':'SENSITIVE'}])
        status,_,raw=self.request('GET','/api/task?id='+'d'*32)
        self.assertEqual(status,200)
        body=json.loads(raw)
        self.assertEqual(body['state'],'unknown')
        self.assertEqual(body['mode'],'unknown')
        self.assertIsNone(body['execution_succeeded'])
        self.assertIsNone(body['tests_passed'])
        self.assertEqual(body['files'],['safe/main.py'])
        self.assertEqual(body['tests'][0]['exit_code'],None)
        self.assertIsNone(body['tests'][0]['passed'])
        self.assertIsNone(body['tests'][1]['passed'])
        self.assertTrue(body['tests'][2]['passed'])
        for forbidden in (b'SENSITIVE',b'SECRET',b'token_file.py',b'outside.py'):
            self.assertNotIn(forbidden,raw)


    def test_explicit_receipt_saved_as_project_memory_and_handoff(self):
        self.seed_task()
        self.login()
        tid='a'*32
        status, _, raw = self.request('POST', '/api/task/remember', {
            'task_id': tid, 'confirm': 'REMEMBER '+tid})
        self.assertEqual(status, 200)
        note=json.loads(raw)
        self.assertEqual(note['kind'], 'task')
        self.assertEqual(note['project_id'], 'alpha')
        self.assertEqual(note['source'], 'bridge-task:'+tid)
        self.assertNotIn('PRIVATE', json.dumps(note))
        from types import SimpleNamespace
        from bridge.store import Store
        from bridge.handoff import build_project_handoff
        backend=SimpleNamespace(store=Store(self.state),project=self.app.project)
        handoff=build_project_handoff(backend,'alpha')
        self.assertTrue(handoff['memories'])
        self.assertIn('任务验收', handoff['memories'][0]['title'])
        for secret in ('PRIVATE PROMPT','PRIVATE LOG','PRIVATE CODE','PRIVATE DIFF'):
            self.assertNotIn(secret,json.dumps(handoff))
        self.assertEqual(self.request('POST','/api/task/remember',{
            'task_id':tid,'confirm':'NO'} )[0],400)

    def test_running_task_receipt_cannot_be_persisted(self):
        self.seed_task('b'*32,extra={'state':'running'})
        self.login()
        tid='b'*32
        self.assertEqual(self.request('POST','/api/task/remember',{
            'task_id':tid,'confirm':'REMEMBER '+tid})[0],409)
        self.assertEqual(self.app.memory.list_notes('alpha'),[])


if __name__=='__main__':
    unittest.main()
