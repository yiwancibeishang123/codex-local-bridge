"""Offline regression tests for the LOCAL read-only registration helper."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


HELPER = Path(__file__).resolve().parents[1] / "scripts/add_readonly_project.py"
spec = importlib.util.spec_from_file_location("add_readonly_project", HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class AddReadOnlyProject(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.root = self.home / "projects" / "new_project"
        self.root.mkdir(parents=True)
        (self.root / "README.md").write_text("Project overview", encoding="utf-8")
        self.config_path = self.home / ".config" / "codex-local-bridge" / "config.json"
        self.config_path.parent.mkdir(parents=True)
        self.state = self.home / ".local" / "share" / "codex-local-bridge"
        self.state.mkdir(parents=True)
        self.cli = self.home / ".local" / "bin" / "codex-local-bridge"
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text("fake cli", encoding="utf-8")
        self.old_config = {
            "state_dir": str(self.state),
            "public_url": "https://existing.example.ts.net",
            "projects": [{"id": "old_project", "root": str(self.home / "projects" / "old")}],
            "codex_command": ["codex", "app-server"]
        }
        self.config_path.write_text(json.dumps(self.old_config), encoding="utf-8")
        self.config_path.chmod(0o600)

    def call(self, args, execute):
        argv = ["add_readonly_project.py", *args]
        with patch.object(helper.Path, "home", return_value=self.home), \
             patch.dict(os.environ, {"BRIDGE_CONFIG": str(self.config_path)}), \
             patch.object(helper, "git_root", return_value=False), \
             patch.object(helper, "execute", side_effect=execute), \
             patch.object(sys, "argv", argv), \
             patch("sys.stdout", new_callable=io.StringIO):
            helper.main()

    def test_registration_preserves_config_and_limits_privileges(self):
        commands = []
        before = self.config_path.read_bytes()
        self.call(["new_project", str(self.root), "--yes"],
                  lambda *args: commands.append(args))
        updated = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["public_url"], self.old_config["public_url"])
        self.assertEqual(updated["codex_command"], self.old_config["codex_command"])
        self.assertEqual(updated["projects"][0], self.old_config["projects"][0])
        registered = updated["projects"][1]
        self.assertEqual(registered["context_files"], ["README.md"])
        self.assertEqual(registered["allowed_modes"], ["read"])
        self.assertFalse(registered["file_write_enabled"])
        self.assertFalse(registered["project_authorization"])
        self.assertEqual(registered["tests"], [])
        self.assertTrue(any("restart" in cmd for cmd in commands))
        self.assertTrue(any("project-policy" in cmd for cmd in commands))
        backups = list((self.state / "registration-backups").glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_duplicate_id_fails_without_changing_config(self):
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "already registered"):
            self.call(["old_project", str(self.root), "--yes"], lambda *args: None)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_dry_run_does_not_modify_config(self):
        before = self.config_path.read_bytes()
        self.call(["new_project", str(self.root), "--dry-run"],
                  lambda *args: self.fail("dry-run should not call systemctl"))
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse((self.state / "registration-backups").exists())


    def test_non_git_project_chooses_small_source_context(self):
        (self.root / "README.md").unlink()
        (self.root / "core.py").write_text("answer = 42\n", encoding="utf-8")
        self.call(["new_project", str(self.root), "--yes"], lambda *args: None)
        updated = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["projects"][1]["context_files"], ["core.py"])

    def test_restart_failure_restores_original_config(self):
        previous = self.config_path.read_bytes()
        restarts = []
        def simulate(*args):
            if "restart" in args:
                restarts.append(args)
                if len(restarts) == 1:
                    raise RuntimeError("simulated restart failure")
        with self.assertRaisesRegex(RuntimeError, "simulated restart failure"):
            self.call(["new_project", str(self.root), "--yes"], simulate)
        self.assertEqual(self.config_path.read_bytes(), previous)
        self.assertEqual(len(restarts), 2)

    def test_registration_rejects_home_root(self):
        previous = self.config_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Refusing home"):
            self.call(["new_project", str(self.home), "--yes"], lambda *args: None)
        self.assertEqual(self.config_path.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
