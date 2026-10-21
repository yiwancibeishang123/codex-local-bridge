"""Codex handoffs include explicit targets and the real unittest test suite."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from bridge.core import Bridge

class CodexHandoffContextTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        base=Path(t.name)
        self.root=base/"airgym";self.root.mkdir()
        self.state=base/"state";self.state.mkdir(mode=0o700)
        self.config={"state_dir":str(self.state),"projects":[{
            "id":"airgym","root":str(self.root),
            "include":["*.py","*.md"],"context_files":["README.md"],
            "allowed_modes":["read","write_test"],
            "project_authorization":True,"file_write_enabled":True,
            "tests":[["/usr/bin/python3","-m","unittest","discover",
                       "-s","bridge_studio_tests","-v"]]}]}
        (self.root/"README.md").write_text("# airgym\n")
        (self.root/"web_probe.py").write_text("MARKER='old'\n")
        suite=self.root/"bridge_studio_tests";suite.mkdir()
        (suite/"test_repair.py").write_text(
            "import unittest\n"
            "class TestRepair(unittest.TestCase):\n"
            "    def test_unit(self): self.assertEqual(4,2+2)\n")
        self.bridge=Bridge(self.config)
        self.bridge.authorization.grant("airgym")

    def test_context_paths_select_target_without_config_edit(self):
        got=self.bridge.submit("airgym","Change web_probe.py","write_test",
                               context_paths=["web_probe.py"])
        task=self.bridge.task(got["id"])
        self.assertEqual(task["context_paths"],["web_probe.py"])
        p=self.bridge.project("airgym")
        sources=self.bridge.sources(p,extra_paths=task["context_paths"],include_tests=True)
        self.assertIn("README.md",sources)
        self.assertIn("web_probe.py",sources)
        self.assertIn("bridge_studio_tests/test_repair.py",sources)
        self.assertEqual(sources["web_probe.py"],"MARKER='old'\n")

    def test_selected_context_is_project_bound_and_limited(self):
        with self.assertRaises(ValueError):
            self.bridge.submit("airgym","inspect","read",context_paths=["../outside.py"])
        with self.assertRaises(ValueError):
            self.bridge.submit("airgym","inspect","read",context_paths=["missing.py"])
        with self.assertRaises(ValueError):
            self.bridge.submit("airgym","inspect","read",context_paths=["x.py"]*41)

    def test_auto_included_tests_actually_execute_in_sandbox(self):
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        import asyncio
        p=self.bridge.project("airgym")
        sources=self.bridge.sources(p,extra_paths=["web_probe.py"],include_tests=True)
        snap=Path(tempfile.mkdtemp(prefix="bridge-test-",dir=self.state))
        for path,content in sources.items():
            full=snap/path
            full.parent.mkdir(parents=True,exist_ok=True)
            full.write_text(content)
        try:
            test_result=asyncio.run(self.bridge.run_test(
                snap,p["tests"][0],"sandbox-test-task"))
            self.assertEqual(test_result["exit_code"],0,test_result["output"])
            self.assertIn("Ran 1 test",test_result["output"])
        finally:
            shutil.rmtree(snap)

if __name__=="__main__":unittest.main()
