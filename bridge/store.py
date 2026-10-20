import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'bridge.sqlite3'
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT, PRIMARY KEY(kind,id))')
            c.execute('CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, at REAL, event TEXT, data TEXT)')
        self.path.chmod(0o600)
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path, timeout=10)
        try:
            with c: yield c
        finally: c.close()
    def put(self, kind, id, data):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)', (kind,id,json.dumps(data)))
    def get(self, kind, id):
        with self.connect() as c:
            row = c.execute('SELECT data FROM records WHERE kind=? AND id=?',(kind,id)).fetchone()
        return json.loads(row[0]) if row else None
    def all(self, kind):
        with self.connect() as c:
            rows = c.execute('SELECT data FROM records WHERE kind=? ORDER BY rowid',(kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]
    def update(self, kind, id, **changes):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT data FROM records WHERE kind=? AND id=?',(kind,id)).fetchone()
            if row is None: raise ValueError('Unknown record')
            data = json.loads(row[0]); data.update(changes)
            c.execute('UPDATE records SET data=? WHERE kind=? AND id=?',(json.dumps(data),kind,id))
        return data
    def consume(self, kind, id):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT data FROM records WHERE kind=? AND id=?',(kind,id)).fetchone()
            c.execute('DELETE FROM records WHERE kind=? AND id=?',(kind,id))
        return json.loads(row[0]) if row else None
    def audit(self, event, data):
        from .security import redact
        with self.connect() as c:
            c.execute('INSERT INTO audit(at,event,data) VALUES (?,?,?)',(time.time(),event,redact(json.dumps(data))))
