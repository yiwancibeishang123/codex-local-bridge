"""Live MCP research-data scopes: locally approved, dynamically checked, no restart.

Project code include patterns remain unchanged. Data scopes have separate root-
inode anchoring, allowed subdirectory, fixed format profile and read/write-review
level. Approval/revocation never travels through the remote OAuth MCP tools.
"""
from __future__ import annotations

import ast
import csv
import fnmatch
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import struct
import time
import zipfile

from .filetools import (_check_path, _safe_open_parent, _sha, _redact,
                        WorkspaceBridge, ProjectPolicy, WorkspaceError)

# No executable formats, archive unpacking, deserialization or arbitrary SQL.
PROFILES = {
    "ros": (".db3", ".bag", ".mcap"),
    "pointcloud": (".pcd", ".ply", ".las", ".laz", ".bin"),
    "arrays": (".npy", ".npz", ".csv", ".tsv", ".parquet", ".feather", ".h5", ".hdf5"),
    "logs": (".jsonl", ".log", ".txt", ".csv", ".tsv", ".json"),
    "images": (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"),
    "documents": (".pdf", ".docx", ".xlsx", ".pptx"),
    "models": (".pt", ".pth", ".onnx", ".safetensors", ".ckpt"),
    "configuration": (".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg"),
}
TEXT_FORMATS = {".csv", ".tsv", ".jsonl", ".json", ".txt", ".log", ".yaml", ".yml", ".ini", ".toml", ".xml", ".cfg"}
WRITE_TEXT_FORMATS = {".csv", ".tsv", ".jsonl", ".json", ".txt", ".log"}
MAX_PREVIEW = 16_384
MAX_SAMPLE_ROWS = 2000
MAX_ANALYSIS_BYTES = 4 * 1024 * 1024
MAX_BYTES_FOR_WRITE = 128 * 1024
REQUEST_TTL = 3600
_CUSTOM_EXTENSION = re.compile(r"^\.[a-z0-9][a-z0-9._+-]{0,15}$")
_DENIED_CUSTOM = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".sh",
                  ".bat", ".cmd", ".exe", ".dll", ".so", ".a", ".o",
                  ".jar", ".class", ".desktop", ".service", ".pem", ".key",
                  ".p12", ".pfx", ".crt", ".der", ".env", ".kdbx", ".gpg",
                  ".asc", ".sql", ".db", ".sqlite", ".sqlite3"}

_SENSITIVE_FIELD = re.compile(r"(?i)(?:password|api[_-]?key|token|secret|credential|authorization|cookie)")


def _types(profiles):
    return {suffix for p in profiles for suffix in PROFILES[p]}


def _root_identity(root: Path):
    s = root.stat()
    return (s.st_dev, s.st_ino)


def _validate_dir(root, relative_dir):
    bits = _check_path(relative_dir, allow_root=True)
    fd = _safe_open_parent(root, bits + ("__bridge_placeholder__",))
    return bits, fd


def _plain(value, limit=100):
    if value is None or type(value) in (int, float, bool):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary: {len(value)} bytes>"
    if isinstance(value, str):
        clean, _ = _redact(value[:min(limit,4096)])
        return clean
    return "<structured value omitted>"


