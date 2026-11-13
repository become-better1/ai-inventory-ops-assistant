"""Atomic plans through the same adapter used by MCP; isolated temporary DBs."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import behavior_plans
from database import connect
from mcp_server import ToolAdapter


class BehaviorPlanChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'plans.sqlite3'
        self.adapter=ToolAdapter(self.path)
        self.api=self.adapter.api

    def tearDown(self):
        self.tmp.cleanup()

    def call(self,name,**params):
        return self.adapter.invoke(name,self.adapter.models[name].model_validate(params))

    def preview(self,actions):
        response=self.call('preview_behavior_plan',actions=actions)
        self.assertTrue(response['ok'],response)
        return response['data']

    def apply(self,preview,key='apply',confirmed=True):
        return self.call('apply_behavior_plan',request_key=key,preview_id=preview['preview_id'],confirmed=confirmed)

    def imported(self):
        return [dict(type='create_flow',client_ref='evening',flow=dict(name='晚间',content='晚间流程'),stages=[
            dict(client_ref='wait',name='等饭',content='安排等饭'),dict(client_ref='after',name='饭后',content='安排饭后')]),
            dict(type='create_rule',client_ref='housework',flow_ref='evening',stage_ref='wait',
                 rule=dict(content='等饭做家务',kind='action',behavior='做家务',source_kind='note_import',source_label='导入示例')),
            dict(type='create_rule',client_ref='paused',flow_ref='evening',stage_ref='wait',
                 rule=dict(content='先不跳绳',kind='action',behavior='跳绳',is_active=False,inactive_reason='先暂停'))]

    def seed(self):
        p=self.preview(self.imported())
        result=self.apply(p)
        self.assertTrue(result['ok'],result)
        return result['data']['new_refs']

    def count_operations(self):
        with closing(connect(self.path)) as c:
            return c.execute('SELECT count(*) FROM operations').fetchone()[0]

    def test_import_preview_no_writes_atomic_apply_and_retry_after_restart(self):
        p=self.preview(self.imported())
        self.assertTrue(p['requires_confirmation'])
        self.assertNotIn('database',p)
        self.assertNotIn('allocated_ids',p)
        self.assertEqual(self.api.routine_flows(),[])
        self.assertEqual(self.api.behavior_rules(),[])
        self.assertEqual(self.count_operations(),0)
        result=self.apply(p)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['data']['new_refs'],p['new_refs'])
        self.assertEqual(self.count_operations(),1)
        self.assertEqual(len(self.api.behavior_rules()),2)
        self.assertEqual(len(self.api.behavior_rules(active_only=True)),1)
        self.adapter=ToolAdapter(self.path)  # no preview survives a process restart
        self.assertEqual(self.apply(p),result)
        changed=self.apply(p,confirmed=False)
        self.assertEqual(changed['error']['code'],'KEY_CONFLICT')
        self.api.undo('undo-import',result['data']['operation_id'])
        self.assertEqual(self.api.routine_flows(),[])
        self.assertEqual(self.api.behavior_rules(),[])

    def test_confirmation_required_then_expired_uncommitted_plan(self):
        p=self.preview(self.imported())
        denied=self.apply(p,confirmed=False)
        self.assertEqual(denied['error']['code'],'CONFIRMATION_REQUIRED')
        self.assertEqual(self.count_operations(),0)
        self.adapter.previews.ttl=0
        expired=self.apply(p)
        self.assertEqual(expired['error']['code'],'PREVIEW_EXPIRED')
        self.assertEqual(self.count_operations(),0)

    def test_single_stage_edit_no_extra_confirmation_and_undo(self):
        refs=self.seed()
        p=self.preview([dict(type='edit_stage',stage_id=refs['wait']['id'],expected_version=1,changes={'name':'等晚饭'})])
        self.assertFalse(p['requires_confirmation'])
        result=self.apply(p,key='edit',confirmed=False)
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.api.routine_stages(refs['evening']['id'])['stages'][0]['name'],'等晚饭')
        self.api.undo('undo-edit',result['data']['operation_id'])
        self.assertEqual(self.api.routine_stages(refs['evening']['id'])['stages'][0]['name'],'等饭')

    def test_reorder_and_insert_undo_preserve_unique_positions(self):
        refs=self.seed()
        fid=refs['evening']['id']
        original=[refs['wait']['id'],refs['after']['id']]
        p=self.preview([dict(type='reorder_stages',flow_id=fid,expected_flow_version=1,ordered_stage_ids=original[::-1])])
        result=self.apply(p,key='reorder')
        self.assertTrue(result['ok'],result)
        self.assertEqual([r['id'] for r in self.api.routine_stages(fid)['stages']],original[::-1])
        self.api.undo('undo-reorder',result['data']['operation_id'])
        p=self.preview([dict(type='insert_stage',client_ref='bath',flow_id=fid,expected_flow_version=3,
                            before_stage_id=original[0],stage={'name':'洗澡','content':'洗澡换衣'})])
        result=self.apply(p,key='insert')
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.api.routine_stages(fid)['stages'][0]['id'],p['new_refs']['bath']['id'])
        self.api.undo('undo-insert',result['data']['operation_id'])
        self.assertEqual([r['id'] for r in self.api.routine_stages(fid)['stages']],original)

    def test_stage_cascade_requires_explicit_scope_and_restores_paused_rule(self):
        refs=self.seed()
        action=dict(type='delete_stage',stage_id=refs['wait']['id'],expected_version=1)
        refused=self.call('preview_behavior_plan',actions=[action])
        self.assertEqual(refused['error']['code'],'REFERENCE_CONFLICT')
        p=self.preview([dict(action,cascade_rules=True)])
        self.assertEqual(len([r for r in p['affected'] if r['entity_type']=='behavior_rules']),2)
        result=self.apply(p,key='delete-stage')
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.api.behavior_rules(),[])
        self.assertEqual(self.api.routine_stages(refs['evening']['id'])['stages'][0]['position'],1)
        self.api.undo('undo-delete-stage',result['data']['operation_id'])
        rows=self.api.behavior_rules()
        self.assertEqual(len(rows),2)
        paused=next(r for r in rows if r['id']==refs['paused']['id'])
        self.assertEqual(paused['is_active'],0)
        self.assertEqual(paused['inactive_reason'],'先暂停')

    def test_flow_cascade_and_new_reference_block_undo(self):
        refs=self.seed()
        p=self.preview([dict(type='delete_flow',flow_id=refs['evening']['id'],expected_version=1,cascade_dependents=True)])
        result=self.apply(p,key='delete-flow')
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.api.routine_flows(),[])
        self.assertEqual(self.api.behavior_rules(),[])
        self.api.undo('undo-delete-flow',result['data']['operation_id'])
        self.assertEqual(len(self.api.routine_stages(refs['evening']['id'])['stages']),2)
        # A separate fresh import cannot be undone once a subsequent rule references it.
        fresh=self.api.create_routine_flow('new-flow',flow={'name':'新流程','content':'新安排'})
        self.api.create_behavior_rule('new-rule',rule=dict(content='后来补充',kind='action',behavior='休息',flow_id=fresh['flow_id']))
        with self.assertRaises(ValueError):
            self.api.undo('undo-parent',fresh['operation_id'])

    def test_later_reference_blocks_whole_import_undo_without_partial_removal(self):
        p=self.preview(self.imported())
        result=self.apply(p)['data']
        refs=result['new_refs']
        self.api.create_behavior_rule('later-reference',rule=dict(content='后续依赖',kind='action',behavior='坐着',
            flow_id=refs['evening']['id'],stage_id=refs['wait']['id']))
        before=self.count_operations()
        with self.assertRaises(ValueError):
            self.api.undo('undo-entire-import',result['operation_id'])
        self.assertEqual(len(self.api.routine_flows()),1)
        self.assertEqual(len(self.api.behavior_rules()),3)
        self.assertEqual(self.count_operations(),before)

    def test_new_rule_after_preview_invalidates_cascade_without_partial_delete(self):
        refs=self.seed()
        p=self.preview([dict(type='delete_stage',stage_id=refs['wait']['id'],expected_version=1,cascade_rules=True)])
        self.api.create_behavior_rule('later',rule=dict(content='新来的准则',kind='action',behavior='休息',
                           flow_id=refs['evening']['id'],stage_id=refs['wait']['id']))
        before=self.count_operations()
        denied=self.apply(p,key='stale')
        self.assertEqual(denied['error']['code'],'PREVIEW_STALE')
        self.assertEqual(self.count_operations(),before)
        self.assertEqual(len(self.api.behavior_rules()),3)
        self.assertEqual(len(self.api.routine_stages(refs['evening']['id'])['stages']),2)

    def test_failure_mid_apply_rolls_back_everything_and_can_retry(self):
        p=self.preview(self.imported())
        real=behavior_plans.replay
        def fail(*args,**kwargs):
            real(*args,**kwargs)
            raise RuntimeError('injected after all row changes')
        with patch('behavior_plans.replay',side_effect=fail):
            denied=self.apply(p)
        self.assertEqual(denied['error']['code'],'INTERNAL_ERROR')
        self.assertEqual(self.api.routine_flows(),[])
        self.assertEqual(self.api.behavior_rules(),[])
        self.assertEqual(self.count_operations(),0)
        self.assertTrue(self.apply(p)['ok'])

    def test_duplicate_and_forward_refs_and_overlapping_actions_rejected(self):
        actions=self.imported()
        wrong=[actions[1],actions[0]]
        self.assertFalse(self.call('preview_behavior_plan',actions=wrong)['ok'])
        duplicate=[actions[0],dict(actions[0])]
        self.assertFalse(self.call('preview_behavior_plan',actions=duplicate)['ok'])
        self.assertEqual(self.api.routine_flows(),[])
        refs=self.seed()
        edit=dict(type='edit_stage',stage_id=refs['wait']['id'],expected_version=1,changes={'name':'改名'})
        self.assertFalse(self.call('preview_behavior_plan',actions=[edit,edit])['ok'])
        fid=refs['evening']['id']
        insert=dict(type='insert_stage',client_ref='new',flow_id=fid,expected_flow_version=1,stage={'name':'新阶段','content':'新阶段'})
        reorder=dict(type='reorder_stages',flow_id=fid,expected_flow_version=1,ordered_stage_ids=[refs['wait']['id'],refs['after']['id']])
        self.assertFalse(self.call('preview_behavior_plan',actions=[insert,reorder])['ok'])
        self.assertEqual(self.api.routine_stages(fid)['stages'][0]['name'],'等饭')

    def test_duplicate_existing_rule_reports_candidates_instead_of_overwriting(self):
        refs=self.seed()
        action=dict(type='create_rule',client_ref='duplicate',rule=dict(content='等饭做家务',kind='action',behavior='做家务',
                                 flow_id=refs['evening']['id'],stage_id=refs['wait']['id']))
        response=self.call('preview_behavior_plan',actions=[action])
        self.assertEqual(response['error']['code'],'POSSIBLE_DUPLICATE')
        self.assertEqual(response['error']['candidates'],[refs['housework']['id']])


if __name__=='__main__':
    unittest.main()
