"""Automatic Bridge task memory: scoped, idempotent, verified status only."""
import json
from pathlib import Path
import tempfile
import unittest
from bridge.core import Bridge
from bridge.auto_memory import capture_finished, load_for_codex
from bridge.memory import MemoryStore


class AutoMemory(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base=Path(temp.name)
        a=base/"alpha"
        b=base/"beta"
        a.mkdir()
        b.mkdir()
        self.cfg={
            "state_dir":str(base/"state"),
            "projects":[
                {"id":"alpha","root":str(a),"include":["*.py"],"allowed_modes":["read"]},
                {"id":"beta","root":str(b),"include":["*.py"],"allowed_modes":["read"]}
            ]}
        self.bridge=Bridge(self.cfg)
        self.bridge.store.put("task","a"*32,{
            "id":"a"*32,"project_id":"alpha","state":"succeeded",
            "execution_succeeded":True,"tests_passed":False,
            "changes_applied":False,"source_commit_created":False,
            "prompt":"NEVER_STORE_PRIVATE_PROMPT","error":"sk-"+"a"*30,
            "thread_id":"PRIVATE_THREAD"})

    def test_capture_persists_across_new_bridges_and_is_idempotent(self):
        tid="a"*32
        first=capture_finished(self.bridge,tid)
        self.assertEqual(first,capture_finished(self.bridge,tid))
        reopened=Bridge(self.cfg)
        notes=MemoryStore(reopened.store.directory,reopened.project)
        alpha=notes.list_notes("alpha")
        self.assertEqual(len(alpha),1)
        self.assertEqual(alpha[0]["source"],"task:"+tid)
        self.assertIn("未通过",alpha[0]["body"])
        self.assertNotIn("PRIVATE",json.dumps(alpha))
        self.assertNotIn("sk-"+"a"*30,json.dumps(alpha))
        self.assertEqual(notes.list_notes("beta"),[])
        context=load_for_codex(reopened,"alpha")
        self.assertIn("untrusted",context)
        self.assertNotIn("PRIVATE",context)
        self.assertEqual(load_for_codex(reopened,"beta"),"")

    def test_failed_and_cancelled_capture_without_claiming_success(self):
        for n,state in ((1,"failed"),(2,"cancelled")):
            tid=f"{n:032x}"
            self.bridge.store.put("task",tid,{
                "id":tid,"project_id":"alpha","state":state,
                "execution_succeeded":False,"tests_passed":None,
                "changes_applied":False})
            self.assertIsNotNone(capture_finished(self.bridge,tid))
        notes=MemoryStore(self.bridge.store.directory,self.bridge.project)
        self.assertEqual(len(notes.list_notes("alpha")),2)

    def test_disabled_project_has_no_memory_access(self):
        self.cfg["projects"][0]["enabled"]=False
        locked=Bridge(self.cfg)
        self.assertRaises(ValueError,capture_finished,locked,"a"*32)
        self.assertRaises(ValueError,load_for_codex,locked,"alpha")

    def test_project_can_opt_out(self):
        self.cfg["projects"][0]["auto_memory"]=False
        b=Bridge(self.cfg)
        self.assertIsNone(capture_finished(b,"a"*32))
        self.assertEqual(load_for_codex(b,"alpha"),"")


if __name__=="__main__":
    unittest.main()
