"""Conflict checked, backed up batch application under the authorization lock."""
import os
import secrets
import json
from pathlib import Path
from .filetools import _check_path, _safe_open_parent, _read_from_parent

def open_parent(root, bits):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        for bit in bits[:-1]:
            try: os.mkdir(bit,mode=0o700,dir_fd=fd)
            except FileExistsError: pass
            nxt=os.open(bit,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            os.close(fd); fd=nxt
        return fd
    except BaseException:
        os.close(fd); raise

def replace_at(fd,name,data,mode):
    temp='.bridge-'+secrets.token_hex(12)
    try:
        out=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(out,'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno()); os.fchmod(f.fileno(),mode)
        os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd); os.fsync(fd)
    finally:
        try: os.unlink(temp,dir_fd=fd)
        except FileNotFoundError: pass

def apply_batch(bridge, task, result, automatic=True):
    # An empty batch writes no source files and must not report an application.
    if not result['edits']:
        return
    p=bridge.project(task['project_id']); records=[]; applied=[]
    backup=bridge.store.directory/'backups'/task['id']
    backup.mkdir(parents=True,mode=0o700,exist_ok=False)
    try:
        for e in result['edits']:
            bits=_check_path(e['path']); fd=open_parent(p['root'],bits)
            try:
                old,mode=_read_from_parent(fd,bits[-1])
                expected=task['baseline'].get(e['path'])
                if old!=(expected.encode() if expected is not None else None):
                    raise ValueError('Original changed since snapshot: '+e['path'])
                record=(fd,bits[-1],old,mode or 0o600,e)
                records.append(record)
            except BaseException:
                os.close(fd); raise
        for fd,name,old,mode,e in records:
            if old is not None:
                dest=backup/e['path']; dest.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
                with dest.open('xb') as out:
                    os.chmod(dest,0o600); out.write(old); out.flush(); os.fsync(out.fileno())
        manifest={'task_id':task['id'],'review_digest':result['review_digest'],
                  'files':[{'path':e['path'],'existed':old is not None,'mode':mode}
                           for fd,name,old,mode,e in records]}
        with (backup/'manifest.json').open('x') as out:
            os.chmod(backup/'manifest.json',0o600)
            json.dump(manifest,out); out.flush(); os.fsync(out.fileno())
        bridge.store.audit('automatic_write_prepared',{'task':task['id'],'paths':[e['path'] for e in result['edits']]})
        for record in records:
            fd,name,old,mode,e=record
            current,_=_read_from_parent(fd,name)
            if current!=old: raise ValueError('Concurrent source modification: '+e['path'])
            replace_at(fd,name,e['content'].encode(),mode & 0o755)
            applied.append(record)
    except BaseException:
        for fd,name,old,mode,e in reversed(applied):
            if old is None: os.unlink(name,dir_fd=fd)
            else: replace_at(fd,name,old,mode & 0o755)
        bridge.store.audit('automatic_write_aborted',{'task':task['id']})
        raise
    finally:
        for fd,*_ in records: os.close(fd)
    bridge.store.update('task',task['id'],changes_applied=True)
    bridge.store.audit('changes_applied_automatically' if automatic else 'changes_applied_locally',{'task':task['id'],'grant_id':task.get('project_grant_id'),
                                                    'review_digest':result['review_digest']})
