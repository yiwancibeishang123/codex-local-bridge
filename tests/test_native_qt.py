"""Real Qt Widgets headless smoke: one native window; no HTTP/browser required."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class NativeQtSmoke(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 optional desktop extra")
    def test_window_creates_and_exits_without_browser(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = base / "state"
            state.mkdir(mode=0o700)
            config = base / "config.json"
            config.write_text(json.dumps({
                "state_dir": str(state),
                "projects": [],
                "codex_command": ["codex", "app-server"],
                "public_url": "http://127.0.0.1:8767"
            }))
            config.chmod(0o600)
            env = dict(os.environ, BRIDGE_CONFIG=str(config),
                       QT_QPA_PLATFORM="offscreen",
                       BRIDGE_STUDIO_SMOKE="1",
                       HTTP_PROXY="", HTTPS_PROXY="", ALL_PROXY="")
            proc = subprocess.run(
                [sys.executable, str(root / "scripts/bridge_studio_native.py")],
                cwd=root, env=env, capture_output=True, text=True, timeout=25)
            self.assertEqual(proc.returncode, 0,
                             "Native Qt failed: " + proc.stderr[-3000:])
            self.assertTrue((state / "studio.sqlite3").is_file())


if __name__=="__main__":
    unittest.main()
