"""Offline migration and recovery checks; no production database access."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import database
from database import connect, initialize, migrate, validate_schema, now, MIGRATIONS
from maintenance import snapshot, restore, digest, inspect_database
from service import Assistant
from file_lock import file_lock, lock_path


class MigrationChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root/'old.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def old_database(self):
        c = connect(self.path)
        c.executescript(database.migration_sql(MIGRATIONS[0]))
        c.execute('INSERT INTO schema_migrations VALUES (?,?,?,?)',
                  (1,'initial_schema',MIGRATIONS[0][3],now()))
        rid, op = str(uuid4()),str(uuid4())
        stamp = now()
        c.execute('INSERT INTO rooms VALUES (?,?,?,?,?,?,?,?)', (rid,'旧书房',None,None,stamp,stamp,1,0))
        row = dict(c.execute('SELECT * FROM rooms').fetchone())
        c.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)', (op,'old','hash','create_room','create_room','{}',None,stamp))
        c.execute('INSERT INTO operation_changes VALUES (?,?,?,?,?,?,?,?)',
                  (op,1,'rooms',rid,'insert',None,json.dumps(row,ensure_ascii=False,sort_keys=True),1))
        c.commit()
        c.close()
        return op

    def test_existing_v1_refused_then_migrated_preserving_history(self):
        op = self.old_database()
        before = digest(self.path)
        with self.assertRaisesRegex(ValueError,'offline migration'):
            initialize(self.path)
        self.assertEqual(digest(self.path),before)
        result = migrate(self.path)
        self.assertEqual(inspect_database(result['backup'])['schema_version'],1)
        self.assertEqual(inspect_database(self.path)['schema_version'],2)
        api = Assistant(self.path)
        self.assertEqual(api.rooms()[0]['name'],'旧书房')
        api.undo('undo-old',op)
        self.assertEqual(api.rooms(),[])
        self.assertFalse(migrate(self.path)['migrated'])

    def test_failure_after_ddl_rolls_back_and_backup_is_restorable(self):
        self.old_database()
        original = database.apply_pending
        def fail(c, version):
            original(c,version)
            raise RuntimeError('injected failure')
        with patch('database.apply_pending', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'injected'):
                migrate(self.path)
        self.assertEqual(inspect_database(self.path)['schema_version'],1)
        with closing(connect(self.path)) as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='behavior_rules'").fetchone())
            self.assertEqual(c.execute('SELECT count(*) FROM operation_changes').fetchone()[0],1)
        backups = list((self.root/'backups').glob('*.sqlite3'))
        self.assertEqual(len(backups),1)
        migrate(self.path)
        restored = self.root/'restored.sqlite3'
        restore(backups[0],restored,confirmed=True)
        self.assertEqual(inspect_database(restored)['schema_version'],1)

    def test_service_lease_prevents_upgrade(self):
        self.old_database()
        before = digest(self.path)
        with file_lock(lock_path(self.path,'service')):
            with self.assertRaises(ValueError):
                migrate(self.path)
        self.assertEqual(digest(self.path),before)

    def test_all_history_verified_and_future_or_missing_versions_refused(self):
        initialize(self.path)
        with closing(connect(self.path)) as c:
            c.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=1")
            c.commit()
        with self.assertRaises(ValueError):
            inspect_database(self.path)
        with self.assertRaises(ValueError):
            migrate(self.path)
        with closing(connect(self.path)) as c:
            c.execute('UPDATE schema_migrations SET checksum=? WHERE version=1',(MIGRATIONS[0][3],))
            c.execute("INSERT INTO schema_migrations VALUES (3,'future','unknown','now')")
            c.commit()
        with self.assertRaises(ValueError):
            initialize(self.path)

    def test_missing_source_not_created_and_bad_sql_file_refused(self):
        with self.assertRaises(ValueError):
            migrate(self.path)
        self.assertFalse(self.path.exists())
        self.old_database()
        with patch('database.MIGRATIONS', [MIGRATIONS[0],(*MIGRATIONS[1][:3],'bad-checksum')]):
            with self.assertRaisesRegex(ValueError,'file checksum'):
                migrate(self.path)
        self.assertEqual(inspect_database(self.path)['schema_version'],1)

    def test_missing_index_detected_even_with_valid_history(self):
        initialize(self.path)
        with closing(connect(self.path)) as c:
            c.execute('DROP INDEX stages_position')
            c.commit()
        with self.assertRaisesRegex(ValueError, 'definition mismatch'):
            inspect_database(self.path)
        with self.assertRaises(ValueError):
            migrate(self.path)

    def test_new_database_has_both_versions_and_constraints(self):
        initialize(self.path)
        with closing(connect(self.path)) as c:
            self.assertEqual([r[0] for r in c.execute('SELECT version FROM schema_migrations ORDER BY version')],[1,2])
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO routine_flows(id,name,content,created_at,updated_at,is_active) VALUES ('bad','x','x','now','now',2)")


if __name__ == '__main__':
    unittest.main()
