"""Offline tests for honest and bounded connection diagnostics."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from bridge.connection_health import ConnectionDiagnostics


class _Socket:
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        home = Path(t.name)
        self.state = home / "state"
        self.state.mkdir(mode=0o700)
        self.config = home / "config.json"
        self.codex = home / "codex"
        self.codex.write_text("#!/bin/sh\n")
        self.codex.chmod(0o700)
        self.data = {
            "port": 8767, "state_dir": str(self.state), "projects": [],
            "codex_command": [str(self.codex), "app-server"],
            "public_url": "https://example.tailnet.ts.net"
        }
        self._write()
        for name in ("bridge.sqlite3", "studio.sqlite3"):
            target = self.state / name
            with sqlite3.connect(target) as db:
                db.execute("CREATE TABLE health (id INTEGER)")
                db.execute("INSERT INTO health VALUES(1)")
            target.chmod(0o600)
        self.calls = []

    def _write(self):
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        self.config.chmod(0o600)

    def fake_run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "systemctl":
            return SimpleNamespace(returncode=0, stdout="")
        if argv[0] == str(self.codex):
            return SimpleNamespace(returncode=0, stdout='{"name":"bridge_memory"}')
        if argv[0] == "/usr/bin/tailscale":
            return SimpleNamespace(returncode=0, stdout='{"BackendState":"Running"}')
        raise AssertionError("unexpected external program")

    def probe(self, addr, timeout=None):
        self.assertEqual(addr, ("127.0.0.1", 8767))
        return _Socket()

    def lookup(self, name):
        return "/usr/bin/tailscale" if name == "tailscale" else (
            "systemctl" if name == "systemctl" else None)

    def diagnostics(self, **kwargs):
        return ConnectionDiagnostics(
            self.config, run=self.fake_run, connect=self.probe,
            lookup=self.lookup, **kwargs)

    def test_all_local_healthy_remote_remains_explicitly_unverified(self):
        report = self.diagnostics().inspect()
        checks = report["checks"]
        self.assertEqual(set(checks), {
            "bridge_config", "bridge_service", "local_mcp",
            "codex_engine", "codex_memory", "memory_database",
            "tailscale", "remote_access"
        })
        for name in ("bridge_config", "bridge_service", "local_mcp",
                     "codex_engine", "codex_memory", "memory_database", "tailscale"):
            self.assertEqual(checks[name]["status"], "ready", name)
        self.assertEqual(checks["remote_access"]["status"], "unknown")
        self.assertFalse(report["remote_oauth_verified"])
        self.assertFalse(report["public_https_verified"])
        self.assertEqual(report["summary"], {"ready":7, "attention":0, "unknown":1})
        self.assertNotIn("example.tailnet", json.dumps(report))
        self.assertTrue(all("shell" not in str(call) for call in self.calls))

    def test_config_permissions_reject_and_return_no_private_values(self):
        self.config.chmod(0o644)
        report = self.diagnostics().inspect()
        self.assertEqual(report["checks"]["bridge_config"]["status"], "attention")
        self.assertEqual(report["checks"]["remote_access"]["status"], "unknown")
        self.assertEqual(self.calls, [])

    def test_missing_codex_tailscale_and_corrupt_memory_report_attentions(self):
        self.codex.unlink()
        (self.state / "studio.sqlite3").write_bytes(b"not a SQLite DB")
        def run(argv, **kw):
            if argv[0] == "systemctl":
                return SimpleNamespace(returncode=3,stdout="")
            return SimpleNamespace(returncode=1,stdout="")
        checker = ConnectionDiagnostics(self.config, run=run,
              connect=lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError()),
              lookup=lambda n: "systemctl" if n == "systemctl" else None)
        checks = checker.inspect()["checks"]
        self.assertEqual(checks["bridge_service"]["status"], "attention")
        self.assertEqual(checks["local_mcp"]["status"], "attention")
        self.assertEqual(checks["codex_engine"]["status"], "attention")
        self.assertEqual(checks["codex_memory"]["status"], "unknown")
        self.assertEqual(checks["tailscale"]["status"], "attention")
        self.assertEqual(checks["memory_database"]["status"], "attention")
        self.assertEqual(checks["remote_access"]["status"], "unknown")

    def test_missing_config_returns_actionable_report_not_traceback(self):
        self.config.unlink()
        report = self.diagnostics().inspect()
        self.assertEqual(report["summary"], {"ready":0,"attention":1,"unknown":7})
        self.assertEqual(report["checks"]["local_mcp"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
