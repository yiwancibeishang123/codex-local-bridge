"""Local project memory; no model, embedding, or network operations.

Pass a trusted project validator returning a truthy registered-project value
(or raising on failure). Updates require the last observed revision. Content
replacement preserves identity and creation time; archival is a soft delete.
Secret screening is deliberately basic and cannot identify every credential.
"""
from contextlib import contextmanager
import hashlib
from collections.abc import Mapping
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import unicodedata
import uuid


KINDS = ('project', 'decision', 'task', 'knowledge')
ORIGINS = ('chatgpt', 'codex', 'studio')
MAX_LIMIT = 100
MAX_OFFSET = 10000
MAX_TITLE = 300
MAX_BODY = 32768
MAX_TAGS = 20
MAX_TAG = 64
MAX_QUERY = 256
_SECRET = re.compile(
    r'sk-[A-Za-z0-9_-]{8,}|'
    r'\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|'
    r'AKIA[A-Z0-9]{16})\b|'
    r'-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY)-----|'
    r'\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|'
    r'\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|'
    r'client[_ -]?secret|pass' + r'word|passwd)\b[\s\"\x27]*[:=]\s*'
    r'[\"\x27]?[^\s\"\x27,;}{]{4,}', re.IGNORECASE)


class RevisionConflict(ValueError):
    """The supplied revision no longer matches the stored note."""


def _text(value, maximum, name, required=False):
    if not isinstance(value, str):
        raise ValueError('Invalid ' + name)
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        raise ValueError('Invalid ' + name) from None
    if size > maximum or '\x00' in value or (required and not value.strip()):
        raise ValueError('Invalid ' + name)
    if _SECRET.search(value):
        raise ValueError('Possible secret rejected')
    return value


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Invalid ' + name)
    return value


def _note_id(value):
    if not isinstance(value, str):
        raise ValueError('Invalid note ID')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError('Invalid note ID') from None
    if str(parsed) != value:
        raise ValueError('Invalid note ID')
    return value


