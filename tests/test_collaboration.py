"""The desktop shows locally persisted evidence, not unverifiable claims."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from bridge.collaboration import CollaborationEvidence


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        home=Path(t.name)
        self.state=home/"state";self.state.mkdir()
        self.root=home/"airgym";self.root.mkdir()
        self.other=home/"other";self.other.mkdir()
        self.cfg={"state_dir":str(self.state),"projects":[
            {"id":"airgym_yingjie","root":str(self.root),"include":["*.py"],
             "allowed_modes":["read"],"file_write_enabled":False},
            {"id":"other","root":str(self.other),"include":["*.py"],
             "allowed_modes":["read"]}]}
        from bridge.core import Bridge
        b=Bridge(self.cfg)
        for n,pid in ((1,"airgym_yingjie"),(2,"other")):
            tid=f"{n:032x}"
            b.store.put("task",tid,{"id":tid,"project_id":pid,"mode":"read",
                                     "state":"succeeded","tests_passed":True,
                                     "changes_applied":False})
        db=self.state/"workspace.sqlite3"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE proposals (id TEXT,project_id TEXT,path TEXT,status TEXT,created_at INTEGER,applied_at INTEGER)")
            conn.executemany("INSERT INTO proposals VALUES(?,?,?,?,?,?)",[
                ("a","airgym_yingjie","web_check.py","applied",100,110),
                ("b","other","secret_other.py","applied",200,210)])
        db.chmod(0o600)

    def test_project_scoped_tasks_and_files_without_content(self):
        proof=CollaborationEvidence(self.cfg).overview("airgym_yingjie")
        self.assertTrue(proof["read_capable"])
        self.assertFalse(proof["write_authorized"])
        self.assertEqual(len(proof["recent_writes"]),1)
        self.assertEqual(proof["recent_writes"][0]["path"],"web_check.py")
        self.assertNotIn("secret_other.py",json.dumps(proof))
        self.assertEqual(proof["recent_tasks"][0]["tests_passed"],True)
        self.assertIn("not a remote OAuth check",proof["note"])

    def test_disabled_project_cannot_browse_local_history(self):
        self.cfg["projects"][0]["enabled"]=False
        with self.assertRaises(ValueError):
            CollaborationEvidence(self.cfg).overview("airgym_yingjie")

if __name__=="__main__": unittest.main()
