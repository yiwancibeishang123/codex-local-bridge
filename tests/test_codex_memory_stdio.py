"""Independent Codex stdio MCP uses ChatGPT's exact local project memory DB."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from bridge.codex_memory_stdio import LocalCodexMemory, make_server, toml_snippet
from bridge.memory import MemoryStore


class CodexLocalMemoryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base=Path(temp.name)
        first=base/"alpha";second=base/"beta"
        first.mkdir();second.mkdir()
        self.config={
            "state_dir":str(base/"state"),
            "projects":[{"id":"alpha","root":str(first),"include":["*.md"],"allowed_modes":["read"]},
                        {"id":"beta","root":str(second),"include":["*.md"],"allowed_modes":["read"]}]}
        self.codex=LocalCodexMemory(self.config)

    def test_memory_shared_bidirectionally_and_scoped(self):
        first=self.codex.save("alpha","算法决策","使用 ROS2 重建",kind="decision",tags=["ros2"])
        note=first["note"]
        self.assertEqual(note["origin"],"codex")
        self.assertEqual(note["source"],"codex-local-stdio")
        # ChatGPT's MemoryStore and Studio read the same SQLite file.
        chatgpt_memory=MemoryStore(self.codex.bridge.store.directory,self.codex.bridge.project)
        chatgpt_notes=chatgpt_memory.search_notes("alpha","ROS2")
        self.assertEqual(chatgpt_notes[0]["id"],note["id"])
        self.assertEqual(self.codex.search("alpha","ROS2")["notes"][0]["id"],note["id"])
        self.assertEqual(self.codex.search("beta")["notes"],[])
        handoff=self.codex.handoff("alpha")
        self.assertTrue(handoff["memories"])
        self.assertIn("untrusted",handoff["warning"].lower())

    def test_secret_rejection_disabled_project_and_invalid_limits(self):
        with self.assertRaises(ValueError):
            self.codex.save("alpha","Credential","api_key=INSECURE_SECRET_LONG_STRING")
        with self.assertRaises(ValueError):
            self.codex.search("alpha",limit=1000)
        self.config["projects"][0]["enabled"]=False
        locked=LocalCodexMemory(self.config)
        with self.assertRaises(ValueError):
            locked.search("alpha")
        with self.assertRaises(ValueError):
            locked.save("alpha","Denied","Denied")

    def test_server_exposes_only_four_memory_tools(self):
        srv=make_server(self.config)
        tools=asyncio.run(srv.list_tools())
        self.assertEqual({x.name for x in tools},{
            "search_bridge_project_memories", "save_bridge_project_memory",
            "get_bridge_project_handoff", "get_bridge_task_receipt"})
        for t in tools:
            self.assertNotIn("request_write_file",t.name)
            self.assertNotIn("delegate_task",t.name)

    def test_example_contains_no_credentials(self):
        snippet=toml_snippet("/opt/bridge/python","/home/user/bridge config.json")
        self.assertIn("mcp_servers.bridge_memory",snippet)
        self.assertIn("BRIDGE_CONFIG",snippet)
        self.assertIn("config.json",snippet)
        self.assertNotIn("sk-",snippet)


if __name__=="__main__":
    unittest.main()
