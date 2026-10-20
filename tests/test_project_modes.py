"""Local-only project permission profiles. No real service or user config touched."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.project_modes import ProjectModes, project_mode
from bridge.core import Bridge


class PermissionProfiles(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root=Path(t.name)
        self.state=self.root/"private"
        self.state.mkdir(mode=0o700)
        self.workspace=self.root/"demo"
        self.workspace.mkdir()
        (self.workspace/"README.md").write_text("# Hello\n")
        self.config_path=self.root/"config.json"
        self.config={
            "state_dir":str(self.state),
            "projects":[{"id":"demo","name":"Demo","root":str(self.workspace),
                         "include":["*.md"],"allowed_modes":["read"],
                         "file_write_enabled":False, "project_authorization":False,
                         "tests":[["/usr/bin/python3","-m","unittest","discover","-v"]]}]
        }
        self.write()
        self.restarts=[]
        self.manager=ProjectModes(self.config_path,restart=lambda: self.restarts.append(1))

    def write(self):
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)

    def test_off_blocks_mcp_and_restores_read(self):
        value=self.manager.change("demo","off",confirmed=True)
        self.assertEqual(value["previous"],"read")
        conf=self.manager.read()
        self.assertEqual(project_mode(conf["projects"][0]),"off")
        self.assertEqual(conf["projects"][0]["allowed_modes"],[])
        self.assertRaises(ValueError,Bridge(conf).project,"demo")
        value=self.manager.change("demo","read",confirmed=True)
        self.assertEqual(value["mode"],"read")
        self.assertEqual(Bridge(self.manager.read()).project("demo")["id"],"demo")
        self.assertEqual(len(self.restarts),2)

    def test_confirmation_required_and_nochange_skips_restart(self):
        with self.assertRaises(PermissionError):
            self.manager.change("demo","write")
        self.assertEqual(self.manager.change("demo","read",confirmed=True)["changed"],False)
        self.assertFalse(self.restarts)

    def test_readwrite_grant_is_local_only_and_tests_required(self):
        with patch("bridge.core.Bridge") as bridge_cls:
            bridge_cls.return_value.authorization.grant.return_value={"ok":True}
            result=self.manager.change("demo","write",confirmed=True)
        self.assertEqual(result["mode"],"write")
        p=self.manager.read()["projects"][0]
        self.assertEqual(project_mode(p),"write")
        self.assertTrue(p["file_write_enabled"])
        self.assertTrue(p["project_authorization"])
        self.assertIn("write_test",p["allowed_modes"])
        bridge_cls.return_value.authorization.grant.assert_called_once_with("demo")
        self.assertTrue(Path(result["backup"]).is_file())

    def test_restart_failure_restores_original_config(self):
        before=self.config_path.read_bytes()
        calls=[]
        def fail_once():
            calls.append(1)
            if len(calls)==1: raise RuntimeError("simulated service failure")
        m=ProjectModes(self.config_path,restart=fail_once)
        with self.assertRaisesRegex(RuntimeError,"simulated"):
            m.change("demo","off",confirmed=True)
        self.assertEqual(self.config_path.read_bytes(),before)
        self.assertEqual(len(calls),2)

    def test_write_denied_without_sandbox_tests(self):
        self.config["projects"][0]["tests"]=[]
        self.write()
        with self.assertRaisesRegex(ValueError,"测试"):
            self.manager.change("demo","write",confirmed=True)
        self.assertFalse(self.restarts)


if __name__=="__main__":
    unittest.main()
