"""Offline shared-memory regressions; all writes use temporary state."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from bridge.memory import MemoryStore, RevisionConflict


class SharedMemory(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.projects = {'alpha', 'beta'}
        self.validator = lambda project: project in self.projects
        self.store = MemoryStore(self.state, self.validator)

    def create(self, project='alpha', **changes):
        values = dict(title='Design', body='SQLite local memory', tags=[' Local ', 'LOCAL'],
                      kind='decision', origin='codex', source='thread:123',
                      provenance='Reviewed by owner')
        values.update(changes)
        return self.store.upsert_note(project, **values)

    def test_legacy_unbound_notes_require_explicit_migration(self):
        seed = self.create('alpha')
        self.projects.add('gamma')
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("""
                INSERT INTO notes(id,project_id,title,body,kind,tags,origin,source,
                                  provenance,created_at,updated_at,revision,archived)
                SELECT ?, 'gamma', title,body,kind,tags,origin,source,
                       provenance,created_at,updated_at,revision,archived
                FROM notes WHERE id=?
            """, (str(uuid.uuid4()), seed['id']))
        with self.assertRaisesRegex(PermissionError, 'Legacy memory'):
            self.store.list_notes('gamma')

    def test_project_id_reuse_refuses_cross_root_memory(self):
        first = self.root / 'project_one'
        second = self.root / 'project_two'
        first.mkdir()
        second.mkdir()
        mapping = {'alpha': {'id': 'alpha', 'root': str(first)}}
        guarded = MemoryStore(self.state, lambda project: mapping.get(project))
        note = guarded.upsert_note('alpha', title='Old project only')
        self.assertEqual(guarded.list_notes('alpha')[0]['id'], note['id'])
        del mapping['alpha']
        with self.assertRaises(ValueError):
            guarded.list_notes('alpha')
        mapping['alpha'] = {'id': 'alpha', 'root': str(second)}
        with self.assertRaisesRegex(PermissionError, 'Project identity changed'):
            guarded.list_notes('alpha')
        with self.assertRaises(PermissionError):
            guarded.upsert_note('alpha', title='New root')

    def test_persistence_identity_and_provenance(self):
        note = self.create(title='中文 ✅')
        self.assertEqual(str(uuid.UUID(note['id'])), note['id'])
        self.assertEqual(note['tags'], ['local'])
        self.assertEqual(note['revision'], 1)
        self.assertFalse(note['archived'])
        self.assertGreater(note['created_at'], 0)
        self.assertEqual(note['created_at'], note['updated_at'])
        other = MemoryStore(self.state, self.validator)
        self.assertEqual(other.list_notes('alpha'), [note])
        updated = other.upsert_note('alpha', note_id=note['id'], expected_revision=1,
                                    title='Revised', origin='chatgpt', source='chat:456',
                                    provenance='Owner correction')
        self.assertEqual(updated['id'], note['id'])
        self.assertEqual(updated['created_at'], note['created_at'])
        self.assertGreaterEqual(updated['updated_at'], note['updated_at'])
        self.assertEqual(updated['source'], 'chat:456')
        self.assertEqual(updated['provenance'], 'Owner correction')
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(self.store.list_notes('alpha'), [updated])

    def test_isolation_and_dynamic_project_validation(self):
        note = self.create()
        self.assertEqual(self.store.list_notes('beta'), [])
        self.assertEqual(self.store.search_notes('beta', 'SQLite'), [])
        self.assertEqual(self.store.statistics('beta')['total'], 0)
        with self.assertRaisesRegex(ValueError, 'Unknown note'):
            self.store.upsert_note('beta', note_id=note['id'], expected_revision=1, title='Cross')
        with self.assertRaisesRegex(ValueError, 'Unknown note'):
            self.store.archive_note('beta', note['id'], expected_revision=1)
        for project in ('unregistered', "alpha' OR 1=1 --"):
            for action in (lambda: self.store.list_notes(project),
                           lambda: self.store.search_notes(project, 'Design'),
                           lambda: self.store.statistics(project),
                           lambda: self.create(project),
                           lambda: self.store.archive_note(project, note['id'], expected_revision=1)):
                with self.assertRaisesRegex(ValueError, 'not registered'):
                    action()
        self.projects.remove('alpha')
        with self.assertRaises(ValueError):
            self.store.list_notes('alpha')
        def broken_validator(project):
            raise RuntimeError('private registry detail')
        broken = MemoryStore(self.state, broken_validator)
        with self.assertRaisesRegex(ValueError, '^Project is not registered$'):
            broken.statistics('alpha')

    def test_literal_search_title_body_and_normalized_tags(self):
        literal = self.create(title='100%_done\\path', body='Unique body needle',
                              tags=[' ＬＯＣＡＬ ', 'local', 'Topic'])
        self.create(title='100XXdoneXpath', body='other', tags=[])
        for query in ('100%_done\\path', 'body needle', 'topic', 'LOCAL', '%', '_', '\\'):
            with self.subTest(query=query):
                self.assertEqual([n['id'] for n in self.store.search_notes('alpha', query)],
                                 [literal['id']])
        self.assertEqual(literal['tags'], ['local', 'topic'])
        self.assertEqual(self.store.search_notes('alpha', "' OR 1=1 --"), [])
        self.assertEqual(self.store.search_notes('alpha', 'missing'), [])

    def test_revision_conflict_and_concurrent_writers(self):
        note = self.create()
        other = MemoryStore(self.state, self.validator)
        def update(store):
            try:
                return store.upsert_note('alpha', note_id=note['id'], expected_revision=1,
                                         title='Concurrent change')['revision']
            except RevisionConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, (self.store, other)))
        self.assertCountEqual(results, [2, 'conflict'])
        with self.assertRaises(RevisionConflict):
            self.store.archive_note('alpha', note['id'], expected_revision=1)
        with self.assertRaises(ValueError):
            self.store.upsert_note('alpha', note_id=note['id'], title='Missing revision')
        self.assertEqual(self.store.list_notes('alpha')[0]['revision'], 2)

    def test_archive_and_statistics(self):
        note = self.create()
        self.create('beta')
        archived = self.store.archive_note('alpha', note['id'], expected_revision=1)
        self.assertTrue(archived['archived'])
        self.assertEqual(archived['revision'], 2)
        self.assertEqual(self.store.list_notes('alpha'), [])
        self.assertEqual(self.store.search_notes('alpha', 'Design'), [])
        self.assertEqual(self.store.list_notes('alpha', include_archived=True), [archived])
        self.assertEqual(self.store.search_notes('alpha', 'Design', include_archived=True), [archived])
        stats = self.store.statistics('alpha')
        self.assertEqual((stats['total'], stats['active'], stats['archived']), (1, 0, 1))
        self.assertEqual(stats['by_kind']['decision'], 1)
        updated = self.store.upsert_note('alpha', note_id=note['id'], expected_revision=2,
                                         title='Retained archive')
        self.assertTrue(updated['archived'])
        self.assertEqual(updated['revision'], 3)

    def test_bounds_and_deterministic_pagination(self):
        for number in range(4):
            self.create(title=str(number))
        # Force equal timestamps to exercise the UUID ordering tie breaker.
        with sqlite3.connect(self.store.path) as connection:
            connection.execute('UPDATE notes SET updated_at=123')
        notes = self.store.list_notes('alpha')
        self.assertEqual([n['id'] for n in notes], sorted(n['id'] for n in notes))
        self.assertEqual(self.store.list_notes('alpha', limit=2, offset=2), notes[2:])
        self.assertEqual(self.store.search_notes('alpha', 'SQLite', limit=2), notes[:2])
        for kwargs in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'limit': 1.5},
                       {'offset': -1}, {'offset': 10001}, {'include_archived': 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.list_notes('alpha', **kwargs)
        for query in ('', '  ', 'q' * 257):
            with self.assertRaises(ValueError):
                self.store.search_notes('alpha', query)
        for changes in ({'title': ''}, {'title': 'x' * 301}, {'body': 'x' * 32769},
                        {'body': '中' * 11000}, {'tags': ['x'] * 21}, {'tags': 'tag'},
                        {'tags': [1]}, {'tags': ['']}, {'tags': ['x' * 65]},
                        {'kind': 'invalid'}, {'origin': 'remote'}, {'body': '\x00'},
                        {'source': 'x' * 1025}, {'provenance': 'x' * 2049}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create(**changes)
        self.assertEqual(self.store.statistics('alpha')['total'], 4)

    def test_obvious_secrets_rejected_in_all_content_fields(self):
        secrets = ['sk-' + 'a' * 24, 'Bearer ' + 'b' * 20,
                   'api_' + 'key="fixturecredential"', 'pass' + 'word=fixturecredential',
                   '-----BEGIN PRIVATE KEY-----', 'ghp_' + 'c' * 30,
                   'AKIA' + 'D' * 16]
        for secret in secrets:
            for field in ('title', 'body', 'tags', 'source', 'provenance'):
                with self.subTest(field=field, pattern=secrets.index(secret)):
                    with self.assertRaisesRegex(ValueError, '^Possible secret rejected$'):
                        self.create(**{field: [secret] if field == 'tags' else secret})
        self.assertEqual(self.store.statistics('alpha')['total'], 0)
        note = self.create()
        with self.assertRaises(ValueError):
            self.store.upsert_note('alpha', note_id=note['id'], expected_revision=1,
                                   title='Update', body=secrets[0])
        self.assertEqual(self.store.list_notes('alpha'), [note])

    def test_private_database_wal_and_distinct_storage(self):
        self.assertEqual(self.store.path.name, 'studio.sqlite3')
        self.assertFalse((self.state / 'bridge.sqlite3').exists())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        with self.store._connect() as connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0], 10000)
            self.create()
            for suffix in ('-wal', '-shm'):
                path = Path(str(self.store.path) + suffix)
                self.assertTrue(path.exists())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unsafe_existing_paths_rejected_without_permission_changes(self):
        unsafe = self.root / 'unsafe'
        unsafe.mkdir(mode=0o755)
        unsafe.chmod(0o755)  # umask 077 in the Bridge test runner
        with self.assertRaises(PermissionError):
            MemoryStore(unsafe, self.validator)
        self.assertEqual(unsafe.stat().st_mode & 0o777, 0o755)
        link = self.root / 'state-link'
        link.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(PermissionError):
            MemoryStore(link, self.validator)
        for mode in ('public', 'symlink', 'hardlink'):
            directory = self.root / mode
            directory.mkdir(mode=0o700)
            target = directory / 'studio.sqlite3'
            if mode == 'public':
                target.touch(mode=0o644)
                target.chmod(0o644)
            elif mode == 'symlink':
                target.symlink_to(self.store.path)
            else:
                os.link(self.store.path, target)
            try:
                with self.assertRaises((PermissionError, OSError)):
                    MemoryStore(directory, self.validator)
            finally:
                if mode == 'hardlink':
                    target.unlink()
        sidecar = Path(str(self.store.path) + '-wal')
        sidecar.symlink_to(self.root / 'outside')
        with self.assertRaises(PermissionError):
            self.store.list_notes('alpha')
        self.assertFalse((self.root / 'outside').exists())


if __name__ == '__main__':
    unittest.main()
