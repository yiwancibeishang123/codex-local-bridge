"""MCP-first whole-folder access acceptance with private temp project roots."""
import base64
import hashlib
from pathlib import Path
import os
import tempfile
import unittest
from bridge.core import Bridge
from bridge.trusted_workspace import TrustedWorkspace, CHUNK, register_trusted_workspace_tools
from bridge.memory import MemoryStore
from bridge.codex_memory_stdio import LocalCodexMemory

class TrustedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home=Path(tmp.name)
        self.root=home/"airgym"
        self.root.mkdir()
        self.state=home/"bridge-state"
        self.state.mkdir(mode=0o700)
        self.dataset=self.root/"datasets"/"mid360s"
        self.dataset.mkdir(parents=True)
        self.data=self.dataset/"training.npz"
        self.data.write_bytes(b"\x00\xfe\x7f" * 100)
        (self.root/".hidden_model").write_bytes(b"model-info")
        self.secret_outside=home/"not-authorized.db3"
        self.secret_outside.write_bytes(b"outside")
        (self.root/"outside_link.db3").symlink_to(self.secret_outside)
        self.config={"state_dir":str(self.state),
                    "public_url":"http://127.0.0.1:8767",
                    "redirect_uris":[],
                    "projects":[
                        {"id":"airgym_yingjie","root":str(self.root),
                         "include":["*.py","*.md"],"allowed_modes":["read","write_test"],
                         "file_write_enabled":True,"project_authorization":True,
                         "tests":[["/usr/bin/python3","-m","unittest","discover","-v"]]},
                    ]}
        self.bridge=Bridge(self.config)
        self.ws=TrustedWorkspace(self.bridge)

    def grant(self):
        self.bridge.authorization.grant("airgym_yingjie")

    def test_one_time_project_write_grant_covers_all_extensions_without_extra_scopes(self):
        self.assertEqual(self.ws.info("airgym_yingjie")["access"],"read_only")
        with self.assertRaises(PermissionError):
            self.ws.write_small("airgym_yingjie","new_report.pcd","pcd")
        self.grant()
        self.assertEqual(self.ws.info("airgym_yingjie")["access"],"read_write")
        result=self.ws.write_small("airgym_yingjie","dataset.bin",
                                   base64.b64encode(bytes((0,255,1))).decode(),
                                   encoding="base64")
        self.assertEqual(result["status"],"applied")
        self.assertEqual((self.root/"dataset.bin").read_bytes(),bytes((0,255,1)))
        payload=self.ws.read("airgym_yingjie","dataset.bin")
        self.assertEqual(payload["encoding"],"base64")
        self.assertEqual(base64.b64decode(payload["data_base64"]),bytes((0,255,1)))
        # Hidden files are allowed within this explicitly configured project root.
        self.assertEqual(self.ws.read("airgym_yingjie",".hidden_model")["content"],"model-info")

    def test_nested_browse_no_symlink_escape_and_paging(self):
        p=self.ws.browse("airgym_yingjie")
        self.assertIn("datasets",[x["path"] for x in p["entries"]])
        self.assertIn("outside_link.db3",[x["path"] for x in p["entries"]])
        self.assertEqual(self.ws.browse("airgym_yingjie","datasets/mid360s")["total"],1)
        with self.assertRaises((OSError,ValueError)):
            self.ws.read("airgym_yingjie","outside_link.db3")
        for bad in ("/etc/passwd","../not-authorized.db3","datasets/../../outside"):
            with self.assertRaises(ValueError):
                self.ws.read("airgym_yingjie",bad)
        self.assertLessEqual(len(self.ws.browse("airgym_yingjie",limit=1)["entries"]),1)

    def test_large_stream_read_and_binary_chunked_write_without_extensions(self):
        self.grant()
        size=CHUNK*2+617
        data=(bytes(range(256))*(size//256+1))[:size]
        sha=hashlib.sha256(data).hexdigest()
        path="datasets/mid360s/new_raw_sensor.bag"
        upload=self.ws.begin("airgym_yingjie",path,len(data),sha)
        for offset in range(0,len(data),CHUNK):
            self.ws.append(upload["upload_id"],offset,
                           base64.b64encode(data[offset:offset+CHUNK]).decode())
        result=self.ws.finish(upload["upload_id"])
        self.assertEqual(result["status"],"applied")
        self.assertFalse(result["backup_saved"])
        self.assertEqual((self.root/path).read_bytes(),data)
        self.assertEqual(self.ws.fingerprint("airgym_yingjie",path)["sha256"],sha)
        sample=self.ws.read("airgym_yingjie",path,offset=CHUNK,length=100)
        self.assertEqual(base64.b64decode(sample["data_base64"]),data[CHUNK:CHUNK+100])

    def test_stale_sha_conflict_and_original_backup_preserved(self):
        self.grant()
        p=self.dataset/"params.yaml"
        p.write_text("old: true\n")
        expected=self.ws.fingerprint("airgym_yingjie","datasets/mid360s/params.yaml")["sha256"]
        saved=self.ws.write_small("airgym_yingjie","datasets/mid360s/params.yaml",
                                  "new: true\n",expected_sha256=expected)
        self.assertTrue(saved["backup_saved"])
        self.assertEqual(p.read_text(),"new: true\n")
        backups=list(self.ws.backup_dir.glob("*.bak"))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_text(),"old: true\n")
        with self.assertRaises(ValueError):
            self.ws.write_small("airgym_yingjie","datasets/mid360s/params.yaml",
                                "overwrite stale",expected_sha256=expected)
        self.assertEqual(p.read_text(),"new: true\n")

    def test_revoked_write_grant_blocks_pending_upload(self):
        self.grant()
        data=b"staged"
        h=hashlib.sha256(data).hexdigest()
        up=self.ws.begin("airgym_yingjie","staged.npy",len(data),h)
        self.ws.append(up["upload_id"],0,base64.b64encode(data).decode())
        self.bridge.authorization.revoke("airgym_yingjie")
        with self.assertRaises(PermissionError):
            self.ws.finish(up["upload_id"])
        self.assertFalse((self.root/"staged.npy").exists())
        self.assertEqual(self.ws.info("airgym_yingjie")["access"],"read_only")
        self.assertEqual(self.ws.read("airgym_yingjie","datasets/mid360s/training.npz")["encoding"],"base64")

    def test_auto_persist_change_to_shared_chatgpt_and_codex_memory(self):
        self.grant()
        result=self.ws.write_small("airgym_yingjie","experiment.csv","t,y\n1,0.7\n")
        self.assertEqual(result["status"],"applied")
        notes=MemoryStore(self.state,self.bridge.project).list_notes("airgym_yingjie",limit=10)
        self.assertTrue(any("experiment.csv" in x["title"] for x in notes))
        local=LocalCodexMemory(self.config)
        found=local.search("airgym_yingjie","experiment.csv")
        self.assertTrue(any("experiment.csv" in x["title"] for x in found["notes"]))
        self.assertEqual(self.ws.changes("airgym_yingjie")["changes"][0]["path"],"experiment.csv")

    def test_mcp_registers_all_full_folder_operations(self):
        from mcp.server.fastmcp import FastMCP
        import asyncio
        server=FastMCP("workspace-test")
        register_trusted_workspace_tools(server,self.bridge)
        names={t.name for t in asyncio.run(server.list_tools())}
        self.assertEqual(names,{
            "workspace_info","browse_workspace","read_workspace_file",
            "hash_workspace_file","write_workspace_file","begin_workspace_write",
            "append_workspace_chunk","finish_workspace_write",
            "recent_workspace_changes"
        })

if __name__=="__main__":
    unittest.main()
