import tempfile
from pathlib import Path
import unittest
from service import Assistant
from database import connect

class ReturnCorrectionChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.api=Assistant(Path(self.tmp.name)/'corrections.sqlite3')
        self.buy=self.api.record_purchase('buy',name='鼠标',quantity=2,
            total_amount_minor=40000,currency='CNY')
        self.ret=self.api.return_item('return',self.buy['item_id'],quantity=1,
            expected_version=1,infer_amount=True)

    def tearDown(self):
        self.tmp.cleanup()

    def net(self):
        return self.api.spending()['by_currency']['CNY']['net_minor']

    def test_correct_and_undo_no_duplicate_deduction_or_inventory_change(self):
        inventory=self.api.inventory()
        corrected=self.api.correct_return_amount('correct',self.ret['event_id'],
            expected_version=1,amount_minor=18000)
        self.assertEqual(self.net(),22000)
        self.assertEqual(self.api.inventory(),inventory)
        events=self.api.return_events()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['amount_minor'],18000)
        self.assertEqual(events[0]['version'],2)
        self.api.undo('undo',corrected['operation_id'])
        self.assertEqual(self.net(),20000)
        self.assertEqual(self.api.inventory(),inventory)
        self.assertEqual(self.api.return_events()[0]['version'],3)

    def test_retry_and_key_conflict(self):
        args=dict(expected_version=1,amount_minor=18000)
        first=self.api.correct_return_amount('correct',self.ret['event_id'],**args)
        self.assertEqual(first,self.api.correct_return_amount('correct',self.ret['event_id'],**args))
        with self.assertRaises(ValueError):
            self.api.correct_return_amount('correct',self.ret['event_id'],expected_version=1,amount_minor=17000)
        self.assertEqual(self.net(),22000)

    def test_unknown_amount_and_later_supplement(self):
        self.api.correct_return_amount('unknown',self.ret['event_id'],expected_version=1,amount_minor=None)
        self.assertIsNone(self.net())
        self.assertFalse(self.api.spending()['complete'])
        self.api.correct_return_amount('known',self.ret['event_id'],expected_version=2,amount_minor=18000)
        self.assertEqual(self.net(),22000)

    def test_total_cap_includes_other_returns_atomic_rollback(self):
        item=self.api.inventory()[0]
        self.api.return_item('second',item['id'],quantity=1,
            expected_version=item['version'],amount_minor=20000)
        with self.assertRaises(ValueError):
            self.api.correct_return_amount('over',self.ret['event_id'],expected_version=1,amount_minor=21000)
        self.assertEqual(self.net(),0)
        self.assertEqual(self.api.return_events()[0]['version'],1)
        c=connect(self.api.path)
        try:
            self.assertEqual(c.execute("SELECT count(*) FROM operations WHERE request_key='over'").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT count(*) FROM item_events WHERE event_type='history_corrected'").fetchone()[0],0)
        finally:
            c.close()

    def test_wrong_currency_or_invalid_values(self):
        for amount in (True,-1,1.5):
            with self.assertRaises(ValueError):
                self.api.correct_return_amount('bad',self.ret['event_id'],expected_version=1,amount_minor=amount)
        for currency in ('USD','cny','???'):
            with self.assertRaises(ValueError):
                self.api.correct_return_amount('currency',self.ret['event_id'],
                    expected_version=1,amount_minor=18000,currency=currency)
        self.assertEqual(self.net(),20000)

    def test_missing_wrong_type_void_or_stale_record(self):
        c=connect(self.api.path)
        try:
            recorded=c.execute("SELECT id FROM item_events WHERE event_type='recorded'").fetchone()[0]
        finally:
            c.close()
        for event_id in ('missing',recorded):
            with self.assertRaises(ValueError):
                self.api.correct_return_amount('wrong',event_id,expected_version=1,amount_minor=0)
        self.api.undo('undo_return',self.ret['operation_id'])
        with self.assertRaises(ValueError):
            self.api.correct_return_amount('void',self.ret['event_id'],expected_version=2,amount_minor=0)
        self.assertEqual(self.net(),40000)

    def test_later_correction_prevents_old_undo(self):
        corrected=self.api.correct_return_amount('first',self.ret['event_id'],expected_version=1,amount_minor=18000)
        with self.assertRaises(ValueError):
            self.api.correct_return_amount('stale',self.ret['event_id'],expected_version=1,amount_minor=17000)
        self.api.correct_return_amount('second',self.ret['event_id'],expected_version=2,amount_minor=17000)
        with self.assertRaises(ValueError):
            self.api.undo('undo_first',corrected['operation_id'])
        with self.assertRaises(ValueError):
            self.api.undo('undo_return',self.ret['operation_id'])
        self.assertEqual(self.net(),23000)

    def test_zero_valid_and_filtered_read(self):
        self.api.correct_return_amount('zero',self.ret['event_id'],expected_version=1,amount_minor=0)
        self.assertEqual(self.net(),40000)
        self.assertEqual(len(self.api.return_events(purchase_id=self.buy['purchase_id'])),1)
        self.assertEqual(self.api.return_events(purchase_id='other'),[])
        self.assertEqual(sum(i['quantity'] for i in self.api.inventory()),1)

    def test_audit_does_not_count_as_money(self):
        result=self.api.correct_return_amount('correct',self.ret['event_id'],expected_version=1,amount_minor=18000)
        c=connect(self.api.path)
        try:
            row=c.execute("SELECT * FROM item_events WHERE operation_id=? AND event_type='history_corrected'",
                          (result['operation_id'],)).fetchone()
            import json
            self.assertEqual(json.loads(row['details_json'])['old_amount_minor'],20000)
            self.assertIsNone(row['amount_minor'])
            snapshots=c.execute('SELECT count(*) FROM operation_changes WHERE operation_id=?',
                                (result['operation_id'],)).fetchone()[0]
            self.assertEqual(snapshots,2)
        finally:
            c.close()

if __name__=='__main__':
    unittest.main(verbosity=2)

