"""Existing-possession tests call the real API with isolated temporary databases."""
import tempfile
from pathlib import Path
import unittest
from database import connect
from service import Assistant, PossibleDuplicate

class OwnedChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = Assistant(Path(self.tmp.name)/'owned.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def count(self, table):
        c = connect(self.api.path)
        try:
            return c.execute('SELECT count(*) FROM '+table).fetchone()[0]
        finally:
            c.close()

    def test_owned_not_a_purchase(self):
        result = self.api.record_owned('owned',name='旧电脑')
        item = self.api.inventory()[0]
        self.assertEqual(item['id'],result['item_id'])
        self.assertIsNone(item['purchase_record_id'])
        self.assertIsNone(result['purchase_id'])
        self.assertEqual(self.count('purchase_records'),0)
        self.assertEqual(self.api.spending()['by_currency'],{})
        self.assertEqual((item['usage_status'],item['condition_status']),('unknown','unknown'))
        self.assertIsNone(item['room_id'])

    def test_attributes_and_quantity(self):
        self.api.record_owned('owned',name='鼠标',quantity=3,unit='只',
            brand='罗技',model='G304',usage_status='not_in_use',
            condition_status='damaged',location_detail='抽屉',feedback='偏重')
        item = self.api.inventory()[0]
        self.assertEqual((item['quantity'],item['unit'],item['model']),(3,'只','G304'))
        self.assertEqual(item['condition_status'],'damaged')
        self.assertEqual(item['feedback'],'偏重')

    def test_retry_key_conflict_and_undo(self):
        result = self.api.record_owned('owned',name='旧电脑')
        self.assertEqual(result,self.api.record_owned('owned',name='旧电脑'))
        with self.assertRaises(ValueError):
            self.api.record_owned('owned',name='旧电脑',quantity=2)
        self.assertEqual(len(self.api.inventory()),1)
        undo = self.api.undo('undo',result['operation_id'])
        self.assertEqual(self.api.inventory(),[])
        self.assertEqual(self.count('purchase_records'),0)
        self.assertEqual(undo,self.api.undo('undo',result['operation_id']))

    def test_repeat_statement_requires_identity_resolution(self):
        result = self.api.record_owned('owned',name='旧电脑')
        with self.assertRaises(PossibleDuplicate) as ctx:
            self.api.record_owned('repeat',name=' 旧电脑 ',quantity=3)
        self.assertEqual(ctx.exception.candidates[0]['item_id'],result['item_id'])
        self.assertEqual(self.count('operations'),1)
        self.assertEqual(self.count('item_events'),1)
        self.assertEqual(self.api.inventory()[0]['quantity'],1)

    def test_matching_purchased_item_and_alias_model(self):
        self.api.record_purchase('buy',name='G304',quantity=1,
            total_amount_minor=20000,currency='CNY')
        with self.assertRaises(PossibleDuplicate):
            self.api.record_owned('mention',name='g304')
        self.api.record_owned('model',name='罗技鼠标',brand='罗技',model='G305')
        with self.assertRaises(PossibleDuplicate):
            self.api.record_owned('alias',name='无线鼠标',brand='罗技',model='g305')

    def test_distinct_addition_keeps_history_separate(self):
        self.api.record_purchase('buy',name='鼠标',quantity=1,
            total_amount_minor=20000,currency='CNY')
        result = self.api.record_owned('another',name='鼠标',quantity=2,confirmed_distinct=True)
        self.assertEqual(sum(i['quantity'] for i in self.api.inventory()),3)
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],20000)
        self.api.undo('undo',result['operation_id'])
        self.assertEqual(sum(i['quantity'] for i in self.api.inventory()),1)
        self.assertEqual(self.count('purchase_records'),1)

    def test_invalid_inputs_no_writes(self):
        invalid = [dict(quantity=True),dict(quantity=0),dict(quantity=-1),
            dict(quantity=1.5),dict(name=' '),dict(unit=''),
            dict(usage_status='daily'),dict(condition_status='probably broken'),
            dict(confirmed_distinct=1),dict(brand=123)]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                params=dict(name='电脑')
                params.update(overrides)
                with self.assertRaises(ValueError):
                    self.api.record_owned('bad',**params)
        self.assertEqual(self.count('operations'),0)
        self.assertEqual(self.api.inventory(),[])

    def test_later_edit_blocks_undo(self):
        owned = self.api.record_owned('owned',name='旧电脑')
        self.api.update_item('edit',owned['item_id'],expected_version=1,usage_status='in_use')
        with self.assertRaises(ValueError):
            self.api.undo('undo',owned['operation_id'])
        self.assertEqual(self.api.inventory()[0]['usage_status'],'in_use')

if __name__=='__main__':
    unittest.main(verbosity=2)

