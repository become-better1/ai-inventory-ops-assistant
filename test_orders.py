import tempfile
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from service import Assistant
from orders import PossibleOrderDuplicate, OrderConflict
from database import connect

class OrderChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.api=Assistant(Path(self.tmp.name)/'orders.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def buy(self,key='buy',**changes):
        data=dict(name='鼠标',quantity=1,total_amount_minor=20000,currency='CNY',
            merchant='商家A',external_order_id='ORDER1',external_line_id='LINE1',
            source_kind='order_image')
        data.update(changes)
        return self.api.record_purchase(key,**data)

    def test_reimport_new_key_does_not_duplicate(self):
        first=self.buy()
        second=self.buy('import_again')
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['purchase_id'],second['purchase_id'])
        self.assertEqual(len(self.api.inventory()),1)
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],20000)

    def test_same_order_different_lines_and_products(self):
        self.buy()
        result=self.buy('keyboard',name='键盘',external_line_id='LINE2')
        self.assertFalse(result['duplicate'])
        self.assertEqual(len(self.api.inventory()),2)

    def test_different_line_same_product_valid(self):
        self.buy()
        self.buy('another_line',external_line_id='LINE2')
        self.assertEqual(len(self.api.inventory()),2)

    def test_different_merchants_orders_are_distinct(self):
        self.buy()
        self.buy('merchant',merchant='商家B')
        self.buy('order',external_order_id='ORDER2')
        self.assertEqual(len(self.api.inventory()),3)

    def test_identity_conflict_cannot_be_overridden(self):
        first=self.buy()
        for changes in (dict(total_amount_minor=18000),dict(quantity=2),
                        dict(name='键盘'),dict(currency='USD'),dict(history_only=True)):
            with self.subTest(changes=changes):
                with self.assertRaises(OrderConflict) as ctx:
                    self.buy('conflict',confirmed_distinct=True,**changes)
                self.assertEqual(ctx.exception.purchase_id,first['purchase_id'])
        self.assertEqual(len(self.api.inventory()),1)

    def test_missing_line_ambiguous_then_explicit_distinct(self):
        first=self.buy(external_line_id=None)
        with self.assertRaises(PossibleOrderDuplicate) as ctx:
            self.buy('repeat',external_line_id=None)
        self.assertEqual(ctx.exception.candidates[0]['purchase_id'],first['purchase_id'])
        self.buy('confirmed',external_line_id=None,confirmed_distinct=True)
        self.assertEqual(len(self.api.inventory()),2)

    def test_partial_identity_cannot_silently_upgrade(self):
        self.buy(external_line_id=None)
        with self.assertRaises(PossibleOrderDuplicate):
            self.buy('better_screenshot')
        self.assertEqual(len(self.api.inventory()),1)

    def test_same_order_different_names_without_lines(self):
        self.buy(external_line_id=None)
        self.buy('keyboard',name='键盘',external_line_id=None)
        self.assertEqual(len(self.api.inventory()),2)

    def test_screenshot_without_order_reference_requires_clarification(self):
        self.buy(merchant=None,external_order_id=None,external_line_id=None)
        with self.assertRaises(PossibleOrderDuplicate):
            self.buy('repeat',merchant=None,external_order_id=None,external_line_id=None)
        self.assertEqual(len(self.api.inventory()),1)

    def test_explicit_new_purchase_without_order_remains_allowed(self):
        self.buy(merchant=None,external_order_id=None,external_line_id=None,source_kind='natural_language')
        self.buy('another',merchant=None,external_order_id=None,external_line_id=None,source_kind='natural_language')
        self.assertEqual(len(self.api.inventory()),2)

    def test_returned_order_reimport_does_not_restore_stock(self):
        first=self.buy()
        self.api.return_item('return',first['item_id'],quantity=1,expected_version=1,infer_amount=True)
        second=self.buy('reimport')
        self.assertTrue(second['duplicate'])
        self.assertEqual(self.api.inventory(),[])
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],0)

    def test_history_only_not_silently_added_to_inventory(self):
        first=self.buy(history_only=True)
        second=self.buy('repeat',history_only=True)
        self.assertTrue(second['duplicate'])
        self.assertEqual(self.api.inventory(),[])
        with self.assertRaises(OrderConflict):
            self.buy('change_mode')
        self.assertIsNone(first['item_id'])

    def test_invalid_inputs_no_partial_records(self):
        for changes in (dict(merchant=' '),dict(external_order_id=''),
                        dict(external_order_id=None),dict(external_line_id=1),
                        dict(source_kind='unknown'),dict(confirmed_distinct=1)):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.buy(**changes)
        self.assertEqual(self.api.inventory(),[])

    def test_normalized_merchant_and_trimmed_ids(self):
        first=self.buy(merchant=' Shop A ',external_order_id=' ORDER1 ',external_line_id=' LINE1 ')
        second=self.buy('again',merchant='shop a')
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['purchase_id'],first['purchase_id'])

    def test_duplicate_still_validates_incoming_dates(self):
        self.buy()
        for changes in (dict(purchase_date='2026-02-30',date_precision='day'),
                        dict(purchase_date='2026',date_precision='unknown'),
                        dict(purchase_date=None,date_precision='day')):
            with self.assertRaises(ValueError):
                self.buy('invalid_duplicate',**changes)
        self.assertEqual(len(self.api.inventory()),1)

    def test_simultaneous_imports_serialize_identity_match(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda key:self.buy(key),('parallel1','parallel2')))
        self.assertEqual(sum(not r['duplicate'] for r in results),1)
        self.assertEqual(len(self.api.inventory()),1)

    def test_undo_creation_allows_new_import(self):
        first=self.buy()
        self.api.undo('undo',first['operation_id'])
        second=self.buy('new_import')
        self.assertFalse(second['duplicate'])
        self.assertNotEqual(first['purchase_id'],second['purchase_id'])
        self.assertEqual(len(self.api.inventory()),1)

    def test_ambiguous_failure_rolls_back_operation(self):
        self.buy(external_line_id=None)
        with self.assertRaises(PossibleOrderDuplicate):
            self.buy('ambiguous',external_line_id=None)
        c=connect(self.api.path)
        try:
            self.assertEqual(c.execute("SELECT count(*) FROM operations WHERE request_key='ambiguous'").fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM purchase_records').fetchone()[0],1)
        finally:
            c.close()

if __name__=='__main__':
    unittest.main(verbosity=2)

