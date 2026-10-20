"""Safe staged backend switch, owner consent, no shell and rollback."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bridge.backend_upgrade import BackendUpgrade, _unit_fields, _quote_unit


class StubService:
    def __init__(self):
        self.restarted=0
        self.stopped=0
        self.fail_next=False

    def stop(self):
        self.stopped+=1

    def restart_verify(self):
        self.restarted+=1
        if self.fail_next:
            self.fail_next=False
            raise RuntimeError("synthetic restart failure")


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.home=Path(t.name)
        self.state=self.home/"state"
        self.state.mkdir(mode=0o700)
        self.config=self.home/"bridge.json"
        self.config.write_text(json.dumps({"state_dir":str(self.state),"projects":[],"port":8767}))
        self.config.chmod(0o600)
        self.unit=self.home/".config/systemd/user/codex-local-bridge.service"
        self.unit.parent.mkdir(parents=True)
        self.old=self.home/"old bridge"
        self.old.mkdir()
        self.unit.write_text("[Unit]\nDescription=Private Codex Local Bridge (OAuth MCP)\n"
            "[Service]\nWorkingDirectory=\"" + str(self.old) + "\"\n"
            "ExecStart=\"" + str(self.old) + "/.venv/bin/codex-local-bridge\" serve\n"
            "UMask=0077\n")
        self.unit.chmod(0o600)
        with sqlite3.connect(self.state/"bridge.sqlite3") as conn:
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.execute("INSERT INTO t VALUES(1)")
        (self.state/"bridge.sqlite3").chmod(0o600)
        self.stage=self.home/"stage checkout"
        self.stage.mkdir()
        self.control=StubService()
        self.upgrade=BackendUpgrade(self.config,home=self.home,control=self.control)
        self.entry={"root":str(self.stage),
                    "bin":str(self.stage/".venv/bin/codex-local-bridge"),
                    "version":"0.6.0a1","revision":"112233445566"}

    def test_preflight_readonly_and_unmanaged_unit_rejected(self):
        before=self.unit.read_bytes()
        with patch.object(self.upgrade,"_candidate",return_value=self.entry):
            plan=self.upgrade.preflight(self.stage)
        self.assertEqual(plan["candidate"]["version"],"0.6.0a1")
        self.assertEqual(self.unit.read_bytes(),before)
        with self.assertRaises(PermissionError):
            self.upgrade.install(self.stage,confirmed=False)
        self.unit.write_text("[Unit]\nDescription=foreign service\n"
                             "[Service]\nWorkingDirectory=/tmp\nExecStart=/tmp/a serve\n")
        self.unit.chmod(0o600)
        with patch.object(self.upgrade,"_candidate",return_value=self.entry):
            with self.assertRaises(PermissionError):
                self.upgrade.preflight(self.stage)

    def test_install_switches_managed_unit_and_snapshots_state(self):
        with patch.object(self.upgrade,"_candidate",return_value=self.entry):
            result=self.upgrade.install(self.stage,confirmed=True)
        self.assertTrue(result["verified"])
        self.assertEqual(result["active"],str(self.stage))
        self.assertEqual(self.control.restarted,1)
        self.assertEqual(self.control.stopped,0)
        self.assertIn(str(self.stage),self.unit.read_text())
        self.assertIn('ExecStart="'+self.entry["bin"]+'" serve',self.unit.read_text())
        self.assertTrue(self.upgrade.snapshots.verify(result["snapshot"]))

    def test_failed_upgrade_recovers_unit_and_database(self):
        old=self.unit.read_bytes()
        self.control.fail_next=True
        with patch.object(self.upgrade,"_candidate",return_value=self.entry):
            with self.assertRaisesRegex(RuntimeError,"previous release was restored"):
                self.upgrade.install(self.stage,confirmed=True)
        self.assertEqual(self.unit.read_bytes(),old)
        self.assertGreaterEqual(self.control.restarted,2)
        self.assertGreaterEqual(self.control.stopped,1)
        with sqlite3.connect(self.state/"bridge.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM t").fetchone()[0],1)

    def test_unit_parser_rejects_foreign_and_multiple_exec(self):
        with self.assertRaises(PermissionError):
            _unit_fields("[Service]\nExecStart=/bin/evil serve")
        with self.assertRaises(ValueError):
            _unit_fields("Description=Private Codex Local Bridge (OAuth MCP)\n"
                         "WorkingDirectory=/home/a\nExecStart=/bin/a serve\n"
                         "ExecStart=/bin/b serve")
        with self.assertRaises(ValueError):
            _quote_unit("/tmp/a\nExecStart=/bin/sh")


if __name__=="__main__":
    unittest.main()
