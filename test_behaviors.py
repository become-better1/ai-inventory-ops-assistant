"""Behavior persistence invariants, using isolated temporary databases only."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from service import Assistant
from database import connect
from maintenance import snapshot, restore


class BehaviorChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.api = Assistant(self.root/'test.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def flow(self, key='flow'):
        return self.api.create_routine_flow(key, flow=dict(name='晚间', content='按实际阶段安排', weekdays=[1,2]),
                stages=[dict(name='等饭', content='做家务'),dict(name='饭后', content='散步')], confirmed_distinct=key != 'flow')

    def rule(self, key='rule', **kwargs):
        values = dict(content='  睡前想看手机先等五分钟  ', kind='avoid', behavior='躺床上看手机',
                      alternative='听歌五分钟', reason='自己的提醒')
        values.update(kwargs)
        return self.api.create_behavior_rule(key, rule=values)

    def test_rule_lifecycle_retry_conflict_and_undo(self):
        result = self.rule()
        self.assertEqual(result, self.rule())
        row = self.api.behavior_rules()[0]
        self.assertEqual(row['content'], '  睡前想看手机先等五分钟  ')
        self.assertIsNone(row['flow_id'])
        with self.assertRaises(ValueError):
            self.rule(content='同键新内容')
        off = self.api.update_behavior_rule('off', row['id'], expected_version=1,
                    changes=dict(is_active=False, inactive_reason='先暂停'))
        self.assertEqual(self.api.behavior_rules(active_only=True), [])
        self.api.undo('undo-off', off['operation_id'])
        self.assertEqual(self.api.behavior_rules()[0]['version'], 3)
        deleted = self.api.delete_behavior_rule('del', row['id'], expected_version=3)
        self.assertEqual(self.api.behavior_rules(), [])
        self.api.undo('undo-del', deleted['operation_id'])
        self.assertEqual(len(self.api.behavior_rules(active_only=True)), 1)
        with self.assertRaises(ValueError):
            self.api.update_behavior_rule('stale',row['id'],expected_version=1,changes=dict(behavior='新动作'))
        with self.assertRaises(ValueError):
            self.api.undo('stale-undo', result['operation_id'])

    def test_scope_and_activation_propagate_without_rewriting_rules(self):
        f = self.flow()
        self.rule(flow_id=f['flow_id'], stage_id=f['stage_ids'][0])
        original = self.api.behavior_rules()[0]
        changed = self.api.update_routine_flow('off', f['flow_id'],expected_version=1,changes=dict(is_active=False))
        self.assertEqual(self.api.behavior_rules(active_only=True), [])
        self.assertEqual(self.api.behavior_rules()[0], original)
        self.api.undo('on',changed['operation_id'])
        paused = self.api.update_routine_stage('pause',f['stage_ids'][0],expected_version=1,changes=dict(is_active=False))
        self.assertEqual(self.api.behavior_rules(active_only=True), [])
        self.api.undo('resume',paused['operation_id'])
        self.assertEqual(len(self.api.behavior_rules(active_only=True)), 1)

    def test_bad_associations_roll_back_operations_and_undo_new_parent(self):
        a, b = self.flow(), self.flow('other')
        with self.assertRaises(ValueError):
            self.rule(flow_id=b['flow_id'],stage_id=a['stage_ids'][0])
        self.assertEqual(self.api.behavior_rules(), [])
        self.rule(flow_id=a['flow_id'],stage_id=a['stage_ids'][0])
        with self.assertRaises(ValueError):
            self.api.undo('undo-parent', a['operation_id'])
        with self.assertRaises(ValueError):
            self.api.delete_routine_stage('remove',a['stage_ids'][0],expected_version=1)
        self.assertEqual(len(self.api.routine_stages(a['flow_id'])['stages']), 2)
        with closing(connect(self.api.path)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],3)

    def test_reorder_insert_delete_and_undo_keep_unique_positions(self):
        f = self.flow()
        swapped = self.api.reorder_routine_stages('swap',f['flow_id'],expected_flow_version=1,
                                                 ordered_stage_ids=list(reversed(f['stage_ids'])))
        self.assertEqual([r['id'] for r in self.api.routine_stages(f['flow_id'])['stages']],list(reversed(f['stage_ids'])))
        self.api.undo('undo-swap',swapped['operation_id'])
        rows = self.api.routine_stages(f['flow_id'])
        self.assertEqual([r['id'] for r in rows['stages']],f['stage_ids'])
        added = self.api.insert_routine_stage('insert',f['flow_id'],expected_flow_version=3,
                    stage=dict(name='洗澡',content='换衣服'),before_stage_id=f['stage_ids'][0])
        self.assertEqual([r['position'] for r in self.api.routine_stages(f['flow_id'])['stages']],[1,2,3])
        self.api.undo('undo-insert',added['operation_id'])
        rows = self.api.routine_stages(f['flow_id'])['stages']
        deleted = self.api.delete_routine_stage('delete',rows[0]['id'],expected_version=rows[0]['version'])
        self.assertEqual(self.api.routine_stages(f['flow_id'])['stages'][0]['position'],1)
        self.api.undo('undo-delete',deleted['operation_id'])
        self.assertEqual([r['id'] for r in self.api.routine_stages(f['flow_id'])['stages']],f['stage_ids'])

    def test_reorder_rejects_missing_duplicate_and_stale_ids(self):
        f = self.flow()
        for ids in [f['stage_ids'][:1], [f['stage_ids'][0]]*2]:
            with self.assertRaises(ValueError):
                self.api.reorder_routine_stages('bad', f['flow_id'],expected_flow_version=1,ordered_stage_ids=ids)
        self.api.update_routine_flow('edit',f['flow_id'],expected_version=1,changes=dict(name='新名字'))
        with self.assertRaises(ValueError):
            self.api.reorder_routine_stages('stale', f['flow_id'],expected_flow_version=1,ordered_stage_ids=f['stage_ids'])

    def test_empty_flow_delete_and_restore(self):
        f = self.api.create_routine_flow('empty',flow=dict(name='临时流程',content='原文'))
        removed = self.api.delete_routine_flow('delete',f['flow_id'],expected_version=1)
        self.assertEqual(self.api.routine_flows(), [])
        self.api.undo('restore',removed['operation_id'])
        self.assertEqual(len(self.api.routine_flows()),1)

    def test_conditions_and_invalid_inputs_are_atomic(self):
        invalid = [dict(conditions={'states':'很累'}),dict(conditions={'weekdays':[True]}),
            dict(conditions={'weekdays':[1,1]}),dict(conditions={'extra':[]}),
            dict(conditions={'time_windows':[{'start':'25:00','end':'08:00'}]}),
            dict(conditions={'time_windows':[{'start':'08:00','end':'08:00'}]}),
            dict(is_active=1),dict(stage_id='unknown'),dict(kind='bad'),dict(reason=123)]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.rule(**value)
        self.assertEqual(self.api.behavior_rules(), [])
        result = self.rule(conditions={'time_windows':[{'start':'22:00','end':'08:00'}]})
        self.assertEqual(self.api.behavior_rules()[0]['conditions']['time_windows'][0]['start'],'22:00')
        with self.assertRaises(ValueError):
            self.rule('duplicate',conditions={'time_windows':[{'start':'22:00','end':'08:00'}]})
        distinct = self.api.create_behavior_rule('distinct',rule=dict(content='  睡前想看手机先等五分钟  ',
              kind='avoid',behavior='其他说明',conditions={'time_windows':[{'start':'22:00','end':'08:00'}]}),confirmed_distinct=True)
        self.assertNotEqual(result['rule_id'],distinct['rule_id'])

    def test_edit_resets_interpretation_and_backup_preserves_new_undo(self):
        r = self.rule(interpretation='原解释')
        edited = self.api.update_behavior_rule('edit',r['rule_id'],expected_version=1,changes=dict(behavior='新行动'))
        self.assertIsNone(self.api.behavior_rules()[0]['interpretation'])
        saved = snapshot(self.api.path,self.root/'backups')
        new = self.root/'restored.sqlite3'
        restore(saved['backup'],new,confirmed=True)
        restored = Assistant(new)
        restored.undo('undo-restored',edited['operation_id'])
        self.assertEqual(restored.behavior_rules()[0]['interpretation'],'原解释')


if __name__ == '__main__':
    unittest.main()
