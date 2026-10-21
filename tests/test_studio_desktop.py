"""Bridge Studio Ubuntu one-click launcher: offline, no systemd/service changes."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "scripts/studio_desktop.py"
SPEC = importlib.util.spec_from_file_location("studio_desktop", SOURCE)
desktop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(desktop)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "home"
        self.home.mkdir()
        self.root = self.home / "software" / "Bridge Studio"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "bridge").mkdir()
        (self.root / "docs/assets").mkdir(parents=True)
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / ".venv/bin/python").write_text("python stub")
        for name in ("scripts/bridge_studio_native.py", "scripts/studio_desktop.py", "scripts/bridge_studio.py",
                     "bridge/memory.py", "docs/assets/bridge-studio-icon.svg"):
            (self.root / name).write_text("dummy source", encoding="utf-8")
        self.state = self.home / ".local/share/codex-local-bridge"
        self.state.mkdir(parents=True)
        self.bridge_config = self.home / ".config/codex-local-bridge/config.json"
        self.bridge_config.parent.mkdir(parents=True)
        self.bridge_config.write_text(json.dumps({"state_dir": str(self.state),
                                                   "projects": []}))
        self.bridge_config.chmod(0o600)
        env = {"BRIDGE_CONFIG": str(self.bridge_config),
               "XDG_CONFIG_HOME": str(self.home / ".config"),
               "XDG_DATA_HOME": str(self.home / ".local/share")}
        self.app = desktop.Desktop(root=self.root, home=self.home, env=env)

    def install(self, **kwargs):
        with patch.object(self.app, "show_key"), patch.object(self.app, "open"), \
             patch.object(self.app, "systemctl",
                          return_value=SimpleNamespace(returncode=0)):
            self.app.install(**kwargs)

    def test_install_creates_clickable_menu_user_service_and_private_manifest(self):
        self.install(launch=False)
        self.assertEqual(self.app.manifest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.app.service.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.app.launcher.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.app.menu.stat().st_mode & 0o777, 0o644)
        menu = self.app.menu.read_text()
        self.assertIn("Terminal=false", menu)
        self.assertIn("Icon=", menu)
        self.assertIn("Exec=", menu)
        self.assertIn(desktop.MARKER, menu)
        service = self.app.service.read_text()
        self.assertIn("ExecStart=", service)
        self.assertIn("BRIDGE_STUDIO_DESKTOP=1", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ReadWritePaths=", service)
        self.assertNotIn("0.0.0.0", service)
        self.assertNotIn("sudo", service)
        self.assertEqual(json.loads(self.app.manifest.read_text())["root"], str(self.root))
        self.install(launch=False)  # re-install is idempotent
        self.assertTrue(self.bridge_config.exists())

    def test_app_icon_restores_config_path_after_new_login(self):
        custom = self.home / ".config/custom-bridge/private.json"
        custom.parent.mkdir(parents=True)
        self.bridge_config.rename(custom)
        self.app.bridge_config = custom
        self.app.env["BRIDGE_CONFIG"] = str(custom)
        self.install(launch=False)
        fresh = desktop.Desktop(root=self.root, home=self.home, env={
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
        })
        self.assertNotEqual(fresh.bridge_config, custom)
        restored = fresh.as_installed()
        self.assertEqual(restored.root, self.root)
        self.assertEqual(restored.bridge_config, custom.resolve())
        self.assertEqual(restored.state(), self.state)
        with patch.object(desktop.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0)), \
             patch.object(desktop.subprocess, "Popen") as popen:
            restored.open()
        self.assertEqual(popen.call_args.args[0][-1],
                         str(self.root / "scripts/bridge_studio_native.py"))

    def test_dry_run_has_no_files_or_service_side_effects(self):
        with patch.object(self.app, "systemctl", side_effect=AssertionError), \
             patch.object(self.app, "show_key", side_effect=AssertionError):
            self.app.install(dry_run=True)
        self.assertFalse(self.app.manifest.exists())
        self.assertFalse(self.app.menu.exists())

    def test_foreign_desktop_file_blocks_install_without_overwrite(self):
        self.app.menu.parent.mkdir(parents=True)
        self.app.menu.write_text("[Desktop Entry]\nName=Other App\n")
        with self.assertRaises(FileExistsError):
            self.install(launch=False)
        self.assertEqual(self.app.menu.read_text(), "[Desktop Entry]\nName=Other App\n")
        self.assertFalse(self.app.manifest.exists())

    def test_uninstall_keeps_bridge_config_and_sqlite_memory(self):
        self.install(launch=False)
        (self.state / "studio.sqlite3").write_text("not deleted")
        with patch.object(self.app, "systemctl",
                          return_value=SimpleNamespace(returncode=0)):
            self.app.uninstall()
        self.assertFalse(self.app.manifest.exists())
        self.assertFalse(self.app.menu.exists())
        self.assertFalse(self.app.launcher.exists())
        self.assertEqual((self.state / "studio.sqlite3").read_text(), "not deleted")
        self.assertTrue(self.bridge_config.exists())

    def test_open_starts_native_qt_without_browser(self):
        self.install(launch=False)
        with patch.object(desktop.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0)) as run, \
             patch.object(desktop.subprocess, "Popen") as popen, \
             patch.object(desktop.shutil, "which",
                          side_effect=AssertionError("browser must not be invoked")):
            self.app.open()
        self.assertIn("PySide6.QtWidgets", run.call_args.args[0][-1])
        self.assertEqual(popen.call_args.args[0][-1],
                         str(self.root / "scripts/bridge_studio_native.py"))
        self.assertNotIn("xdg-open", str(popen.call_args))

    def test_missing_qt_blocks_native_launch_safely(self):
        self.install(launch=False)
        with patch.object(desktop.subprocess, "run",
                          return_value=SimpleNamespace(returncode=1)), \
             patch.object(desktop.subprocess, "Popen",
                          side_effect=AssertionError("no launch without Qt")):
            with self.assertRaisesRegex(RuntimeError, "PySide6"):
                self.app.open()

    def test_unsafe_config_and_symlink_rejected(self):
        self.bridge_config.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.app.state()
        self.bridge_config.chmod(0o600)
        self.app.menu.parent.mkdir(parents=True)
        self.app.menu.symlink_to(self.bridge_config)
        with self.assertRaises(FileExistsError):
            self.install(launch=False)

    def test_quoted_exec_cannot_inject_control_sequences(self):
        self.assertEqual(desktop.quoted("/home/a b/launch"), '"/home/a b/launch"')
        self.assertRaises(ValueError, desktop.quoted, "/tmp/name\nExec=evil")


if __name__ == "__main__":
    unittest.main()
