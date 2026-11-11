import tempfile
from pathlib import Path
import unittest
from service import Assistant
from database import connect

class RuleChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.api=Assistant(Path(self.tmp.name)/'rules.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def create(self,**kwargs):
        args=dict(content='超过 500 元先考虑三天。',scope={'min_amount_minor':50000,'currency':'CNY'},
                  interpretation='适用于单次超过 500 元的购买。')
        args.update(kwargs)
        return self.api.create_rule('create',**args)

    def test_create_preserves_original_and_unknown_scope(self):
        raw='  买电子产品之前，先看看家里有没有能用的。  '
        result=self.api.create_rule('create',content=raw)
        rule=self.api.rules()[0]
        self.assertEqual(rule['id'],result['rule_id'])
        self.assertEqual(rule['content'],raw)
        self.assertEqual(rule['scope'],{})
        self.assertIsNone(rule['interpretation'])

    def test_retry_and_content_conflict(self):
        result=self.create()
        self.assertEqual(result,self.create())
        with self.assertRaises(ValueError):
            self.create(content='另一条准则')
        self.assertEqual(len(self.api.rules()),1)

    def test_duplicate_new_request_rejected(self):
        self.create()
        with self.assertRaises(ValueError):
            self.api.create_rule('repeat',content=' 超过500元先考虑三天。 ')
        self.assertEqual(len(self.api.rules()),1)

    def test_content_change_clears_old_interpretation_and_undo(self):
        result=self.create()
        edited=self.api.update_rule('edit',result['rule_id'],expected_version=1,
                                    content='超过 1000 元先考虑一周。')
        rule=self.api.rules()[0]
        self.assertEqual(rule['scope'],{})
        self.assertIsNone(rule['interpretation'])
        self.api.undo('undo',edited['operation_id'])
        rule=self.api.rules()[0]
        self.assertEqual(rule['content'],'超过 500 元先考虑三天。')
        self.assertEqual(rule['scope']['min_amount_minor'],50000)

    def test_content_and_new_scope_together(self):
        result=self.create()
        self.api.update_rule('edit',result['rule_id'],expected_version=1,
            content='只对电子产品考虑三天。',scope={'category':'电子产品'},
            interpretation='仅电子产品')
        self.assertEqual(self.api.rules()[0]['scope'],{'category':'电子产品'})

    def test_disable_enable_and_filters(self):
        result=self.create()
        disabled=self.api.update_rule('off',result['rule_id'],expected_version=1,is_active=False)
        self.assertEqual(len(self.api.rules()),1)
        self.assertEqual(self.api.rules(active_only=True),[])
        self.api.undo('undo',disabled['operation_id'])
        self.assertEqual(len(self.api.rules(active_only=True)),1)
        self.api.update_rule('on',result['rule_id'],expected_version=3,is_active=True)
        self.assertEqual(len(self.api.rules(active_only=True)),1)

    def test_delete_and_restore(self):
        result=self.create()
        deleted=self.api.delete_rule('delete',result['rule_id'],expected_version=1)
        self.assertEqual(self.api.rules(),[])
        self.assertEqual(deleted,self.api.delete_rule('delete',result['rule_id'],expected_version=1))
        self.api.undo('undo',deleted['operation_id'])
        self.assertEqual(len(self.api.rules()),1)

    def test_undo_creation_and_stale_edits(self):
        result=self.create()
        self.api.undo('undo',result['operation_id'])
        self.assertEqual(self.api.rules(),[])
        with self.assertRaises(ValueError):
            self.api.update_rule('edit',result['rule_id'],expected_version=2,content='新内容')
        with self.assertRaises(ValueError):
            self.api.delete_rule('delete',result['rule_id'],expected_version=2)

    def test_later_edit_conflict_no_overwrite(self):
        result=self.create()
        edited=self.api.update_rule('edit',result['rule_id'],expected_version=1,is_active=False)
        with self.assertRaises(ValueError):
            self.api.update_rule('stale',result['rule_id'],expected_version=1,content='错误覆盖')
        self.api.update_rule('later',result['rule_id'],expected_version=2,content='后来修改')
        with self.assertRaises(ValueError):
            self.api.undo('undo',edited['operation_id'])
        self.assertEqual(self.api.rules()[0]['content'],'后来修改')

    def test_invalid_values_leave_database_empty(self):
        invalid=[dict(content=' '),dict(scope=[]),dict(scope={1:'bad'}),
            dict(scope={'bad':float('nan')}),dict(scope={'bad':(1,2)}),
            dict(interpretation=123)]
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    self.create(**values)
        c=connect(self.api.path)
        try:
            self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],0)
        finally:
            c.close()

    def test_unknown_fields_and_invalid_flags(self):
        result=self.create()
        for changes in ({'is_active':1},{'scope':None},{'is_void':1},{}):
            with self.assertRaises(ValueError):
                self.api.update_rule('bad',result['rule_id'],expected_version=1,**changes)
        with self.assertRaises(ValueError):
            self.api.rules(active_only=1)

    def test_rules_do_not_block_purchases_or_change_on_exception(self):
        self.create()
        original=self.api.rules()[0]
        self.api.record_purchase('buy',name='临时需要的设备',quantity=1,
            total_amount_minor=80000,currency='CNY')
        self.assertEqual(self.api.rules()[0],original)
        self.assertEqual(self.api.spending()['by_currency']['CNY']['net_minor'],80000)

    def test_restore_conflicting_replacement_rolls_back(self):
        original=self.create()
        deleted=self.api.delete_rule('delete',original['rule_id'],expected_version=1)
        replacement=self.api.create_rule('replacement',content='超过 500 元先考虑三天。')
        with self.assertRaises(ValueError):
            self.api.undo('restore',deleted['operation_id'])
        self.assertEqual(len(self.api.rules()),1)
        self.assertEqual(self.api.rules()[0]['id'],replacement['rule_id'])

if __name__=='__main__':
    unittest.main(verbosity=2)

