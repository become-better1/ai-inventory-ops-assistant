import tempfile
from pathlib import Path
import unittest
from database import connect
from service import Assistant

class QuantityChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.api=Assistant(Path(self.tmp.name)/'quantities.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def purchase(self):
        return self.api.record_purchase('buy',name='鼠标',quantity=2,
            total_amount_minor=40000,currency='CNY')

    def total(self):
        return sum(r['quantity'] for r in self.api.inventory())

    def preview(self,total,**kwargs):
        return self.api.preview_quantity_correction(name='鼠标',target_total=total,**kwargs)

    def test_increase_total_not_add_target_and_undo(self):
        self.purchase()
        result=self.api.correct_quantity('correct',plan=self.preview(3))
        self.assertEqual(self.total(),3)
        unknown=[i for i in self.api.inventory() if i['purchase_record_id'] is None]
        self.assertEqual(len(unknown),1)
        self.assertEqual(unknown[0]['quantity'],1)
        self.assertEqual(unknown[0]['usage_status'],'unknown')
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],40000)
        self.api.undo('undo',result['operation_id'])
        self.assertEqual(self.total(),2)

    def test_decrease_splits_preserves_source_amount_and_undo(self):
        b=self.purchase()
        result=self.api.correct_quantity('correct',plan=self.preview(1))
        self.assertEqual(self.total(),1)
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],40000)
        c=connect(self.api.path)
        try:
            rows=c.execute('SELECT * FROM items WHERE is_void=0').fetchall()
            self.assertEqual(sum(i['quantity'] for i in rows),2)
            removed=next(i for i in rows if i['ownership_status']=='count_corrected')
            self.assertEqual(removed['purchase_record_id'],b['purchase_id'])
            self.assertEqual(removed['quantity'],1)
            self.assertEqual(c.execute('SELECT quantity FROM purchase_records').fetchone()[0],2)
        finally:
            c.close()
        self.api.undo('undo',result['operation_id'])
        self.assertEqual(self.total(),2)
        self.assertEqual(len(self.api.inventory()),1)

    def test_multi_group_decrease_requires_specific_allocation(self):
        b=self.purchase()
        extra=self.api.record_owned('extra',name='鼠标',confirmed_distinct=True)
        plan=self.preview(2)
        self.assertTrue(plan['needs_allocation'])
        with self.assertRaises(ValueError):
            self.api.correct_quantity('ambiguous',plan=plan)
        self.assertEqual(self.total(),3)
        explicit=self.preview(2,removals={extra['item_id']:1})
        result=self.api.correct_quantity('correct',plan=explicit)
        self.assertEqual(self.total(),2)
        self.assertEqual(self.api.inventory()[0]['id'],b['item_id'])
        self.api.undo('undo',result['operation_id'])
        self.assertEqual(self.total(),3)

    def test_batch_zero_requires_confirmation_and_undo(self):
        self.purchase()
        self.api.record_owned('extra',name='鼠标',confirmed_distinct=True)
        plan=self.preview(0)
        self.assertTrue(plan['requires_confirmation'])
        with self.assertRaises(ValueError):
            self.api.correct_quantity('clear',plan=plan)
        result=self.api.correct_quantity('clear',plan=plan,confirmed=True)
        self.assertEqual(self.total(),0)
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],40000)
        self.api.undo('undo',result['operation_id'])
        self.assertEqual(self.total(),3)

    def test_single_group_zero_keeps_positive_history_quantity(self):
        self.purchase()
        result=self.api.correct_quantity('clear',plan=self.preview(0))
        self.assertEqual(self.total(),0)
        c=connect(self.api.path)
        try:
            row=c.execute('SELECT * FROM items').fetchone()
            self.assertEqual(row['quantity'],2)
            self.assertEqual(row['ownership_status'],'count_corrected')
        finally:
            c.close()
        self.api.undo('undo',result['operation_id'])
        self.assertEqual(self.total(),2)

    def test_new_record_scope_and_unit_separation(self):
        self.api.record_owned('pack',name='鼠标',quantity=2,unit='盒')
        plan=self.preview(3)
        self.assertEqual(plan['current_total'],0)
        self.api.correct_quantity('new',plan=plan)
        rows=self.api.inventory()
        self.assertEqual({r['unit']:r['quantity'] for r in rows},{'件':3,'盒':2})
        self.assertEqual(self.api.spending()['by_currency'],{})

    def test_retry_and_conflicting_key(self):
        self.purchase()
        plan=self.preview(3)
        result=self.api.correct_quantity('correct',plan=plan)
        self.assertEqual(result,self.api.correct_quantity('correct',plan=plan))
        self.assertEqual(self.total(),3)
        with self.assertRaises(ValueError):
            self.api.correct_quantity('correct',plan=self.preview(4))

    def test_new_group_invalidates_old_preview(self):
        self.purchase()
        plan=self.preview(3)
        self.api.record_owned('extra',name='鼠标',confirmed_distinct=True)
        with self.assertRaises(ValueError):
            self.api.correct_quantity('stale',plan=plan)
        self.assertEqual(self.total(),3)

    def test_modified_group_invalidates_preview(self):
        b=self.purchase()
        plan=self.preview(1)
        self.api.update_item('edit',b['item_id'],expected_version=1,condition_status='damaged')
        with self.assertRaises(ValueError):
            self.api.correct_quantity('stale',plan=plan)
        self.assertEqual(self.total(),2)

    def test_exited_items_not_in_total(self):
        b=self.purchase()
        self.api.return_item('return',b['item_id'],quantity=1,expected_version=1,infer_amount=True)
        self.assertEqual(self.preview(2)['current_total'],1)
        self.api.correct_quantity('correct',plan=self.preview(2))
        self.assertEqual(self.total(),2)
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],20000)

    def test_invalid_counts_and_allocations_do_not_write(self):
        b=self.purchase()
        for total in (True,-1,1.5):
            with self.assertRaises(ValueError):
                self.preview(total)
        for removals in ({'missing':1},{b['item_id']:3},{b['item_id']:True},{b['item_id']:2},[]):
            with self.subTest(removals=removals):
                with self.assertRaises(ValueError):
                    self.preview(1,removals=removals)
        with self.assertRaises(ValueError):
            self.preview(3,removals={b['item_id']:1})
        self.assertEqual(self.total(),2)

    def test_unchanged_total_no_write(self):
        self.purchase()
        plan=self.preview(2)
        self.assertTrue(plan['unchanged'])
        with self.assertRaises(ValueError):
            self.api.correct_quantity('nochange',plan=plan)
        c=connect(self.api.path)
        try:
            self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],1)
        finally:
            c.close()

    def test_later_changes_block_undo(self):
        self.purchase()
        corrected=self.api.correct_quantity('correct',plan=self.preview(3))
        new=next(i for i in self.api.inventory() if i['purchase_record_id'] is None)
        self.api.update_item('edit',new['id'],expected_version=new['version'],feedback='good')
        with self.assertRaises(ValueError):
            self.api.undo('undo',corrected['operation_id'])
        self.assertEqual(self.total(),3)

if __name__=='__main__':
    unittest.main(verbosity=2)

