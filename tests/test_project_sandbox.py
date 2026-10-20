"""Project permission sandbox: local write paths cannot escape a scoped project."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.project_modes import ProjectModes
from bridge.project_sandbox import SystemdProjectSandbox, safe_root, render_sandbox, HEADER

class SandboxTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home=Path(temp.name)
        self.root=self.home/"research project"
        self.root.mkdir()
        self.unit=self.home/".config/systemd/user"
        self.sandbox=SystemdProjectSandbox(unit_dir=self.unit,home=self.home)

    def test_allowlist_precise_and_deterministic(self):
        project={"id":"research","root":str(self.root),"allowed_modes":["read","write_test"],
                 "file_write_enabled":True,"project_authorization":True}
        text=render_sandbox([project],self.home)
        self.assertIn('ReadWritePaths="'+str(self.root)+'"',text)
        self.assertNotIn('ReadWritePaths="'+str(self.home)+'"',text)
        self.assertEqual(render_sandbox([dict(project,enabled=False)],self.home),None)
        self.assertEqual(render_sandbox([dict(project,file_write_enabled=False)],self.home),None)
        before=self.sandbox.apply([project])
        self.assertIsNone(before)
        self.assertEqual(self.sandbox.snapshot(),text)
        self.sandbox.restore(None)
        self.assertFalse(self.sandbox.dropin.exists())

    def test_denies_home_root_outside_symlinks_and_malicious_control_chars(self):
        with self.assertRaises(ValueError):
            safe_root(str(self.home),self.home)
        with self.assertRaises(ValueError):
            safe_root("/",self.home)
        outside=Path("/var/tmp")
        with self.assertRaises(ValueError):
            safe_root(str(outside),self.home)
        link=self.home/"external"
        link.symlink_to("/var/tmp")
        with self.assertRaises(ValueError):
            safe_root(str(link),self.home)
        weird=self.home/"newline\n"
        weird.mkdir()
        with self.assertRaises(ValueError):
            safe_root(str(weird),self.home)

    def test_foreign_dropin_is_never_overwritten(self):
        self.sandbox.dropin.parent.mkdir(parents=True)
        self.sandbox.dropin.write_text("[Service]\nReadWritePaths=/\n")
        self.sandbox.dropin.chmod(0o600)
        with self.assertRaises(PermissionError):
            self.sandbox.apply([])
        self.assertIn("ReadWritePaths=/",self.sandbox.dropin.read_text())
        self.sandbox.dropin.unlink()
        self.sandbox.dropin.symlink_to(self.root)
        with self.assertRaises(PermissionError):
            self.sandbox.snapshot()

    def test_mode_switch_updates_and_revokes_managed_dropin(self):
        state=self.home/"state"
        state.mkdir()
        conf=self.home/"bridge.json"
        record={"id":"research","root":str(self.root),"include":["*.py"],
                "allowed_modes":["read"],"project_authorization":False,
                "file_write_enabled":False,
                "tests":[["/usr/bin/python3","-m","unittest","discover","-v"]]}
        conf.write_text(json.dumps({"state_dir":str(state),"projects":[record]}))
        conf.chmod(0o600)
        restarts=[]
        manager=ProjectModes(conf,restart=lambda: restarts.append(1),sandbox=self.sandbox)
        from bridge.core import Bridge
        with patch("bridge.policy.ProjectAuthorization.grant") as grant:
            manager.change("research","write",confirmed=True)
            grant.assert_called_once()
        self.assertIsNotNone(self.sandbox.snapshot())
        with patch("bridge.policy.ProjectAuthorization.revoke") as revoke:
            manager.change("research","read",confirmed=True)
            revoke.assert_called_once()
        self.assertIsNone(self.sandbox.snapshot())
        self.assertEqual(len(restarts),2)

    def test_service_restart_failure_restores_both_config_and_allowlist(self):
        state=self.home/"state";state.mkdir()
        conf=self.home/"bridge.json"
        record={"id":"research","root":str(self.root),"include":["*.py"],
                "allowed_modes":["read"],"project_authorization":False,
                "file_write_enabled":False,"tests":[]}
        original=json.dumps({"state_dir":str(state),"projects":[record]})
        conf.write_text(original);conf.chmod(0o600)
        tries=[]
        def once():
            tries.append(1)
            if len(tries)==1:raise RuntimeError("restart failed")
        manager=ProjectModes(conf,restart=once,sandbox=self.sandbox)
        with self.assertRaises(RuntimeError):
            manager.change("research","off",confirmed=True)
        self.assertEqual(conf.read_text(),original)
        self.assertIsNone(self.sandbox.snapshot())
        self.assertEqual(len(tries),2)

if __name__=="__main__":
    unittest.main()
