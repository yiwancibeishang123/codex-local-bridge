"""Consistent online WAL snapshots, strict integrity verification and recovery."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bridge.snapshots import StateSnapshots


class DummyService:
    def __init__(self):
        self.stopped = 0
        self.restarted = 0
        self.fail_next = False

    def stop(self):
        self.stopped += 1

    def restart_verify(self):
        self.restarted += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("Simulated service failed")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.config = self.base / "private.json"
        self.config.write_text(json.dumps({"state_dir":str(self.state),"projects":[]}))
        self.config.chmod(0o600)
        self.unit = self.base / "service"
        self.unit.write_text("Description=Private Codex Local Bridge (OAuth MCP)\nExecStart=old serve\n")
        self.unit.chmod(0o600)
        self.service = DummyService()
        self.manager = StateSnapshots(self.config, self.unit)
        for name in ("bridge.sqlite3","studio.sqlite3"):
            with sqlite3.connect(self.state / name) as c:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT)")
                c.execute("INSERT INTO notes(title) VALUES('before')")
            (self.state / name).chmod(0o600)

    def entries(self,name):
        with sqlite3.connect(self.state / name) as db:
            return [row[0] for row in db.execute("SELECT title FROM notes ORDER BY id")]

    def test_backup_captures_wal_without_changing_live_db(self):
        data=self.manager.create()
        self.assertEqual(set(data["files"]),
                         {"config.json","bridge.sqlite3","studio.sqlite3","service.unit"})
        self.assertTrue(self.manager.verify(data["id"]))
        self.assertEqual(self.entries("studio.sqlite3"),["before"])
        for file in self.manager.root.joinpath(data["id"]).iterdir():
            self.assertEqual(file.stat().st_mode & 0o777,0o600)
        self.assertEqual((self.manager.root / data["id"]).stat().st_mode & 0o777,0o700)

    def test_restore_reverts_db_config_unit_and_keeps_rescue(self):
        data=self.manager.create()
        for name in ("bridge.sqlite3","studio.sqlite3"):
            with sqlite3.connect(self.state / name) as c:
                c.execute("INSERT INTO notes(title) VALUES('after')")
        self.config.write_text(json.dumps({"state_dir":str(self.state),"projects":[{"id":"after"}]}))
        self.config.chmod(0o600)
        self.unit.write_text("updated service")
        self.unit.chmod(0o600)
        with self.assertRaises(PermissionError):
            self.manager.restore(data["id"],confirmed=False,service=self.service)
        result=self.manager.restore(data["id"],confirmed=True,service=self.service)
        self.assertTrue(result["verified"])
        self.assertNotEqual(result["rescue"],data["id"])
        self.assertEqual(self.entries("bridge.sqlite3"),["before"])
        self.assertEqual(self.entries("studio.sqlite3"),["before"])
        self.assertNotIn('"after"',self.config.read_text())
        self.assertIn("old serve",self.unit.read_text())
        self.assertEqual(self.service.stopped,1)
        self.assertEqual(self.service.restarted,1)

    def test_corrupt_snapshot_rejected_before_stop(self):
        data=self.manager.create()
        snapshot=self.manager.root / data["id"]
        note=snapshot / "studio.sqlite3"
        note.write_bytes(b"not sqlite")
        with self.assertRaises(ValueError):
            self.manager.verify(data["id"])
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            self.manager.restore(data["id"],confirmed=True,service=self.service)
        self.assertEqual(self.service.stopped,0)

    def test_recovery_failure_restores_previous_state(self):
        data=self.manager.create()
        with sqlite3.connect(self.state / "studio.sqlite3") as db:
            db.execute("INSERT INTO notes(title) VALUES('changed')")
        self.service.fail_next=True
        with self.assertRaisesRegex(RuntimeError,"Simulated"):
            self.manager.restore(data["id"],confirmed=True,service=self.service)
        self.assertEqual(self.entries("studio.sqlite3"),["before","changed"])
        self.assertEqual(self.service.restarted,2)

    def test_dangling_link_and_path_traversal_rejected(self):
        data=self.manager.create()
        with self.assertRaises(ValueError):
            self.manager.verify("../" + data["id"])
        note=self.manager.root / data["id"] / "config.json"
        note.unlink()
        note.symlink_to(self.config)
        with self.assertRaises(PermissionError):
            self.manager.verify(data["id"])


if __name__=="__main__":
    unittest.main()
