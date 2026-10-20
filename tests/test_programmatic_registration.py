"""Native app may invoke the hardened read-only registrar without a source script."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import add_readonly_project as helper


class PackagedRegistrationTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.home=Path(t.name)
        self.project=self.home/"work"/"research"
        self.project.mkdir(parents=True)
        (self.project/"README.md").write_text("## Research\n",encoding="utf-8")
        self.state=self.home/"state"
        self.state.mkdir(mode=0o700)
        self.config=self.home/"custom_bridge.json"
        self.config.write_text(json.dumps({
            "state_dir":str(self.state), "projects":[],
            "codex_command":["/usr/bin/codex", "app-server"]
        }),encoding="utf-8")
        self.config.chmod(0o600)
        cli=self.home/".local/bin/codex-local-bridge"
        cli.parent.mkdir(parents=True)
        cli.write_text("test cli",encoding="utf-8")
        self.calls=[]

    def test_programmatic_register_accepts_selected_config_without_script_path(self):
        with patch.object(helper.Path,"home",return_value=self.home), \
             patch.object(helper,"git_root",return_value=False), \
             patch.object(helper,"execute",side_effect=lambda *args:self.calls.append(args)), \
             patch.dict(os.environ,{"BRIDGE_CONFIG":"/no/such/old/config.json"}), \
             redirect_stdout(io.StringIO()):
            helper.main(["research",str(self.project),"--yes"],config_file=self.config)
        saved=json.loads(self.config.read_text())
        self.assertEqual(saved["projects"][0]["id"],"research")
        self.assertEqual(saved["projects"][0]["allowed_modes"],["read"])
        self.assertEqual(saved["projects"][0]["context_files"],["README.md"])
        self.assertTrue(any("restart" in line for line in self.calls))
        self.assertTrue(any((self.state/"registration-backups").glob("*.json")))

if __name__=="__main__":
    unittest.main()