class MemoryStore:
    """Private studio.sqlite3 storage with bounded, project-scoped operations.

    Existing unsafe paths are rejected, never chmodded. The validator must be
    supplied by local configuration, not by a remote request. Each operation
    revalidates its project so removal from the registry takes effect promptly.
    Limits are UTF-8 bytes; pagination rejects values outside documented bounds.
    """

    def __init__(self, state_dir, project_validator, *, timeout=10):
        if not callable(project_validator):
            raise TypeError('A registered-project validator is required')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise ValueError('Invalid timeout')
        self._validate_project = project_validator
        self.timeout = timeout
        self.directory = Path(os.path.abspath(Path(state_dir).expanduser()))
        # Refuse symlink ancestors before creating or opening database files.
        for path in reversed((self.directory, *self.directory.parents)):
            if path.is_symlink():
                raise PermissionError('Symlink state path rejected')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._private(self.directory, directory=True)
        self.path = self.directory / 'studio.sqlite3'
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self.path, flags, 0o600)
        try:
            self._check_info(os.fstat(fd), False)
        finally:
            os.close(fd)
        with self._connect() as connection:
            if connection.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise RuntimeError('WAL unavailable')
            connection.execute('''CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('project','decision','task','knowledge')),
                tags TEXT NOT NULL,
                origin TEXT NOT NULL CHECK(origin IN ('chatgpt','codex','studio')),
                source TEXT NOT NULL,
                provenance TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0),
                archived INTEGER NOT NULL CHECK(archived IN (0,1))
            )''')
            connection.execute('''CREATE INDEX IF NOT EXISTS notes_project_order
                ON notes(project_id, archived, updated_at DESC, id)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS project_bindings (
                project_id TEXT PRIMARY KEY, identity TEXT NOT NULL, bound_at REAL NOT NULL)''')
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(notes)')}
            required = {'id','project_id','title','body','kind','tags','origin','source',
                        'provenance','created_at','updated_at','revision','archived'}
            if not required.issubset(columns):
                raise RuntimeError('Incompatible Studio memory schema; migrate locally')

    @staticmethod
    def _check_info(info, directory):
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        mode = 0o700 if directory else 0o600
        if (not correct_type or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != mode
                or (not directory and info.st_nlink != 1)):
            raise PermissionError('Memory paths must be private and owner-owned')

    def _private(self, path, directory=False):
        self._check_info(path.lstat(), directory)

    @contextmanager
    def _connect(self):
        self._private(self.directory, directory=True)
        self._private(self.path)
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.path) + suffix)
            try:
                self._private(sidecar)
            except FileNotFoundError:
                pass
        connection = sqlite3.connect(self.path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            with connection:
                yield connection
        finally:
            connection.close()

    def _project(self, project_id):
        if (not isinstance(project_id, str) or not project_id
                or len(project_id) > 128):
            raise ValueError('Project is not registered')
        try:
            record = self._validate_project(project_id)
        except Exception:
            record = False
        if not record:
            raise ValueError('Project is not registered')
        # Bind notes to the actual configured project root, not merely its ID.
        # A deleted project's ID cannot silently expose its retained notes to a
        # different root. A future explicit migration can move a binding.
        if isinstance(record, Mapping) and isinstance(record.get('root'), str):
            root = Path(record['root']).expanduser().resolve()
            identity = 'root:' + str(root)
            try:
                st = root.stat()
            except OSError:
                raise ValueError('Registered project root unavailable') from None
            identity += ':' + str(st.st_dev) + ':' + str(st.st_ino)
        elif isinstance(record, str):
            identity = 'identity:' + record
        else:
            identity = 'legacy:' + project_id
        fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute(
                'SELECT identity FROM project_bindings WHERE project_id=?', (project_id,)).fetchone()
            if previous and previous['identity'] != fingerprint:
                raise PermissionError('Project identity changed; local memory migration required')
            if not previous:
                # Do not adopt legacy notes with an unverified project ID.
                legacy = connection.execute(
                    'SELECT 1 FROM notes WHERE project_id=? LIMIT 1', (project_id,)).fetchone()
                if legacy:
                    raise PermissionError('Legacy memory needs an explicit local migration')
                connection.execute(
                    'INSERT INTO project_bindings(project_id,identity,bound_at) VALUES(?,?,?)',
                    (project_id, fingerprint, time.time()))
        return project_id

    @staticmethod
    def _decode(row):
        result = dict(row)
        result['tags'] = json.loads(result['tags'])
        result['archived'] = bool(result['archived'])
        return result

    def list_notes(self, project_id, *, limit=50, offset=0, include_archived=False):
        """Return at most 100 notes, newest first with UUID tie breaking."""
        return self._list(project_id, None, limit, offset, include_archived)

    def search_notes(self, project_id, query, *, limit=50, offset=0,
                     include_archived=False):
        """Search title/body/tags for a literal substring (LIKE metacharacters escaped)."""
        query = _text(query, MAX_QUERY, 'query', required=True)
        return self._list(project_id, query, limit, offset, include_archived)

    def _list(self, project_id, query, limit, offset, include_archived):
        project_id = self._project(project_id)
        _integer(limit, 1, MAX_LIMIT, 'limit')
        _integer(offset, 0, MAX_OFFSET, 'offset')
        if type(include_archived) is not bool:
            raise ValueError('Invalid include_archived')
        sql = 'SELECT * FROM notes WHERE project_id=?'
        parameters = [project_id]
        if not include_archived:
            sql += ' AND archived=0'
        if query is not None:
            escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            pattern = '%' + escaped + '%'
            sql += " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM json_each(notes.tags) WHERE value LIKE ? ESCAPE '\\'))"
            tag_query = unicodedata.normalize('NFKC', query).casefold()
            tag_query = tag_query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            parameters.extend((pattern, pattern, '%' + tag_query + '%'))
        sql += ' ORDER BY updated_at DESC, id ASC LIMIT ? OFFSET ?'
        parameters.extend((limit, offset))
        with self._connect() as connection:
            return [self._decode(row) for row in connection.execute(sql, parameters)]

    def upsert_note(self, project_id, *, title, body='', kind='knowledge',
                    tags=None, origin='studio', source='', provenance='',
                    note_id=None, expected_revision=None):
        """Create a UUID note or replace content using an exact positive revision.

        source and provenance are bounded caller-supplied attribution text, not
        trusted identity claims. Archived notes remain archived when replaced.
        """
        project_id = self._project(project_id)
        title = _text(title, MAX_TITLE, 'title', required=True).strip()
        body = _text(body, MAX_BODY, 'body')
        source = _text(source, 1024, 'source')
        provenance = _text(provenance, 2048, 'provenance')
        if kind not in KINDS or origin not in ORIGINS:
            raise ValueError('Invalid kind or origin')
        if tags is None:
            tags = []
        if not isinstance(tags, list) or len(tags) > MAX_TAGS:
            raise ValueError('Invalid tags')
        normalized = []
        for tag in tags:
            tag = _text(tag, MAX_TAG, 'tag', required=True)
            tag = unicodedata.normalize('NFKC', tag).strip().casefold()
            normalized.append(_text(tag, MAX_TAG, 'tag', required=True))
        encoded_tags = json.dumps(sorted(set(normalized)), ensure_ascii=False)
        creating = note_id is None
        if creating:
            if expected_revision is not None:
                raise ValueError('New notes must omit expected_revision')
            note_id = str(uuid.uuid4())
        else:
            _note_id(note_id)
            _integer(expected_revision, 1, 2**63 - 2, 'expected_revision')
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            now = time.time()
            values = (title, body, kind, encoded_tags, origin, source, provenance)
            if creating:
                connection.execute('''INSERT INTO notes
                    (id,project_id,title,body,kind,tags,origin,source,provenance,
                     created_at,updated_at,revision,archived)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,1,0)''',
                    (note_id, project_id, *values, now, now))
            else:
                old = connection.execute('SELECT * FROM notes WHERE project_id=? AND id=?',
                                         (project_id, note_id)).fetchone()
                if old is None:
                    raise ValueError('Unknown note')
                if old['revision'] != expected_revision:
                    raise RevisionConflict('Revision conflict')
                now = max(now, old['updated_at'])
                cursor = connection.execute('''UPDATE notes SET title=?,body=?,kind=?,tags=?,
                    origin=?,source=?,provenance=?,updated_at=?,revision=revision+1
                    WHERE project_id=? AND id=? AND revision=?''',
                    (*values, now, project_id, note_id, expected_revision))
                if cursor.rowcount != 1:
                    raise RevisionConflict('Revision conflict')
            row = connection.execute('SELECT * FROM notes WHERE project_id=? AND id=?',
                                     (project_id, note_id)).fetchone()
            return self._decode(row)

    def archive_note(self, project_id, note_id, *, expected_revision):
        """Soft-delete a note, advancing its revision even if already archived."""
        project_id = self._project(project_id)
        _note_id(note_id)
        _integer(expected_revision, 1, 2**63 - 2, 'expected_revision')
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM notes WHERE project_id=? AND id=?',
                                     (project_id, note_id)).fetchone()
            if row is None:
                raise ValueError('Unknown note')
            if row['revision'] != expected_revision:
                raise RevisionConflict('Revision conflict')
            connection.execute('''UPDATE notes SET archived=1,revision=revision+1,updated_at=?
                WHERE project_id=? AND id=? AND revision=?''',
                (max(time.time(), row['updated_at']), project_id, note_id, expected_revision))
            return self._decode(connection.execute(
                'SELECT * FROM notes WHERE project_id=? AND id=?',
                (project_id, note_id)).fetchone())

    def statistics(self, project_id):
        """Return counts only for the validated project, including archived notes."""
        project_id = self._project(project_id)
        with self._connect() as connection:
            rows = connection.execute('''SELECT kind,archived,COUNT(*) AS count FROM notes
                WHERE project_id=? GROUP BY kind,archived''', (project_id,)).fetchall()
        result = {'project_id': project_id, 'total': 0, 'active': 0,
                  'archived': 0, 'by_kind': dict.fromkeys(KINDS, 0)}
        for row in rows:
            count = row['count']
            result['total'] += count
            result['archived' if row['archived'] else 'active'] += count
            result['by_kind'][row['kind']] += count
        return result
