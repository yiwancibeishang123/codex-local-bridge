"""Trusted local project workspaces for ChatGPT web and Codex.

The local owner has already selected each project's root in the private Bridge
config. A current local project grant enables direct write for every *regular*
file in that project. No extension allowlists or individual approval prompts.

Stream large files in bounded chunks; never put entire binary data into model
context. Paths are project-relative and traversed using Linux no-follow FDs.
Writes retain an owner-private backup and use atomic replace/checksums.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import stat
import threading
import time
from .memory import MemoryStore

CHUNK = 48 * 1024
MAX_ENTRIES = 300
MAX_TRANSFER_BYTES = 16 * 1024**3
RESERVED = {".bridge-upload", ".bridge-write"}
_local_lock = threading.RLock()


def parts_of(path, allow_root=False):
    if path == "." and allow_root:
        return ()
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError("Provide a project-relative POSIX path")
    bits = path.split("/")
    if len(bits)>64 or len(path.encode("utf-8"))>4000 or any(
            x in ("",".","..") or x.startswith(".bridge-upload-") or
            x.startswith(".bridge-write-") for x in bits):
        raise ValueError("Unsafe project-relative path")
    return tuple(bits)


def project_fd(root, bits):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in bits:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                            os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


def regular_file(dirfd, name, missing=False):
    try:
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=dirfd)
    except FileNotFoundError:
        if missing:return None,None
        raise
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1:
            raise ValueError("Only regular, non-hardlinked files inside project root")
        return fd,s
    except Exception:
        os.close(fd)
        raise


def sha_file(fd):
    digest=hashlib.sha256()
    os.lseek(fd,0,os.SEEK_SET)
    while True:
        buf=os.read(fd,1024*1024)
        if not buf:break
        digest.update(buf)
    return digest.hexdigest()


def decode_blob(encoded):
    if not isinstance(encoded,str) or len(encoded)>((CHUNK+2)//3)*4+8:
        raise ValueError("Chunk must be base64, max 48 KiB")
    try:
        raw=base64.b64decode(encoded,validate=True)
    except (ValueError,binascii.Error):
        raise ValueError("Invalid base64 chunk") from None
    if len(raw)>CHUNK:raise ValueError("Chunk too large")
    return raw


class TrustedWorkspace:
    def __init__(self,bridge):
        self.bridge=bridge
        self.store=bridge.store
        self.upload_dir=self.store.directory/"trusted-workspace-staging"
        self.backup_dir=self.store.directory/"trusted-workspace-backups"

    def _project(self,pid,write=False):
        p=self.bridge.project(pid)
        if write and (p.get("file_write_enabled") is not True or
                      self.bridge.authorization.active(pid) is None):
            raise PermissionError("Project is read-only; enable project read/write once locally")
        return p

    def _at(self,pid,path,write=False):
        p=self._project(pid,write=write)
        bits=parts_of(path)
        fd=project_fd(p["root"],bits[:-1])
        return p,fd,bits[-1]

    def info(self,pid):
        p=self._project(pid)
        return {"project_id":pid,"access":"read_write" if
                p.get("file_write_enabled") is True and self.bridge.authorization.active(pid)
                else "read_only", "coverage":"all regular files below registered project root",
                "no_extension_filter":True,"direct_write":True,
                "binary_and_large_files":"48 KiB chunks; all bytes remain local until requested",
                "shared_memory":"studio.sqlite3 for ChatGPT MCP and local Codex memory MCP",
                "note":"No repeated file-level permission requests. Symlink escapes and unsafe non-regular files remain unsupported."}

    def browse(self,pid,relative_dir=".",offset=0,limit=100):
        p=self._project(pid)
        if type(offset)!=int or offset<0 or offset>5_000_000 or type(limit)!=int or not 1<=limit<=MAX_ENTRIES:
            raise ValueError("Invalid directory page")
        bits=parts_of(relative_dir,True)
        fd=project_fd(p["root"],bits)
        try:
            entries=[]
            names=sorted(os.listdir(fd))
            if len(names)>1_000_000:raise ValueError("Select a narrower directory")
            available=0
            for name in names:
                if name in (".","..") or name.startswith(".bridge-write-") or name.startswith(".bridge-upload-"):
                    continue
                try:
                    st=os.stat(name,dir_fd=fd,follow_symlinks=False)
                except OSError:continue
                if stat.S_ISDIR(st.st_mode):kind="directory"
                elif stat.S_ISREG(st.st_mode) and st.st_nlink==1:kind="file"
                elif stat.S_ISLNK(st.st_mode):kind="symlink"
                else:continue
                if available>=offset and len(entries)<limit:
                    item={"path":"/".join(bits+(name,)),"type":kind}
                    if kind=="file":item["bytes"]=st.st_size
                    entries.append(item)
                available+=1
            return {"project_id":pid,"directory":relative_dir,
                    "entries":entries,"total":available,
                    "next_offset":offset+len(entries) if offset+len(entries)<available else None}
        finally:os.close(fd)

    def read(self,pid,relative_path,offset=0,length=CHUNK,as_text="auto"):
        if type(offset)!=int or not 0<=offset<=MAX_TRANSFER_BYTES or (
            type(length)!=int or not 1<=length<=CHUNK):
            raise ValueError("Invalid offset/length")
        if as_text not in ("auto","text","base64"):
            raise ValueError("as_text must be auto, text or base64")
        _,dirfd,name=self._at(pid,relative_path)
        try:fd,st=regular_file(dirfd,name)
        finally:os.close(dirfd)
        try:
            if st.st_size>MAX_TRANSFER_BYTES:raise ValueError("File over maximum streaming size")
            os.lseek(fd,offset,os.SEEK_SET)
            data=os.read(fd,length)
            result={"project_id":pid,"path":relative_path,"offset":offset,
                    "bytes_read":len(data),"file_size":st.st_size,
                    "mtime_ns":st.st_mtime_ns,"eof":offset+len(data)>=st.st_size}
            try:
                # Don't corrupt UTF-8 by silently replacing truncated multi-byte
                # sequences: caller can move offset or use base64.
                text=data.decode("utf-8")
                if "\x00" in text:raise UnicodeError("binary")
            except UnicodeError:
                if as_text=="text":
                    raise ValueError("Chunk is not valid UTF-8; use base64 mode")
                text=None
            if as_text=="base64" or text is None:
                result["encoding"]="base64"
                result["data_base64"]=base64.b64encode(data).decode("ascii")
            else:
                result["encoding"]="utf-8"
                result["content"]=text
            return result
        finally:os.close(fd)

    def fingerprint(self,pid,relative_path):
        _,dirfd,name=self._at(pid,relative_path)
        try:fd,st=regular_file(dirfd,name)
        finally:os.close(dirfd)
        try:
            return {"project_id":pid,"path":relative_path,
                    "size":st.st_size,"sha256":sha_file(fd),"mtime_ns":st.st_mtime_ns}
        finally:os.close(fd)

    def _private_dirs(self):
        for p in (self.upload_dir,self.backup_dir):
            if p.is_symlink():raise PermissionError("Unsafe Bridge private directory")
            p.mkdir(parents=True,mode=0o700,exist_ok=True)
            st=p.stat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700:
                raise PermissionError("Bridge private directory must be 0700 owner-only")

    def begin(self,pid,relative_path,total_bytes,sha256,expected_sha256=None):
        self._project(pid,write=True)
        parts_of(relative_path)
        if type(total_bytes)!=int or not 0<=total_bytes<=MAX_TRANSFER_BYTES:
            raise ValueError("Invalid target file size")
        if (not isinstance(sha256,str) or len(sha256)!=64 or
                any(c not in "0123456789abcdef" for c in sha256)):
            raise ValueError("Upload SHA-256 must be lowercase hex")
        if expected_sha256 is not None and (expected_sha256!="ABSENT" and
                (not isinstance(expected_sha256,str) or len(expected_sha256)!=64 or
                 any(c not in "0123456789abcdef" for c in expected_sha256))):
            raise ValueError("Invalid previous SHA-256")
        self._private_dirs()
        _,dirfd,name=self._at(pid,relative_path,write=True)
        try:
            old,st=regular_file(dirfd,name,missing=True)
            if old is not None:
                try: observed=sha_file(old)
                finally:os.close(old)
            else:observed="ABSENT"
        finally:os.close(dirfd)
        if expected_sha256 is not None and observed!=expected_sha256:
            raise ValueError("Target changed; read its SHA-256 again")
        tid=secrets.token_hex(16)
        f=os.open(self.upload_dir/(tid+".part"),os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        os.close(f)
        item={"id":tid,"project_id":pid,"path":relative_path,
              "total_bytes":total_bytes,"sha256":sha256,"prior_sha256":observed,
              "received_bytes":0,"created_at":time.time(),"state":"uploading"}
        self.store.put("trusted_upload",tid,item)
        return {"upload_id":tid,"received_bytes":0,"total_bytes":total_bytes,
                "next_step":"append_workspace_chunk then finish_workspace_write"}

    def append(self,upload_id,offset,data_base64):
        data=decode_blob(data_base64)
        with _local_lock:
            item=self._upload(upload_id)
            if item["state"]!="uploading":raise ValueError("Upload already finished")
            self._project(item["project_id"],write=True)
            if type(offset)!=int or offset!=item["received_bytes"] or offset+len(data)>item["total_bytes"]:
                raise ValueError("Chunk must match next expected offset; no gaps or overlaps")
            path=self.upload_dir/(upload_id+".part")
            fd=os.open(path,os.O_WRONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_APPEND)
            try:
                if os.fstat(fd).st_size!=offset:raise ValueError("Staging file changed")
                os.write(fd,data);os.fsync(fd)
            finally:os.close(fd)
            item=self.store.update("trusted_upload",upload_id,received_bytes=offset+len(data))
            return {"upload_id":upload_id,"received_bytes":item["received_bytes"],
                    "total_bytes":item["total_bytes"]}

    def _upload(self,uid):
        if not isinstance(uid,str) or len(uid)!=32 or any(c not in "0123456789abcdef" for c in uid):
            raise ValueError("Invalid upload ID")
        x=self.store.get("trusted_upload",uid)
        if x is None or time.time()-x["created_at"]>86400:
            raise ValueError("Unknown or expired upload")
        return x

    def finish(self,upload_id):
        with _local_lock:
            item=self._upload(upload_id)
            self._project(item["project_id"],write=True)
            if item["state"]!="uploading" or item["received_bytes"]!=item["total_bytes"]:
                raise ValueError("Incomplete upload")
            staging=self.upload_dir/(upload_id+".part")
            fd=os.open(staging,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:
                if os.fstat(fd).st_size!=item["total_bytes"] or sha_file(fd)!=item["sha256"]:
                    raise ValueError("Upload checksum mismatch")
            finally:os.close(fd)
            # Existing local project grant is held during atomic write; revoke
            # returns only after all in-progress writes complete.
            with self.bridge.authorization.locked():
                self._project(item["project_id"],write=True)
                p,dirfd,name=self._at(item["project_id"],item["path"],write=True)
                try:
                    old,info=regular_file(dirfd,name,missing=True)
                    if old is not None:
                        try:now=sha_file(old)
                        finally:os.close(old)
                    else:now="ABSENT"
                    if now!=item["prior_sha256"]:raise ValueError("File changed during upload; try again")
                    if shutil.disk_usage(p["root"]).free < item["total_bytes"]+(info.st_size if info else 0)+8*1024**2:
                        raise OSError("Insufficient disk space for safe backup")
                    backup=None
                    if info:
                        self._private_dirs()
                        backup=self.backup_dir/(upload_id+".bak")
                        source,_=regular_file(dirfd,name)
                        try:
                            bfd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
                            try:
                                while True:
                                    b=os.read(source,1024*1024)
                                    if not b:break
                                    os.write(bfd,b)
                                os.fsync(bfd)
                            finally:os.close(bfd)
                        finally:os.close(source)
                    tmp=".bridge-write-"+secrets.token_hex(10)
                    wf=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=dirfd)
                    try:
                        with open(staging,"rb") as source:
                            while True:
                                b=source.read(1024*1024)
                                if not b:break
                                os.write(wf,b)
                        os.fsync(wf)
                        os.fchmod(wf,stat.S_IMODE(info.st_mode)&0o755 if info else 0o600)
                        os.replace(tmp,name,src_dir_fd=dirfd,dst_dir_fd=dirfd)
                        os.fsync(dirfd)
                    finally:
                        os.close(wf)
                        try:os.unlink(tmp,dir_fd=dirfd)
                        except FileNotFoundError:pass
                finally:os.close(dirfd)
                self.store.update("trusted_upload",upload_id,state="applied")
                self.store.audit("trusted_workspace_file_written",{"project":item["project_id"],
                     "path":item["path"],"sha256":item["sha256"],"bytes":item["total_bytes"]})
                self.store.put("trusted_change",upload_id,{
                    "id":upload_id,"project_id":item["project_id"],"path":item["path"],
                    "sha256":item["sha256"],"previous_sha256":now,
                    "bytes":item["total_bytes"],"at":time.time()})
                if self.bridge.project(item["project_id"]).get("auto_memory",True) is not False:
                    try:
                        m=MemoryStore(self.store.directory,self.bridge.project)
                        m.upsert_note(item["project_id"],
                            title="Workspace changed: "+item["path"][-250:],
                            body="File updated through ChatGPT web MCP. Path: "+
                                 item["path"][:1800]+"\nSHA-256: "+item["sha256"]+
                                 "\nBytes: "+str(item["total_bytes"])+
                                 "\nReview the current file before further edits.",
                            kind="task",origin="chatgpt",
                            source="trusted-workspace-mcp",
                            provenance="Automatic applied-file receipt, not a transcript.",
                            tags=["workspace","file-change"])
                    except (ValueError,PermissionError,OSError):
                        self.store.audit("trusted_workspace_memory_skipped",
                                         {"project":item["project_id"],"id":upload_id})
            try:staging.unlink()
            except FileNotFoundError:pass
            return {"status":"applied","project_id":item["project_id"],
                    "path":item["path"],"new_sha256":item["sha256"],
                    "previous_sha256":item["prior_sha256"],"bytes":item["total_bytes"],
                    "backup_saved":bool(info)}

    def write_small(self,pid,relative_path,content,encoding="utf-8",expected_sha256=None):
        if encoding not in ("utf-8","base64"):raise ValueError("Expected utf-8 or base64")
        if not isinstance(content,str):raise ValueError("File content must be a string")
        payload=content.encode("utf-8") if encoding=="utf-8" else decode_blob(content)
        if len(payload)>CHUNK:raise ValueError("For files over 48 KiB use chunked workspace upload")
        up=self.begin(pid,relative_path,len(payload),hashlib.sha256(payload).hexdigest(),
                      expected_sha256)
        self.append(up["upload_id"],0,base64.b64encode(payload).decode("ascii"))
        return self.finish(up["upload_id"])

    def changes(self,pid,limit=20):
        self._project(pid)
        if type(limit)!=int or not 1<=limit<=100:raise ValueError("Bad limit")
        allitems=self.store.all("trusted_change")
        matching=[x for x in allitems if x.get("project_id")==pid]
        matching.sort(key=lambda x:x["at"],reverse=True)
        return {"project_id":pid,"changes":matching[:limit]}


def register_trusted_workspace_tools(mcp,bridge):
    from mcp.types import ToolAnnotations
    api=TrustedWorkspace(bridge)
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
    write=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=False)

    @mcp.tool(annotations=read)
    def workspace_info(project_id:str)->dict:
        """Check direct whole-folder read/write via existing locally granted project. No extra file-type scopes."""
        return api.info(project_id)

    @mcp.tool(annotations=read)
    def browse_workspace(project_id:str,relative_dir:str=".",offset:int=0,limit:int=100)->dict:
        """Browse every regular, hidden and binary file under a registered project, paginated."""
        return api.browse(project_id,relative_dir,offset,limit)

    @mcp.tool(annotations=read)
    def read_workspace_file(project_id:str,relative_path:str,offset:int=0,
                            length:int=CHUNK,as_text:str="auto")->dict:
        """Read any regular file as UTF-8 or base64 in bounded 48KiB slices, including multi-GB files."""
        return api.read(project_id,relative_path,offset,length,as_text)

    @mcp.tool(annotations=read)
    def hash_workspace_file(project_id:str,relative_path:str)->dict:
        """Compute the exact SHA-256 of any regular project file locally without transmitting file bytes."""
        return api.fingerprint(project_id,relative_path)

    @mcp.tool(annotations=write)
    def write_workspace_file(project_id:str,relative_path:str,content:str,encoding:str="utf-8",
                             expected_sha256:str|None=None)->dict:
        """Directly save a small text or binary file in one call; current project write grant auto-applies, backup kept."""
        return api.write_small(project_id,relative_path,content,encoding,expected_sha256)

    @mcp.tool(annotations=write)
    def begin_workspace_write(project_id:str,relative_path:str,total_bytes:int,sha256:str,
                              expected_sha256:str|None=None)->dict:
        """Begin an authorized all-format file write. Existing project write grant is sufficient; automatic backups."""
        return api.begin(project_id,relative_path,total_bytes,sha256,expected_sha256)

    @mcp.tool(annotations=write)
    def append_workspace_chunk(upload_id:str,offset:int,data_base64:str)->dict:
        """Append an exact consecutive 48KiB-or-smaller binary/text chunk to local staging."""
        return api.append(upload_id,offset,data_base64)

    @mcp.tool(annotations=write)
    def finish_workspace_write(upload_id:str)->dict:
        """Atomically publish staged file with original SHA and local backup; auto-record shared project memory."""
        return api.finish(upload_id)

    @mcp.tool(annotations=read)
    def recent_workspace_changes(project_id:str,limit:int=20)->dict:
        """See persistent actual web file changes, shared with Codex through project memory handoff."""
        return api.changes(project_id,limit)
    return api
