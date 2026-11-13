"""F13 strict nested inputs, matching, pagination and mutations over MCP."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from mcp import Client
from mcp_server import create_server,ToolAdapter,INSTRUCTIONS
from behavior_protocol import MODELS


class BehaviorMCPChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'protocol.sqlite3'
        self.client=Client(create_server(self.path),raise_exceptions=True)

    def _callTestMethod(self,method):
        async def run():
            async with self.client:
                await method()
        self._callMaybeAsync(run)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def call(self,name,**params):
        result=await self.client.call_tool(name,{'request':params})
        self.assertFalse(result.is_error)
        return result.structured_content

    async def create(self,key,**extra):
        rule=dict(content=key,kind='action',behavior='听音乐')
        rule.update(extra)
        return await self.call('create_behavior_rule',request_key=key,rule=rule)

    async def test_rule_crud_partial_update_null_undo_and_error_codes(self):
        r=await self.create('rule',reason='休息',alternative='坐着',interpretation='解释',conditions={'states':['很累']})
        data=r['data']
        updated=await self.call('update_behavior_rule',request_key='edit',rule_id=data['rule_id'],expected_version=1,changes={'reason':None})
        row=(await self.call('behavior_rules',rule_id=data['rule_id']))['data']['items'][0]
        self.assertIsNone(row['reason'])
        self.assertEqual(row['alternative'],'坐着')
        self.assertEqual(row['conditions'],{'states':['很累']})
        self.assertEqual(row['interpretation'],'解释')
        stale=await self.call('update_behavior_rule',request_key='stale',rule_id=data['rule_id'],expected_version=1,changes={'is_active':False})
        self.assertEqual(stale['error']['code'],'VERSION_CONFLICT')
        await self.call('undo',request_key='undo',operation_id=updated['data']['operation_id'])
        deleted=await self.call('delete_behavior_rule',request_key='delete',rule_id=data['rule_id'],expected_version=3)
        self.assertTrue(deleted['ok'])
        self.assertEqual((await self.call('behavior_rules'))['data']['items'],[])
        await self.call('undo',request_key='restore',operation_id=deleted['data']['operation_id'])
        duplicate=await self.call('create_behavior_rule',request_key='duplicate',rule={'content':'rule','kind':'action','behavior':'听音乐','conditions':{'states':['很累']}})
        self.assertEqual(duplicate['error']['code'],'POSSIBLE_DUPLICATE')
        self.assertEqual(duplicate['error']['candidates'],[data['rule_id']])
        self.assertEqual(await self.create('rule',reason='休息',alternative='坐着',interpretation='解释',conditions={'states':['很累']}),r)

    async def test_nested_strict_inputs_cannot_clear_or_override_fields(self):
        for rule in [{'content':'x','kind':'action','behavior':'x','is_active':1},
                     {'content':'x','kind':'action','behavior':'x','conditions':{'states':'很累'}},
                     {'content':'x','kind':'action','behavior':'x','conditions':{'weekdays':[True]}},
                     {'content':'x','kind':'action','behavior':'x','conditions':{'unknown':[]}},
                     {'content':'x','kind':'action','behavior':'x','table':'items'}]:
            result=await self.client.call_tool('create_behavior_rule',{'request':{'request_key':'bad','rule':rule}})
            self.assertTrue(result.is_error)
        r=(await self.create('valid'))['data']
        for changes in [{'content':None},{'conditions':None},{'request_key':'override'}]:
            result=await self.client.call_tool('update_behavior_rule',{'request':dict(request_key='bad',rule_id=r['rule_id'],expected_version=1,changes=changes)})
            self.assertTrue(result.is_error)
        empty=await self.call('update_behavior_rule',request_key='empty',rule_id=r['rule_id'],expected_version=1,changes={})
        self.assertFalse(empty['ok'])
        self.assertEqual((await self.call('behavior_rules'))['data']['items'][0]['version'],1)

    async def test_flow_creation_matching_missing_stage_and_disable(self):
        flow=(await self.call('create_routine_flow',request_key='flow',flow={'name':'晚间','content':'按阶段'},
                              stages=[{'name':'回家','content':'洗澡','reference_time':'19:00'}]))['data']
        await self.create('home',flow_id=flow['flow_id'],stage_id=flow['stage_ids'][0])
        unknown=(await self.call('behavior_candidates',context={'location':'家里'}))['data']
        self.assertEqual(unknown['status'],'needs_context')
        self.assertEqual(unknown['missing_context'][0],'stage')
        context={'stage_id':flow['stage_ids'][0],'local_datetime':'2026-11-13T23:00:00+08:00'}
        matched=(await self.call('behavior_candidates',context=context))['data']
        self.assertEqual(matched['status'],'matched')
        await self.call('update_routine_flow',request_key='off',flow_id=flow['flow_id'],expected_version=1,changes={'is_active':False})
        self.assertEqual((await self.call('behavior_candidates',context=context))['data']['status'],'no_match')
        stages=(await self.call('routine_stages',flow_id=flow['flow_id']))['data']
        self.assertEqual(stages['stages'][0]['reference_time'],'19:00')

    async def test_candidate_pagination_preserves_clock_and_accumulates_all(self):
        for i in range(3):
            await self.create('r'+str(i),conditions={'states':['很累']})
        first=(await self.call('behavior_candidates',limit=1))['data']
        self.assertTrue(first['has_more'])
        self.assertFalse(first['evaluation_complete'])
        seen=[r['rule_id'] for r in first['possible']]
        current=first
        while current['has_more']:
            current=(await self.call('behavior_candidates',limit=1,cursor=current['next_cursor']))['data']
            self.assertEqual(current['evaluated_at'],first['evaluated_at'])
            seen.extend(r['rule_id'] for r in current['possible'])
        self.assertEqual(len(set(seen)),3)
        self.assertTrue(current['evaluation_complete'])

    async def test_list_query_pagination_binds_filters_and_rejects_changed_data(self):
        for i in range(3):
            await self.create('rule'+str(i))
        first=(await self.call('behavior_rules',query='rule',limit=1))['data']
        second=(await self.call('behavior_rules',query='rule',limit=1,cursor=first['next_cursor']))['data']
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        wrong=await self.call('behavior_rules',query='different',limit=1,cursor=first['next_cursor'])
        self.assertFalse(wrong['ok'])
        await self.create('rule-new')
        stale=await self.call('behavior_rules',query='rule',limit=1,cursor=first['next_cursor'])
        self.assertFalse(stale['ok'])

    async def test_atomic_plan_via_protocol_and_idempotent_retry(self):
        actions=[{'type':'create_flow','client_ref':'f','flow':{'name':'测试流程','content':'流程原文'},
                  'stages':[{'client_ref':'s','name':'等饭','content':'等饭阶段'}]},
                 {'type':'create_rule','client_ref':'r','flow_ref':'f','stage_ref':'s',
                  'rule':{'content':'等饭听歌','kind':'action','behavior':'听歌'}}]
        preview=(await self.call('preview_behavior_plan',actions=actions))['data']
        self.assertNotIn('state_digest',preview)
        self.assertNotIn('database',preview)
        denied=await self.call('apply_behavior_plan',request_key='plan',preview_id=preview['preview_id'])
        self.assertEqual(denied['error']['code'],'CONFIRMATION_REQUIRED')
        applied=await self.call('apply_behavior_plan',request_key='plan',preview_id=preview['preview_id'],confirmed=True)
        self.assertTrue(applied['ok'])
        rows=(await self.call('behavior_rules'))['data']['items']
        self.assertEqual(rows[0]['id'],preview['new_refs']['r']['id'])
        repeated=await self.call('apply_behavior_plan',request_key='plan',preview_id=preview['preview_id'],confirmed=True)
        self.assertEqual(applied,repeated)
        await self.call('undo',request_key='undo-plan',operation_id=applied['data']['operation_id'])
        self.assertEqual((await self.call('behavior_rules'))['data']['items'],[])
        self.assertEqual((await self.call('routine_flows'))['data']['items'],[])

    async def test_plan_protocol_rejects_unknown_actions_and_replacement_payload(self):
        for actions in [[{'type':'sql','sql':'DROP TABLE items'}],
                        [{'type':'create_rule','client_ref':'x','rule':{'content':'x','kind':'action','behavior':'x'},'file_path':'private'}]]:
            result=await self.client.call_tool('preview_behavior_plan',{'request':{'actions':actions}})
            self.assertTrue(result.is_error)
        result=await self.client.call_tool('apply_behavior_plan',{'request':{
            'request_key':'bad','preview_id':'invented','confirmed':True,'actions':[]}})
        self.assertTrue(result.is_error)
        invalid=await self.call('apply_behavior_plan',request_key='invalid',preview_id='invented',confirmed=True)
        self.assertEqual(invalid['error']['code'],'PREVIEW_EXPIRED')


class PagingAndInstructionsChecks(unittest.TestCase):
    def test_expired_tampered_cursor_and_behavior_contract(self):
        from query_paging import QueryPager
        from unittest.mock import patch
        pager=QueryPager()
        rows=[{'id':'a'},{'id':'b'}]
        token=pager.page(rows,{'x':1},1)['next_cursor']
        with self.assertRaises(ValueError):
            pager.decode(token+'bad',{'x':1})
        with patch('query_paging.time',return_value=10**12),self.assertRaises(ValueError):
            pager.decode(token,{'x':1})
        for phrase in ['建议过不算已完成','先确认影响下一步','今天跳过不改长期准则','阶段不明确先问','superseded不推荐']:
            self.assertIn(phrase,INSTRUCTIONS)


if __name__=='__main__':
    unittest.main()
