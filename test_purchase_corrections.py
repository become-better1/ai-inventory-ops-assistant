import tempfile
from pathlib import Path
import unittest
from service import Assistant

class PurchaseCorrectionChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.api=Assistant(Path(self.tmp.name)/'purchase.sqlite3')
        self.p=self.api.record_purchase('buy',name='雨伞',quantity=1)

    def tearDown(self):
        self.tmp.cleanup()

    def correct(self,**changes):
        args=dict(expected_version=1,total_amount_minor=2300,currency='CNY')
        args.update(changes)
        return self.api.correct_purchase_amount('correct',self.p['purchase_id'],**args)

    def test_fill_unknown_and_undo_without_inventory_change(self):
        before=self.api.inventory()
        r=self.correct()
        self.assertEqual(self.api.inventory(),before)
        self.assertEqual(len(self.api.purchase_records()),1)
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],2300)
        self.api.undo('undo',r['operation_id'])
        p=self.api.purchase_records()[0]
        self.assertIsNone(p['total_amount_minor'])
        self.assertIsNone(p['currency'])
        self.assertEqual(self.api.inventory(),before)

    def test_retry_and_stale_key(self):
        r=self.correct()
        self.assertEqual(r,self.correct())
        with self.assertRaises(ValueError):
            self.correct(total_amount_minor=2500)
        with self.assertRaises(ValueError):
            self.api.correct_purchase_amount('stale',self.p['purchase_id'],
                expected_version=1,total_amount_minor=2500)

    def test_known_currency_cannot_change(self):
        self.correct()
        with self.assertRaises(ValueError):
            self.api.correct_purchase_amount('wrong',self.p['purchase_id'],
                expected_version=2,total_amount_minor=2300,currency='USD')

    def test_reject_below_existing_return_deduction(self):
        self.correct()
        self.api.return_item('return',self.p['item_id'],quantity=1,expected_version=1,amount_minor=2000)
        with self.assertRaises(ValueError):
            self.api.correct_purchase_amount('too_low',self.p['purchase_id'],
                expected_version=2,total_amount_minor=1800)
        self.assertEqual(self.api.purchase_records()[0]['total_amount_minor'],2300)

    def test_invalid_and_removed_records(self):
        for amount in (True,-1,1.5):
            with self.assertRaises(ValueError):
                self.correct(total_amount_minor=amount)
        with self.assertRaises(ValueError):
            self.correct(currency=None)
        self.api.undo('undo_buy',self.p['operation_id'])
        with self.assertRaises(ValueError):
            self.correct(expected_version=2)

    def test_history_only_and_zero_amount(self):
        other=self.api.record_purchase('history',name='旧购买',quantity=1,history_only=True)
        self.api.correct_purchase_amount('free',other['purchase_id'],expected_version=1,
            total_amount_minor=0,currency='CNY')
        self.assertEqual(len(self.api.inventory()),1)
        self.assertEqual(self.api.purchase_records(purchase_id=other['purchase_id'])[0]['total_amount_minor'],0)

if __name__=='__main__':
    unittest.main(verbosity=2)

