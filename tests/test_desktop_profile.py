"""Owner-local first-run config selection without OAuth/key duplication."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from bridge.desktop_profile import config_location, remember_config, profile_file


class ProfileTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.home=Path(t.name)
        self.config=self.home/"custom settings"/"config.json"
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"projects":[],"state_dir":str(self.home/"state")}))
        self.config.chmod(0o600)

    def test_first_run_can_remember_custom_config_and_restart_without_env(self):
        self.assertEqual(config_location(home=self.home,environ={}),
            self.home/".config/codex-local-bridge/config.json")
        self.assertEqual(remember_config(self.config,home=self.home),self.config)
        self.assertEqual(config_location(home=self.home,environ={}),self.config)
        st=profile_file(self.home).stat()
        self.assertEqual(st.st_mode&0o777,0o600)
        self.assertEqual(profile_file(self.home).parent.stat().st_mode&0o777,0o700)
        raw=profile_file(self.home).read_text()
        self.assertIn(str(self.config),raw)
        self.assertNotIn("refresh_token",raw)
        self.assertEqual(config_location(home=self.home,environ={"BRIDGE_CONFIG":str(self.config)}),
                         self.config)

    def test_refuse_insecure_config_profile_and_symlinks(self):
        self.config.chmod(0o644)
        with self.assertRaises(PermissionError):
            remember_config(self.config,home=self.home)
        self.config.chmod(0o600)
        self.config.unlink()
        other=self.home/"other.json"
        other.write_text("{}")
        self.config.symlink_to(other)
        with self.assertRaises(PermissionError):
            remember_config(self.config,home=self.home)
        self.config.unlink()
        self.config.write_text(json.dumps({"projects":[],"state_dir":str(self.home/"state")}))
        self.config.chmod(0o600)
        remember_config(self.config,home=self.home)
        profile_file(self.home).chmod(0o644)
        with self.assertRaises(PermissionError):
            config_location(home=self.home,environ={})


if __name__=="__main__":
    unittest.main()
