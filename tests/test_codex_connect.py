"""One-click Codex memory MCP registration: local confirmation and config backup."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from bridge.codex_connect import register_codex_memory, autodetect_backend_python

class ConnectTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.home=Path(t.name)
        self.config=self.home/"bridge.json"
        self.config.write_text("{}")
        self.codex=self.home/"bin/codex"
        self.codex.parent.mkdir()
        self.codex.write_text("#!/bin/sh\n")
        self.codex.chmod(0o755)
        self.python=self.home/"bridge/.venv/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.symlink_to(Path(sys.executable).resolve())
        self.codex_dir=self.home/".codex"
        self.codex_dir.mkdir()
        self.settings=self.codex_dir/"config.toml"
        self.settings.write_text("# existing custom settings\n")

    def register(self, *, response=(1,0,0), confirmed=True):
        calls=[]
        sequence=list(response)
        def fake_run(argv,**kwargs):
            calls.append(list(argv))
            return SimpleNamespace(returncode=sequence.pop(0),stdout="",stderr="")
        value=register_codex_memory(
            {"codex_command":[str(self.codex),"app-server"]},self.config,
            self.python,confirmed=confirmed,home=self.home,run=fake_run)
        return value,calls

    def test_register_only_after_explicit_confirmation_and_backup(self):
        value,calls=self.register()
        self.assertTrue(value["connected"])
        self.assertTrue(value["backup_available"])
        self.assertEqual([x[1:3] for x in calls],
                         [["mcp","get"],["mcp","add"],["mcp","get"]])
        self.assertIn("--env",calls[1])
        self.assertEqual(calls[1][-3:], [str(self.python),"-m","bridge.codex_memory_stdio"])
        self.assertNotIn("shell",str(calls))
        self.assertTrue(list((self.home/".local/share/codex-local-bridge/codex-config-backups").glob("*.toml")))

    def test_denies_overwrite_of_existing_provider(self):
        with self.assertRaises(FileExistsError):
            self.register(response=(0,))
        self.assertFalse((self.home/".local/share/codex-local-bridge/codex-config-backups").exists())

    def test_denies_unconfirmed_changes_and_unsafe_codex_settings(self):
        with self.assertRaises(PermissionError):
            self.register(confirmed=False)
        self.settings.unlink()
        self.settings.symlink_to(self.config)
        with self.assertRaises(PermissionError):
            self.register()

    def test_autodetect_preserves_virtualenv_python_symlink(self):
        result=autodetect_backend_python(home=self.home,root=self.home/"bridge")
        self.assertEqual(result,str(self.python))
        self.assertNotEqual(result,str(Path(sys.executable).resolve()))

if __name__=="__main__":
    unittest.main()
