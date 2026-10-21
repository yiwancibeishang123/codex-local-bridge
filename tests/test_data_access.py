"""MCP-first data permissions + large scientific data inspection.

Uses only temporary files. Verifies local-only scope activation, dynamic ACL
without restart, no symlink escape, bounded reads, and no binary rewriting.
"""
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from bridge.core import Bridge
from bridge.data_access import DataAccess


def make_npy(path):
    header=str({"descr":"<f4","fortran_order":False,"shape":(1000000,3)})+" "
    raw=header.encode("latin1")
    raw+=b" " * ((-(10+len(raw)+1))%16)+b"\n"
    with open(path,"wb") as f:
        f.write(b"\x93NUMPY\x01\x00"+struct.pack("<H",len(raw))+raw)
        f.truncate(12*1000000+len(raw)+10)


class DataAccessTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        base=Path(t.name)
        self.state=base/"state";self.state.mkdir(mode=0o700)
        self.home=base/"airgym";self.home.mkdir()
        self.other=base/"other";self.other.mkdir()
        self.dataset=self.home/"datasets";self.dataset.mkdir()
        self.nested=self.dataset/"rosbag";self.nested.mkdir()
        self.config={"state_dir":str(self.state),"projects":[
            {"id":"airgym","root":str(self.home),"include":["*.py","*.md"],
             "allowed_modes":["read","write_test"],"file_write_enabled":True,
             "project_authorization":True, "tests":[["/usr/bin/python3","-m","unittest","discover","-v"]]},
            {"id":"other","root":str(self.other),"include":["*.py"],"allowed_modes":["read"]}]}
        self.bridge=Bridge(self.config)
        self.data=DataAccess(self.bridge)
        lines=["time,altitude_m,temperature\n"]+[
            f"{i},{i/10:.1f},{20+i%6}\n" for i in range(300000)]
        (self.dataset/"flight.csv").write_text("".join(lines),encoding="utf-8")
        (self.dataset/"telemetry.jsonl").write_text(
            '{"step":1,"altitude":2.0}\n{"step":2,"altitude":3.0}\n')
        make_npy(self.dataset/"state.npy")
        (self.dataset/"map.pcd").write_bytes(
            b"# .PCD v0.7\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\n"
            b"TYPE F F F\nCOUNT 1 1 1\nWIDTH 2\nHEIGHT 1\n"
            b"POINTS 2\nDATA binary\n"+b"\x00"*24)
        (self.other/"other.csv").write_text("secret\nnot-shared\n")
        self.db=self.nested/"rosbag_0.db3"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE topics (id INTEGER,name TEXT,type TEXT,serialization_format TEXT)")
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, timestamp INTEGER, data BLOB)")
            conn.execute("INSERT INTO topics VALUES(1,'/livox/lidar','livox_ros_driver2/msg/CustomMsg','cdr')")
            conn.execute("INSERT INTO messages VALUES(1,1,123,b'abc')")
        self.db.chmod(0o600)

    def authorize(self,profiles=("arrays",),directory="datasets",access="read"):
        req=self.data.request("airgym",list(profiles),directory,access)
        self.assertEqual(req["status"],"pending_local_confirmation")
        with self.assertRaises(PermissionError):
            self.data.read_chunk("airgym","datasets/flight.csv")
        self.data.approve_local(req["request_id"])
        return req

    def test_live_approval_and_data_scope_no_restart(self):
        req=self.authorize(("arrays","logs"))
        self.assertEqual(self.data.request_status(req["request_id"])["state"],"approved")
        result=self.data.list_files("airgym","datasets")
        self.assertIn("datasets/flight.csv",[x.get("path") for x in result["entries"]])
        result=self.data.read_chunk("airgym","datasets/flight.csv",offset=100,length=512)
        self.assertEqual(result["bytes_read"],512)
        self.assertEqual(self.data.status("airgym")["scopes"][0]["directory"],"datasets")
        self.assertEqual(self.data.status("other")["scopes"],[])
        self.data.revoke_local("airgym")
        with self.assertRaises(PermissionError):
            self.data.inspect("airgym","datasets/flight.csv")

    def test_large_csv_is_bounded_and_computed_locally(self):
        self.authorize(("arrays",))
        stats=self.data.inspect("airgym","datasets/flight.csv",max_rows=2)
        self.assertGreater(stats["bytes"],1_000_000)
        self.assertEqual(stats["analysis"]["columns"],["time","altitude_m","temperature"])
        self.assertEqual(len(stats["analysis"]["sample_rows"]),2)
        self.assertGreater(stats["analysis"]["numeric_summary"]["altitude_m"]["max"],20)
        self.assertLessEqual(stats["analysis"]["sampled_count"],2000)
        self.assertTrue(stats["analysis"]["truncated"])

    def test_rosbag_sqlite_headers_pointcloud_npy_and_jsonl(self):
        self.authorize(("ros","pointcloud","arrays","logs"))
        p=self.data.inspect("airgym","datasets/state.npy")
        self.assertIn("1000000",p["analysis"]["shape"])
        self.assertEqual(p["analysis"]["dtype"],"<f4")
        p=self.data.inspect("airgym","datasets/map.pcd")
        self.assertIn("DATA binary",p["analysis"]["header"])
        p=self.data.inspect("airgym","datasets/rosbag/rosbag_0.db3")
        self.assertEqual(p["analysis"]["format"],"rosbag2_sqlite")
        self.assertEqual(p["analysis"]["metadata"]["topics"][0]["name"],"/livox/lidar")
        p=self.data.inspect("airgym","datasets/telemetry.jsonl",max_rows=2)
        self.assertEqual(p["analysis"]["sample_rows"][0]["step"],1)
        with self.assertRaises(ValueError):
            self.data.read_chunk("airgym","datasets/state.npy")

    def test_subfolder_scope_and_non_symlink_escapes(self):
        self.authorize(("ros",),"datasets/rosbag")
        p=self.data.list_files("airgym",".")
        self.assertEqual(p["entries"],[{"directory":"datasets"}])
        self.assertEqual(self.data.list_files("airgym","datasets/rosbag")["total"],1)
        with self.assertRaises(PermissionError):
            self.data.inspect("airgym","datasets/state.npy")
        (self.dataset/"link.db3").symlink_to(self.db)
        with self.assertRaises((PermissionError,OSError,ValueError)):
            self.data.inspect("airgym","datasets/link.db3")
        with self.assertRaises(ValueError):
            self.data.list_files("airgym","../")
        with self.assertRaises((ValueError,PermissionError)):
            self.data.request("airgym",["ros"],"datasets/../other")

    def test_scopes_cannot_expand_without_local_action(self):
        req=self.data.request("airgym",["ros","pointcloud"],".")
        self.assertEqual(self.data.status("airgym")["scopes"],[])
        with self.assertRaises(PermissionError):
            self.data.inspect("airgym","datasets/map.pcd")
        self.data.approve_local(req["request_id"])
        self.assertEqual(len(self.data.status("airgym")["scopes"]),1)
        with self.assertRaises(ValueError):
            self.data.approve_local(req["request_id"])
        with self.assertRaises(ValueError):
            self.data.request("airgym",["../../anything"])
        with self.assertRaises(PermissionError):
            self.data.request("other",["arrays"],access="write_review")

    def test_write_review_never_auto_applies_and_blocks_binary(self):
        self.bridge.authorization.grant("airgym")
        req=self.data.request("airgym",["arrays"],"datasets",access="write_review")
        self.data.approve_local(req["request_id"])
        original=self.data.read_chunk("airgym","datasets/flight.csv",length=16)["content"]
        with self.assertRaises(ValueError):
            self.data.request_text_write("airgym","datasets/flight.csv","bad","ABSENT")
        with self.assertRaises(PermissionError):
            self.data.request_text_write("airgym","datasets/state.npy","bad","ABSENT")
        small=self.dataset/"short.csv"
        small.write_text("x,y\n1,2\n")
        import hashlib
        expected=hashlib.sha256(small.read_bytes()).hexdigest()
        res=self.data.request_text_write("airgym","datasets/short.csv","x,y\n5,6\n",expected)
        self.assertEqual(res["status"],"pending_local_approval")
        self.assertEqual(small.read_text(),"x,y\n1,2\n")
        status=self.data.approve_text_write_local(res["proposal_id"])
        self.assertEqual(status["status"],"applied")
        self.assertEqual(small.read_text(),"x,y\n5,6\n")

    def test_http_mcp_exports_profiles_and_scopes(self):
        from bridge.data_access import register_data_tools
        from mcp.server.fastmcp import FastMCP
        import asyncio
        app=FastMCP("test-data")
        register_data_tools(app,self.bridge)
        names={t.name for t in asyncio.run(app.list_tools())}
        self.assertEqual(names,{
            "get_project_data_access", "list_data_profiles",
            "request_data_access", "get_data_access_request",
            "list_project_data_files", "inspect_project_data",
            "read_project_data_chunk", "request_data_text_write"})

if __name__=="__main__":
    unittest.main()
