"""Per-project auto-memory is user-controlled, backed up and rolled back."""
import json
from pathlib import Path
import tempfile
import unittest

from bridge.memory_preferences import AutoMemoryPreferences


class MemoryPreferenceTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.home=Path(t.name)
        self.state=self.home/"state"
        self.state.mkdir(mode=0o700)
        self.config=self.home/"bridge.json"
        self.record={"state_dir":str(self.state),"projects":[
            {"id":"demo","root":str(self.home/"demo"),"allowed_modes":["read"],
             "file_write_enabled":False,"project_authorization":False},
            {"id":"second","root":str(self.home/"second"),"auto_memory":False}]}
        self.config.write_text(json.dumps(self.record))
        self.config.chmod(0o600)
        self.restarts=[]
        self.prefs=AutoMemoryPreferences(self.config,restart=lambda:self.restarts.append(True))

    def test_set_and_reopen_only_one_project_and_preserve_permissions(self):
        self.assertTrue(self.prefs.read("demo"))
        self.assertFalse(self.prefs.read("second"))
        original=self.config.read_bytes()
        result=self.prefs.set("demo",False,confirmed=True)
        self.assertTrue(result["changed"])
        self.assertEqual(self.restarts,[True])
        self.assertFalse(self.prefs.read("demo"))
        self.assertFalse(self.prefs.read("second"))
        saved=json.loads(self.config.read_text())
        self.assertEqual(saved["projects"][0]["allowed_modes"],["read"])
        self.assertFalse(saved["projects"][0]["file_write_enabled"])
        self.assertEqual(Path(result["backup"]).read_bytes(),original)
        self.assertEqual(Path(result["backup"]).stat().st_mode&0o777,0o600)
        self.assertEqual(self.prefs.set("demo",False,confirmed=True)["changed"],False)
        self.assertEqual(len(self.restarts),1)

    def test_restart_failure_restores_private_config(self):
        original=self.config.read_bytes()
        tries=[]
        def fail_once():
            tries.append(1)
            if len(tries)==1:
                raise RuntimeError("restart test failure")
        prefs=AutoMemoryPreferences(self.config,restart=fail_once)
        with self.assertRaisesRegex(RuntimeError,"restart test failure"):
            prefs.set("demo",False,confirmed=True)
        self.assertEqual(self.config.read_bytes(),original)
        self.assertEqual(len(tries),2)

    def test_confirmation_and_unknown_project_rejected(self):
        with self.assertRaises(PermissionError):
            self.prefs.set("demo",False)
        with self.assertRaises(ValueError):
            self.prefs.set("no_such_project",False,confirmed=True)
        self.config.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.prefs.read("demo")


if __name__=="__main__":
    unittest.main()
