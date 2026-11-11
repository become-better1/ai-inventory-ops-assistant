import tempfile
from pathlib import Path
import json
import sqlite3
import unittest
from maintenance import snapshot,restore,verify_backup,digest
from file_lock import file_lock,lock_path
from database import initialize,connect
from service import Assistant

class MaintenanceChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.path=self.root/'personal.sqlite3'
        self.api=Assistant(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self):
        return self.api.record_purchase('buy',name='鼠标',quantity=2,
            total_amount_minor=40000,currency='CNY')

    def backup(self):
        return snapshot(self.path,self.root/'backups')

    def test_backup_restore_preserves_undo_and_safety_copy(self):
        b=self.seed()
        returned=self.api.return_item('return',b['item_id'],quantity=1,expected_version=1,infer_amount=True)
        saved=self.backup()
        self.api.undo('undo',returned['operation_id'])
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],40000)
        result=restore(saved['backup'],self.path,confirmed=True)
        self.assertTrue(Path(result['safety_backup']).is_file())
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],20000)
        self.api.undo('undo_again',returned['operation_id'])
        self.assertEqual(sum(i['quantity'] for i in self.api.inventory()),2)

    def test_live_service_lease_refuses_restore(self):
        self.seed()
        saved=self.backup()
        before=digest(self.path)
        with file_lock(lock_path(self.path,'service')):
            with self.assertRaises(ValueError):
                restore(saved['backup'],self.path,confirmed=True)
        self.assertEqual(digest(self.path),before)

    def test_confirmation_required_and_same_source_rejected(self):
        saved=self.backup()
        with self.assertRaises(ValueError):
            restore(saved['backup'],self.path)
        with self.assertRaises(ValueError):
            restore(saved['backup'],saved['backup'],confirmed=True)

    def test_corrupt_backup_refused_without_changing_target(self):
        self.seed()
        saved=self.backup()
        source=Path(saved['backup'])
        source.write_bytes(source.read_bytes()+b'corrupt')
        before=digest(self.path)
        with self.assertRaises(ValueError):
            restore(source,self.path,confirmed=True)
        self.assertEqual(digest(self.path),before)

    def test_schema_mismatch_refused(self):
        saved=self.backup()
        source=Path(saved['backup'])
        with sqlite3.connect(source) as c:
            c.execute("UPDATE schema_migrations SET checksum='unsupported'")
        c.close()
        manifest=Path(saved['manifest'])
        meta=json.loads(manifest.read_text())
        meta['sha256']=digest(source)
        meta['size_bytes']=source.stat().st_size
        manifest.write_text(json.dumps(meta))
        before=digest(self.path)
        with self.assertRaises(ValueError):
            restore(source,self.path,confirmed=True)
        self.assertEqual(digest(self.path),before)

    def test_restore_new_path(self):
        self.seed()
        saved=self.backup()
        new=self.root/'new'/'restored.sqlite3'
        result=restore(saved['backup'],new,confirmed=True)
        self.assertIsNone(result['safety_backup'])
        self.assertEqual(sum(i['quantity'] for i in Assistant(new).inventory()),2)

    def test_backup_wal_includes_committed_rows(self):
        c=connect(self.path)
        c.execute('PRAGMA journal_mode=WAL')
        self.seed()
        saved=self.backup()
        c.close()
        restored=Assistant(saved['backup'])
        self.assertEqual(len(restored.inventory()),1)
        self.assertEqual(verify_backup(saved['backup'])['schema_version'],1)

    def test_missing_source_does_not_create_database(self):
        missing=self.root/'missing.sqlite3'
        with self.assertRaises(ValueError):
            snapshot(missing,self.root/'backups')
        self.assertFalse(missing.exists())

    def test_unique_backups_and_missing_manifest(self):
        one=self.backup()
        two=self.backup()
        self.assertNotEqual(one['backup'],two['backup'])
        Path(one['manifest']).unlink()
        with self.assertRaises(ValueError):
            verify_backup(one['backup'])

if __name__=='__main__':
    unittest.main(verbosity=2)

