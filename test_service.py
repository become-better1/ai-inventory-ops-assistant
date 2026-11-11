import sqlite3
import tempfile
from pathlib import Path
import unittest
from service import Assistant
from database import connect

class ServiceChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.api=Assistant(Path(self.tmp.name)/'service.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def purchase(self, key='buy', **overrides):
        args=dict(name='G304',quantity=2,total_amount_minor=40000,currency='CNY',received=True)
        args.update(overrides)
        return self.api.record_purchase(key,**args)

    def net(self):
        return self.api.spending()['by_currency']['CNY']['net_minor']

    def test_full_flow(self):
        bought=self.purchase()
        changed=self.api.update_item('damage',bought['item_id'],expected_version=1,
            condition_status='damaged',usage_status='not_in_use')
        returned=self.api.return_item('return',bought['item_id'],quantity=1,
            expected_version=2,infer_amount=True)
        self.assertEqual(sum(i['quantity'] for i in self.api.inventory()),1)
        self.assertEqual(self.net(),20000)
        self.api.undo('undo',returned['operation_id'])
        self.assertEqual(sum(i['quantity'] for i in self.api.inventory()),2)
        self.assertEqual(self.net(),40000)
        item=self.api.inventory()[0]
        self.assertEqual(item['condition_status'],'damaged')
        self.assertEqual(item['usage_status'],'not_in_use')
        self.assertEqual(item['quantity'],2)
        r=self.api.return_item('return180',item['id'],quantity=1,
            expected_version=item['version'],amount_minor=18000)
        self.assertEqual(self.net(),22000)
        self.api.undo('undo180',r['operation_id'])
        self.assertEqual(self.net(),40000)

    def test_retries_and_key_conflicts(self):
        first=self.purchase()
        self.assertEqual(first,self.purchase())
        with self.assertRaises(ValueError):
            self.purchase(quantity=3)
        r=self.api.return_item('return',first['item_id'],quantity=2,
            expected_version=1,infer_amount=True)
        self.assertEqual(r,self.api.return_item('return',first['item_id'],quantity=2,
            expected_version=1,infer_amount=True))
        with self.assertRaises(ValueError):
            self.api.return_item('duplicate',first['item_id'],quantity=2,
                expected_version=2,infer_amount=True)
        self.assertEqual(self.net(),0)

    def test_failed_return_rolls_back_all_records(self):
        b=self.purchase()
        with self.assertRaises(ValueError):
            self.api.return_item('bad',b['item_id'],quantity=1,expected_version=1,amount_minor=50000)
        self.assertEqual(self.api.inventory()[0]['quantity'],2)
        self.assertEqual(self.net(),40000)
        c=connect(self.api.path)
        try:
            self.assertEqual(c.execute('SELECT count(*) FROM items').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],1)
        finally:
            c.close()

    def test_unknown_amount_not_reported_as_final(self):
        b=self.purchase()
        self.api.return_item('return',b['item_id'],quantity=1,expected_version=1)
        report=self.api.spending()
        self.assertFalse(report['complete'])
        self.assertIsNone(report['by_currency']['CNY']['net_minor'])
        self.assertEqual(report['by_currency']['CNY']['unknown_amount_count'],1)

    def test_history_only_and_undo_insert(self):
        b=self.purchase(history_only=True)
        self.assertEqual(self.api.inventory(),[])
        self.assertEqual(self.net(),40000)
        self.api.undo('undo',b['operation_id'])
        self.assertEqual(self.api.spending()['by_currency'],{})

    def test_undo_purchase_new_reference_blocked(self):
        b=self.purchase()
        self.api.update_item('edit',b['item_id'],expected_version=1,feedback='too heavy')
        with self.assertRaises(ValueError):
            self.api.undo('undo',b['operation_id'])
        self.assertEqual(len(self.api.inventory()),1)

    def test_undo_later_edit_conflict(self):
        b=self.purchase()
        r=self.api.return_item('return',b['item_id'],quantity=2,
            expected_version=1,infer_amount=True)
        self.api.update_item('later',b['item_id'],expected_version=2,feedback='corrected')
        with self.assertRaises(ValueError):
            self.api.undo('undo',r['operation_id'])
        self.assertEqual(self.net(),0)

    def test_undo_only_once_and_retry(self):
        b=self.purchase()
        first=self.api.undo('undo',b['operation_id'])
        self.assertEqual(first,self.api.undo('undo',b['operation_id']))
        with self.assertRaises((ValueError,sqlite3.IntegrityError)):
            self.api.undo('again',b['operation_id'])
        with self.assertRaises(ValueError):
            self.api.undo('undo_undo',first['operation_id'])

    def test_rounding_remainder(self):
        b=self.purchase(quantity=3,total_amount_minor=100)
        for n,expected in [(1,67),(2,34),(3,0)]:
            item=self.api.inventory()[0]
            self.api.return_item('r'+str(n),item['id'],quantity=1,
                expected_version=item['version'],infer_amount=True)
            self.assertEqual(self.net(),expected)

    def test_separate_currencies(self):
        self.purchase()
        self.purchase('usd',quantity=1,total_amount_minor=1000,currency='USD')
        report=self.api.spending()['by_currency']
        self.assertEqual(report['CNY']['net_minor'],40000)
        self.assertEqual(report['USD']['net_minor'],1000)

    def test_validation_and_unknown_purchase(self):
        for bad in (True,0,-1,1.5):
            with self.subTest(quantity=bad):
                with self.assertRaises(ValueError):
                    self.purchase(quantity=bad)
        self.purchase(total_amount_minor=None,currency=None)
        self.assertFalse(self.api.spending()['complete'])
        self.assertEqual(self.api.spending()['unknown_currency_count'],1)

    def test_stale_version_and_unrelated_read(self):
        b=self.purchase()
        self.api.update_item('edit',b['item_id'],expected_version=1,feedback='good')
        with self.assertRaises(ValueError):
            self.api.update_item('stale',b['item_id'],expected_version=1,feedback='bad')
        self.assertEqual(self.api.inventory()[0]['feedback'],'good')

    def test_undo_purchase_and_plain_edit(self):
        b=self.purchase()
        edit=self.api.update_item('edit',b['item_id'],expected_version=1,feedback='good')
        self.api.undo('undo_edit',edit['operation_id'])
        self.assertIsNone(self.api.inventory()[0]['feedback'])
        # Whole-row versions intentionally prevent undoing older operations after later edits.
        with self.assertRaises(ValueError):
            self.api.undo('undo_buy',b['operation_id'])

    def test_clean_purchase_undo(self):
        b=self.purchase()
        self.api.undo('undo',b['operation_id'])
        self.assertEqual(self.api.inventory(),[])
        self.assertEqual(self.api.spending()['by_currency'],{})

if __name__=='__main__':
    unittest.main(verbosity=2)

