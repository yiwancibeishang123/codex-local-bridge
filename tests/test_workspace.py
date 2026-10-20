import tempfile
import unittest
from pathlib import Path
from bridge.filetools import WorkspaceBridge, ProjectPolicy, WorkspaceError

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "proj"
        self.project.mkdir()
        self.state = self.base / "private_state"
        self.bridge = WorkspaceBridge({"demo": ProjectPolicy(self.project, ("*.py", "*.txt"), True)}, self.state)
        (self.project / "hello.py").write_text("print('before')\n", encoding="utf-8")

    def test_read_and_safe_modify(self):
        state = self.bridge.read_file("demo", "hello.py")
        proposed = self.bridge.request_edit_file("demo", "hello.py", "before", "after", state["sha256"])
        self.assertIn("pending", proposed["status"])
        self.assertEqual((self.project / "hello.py").read_text(), "print('before')\n")
        reviewed = self.bridge.review_proposal_local(proposed["proposal_id"])
        self.assertIn("+print('after')", reviewed["diff"])
        output = self.bridge.approve_and_apply(proposed["proposal_id"])
        self.assertEqual(output["status"], "applied")
        self.assertEqual((self.project / "hello.py").read_text(), "print('after')\n")
        self.assertEqual(Path(output["backup"]).read_text(), "print('before')\n")
        self.assertEqual(self.bridge.get_write_status(proposed["proposal_id"])["status"], "applied")

    def test_new_file_requires_approval(self):
        prop = self.bridge.request_write_file("demo", "new.txt", "created", "ABSENT")
        self.assertFalse((self.project / "new.txt").exists())
        self.bridge.approve_and_apply(prop["proposal_id"])
        self.assertEqual((self.project / "new.txt").read_text(), "created")

    def test_concurrent_conflict(self):
        h = self.bridge.read_file("demo", "hello.py")["sha256"]
        prop = self.bridge.request_write_file("demo", "hello.py", "new content", h)
        (self.project / "hello.py").write_text("other change")
        with self.assertRaises(WorkspaceError):
            self.bridge.approve_and_apply(prop["proposal_id"])
        self.assertEqual((self.project / "hello.py").read_text(), "other change")

    def test_safety(self):
        for name in ("../secret.py", "/etc/passwd", ".env", "nested/../../x.py", "keys/credentials.json", "x\\y.py"):
            with self.subTest(name=name), self.assertRaises(WorkspaceError):
                self.bridge.read_file("demo", name)
        outside = self.base / "outside.txt"
        outside.write_text("secret")
        (self.project / "link.py").symlink_to(outside)
        import os
        os.link(outside, self.project / "hardlink.py")
        with self.assertRaises((OSError, WorkspaceError)):
            self.bridge.read_file("demo", "hardlink.py")
        with self.assertRaises((OSError, WorkspaceError)):
            self.bridge.read_file("demo", "link.py")
        (self.project / "sub").mkdir()
        (self.project / "sub" / "ok.py").write_text("print('ok')\n")
        self.assertIn("print('ok')", self.bridge.read_file("demo", "sub/ok.py")["content"])
        (self.project / "TokenScope").mkdir()
        (self.project / "TokenScope" / "app.py").write_text("print('okay')")
        self.assertIn("okay", self.bridge.read_file("demo", "TokenScope/app.py")["content"])
        (self.project / "alink").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises((OSError, WorkspaceError)):
            self.bridge.read_file("demo", "alink/outside.txt")

    def test_scope_and_list(self):
        (self.project / "blocked.md").write_text("not whitelisted")
        with self.assertRaises(WorkspaceError):
            self.bridge.read_file("demo", "blocked.md")
        listed = self.bridge.list_files("demo")
        self.assertIn("hello.py", [x.get("path") for x in listed["entries"]])
        self.assertNotIn("blocked.md", [x.get("path") for x in listed["entries"]])

    def test_redaction_in_remote_read(self):
        (self.project / "safe.py").write_text("API_KEY = 'sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ'\nprint('hi')\n")
        result = self.bridge.read_file("demo", "safe.py")
        self.assertTrue(result["redacted"])
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", result["content"])
        proposal = self.bridge.request_edit_file("demo", "safe.py", "hi", "hello", result["sha256"])
        self.bridge.approve_and_apply(proposal["proposal_id"])
        self.assertIn("sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ", (self.project / "safe.py").read_text())

    def test_write_disabled(self):
        bridge = WorkspaceBridge({"demo": ProjectPolicy(self.project, ("*.py",), False)}, self.base / "other_state")
        with self.assertRaises(WorkspaceError):
            bridge.request_write_file("demo", "new.py", "x", "ABSENT")

if __name__ == "__main__":
    unittest.main()
