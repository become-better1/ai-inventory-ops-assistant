"""Executable schema/workflow verification using temporary databases only.
The Scenario class is a test fixture, not a public business API.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from database import initialize, connect, now

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

class Scenario:
    def __init__(self, conn):
        self.c = conn
        self.op = None
        self.seq = 0

    def get(self, table, key):
        row = self.c.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
        return dict(row) if row else None

    @contextmanager
    def operation(self, key, payload, undo_of=None):
        digest = hashlib.sha256(encode(payload).encode()).hexdigest()
        self.c.execute('BEGIN IMMEDIATE')
        try:
            previous = self.c.execute('SELECT * FROM operations WHERE request_key=?', (key,)).fetchone()
            if previous:
                if previous['request_hash'] != digest:
                    raise ValueError('Idempotency key reused with different content')
                yield False
            else:
                self.op, self.seq = key, 0
                self.c.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)',
                    (key, key, digest, 'undo' if undo_of else 'scenario',
                     'Verification fixture', '{}', undo_of, now()))
                yield True
                self.validate()
            self.c.commit()
        except BaseException:
            self.c.rollback()
            raise

    def insert(self, table, values):
        data = dict(values, created_at=now(), updated_at=now(), version=1, is_void=0)
        self.c.execute(f"INSERT INTO {table} ({','.join(data)}) VALUES ({','.join('?' for _ in data)})", tuple(data.values()))
        self.snapshot(table, None, self.get(table, data['id']))

    def update(self, table, key, **values):
        before = self.get(table, key)
        values.update(version=before['version']+1, updated_at=now())
        self.c.execute(f"UPDATE {table} SET {','.join(k+'=?' for k in values)} WHERE id=?",
                       (*values.values(), key))
        self.snapshot(table, before, self.get(table, key))

    def snapshot(self, table, before, after):
        self.seq += 1
        self.c.execute('INSERT INTO operation_changes VALUES (?,?,?,?,?,?,?,?)',
            (self.op, self.seq, table, after['id'], 'update' if before else 'insert',
             encode(before) if before else None, encode(after), after['version']))

    def event(self, key, item, kind, amount=None):
        target = self.get('items', item)
        self.insert('item_events', dict(id=key, operation_id=self.op, item_id=item,
            purchase_record_id=target['purchase_record_id'], event_type=kind,
            quantity=target['quantity'], amount_minor=amount,
            currency='CNY' if amount is not None else None))

    def validate(self):
        for p in self.c.execute('SELECT * FROM purchase_records WHERE is_void=0'):
            items = self.c.execute('SELECT * FROM items WHERE purchase_record_id=? AND is_void=0', (p['id'],)).fetchall()
            if sum(i['quantity'] for i in items) > p['quantity'] or any(i['unit'] != p['unit'] for i in items):
                raise ValueError('Purchase quantity/unit mismatch')
            events = self.c.execute("SELECT * FROM item_events WHERE purchase_record_id=? AND is_void=0 AND event_type IN ('returned','cancelled')",
                                    (p['id'],)).fetchall()
            if p['total_amount_minor'] is not None and sum(e['amount_minor'] or 0 for e in events) > p['total_amount_minor']:
                raise ValueError('Return deductions exceed purchase')
            if any(e['amount_minor'] is not None and e['currency'] != p['currency'] for e in events):
                raise ValueError('Currency mismatch')

    def seed(self):
        with self.operation('O1', {'purchase':40000}) as fresh:
            if fresh:
                self.insert('rooms', dict(id='R1', name='书房'))
                self.insert('purchase_records', dict(id='P1', product_name='G304',quantity=2,
                    unit='件',total_amount_minor=40000,currency='CNY',
                    import_mode='with_inventory',source_kind='natural_language'))
                self.insert('items',dict(id='I1',name='G304',quantity=2,unit='件',
                    purchase_record_id='P1',room_id='R1',delivery_status='received',
                    source_kind='natural_language'))
                self.event('E1','I1','recorded')

    def split(self):
        with self.operation('O2', {'split':'I1'}) as fresh:
            if fresh:
                self.update('items','I1',quantity=1)
                self.insert('items',dict(id='I2',name='G304',quantity=1,unit='件',
                    parent_item_id='I1',purchase_record_id='P1',room_id='R1',
                    delivery_status='received',condition_status='damaged',
                    usage_status='not_in_use',source_kind='natural_language'))
                self.event('E2','I2','split')

    def returned(self, key='O3', amount=20000):
        with self.operation(key, {'return':'I2','amount':amount}) as fresh:
            if fresh:
                if self.get('items','I2')['ownership_status'] != 'held':
                    raise ValueError('Item already removed')
                self.update('items','I2',ownership_status='returned')
                self.event('E'+key,'I2','returned',amount)

    def undo_return(self, target='O3'):
        with self.operation('U'+target, {'undo':target}, undo_of=target) as fresh:
            if fresh:
                changes = self.c.execute('SELECT * FROM operation_changes WHERE operation_id=? ORDER BY sequence_no DESC',(target,)).fetchall()
                for change in changes:
                    current = self.get(change['entity_type'],change['entity_id'])
                    if current['version'] != change['expected_version']:
                        raise ValueError('Undo conflict')
                for change in changes:
                    if change['before_json'] is None:
                        self.update(change['entity_type'],change['entity_id'],is_void=1)
                    else:
                        before = json.loads(change['before_json'])
                        for field in ('id','version','updated_at','created_at'):
                            before.pop(field)
                        self.update(change['entity_type'],change['entity_id'],**before)
                self.event('EU'+target,'I2','restored')

    def totals(self):
        quantity = self.c.execute('SELECT coalesce(sum(quantity),0) FROM current_inventory').fetchone()[0]
        cost = self.c.execute('SELECT sum(total_amount_minor) FROM purchase_records WHERE is_void=0').fetchone()[0] or 0
        deduction = self.c.execute("SELECT coalesce(sum(amount_minor),0) FROM item_events WHERE is_void=0 AND event_type IN ('returned','cancelled')").fetchone()[0]
        return quantity, cost-deduction

class LocalChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'test.sqlite3'
        initialize(self.path)
        self.c = connect(self.path)
        self.s = Scenario(self.c)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def prepare(self):
        self.s.seed()
        self.s.split()

    def test_purchase_return_undo_and_explicit_amount(self):
        self.prepare()
        self.assertEqual(self.s.totals(), (2,40000))
        self.s.returned()
        self.assertEqual(self.s.totals(), (1,20000))
        self.s.undo_return()
        self.assertEqual(self.s.totals(), (2,40000))
        item = self.s.get('items','I2')
        self.assertEqual((item['condition_status'],item['usage_status'],item['room_id']),('damaged','not_in_use','R1'))
        self.s.returned('O5',18000)
        self.assertEqual(self.s.totals(), (1,22000))
        self.assertEqual(self.s.get('purchase_records','P1')['total_amount_minor'],40000)

    def test_idempotent_retry_and_key_conflict(self):
        self.s.seed()
        self.s.seed()
        self.assertEqual(self.s.totals(), (2,40000))
        with self.assertRaises(ValueError):
            with self.s.operation('O1', {'purchase':1}):
                pass

    def test_return_retry_and_duplicate_rejected(self):
        self.prepare()
        self.s.returned()
        self.s.returned()
        with self.assertRaises(ValueError):
            self.s.returned('duplicate')
        self.assertEqual(self.s.totals(),(1,20000))

    def test_undo_conflict_rolls_back(self):
        self.prepare()
        self.s.returned()
        with self.s.operation('later', {'location':None}):
            self.s.update('items','I2',room_id=None)
        with self.assertRaises(ValueError):
            self.s.undo_return()
        self.assertEqual(self.s.totals(), (1,20000))
        self.assertIsNone(self.s.get('operations','UO3'))

    def test_atomic_rollback_on_excess_deduction(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.s.returned(amount=50000)
        self.assertEqual(self.s.totals(),(2,40000))
        self.assertIsNone(self.s.get('operations','O3'))
        self.assertIsNone(self.s.get('item_events','EO3'))

    def test_unknown_return_amount_remains_unknown(self):
        self.prepare()
        self.s.returned(amount=None)
        self.assertEqual(self.s.totals()[0],1)
        self.assertIsNone(self.s.get('item_events','EO3')['amount_minor'])
        # A future analysis API must label this total incomplete, not claim a final net spend.

    def test_schema_constraints(self):
        self.s.seed()
        invalid = [
            ("UPDATE purchase_records SET date_precision='day',purchase_date=NULL WHERE id='P1'",),
            ("UPDATE purchase_records SET date_precision='day',purchase_date='abcdefghij' WHERE id='P1'",),
            ("UPDATE items SET quantity=0 WHERE id='I1'",),
            ("UPDATE items SET usage_status='daily' WHERE id='I1'",),
            ("UPDATE items SET room_id='missing' WHERE id='I1'",),
            ("UPDATE items SET parent_item_id=id WHERE id='I1'",),
            ("UPDATE items SET common_purposes_json='{}' WHERE id='I1'",),
            ("UPDATE purchase_records SET total_amount_minor=-1 WHERE id='P1'",),
            ("UPDATE purchase_records SET total_amount_minor=1.5 WHERE id='P1'",),
            ("UPDATE purchase_records SET purchase_date='2026-02-30',date_precision='day' WHERE id='P1'",),
        ]
        for (sql,) in invalid:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.c.execute(sql)
                self.c.rollback()

    def test_history_only_and_unknowns(self):
        with self.s.operation('history', {'history':True}):
            self.s.insert('purchase_records',dict(id='H',product_name='旧电脑',
                quantity=1,unit='件',import_mode='history_only',source_kind='natural_language'))
        self.assertEqual(self.s.totals()[0],0)
        self.assertIsNone(self.s.get('purchase_records','H')['purchase_date'])
        self.assertIsNone(self.s.get('purchase_records','H')['total_amount_minor'])

    def test_quantity_validation_rollback(self):
        self.s.seed()
        with self.assertRaises(ValueError):
            with self.s.operation('bad', {'quantity':3}):
                self.s.update('items','I1',quantity=3)
        self.assertEqual(self.s.totals(), (2,40000))

    def test_backup_restore_and_undo(self):
        self.prepare()
        self.s.returned()
        dest = connect(Path(self.tmp.name)/'backup.sqlite3')
        self.c.backup(dest)
        try:
            restored = Scenario(dest)
            restored.undo_return()
            self.assertEqual(restored.totals(), (2,40000))
            self.assertEqual(dest.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(dest.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            dest.close()

    def test_reinitialize_preserves_data(self):
        self.s.seed()
        initialize(self.path)
        self.assertEqual(self.s.totals(), (2,40000))

if __name__ == '__main__':
    unittest.main(verbosity=2)