class DataAccess:
    def __init__(self, bridge):
        self.bridge = bridge
        self.store = bridge.store

    def _project(self, project_id):
        return self.bridge.project(project_id)

    def profiles(self, project_id):
        self._project(project_id)
        return {"project_id": project_id, "profiles": [
            {"id": profile, "extensions": list(ext), "write_review_supported":
             any(x in WRITE_TEXT_FORMATS for x in ext)}
            for profile, ext in PROFILES.items()],
            "notice": "Named format profiles or owner-approved custom extensions are supported. Large data stays local; binary modification is unavailable."}

    def _approved(self, project_id):
        project = self._project(project_id)
        saved = self.store.get("data_scope", project_id) or {}
        if tuple(saved.get("root_identity", ())) != _root_identity(Path(project["root"])):
            return []
        scopes = saved.get("scopes", [])
        return [entry for entry in scopes if entry.get("directory") is not None]

    def status(self, project_id):
        p = self._project(project_id)
        scopes = self._approved(project_id)
        return {"project_id": project_id, "root": "<local project root>",
                "scopes": scopes, "project_write_granted": bool(self.bridge.authorization.active(project_id)),
                "code_include": p.get("include",[]),
                "notice": "New data scope approvals take effect immediately without reloading the desktop app or MCP."}

    def request(self, project_id, profiles, directory=".", access="read", custom_extensions=None):
        p = self._project(project_id)
        custom = [] if custom_extensions is None else custom_extensions
        if (not isinstance(custom,list) or len(custom)>16
                or any(type(x) is not str or
                       not _CUSTOM_EXTENSION.fullmatch(x) or x in _DENIED_CUSTOM
                       for x in custom) or len(set(custom))!=len(custom)):
            raise ValueError("Custom extensions must be safe lowercase data suffixes, e.g. .dat")
        if (not isinstance(profiles, list) or len(profiles)>8
                or any(type(x) is not str or x not in PROFILES for x in profiles)
                or len(set(profiles)) != len(profiles)
                or not (profiles or custom)):
            raise ValueError("Choose one or more named data profiles or custom data extensions")
        if access not in ("read", "write_review"):
            raise ValueError("Access must be read or write_review")
        if access == "write_review":
            if not self.bridge.authorization.active(project_id):
                raise PermissionError("Write-review requires an existing locally authorized writable project")
            if not ((_types(profiles) | set(custom)) & WRITE_TEXT_FORMATS):
                raise ValueError("This scope contains no writable text formats")
        _, fd = _validate_dir(Path(p["root"]), directory)
        os.close(fd)
        request_id = secrets.token_hex(16)
        req = {"id":request_id, "project_id":project_id, "profiles":profiles,
               "custom_extensions":custom, "directory":directory, "access":access,
               "root_identity":list(_root_identity(Path(p["root"]))),
               "created_at":time.time(), "state":"pending"}
        self.store.put("data_access_request", request_id, req)
        self.store.audit("data_scope_requested",{"project":project_id, "id":request_id,
                                                "profile_count":len(profiles), "access":access})
        return {"request_id":request_id, "status":"pending_local_confirmation",
                "project_id":project_id, "directory":directory, "profiles":profiles,
                "custom_extensions":custom, "access":access, "local_action": "codex-local-bridge approve-data-scope "+request_id,
                "notice":"A trusted local owner must confirm before this scope takes effect."}

    def request_status(self, request_id):
        if not re.fullmatch(r"[0-9a-f]{32}",request_id):
            raise ValueError("Invalid request ID")
        q = self.store.get("data_access_request",request_id)
        if not q: raise ValueError("Unknown request")
        self._project(q["project_id"])
        return {k:q[k] for k in ("id","project_id","directory","profiles",
                                 "custom_extensions","access","state")}

    def approve_local(self, request_id):
        if not re.fullmatch(r"[0-9a-f]{32}",request_id):
            raise ValueError("Invalid request ID")
        with self.bridge.authorization.locked():
            req=self.store.get("data_access_request",request_id)
            if not req or req["state"]!="pending" or time.time()-req["created_at"]>REQUEST_TTL:
                raise ValueError("Missing, expired or already processed request")
            p=self._project(req["project_id"])
            root=Path(p["root"])
            if list(_root_identity(root)) != req["root_identity"]:
                raise PermissionError("Project root changed since request")
            _, fd = _validate_dir(root,req["directory"])
            os.close(fd)
            if req["access"]=="write_review" and not self.bridge.authorization.active(p["id"]):
                raise PermissionError("Project write grant is not active")
            current=self.store.get("data_scope",p["id"]) or {}
            same = [e for e in current.get("scopes",[])
                    if e["directory"]==req["directory"] and e["access"]==req["access"]]
            profiles = sorted(set(req["profiles"]).union(
                *(set(e["profiles"]) for e in same)))
            custom = sorted(set(req["custom_extensions"]).union(
                *(set(e.get("custom_extensions",[])) for e in same)))
            scopes=[e for e in current.get("scopes",[])
                    if not (e["directory"]==req["directory"] and e["access"]==req["access"])]
            scopes.append({"directory":req["directory"],"profiles":profiles,
                           "custom_extensions":custom, "access":req["access"],
                           "authorized_at":int(time.time())})
            self.store.put("data_scope",p["id"],{"root_identity":req["root_identity"],"scopes":scopes})
            self.store.update("data_access_request",request_id,state="approved")
            self.store.audit("data_scope_approved",{"project":p["id"],"request":request_id})
            return self.status(p["id"])

    def revoke_local(self, project_id):
        self._project(project_id)
        with self.bridge.authorization.locked():
            p=self._project(project_id)
            self.store.put("data_scope",project_id,
                           {"root_identity":list(_root_identity(Path(p["root"]))),"scopes":[]})
            self.store.audit("data_scope_revoked",{"project":project_id})
        return self.status(project_id)

    def _authorized(self, project_id, rel, *, write=False):
        p=self._project(project_id)
        bits=_check_path(rel)
        suffix=Path(bits[-1]).suffix.lower()
        # The owner has already granted this exact project automatic write.
        # Do not force a second permission workflow just to inspect its data.
        # Data writes still require their normal separate review profile.
        if not write and self.bridge.authorization.active(project_id):
            return p,bits,suffix
        for scope in self._approved(project_id):
            base=scope["directory"]
            if base != "." and not rel.startswith(base+"/"):
                continue
            if suffix in (_types(scope["profiles"]) | set(scope.get("custom_extensions",[]))):
                if write and (scope["access"]!="write_review"
                              or suffix not in WRITE_TEXT_FORMATS
                              or not self.bridge.authorization.active(project_id)):
                    continue
                return p, bits, suffix
        raise PermissionError("Data access requires a local owner-approved profile for this directory and format")

    def _file(self, project_id, rel, *, write=False):
        p, bits, ext = self._authorized(project_id, rel, write=write)
        fd = _safe_open_parent(Path(p["root"]),bits)
        try:
            file_fd=os.open(bits[-1],os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        finally:
            os.close(fd)
        try:
            st=os.fstat(file_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:
                raise PermissionError("Only ordinary unlinked project files are eligible")
            if st.st_size>1000*1024**3:
                raise ValueError("Data file exceeds 1TB bound")
            return file_fd,st,ext
        except Exception:
            os.close(file_fd)
            raise

    def list_files(self, project_id, relative_dir=".", offset=0, limit=100):
        p=self._project(project_id)
        if type(offset)!=int or offset<0 or offset>100000 or type(limit)!=int or not 1<=limit<=200:
            raise ValueError("Invalid directory pagination")
        bits,fd=_validate_dir(Path(p["root"]),relative_dir)
        trusted=bool(self.bridge.authorization.active(project_id))
        try:
            items=sorted(os.listdir(fd))
            if len(items)>100000:
                raise ValueError("Directory has too many entries; select a narrower subdirectory")
            entries=[]
            total=0
            for name in items:
                try:
                    rel="/".join(bits+(name,))
                    _check_path(rel)
                    st=os.stat(name,dir_fd=fd,follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        # Reveal subdirectory names only within explicitly approved parent trees.
                        if not trusted and not any(sc["directory"]=="." or rel.startswith(sc["directory"]+"/")
                                   or sc["directory"].startswith(rel+"/") or sc["directory"]==rel
                                   for sc in self._approved(project_id)):
                            continue
                        item={"directory":rel}
                    elif stat.S_ISREG(st.st_mode) and st.st_nlink==1:
                        try: self._authorized(project_id,rel)
                        except (ValueError,PermissionError): continue
                        item={"path":rel,"bytes":st.st_size,"extension":Path(name).suffix.lower()}
                    else:continue
                    if total>=offset and len(entries)<limit:
                        entries.append(item)
                    total+=1
                except (OSError,ValueError):
                    continue
            return {"project_id":project_id,"directory":relative_dir,
                    "entries":entries,"total":total,
                    "next_offset":offset+len(entries) if offset+len(entries)<total else None}
        finally:
            os.close(fd)

    def read_chunk(self,project_id,relative_path,offset=0,length=8192):
        if type(offset)!=int or offset<0 or type(length)!=int or not 1<=length<=MAX_PREVIEW:
            raise ValueError("Bad read offset/length")
        fd,st,ext=self._file(project_id,relative_path)
        try:
            known_binary = set().union(*map(set, PROFILES.values())) - TEXT_FORMATS
            if ext in known_binary:
                raise ValueError("Binary/scientific data cannot be dumped as text; use inspect_project_data")
            if offset>=st.st_size:
                return {"path":relative_path,"offset":offset,"bytes_read":0,"content":"","eof":True}
            os.lseek(fd,offset,os.SEEK_SET)
            data=os.read(fd,length)
            if b"\x00" in data:
                raise ValueError("Selected custom extension contains binary data; use inspect_project_data")
            # Reject content that is not UTF-8 for a custom suffix. Non-zero
            # offsets must be placed at UTF-8 character boundaries.
            if ext not in TEXT_FORMATS:
                data.decode("utf-8",errors="strict")
            # A credentials pattern may cross the requested chunk boundary.
            # Reject the entire chunk rather than returning partial secrets.
            os.lseek(fd,max(0,offset-512),os.SEEK_SET)
            guard=os.read(fd,min(st.st_size-max(0,offset-512),length+1024))
            if _redact(guard.decode("utf-8",errors="replace"))[1]:
                return {"project_id":project_id,"path":relative_path,
                        "offset":offset,"bytes_read":len(data),"size":st.st_size,
                        "content":"[REDACTED_SENSITIVE_CHUNK]","redacted":True,
                        "eof":offset+len(data)>=st.st_size}
            content,redacted=_redact(data.decode("utf-8",errors="replace"))
            return {"project_id":project_id,"path":relative_path,"offset":offset,
                    "bytes_read":len(data),"size":st.st_size,"content":content,
                    "redacted":redacted,"eof":offset+len(data)>=st.st_size}
        finally:os.close(fd)

    @staticmethod
    def _preview(fd,ext,st,max_rows):
        # Only the bounded first 4MB is analyzed, never a massive data transfer.
        os.lseek(fd,0,os.SEEK_SET)
        sample=os.read(fd,min(st.st_size,MAX_ANALYSIS_BYTES))
        if ext in (".csv",".tsv"):
            delimiter="\t" if ext==".tsv" else ","
            text=sample.decode("utf-8",errors="replace")
            reader=csv.reader(io.StringIO(text),delimiter=delimiter)
            head=next(reader,[])
            if len(head)>128:
                head=head[:128]
            counts=[0]*len(head);total=[0.]*len(head);lowest=[float("inf")]*len(head);highest=[float("-inf")]*len(head)
            output=[];scanned=0
            for row in reader:
                if scanned>=MAX_SAMPLE_ROWS:break
                scanned+=1
                if len(output)<max_rows:
                    output.append([
                        "[REDACTED_FIELD]" if _SENSITIVE_FIELD.search(head[i])
                        else _plain(cell,160)
                        for i,cell in enumerate(row[:len(head)])])
                for i,cell in enumerate(row[:len(head)]):
                    if _SENSITIVE_FIELD.search(head[i]):continue
                    try:value=float(cell)
                    except ValueError:continue
                    if not math.isfinite(value):continue
                    counts[i]+=1;total[i]+=value
                    lowest[i]=min(lowest[i],value);highest[i]=max(highest[i],value)
            stats={head[i]:{"count":counts[i],"min":lowest[i],"max":highest[i],
                             "mean":total[i]/counts[i]} for i in range(len(head)) if counts[i]}
            return {"format":"delimited","columns":[_plain(h,160) for h in head],
                    "sample_rows":output,"sampled_count":scanned,"numeric_summary":stats,
                    "truncated":len(sample)<st.st_size or scanned>=MAX_SAMPLE_ROWS}
        if ext==".jsonl":
            lines=sample.decode("utf-8",errors="replace").splitlines()[:MAX_SAMPLE_ROWS]
            rows=[];keys=set()
            for line in lines:
                try:
                    item=json.loads(line)
                    if isinstance(item,dict):
                        keys.update(str(k)[:100] for k in list(item)[:64])
                        if len(rows)<max_rows:
                            rows.append({str(k)[:100]:(
                                "[REDACTED_FIELD]" if _SENSITIVE_FIELD.search(str(k))
                                else _plain(v,160)) for k,v in list(item.items())[:32]})
                except (ValueError,TypeError):continue
            return {"format":"jsonl","fields":sorted(keys)[:128],"sample_rows":rows,
                    "sampled_count":len(lines),"truncated":len(sample)<st.st_size}
        if ext in (".pcd",".ply"):
            # Read only ASCII header, not point array bytes.
            marker=b"DATA " if ext==".pcd" else b"end_header"
            header=sample[:min(len(sample),65536)].splitlines(keepends=True)
            lines=[];found=False
            for line in header[:200]:
                if b"\x00" in line or len(line)>512:break
                lines.append(_plain(line.decode("utf-8",errors="replace").strip(),300))
                if line.lstrip().startswith(marker):
                    found=True;break
            return {"format":"pointcloud_header","header":lines,"complete_header":found}
        if ext==".npy":
            if not sample.startswith(b"\x93NUMPY") or len(sample)<12:
                return {"format":"npy","valid_header":False}
            major=sample[6]
            offset=10 if major==1 else 12
            length=int.from_bytes(sample[8:10] if major==1 else sample[8:12],"little")
            if length>65536 or offset+length>len(sample):
                return {"format":"npy","valid_header":False}
            try:
                head=ast.literal_eval(sample[offset:offset+length].decode("latin-1").strip())
                if not isinstance(head,dict):raise ValueError()
                return {"format":"npy","shape":str(head.get("shape"))[:200],
                        "dtype":str(head.get("descr"))[:160],
                        "fortran_order":bool(head.get("fortran_order"))}
            except (ValueError,TypeError,MemoryError,SyntaxError,RecursionError):
                return {"format":"npy","valid_header":False}
        if ext==".npz":
            # No decompression or pickle loading. Only ZIP central directory.
            with os.fdopen(os.dup(fd),"rb") as fh:
                with zipfile.ZipFile(fh) as archive:
                    items=archive.infolist()[:80]
                    return {"format":"npz","arrays":[{"name":_plain(x.filename,140),
                        "uncompressed_bytes":x.file_size,"compressed_bytes":x.compress_size}
                        for x in items],"entry_count":len(archive.infolist())}
        return {"format":ext.lstrip("."),"head_bytes_hex":sample[:32].hex(),
                "note":"Only metadata/headers are inspected; no unsafe deserialization."}

    def inspect(self,project_id,relative_path,max_rows=8):
        if type(max_rows)!=int or not 0<=max_rows<=20:
            raise ValueError("max_rows must be 0..20")
        fd,st,ext=self._file(project_id,relative_path)
        try:
            result={"project_id":project_id,"path":relative_path,
                    "bytes":st.st_size,"extension":ext,
                    "analyzed_bytes_max":MAX_ANALYSIS_BYTES,
                    "mtime_ns":st.st_mtime_ns}
            if ext==".db3":
                # SQLite is opened via verified FD, immutable read-only and no SQL supplied by user.
                sig=os.pread(fd,16,0)
                if sig!=b"SQLite format 3\x00":
                    raise ValueError("This .db3 is not an SQLite3 database")
                uri="file:/proc/self/fd/"+str(fd)+"?mode=ro&immutable=1"
                with sqlite3.connect(uri,uri=True,timeout=3) as db:
                    db.execute("PRAGMA query_only=ON")
                    names=[str(x[0]) for x in db.execute(
                      "SELECT name FROM sqlite_master WHERE type='table' "
                      "AND name NOT LIKE 'sqlite_%' LIMIT 50")]
                    info={"tables":names}
                    if "topics" in names:
                        info["topics"]=[{"id":_plain(row[0]),"name":_plain(row[1],150),
                            "type":_plain(row[2],150)} for row in db.execute(
                            "SELECT id,name,type FROM topics LIMIT 64")]
                    if "messages" in names:
                        info["first_messages"]=[{"timestamp":r[0],"topic_id":r[1]}
                          for r in db.execute("SELECT timestamp,topic_id FROM messages LIMIT 12")]
                    result["analysis"]={"format":"rosbag2_sqlite","metadata":info}
            else:
                result["analysis"]=self._preview(fd,ext,st,max_rows)
            return result
        finally:os.close(fd)

    def request_text_write(self,project_id,relative_path,new_content,expected_sha256):
        p,bits,ext=self._authorized(project_id,relative_path,write=True)
        if not isinstance(new_content,str) or "\x00" in new_content:
            raise ValueError("Only UTF-8 text data can be proposed for review")
        if len(new_content.encode("utf-8"))>MAX_BYTES_FOR_WRITE:
            raise ValueError("Text data edit exceeds 128 KiB; use local data processing")
        # Explicit approval always required: no automatic overwriting of data.
        ws=WorkspaceBridge({project_id:ProjectPolicy(Path(p["root"]),(relative_path,),True)},
                           self.store.directory/"filetools",authorization=None)
        return ws.request_write_file(project_id,relative_path,new_content,expected_sha256)

    def approve_text_write_local(self,proposal_id):
        if not re.fullmatch(r"[0-9a-f]{32}",proposal_id):
            raise ValueError("Invalid proposal")
        # The original full proposal record is in the existing workspace audit DB.
        with sqlite3.connect(self.store.directory/"filetools/workspace.sqlite3",timeout=10) as db:
            row=db.execute("SELECT project_id,path FROM proposals WHERE id=?",(proposal_id,)).fetchone()
        if not row:raise ValueError("Unknown proposal")
        project_id,path=row
        p,_,_=self._authorized(project_id,path,write=True)
        ws=WorkspaceBridge({project_id:ProjectPolicy(Path(p["root"]),(path,),True)},
                           self.store.directory/"filetools",authorization=None)
        return ws.approve_and_apply(proposal_id)


def register_data_tools(mcp, bridge):
    from mcp.types import ToolAnnotations
    api=DataAccess(bridge)
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
    write=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=False)

    @mcp.tool(annotations=read)
    def get_project_data_access(project_id:str)->dict:
        """Show live approved formats and directory scopes (no desktop refresh or MCP restart)."""
        return api.status(project_id)

    @mcp.tool(annotations=read)
    def list_data_profiles(project_id:str)->dict:
        """Discover safe scientific data formats available for local owner approval."""
        return api.profiles(project_id)

    @mcp.tool(annotations=write)
    def request_data_access(project_id:str,profiles:list[str],directory:str=".",access:str="read",custom_extensions:list[str]|None=None)->dict:
        """Request format presets or custom safe extensions for a directory; local owner approves, live without restart."""
        return api.request(project_id,profiles,directory,access,custom_extensions)

    @mcp.tool(annotations=read)
    def get_data_access_request(request_id:str)->dict:
        """Check whether the local owner approved a data access request."""
        return api.request_status(request_id)

    @mcp.tool(annotations=read)
    def list_project_data_files(project_id:str,relative_dir:str=".",offset:int=0,limit:int=100)->dict:
        """List locally approved data files, including big binary formats and nested directories, with pagination."""
        return api.list_files(project_id,relative_dir,offset,limit)

    @mcp.tool(annotations=read)
    def inspect_project_data(project_id:str,relative_path:str,max_rows:int=8)->dict:
        """Inspect large file metadata and bounded local CSV, JSONL, NPY, PCD and ROSbag DB3 samples; never load models."""
        return api.inspect(project_id,relative_path,max_rows)

    @mcp.tool(annotations=read)
    def read_project_data_chunk(project_id:str,relative_path:str,offset:int=0,length:int=8192)->dict:
        """Read a bounded UTF-8 text-data slice; binary data requires specialized inspection."""
        return api.read_chunk(project_id,relative_path,offset,length)

    @mcp.tool(annotations=write)
    def request_data_text_write(project_id:str,relative_path:str,new_content:str,expected_sha256:str)->dict:
        """Propose changing small UTF-8 data; ALWAYS requires separate local approval; binary writes are not supported."""
        return api.request_text_write(project_id,relative_path,new_content,expected_sha256)

    return api
