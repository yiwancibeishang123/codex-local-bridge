"""GUI-selectable safe unittest profiles fix the missing tests field for AirGym.

No actual source workspace, ROS simulator or systemd user service is touched.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from bridge.project_tests import ProjectTestProfiles, available_profiles, SMOKE_DIR, SMOKE_NAME
from bridge.project_modes import ProjectModes


class TestProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.state=self.home/"state";self.state.mkdir(mode=0o700)
        self.root=self.home/"airgym"
        self.root.mkdir()
        (self.root/"rl.py").write_text("x=1\n")
        self.cfg=self.home/"config.json"
        self.data={"state_dir":str(self.state),
                   "projects":[{"id":"airgym_yingjie","root":str(self.root),
                                "include":["*.py","*.md"],
                                "allowed_modes":["read"],"tests":[],
                                "file_write_enabled":False,
                                "project_authorization":False}]}
        self.cfg.write_text(json.dumps(self.data))
        self.cfg.chmod(0o600)
        self.restarts=[]
        self.profile=ProjectTestProfiles(self.cfg,restart=lambda:self.restarts.append(1))
        patcher=patch("bridge.project_tests.safe_root",side_effect=lambda v:self.root)
        patcher.start();self.addCleanup(patcher.stop)

    def test_creates_explicit_safe_syntax_unittest_and_permissions_can_be_elevated_after(self):
        options=available_profiles(self.data["projects"][0])
        self.assertIn("syntax_smoke",[x["id"] for x in options])
        result=self.profile.set("airgym_yingjie","syntax_smoke",confirmed=True)
        self.assertEqual(result["scope"],"syntax_only")
        self.assertTrue((self.root/SMOKE_DIR/SMOKE_NAME).exists())
        self.assertEqual(self.restarts,[1])
        saved=json.loads(self.cfg.read_text())
        self.assertEqual(saved["projects"][0]["allowed_modes"],["read"])
        self.assertEqual(saved["projects"][0]["tests"],[["/usr/bin/python3","-m","unittest","discover","-s",SMOKE_DIR,"-v"]])
        from bridge.policy import ordinary_tests
        self.assertTrue(ordinary_tests(saved["projects"][0]["tests"]))
        self.assertTrue(Path(result["backup"]).is_file())

    def test_profile_failure_removes_new_test_file_and_restores_config(self):
        before=self.cfg.read_bytes()
        calls=[]
        def fail_once():
            calls.append(1)
            if len(calls)==1:
                raise RuntimeError("simulated failure")
        p=ProjectTestProfiles(self.cfg,restart=fail_once)
        with self.assertRaisesRegex(RuntimeError,"simulated"):
            p.set("airgym_yingjie","syntax_smoke",confirmed=True)
        self.assertEqual(self.cfg.read_bytes(),before)
        self.assertFalse((self.root/SMOKE_DIR/SMOKE_NAME).exists())
        self.assertEqual(len(calls),2)

    def test_configure_existing_unittest_directory(self):
        suite=self.root/"tests";suite.mkdir()
        (suite/"test_code.py").write_text("import unittest\nclass TestA(unittest.TestCase):\n    def test_ok(self): self.assertEqual(2,2)\n")
        result=self.profile.set("airgym_yingjie","tests",confirmed=True)
        self.assertEqual(result["profile"],"tests")
        self.assertFalse((self.root/SMOKE_DIR).exists())

    def test_deny_unconfirmed_and_unavailable_suites(self):
        with self.assertRaises(PermissionError):
            self.profile.set("airgym_yingjie","syntax_smoke")
        with self.assertRaises(ValueError):
            self.profile.set("airgym_yingjie","tests",confirmed=True)
        self.assertEqual(self.restarts,[])

if __name__=="__main__":unittest.main()
