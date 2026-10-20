"""Regression tests of the actual module and filesystem, plus no-op orchestration.

The orchestration test uses deterministic engine output; real engine coverage is
separately enabled by BRIDGE_LIVE_CODEX and checked again after deployment.
"""
import asyncio
from pathlib import Path
import pytest
from bridge.core import Bridge
from bridge.writeback import apply_batch
from tests.test_bridge import config


@pytest.mark.parametrize('automatic',[True,False])
def test_empty_batch_preserves_false_status_without_backup_or_audit(config,automatic):
    b=Bridge(config);t=b.submit('demo','No modifications')
    source=Path(b.project('demo')['root'])/'calculator.py';before=source.read_bytes()
    with b.store.connect() as db: audit_before=db.execute('SELECT count(*) FROM audit').fetchone()[0]
    apply_batch(b,b.task(t['id']),{'edits':[]},automatic=automatic)
    assert b.status(t['id'])['changes_applied'] is False
    assert source.read_bytes()==before
    assert not (b.store.directory/'backups'/t['id']).exists()
    with b.store.connect() as db: assert db.execute('SELECT count(*) FROM audit').fetchone()[0]==audit_before


@pytest.mark.parametrize('automatic',[True,False])
def test_nonempty_batch_still_backs_up_writes_and_reports_application(config,automatic):
    b=Bridge(config);t=b.submit('demo','Function-level writeback fixture')
    source=Path(b.project('demo')['root'])/'calculator.py';before=source.read_text()
    content='def add(a, b):\n    return a + b\n'
    task=b.store.update('task',t['id'],baseline={'calculator.py':before},state='succeeded')
    apply_batch(b,task,{'edits':[{'path':'calculator.py','content':content}],
                       'review_digest':'unit-fixture'},automatic=automatic)
    assert source.read_text()==content
    assert b.status(t['id'])['changes_applied'] is True
    backup=b.store.directory/'backups'/t['id']
    assert (backup/'calculator.py').read_text()==before
    assert (backup/'manifest.json').is_file()
    event='changes_applied_automatically' if automatic else 'changes_applied_locally'
    with b.store.connect() as db: assert db.execute('SELECT count(*) FROM audit WHERE event=?',(event,)).fetchone()[0]==1


def test_noop_task_runs_tests_but_does_not_report_source_application(config,monkeypatch):
    config['projects'][0].update(file_write_enabled=True,project_authorization=True,
        allowed_modes=['read','write_test'],tests=[['/usr/bin/python3','-m','unittest','discover','-v']])
    root=Path(config['projects'][0]['root']);(root/'calculator.py').write_text('def add(a, b):\n    return a + b\n')
    async def no_edits(server,task,context,on_thread,on_event):
        await on_thread('deterministic-noop-fixture')
        return {'summary':'No source changes needed','edits':[]}
    monkeypatch.setattr('bridge.appserver.AppServer.run',no_edits)
    b=Bridge(config);b.authorization.grant('demo');t=b.submit('demo','Inspect without edits','write_test')
    asyncio.run(b.execute(t['id']))
    status=b.status(t['id']);assert status['state']=='succeeded',status
    assert status['execution_succeeded'] and status['tests_passed']
    assert status['changes_applied'] is False
    assert b.result(t['id'])['diff']==''
    assert b.result(t['id'])['tests'][0]['exit_code']==0
    assert not (b.store.directory/'backups'/t['id']).exists()
