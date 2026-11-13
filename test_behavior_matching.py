"""Matching and read-only guarantees with fixed local time and temporary data."""
from contextlib import closing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from service import Assistant
from database import connect


class MatchingChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.api=Assistant(Path(self.tmp.name)/'matching.sqlite3')
        self.env=patch.dict(os.environ,{'ASSISTANT_TIMEZONE':'Asia/Shanghai'})
        self.env.start()
        self.f=self.api.create_routine_flow('flow',flow=dict(name='晚间',content='晚间流程'),
            stages=[dict(name='回家',content='洗澡',reference_time='19:00'),dict(name='睡前',content='听音乐',reference_time='22:00')])
        self.seq=0

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def add(self,**extra):
        self.seq+=1
        values=dict(content='准则'+str(self.seq),kind='action',behavior='行动'+str(self.seq))
        values.update(extra)
        return self.api.create_behavior_rule('rule'+str(self.seq),rule=values)['rule_id']

    def match(self,**context):
        context.setdefault('local_datetime','2026-11-13T22:30:00+08:00')
        return self.api.behavior_candidates(context=context)

    def test_late_arrival_uses_actual_stage_and_never_reference_time(self):
        home=self.add(flow_id=self.f['flow_id'],stage_id=self.f['stage_ids'][0])
        sleep=self.add(flow_id=self.f['flow_id'],stage_id=self.f['stage_ids'][1])
        result=self.match(stage_id=self.f['stage_ids'][0])
        self.assertEqual(result['status'],'matched')
        self.assertEqual([r['rule_id'] for r in result['matched']],[home])
        self.assertEqual(result['excluded'][0]['rule_id'],sleep)
        unknown=self.match(location='家里',trigger='想看手机')
        self.assertEqual(unknown['status'],'needs_context')
        self.assertEqual(unknown['missing_context'][0],'stage')
        self.assertEqual(len(unknown['possible']),2)
        self.assertTrue(unknown['requires_stage_confirmation'])
        self.assertIn('等饭',unknown['next_question'])
        self.assertTrue(all('behavior' not in r and 'content' not in r for r in unknown['possible']))

    def test_fatigue_unknown_then_specific_overrides_general(self):
        general=self.add(flow_id=self.f['flow_id'],stage_id=self.f['stage_ids'][0],behavior='做家务')
        tired=self.add(flow_id=self.f['flow_id'],stage_id=self.f['stage_ids'][0],behavior='坐着听音乐',conditions={'states':['很累']})
        result=self.match(stage_id=self.f['stage_ids'][0])
        self.assertEqual(result['status'],'needs_context')
        self.assertIn('states',result['missing_context'])
        result=self.match(stage_id=self.f['stage_ids'][0],states=['很累'])
        self.assertEqual([r['rule_id'] for r in result['matched']],[tired])
        self.assertEqual(result['superseded'][0]['rule_id'],general)
        self.assertEqual(result['superseded'][0]['superseded_by'],[tired])
        self.assertEqual(result['status'],'matched')

    def test_missing_fatigue_plan_not_fabricated_and_no_writes(self):
        before=self.api.behavior_rules()
        with closing(connect(self.api.path)) as c:
            ops=c.execute('SELECT count(*) FROM operations').fetchone()[0]
        result=self.match(states=['很累'],stage_id=self.f['stage_ids'][0])
        self.assertEqual(result['status'],'no_match')
        self.assertEqual(result['matched'],[])
        self.assertEqual(self.api.behavior_rules(),before)
        with closing(connect(self.api.path)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],ops)

    def test_nonidentical_natural_language_retained_for_semantic_review(self):
        rid=self.add(kind='avoid',behavior='躺着刷手机',reason='自己的担忧',alternative='先等五分钟',
                     conditions={'locations':['家里'],'triggers':['想看手机']})
        result=self.match(location='床上',trigger='想刷手机')
        possible=result['possible'][0]
        self.assertEqual(possible['rule_id'],rid)
        self.assertTrue(possible['semantic_review_required'])
        self.assertEqual(possible['reason'],'自己的担忧')
        self.assertEqual(result['excluded'],[])

    def test_time_windows_midnight_and_end_exclusive(self):
        rid=self.add(conditions={'time_windows':[{'start':'22:00','end':'08:00'}]})
        for value in ['2026-11-13T22:00:00+08:00','2026-11-14T00:30:00+08:00','2026-11-14T07:59:00+08:00']:
            self.assertEqual(self.match(local_datetime=value)['matched'][0]['rule_id'],rid)
        for value in ['2026-11-13T21:59:00+08:00','2026-11-14T08:00:00+08:00']:
            self.assertEqual(self.match(local_datetime=value)['status'],'no_match')

    def test_explicit_flow_overrides_schedule_but_not_rule_weekday(self):
        self.api.update_routine_flow('days',self.f['flow_id'],expected_version=1,changes={'weekdays':[1]})
        rid=self.add(flow_id=self.f['flow_id'])
        self.assertEqual(self.match()['status'],'no_match')  # Friday
        self.assertEqual(self.match(flow_id=self.f['flow_id'])['matched'][0]['rule_id'],rid)
        self.api.update_behavior_rule('rule-days',rid,expected_version=1,changes={'conditions':{'weekdays':[1]}})
        self.assertEqual(self.match(flow_id=self.f['flow_id'])['status'],'no_match')

    def test_disabled_rules_and_parents_never_recommended(self):
        rid=self.add(flow_id=self.f['flow_id'],stage_id=self.f['stage_ids'][0])
        self.api.update_routine_flow('off',self.f['flow_id'],expected_version=1,changes={'is_active':False})
        result=self.match(stage_id=self.f['stage_ids'][0])
        self.assertEqual(result['status'],'no_match')
        self.assertEqual(result['excluded'][0]['rule_id'],rid)
        self.assertEqual(result['possible'],[])

    def test_overlap_is_not_resolved_by_creation_order(self):
        a=self.add(flow_id=self.f['flow_id'],stage_id=self.f['stage_ids'][0],behavior='坐着休息')
        b=self.add(flow_id=self.f['flow_id'],stage_id=self.f['stage_ids'][0],behavior='立即出门')
        result=self.match(stage_id=self.f['stage_ids'][0])
        self.assertEqual(result['status'],'conflict')
        self.assertEqual(set(result['conflicts'][0]['rule_ids']),{a,b})

    def test_incomparable_specific_arrangements_require_review(self):
        a=self.add(flow_id=self.f['flow_id'],stage_id=self.f['stage_ids'][0],conditions={'states':['很累']})
        b=self.add(flow_id=self.f['flow_id'],stage_id=self.f['stage_ids'][0],conditions={'states':['无聊']})
        result=self.match(stage_id=self.f['stage_ids'][0],states=['很累','无聊'])
        self.assertEqual(result['status'],'conflict')
        self.assertEqual(set(result['conflicts'][0]['rule_ids']),{a,b})
        self.assertEqual(result['superseded'],[])

    def test_selected_scope_does_not_claim_exhaustive_search(self):
        rid=self.add()
        result=self.api.behavior_candidates(candidate_ids=[],context={})
        self.assertEqual(result['candidate_scope'],'selected_rules')
        self.assertEqual(result['status'],'no_match')
        result=self.api.behavior_candidates(candidate_ids=[rid],context={})
        self.assertEqual(result['matched'][0]['rule_id'],rid)

    def test_invalid_time_context_and_foreign_stage_rejected(self):
        for context in [{'local_datetime':'2026-11-13T22:00:00'},
                        {'local_datetime':'2026-11-13T22:00:00+00:00'},
                        {'timezone':'UTC'},{'states':'很累'},{'location':None},{'x':1}]:
            with self.subTest(context=context),self.assertRaises(ValueError):
                self.api.behavior_candidates(context=context)
        other=self.api.create_routine_flow('other',flow=dict(name='其他',content='其他流程'))
        with self.assertRaises(ValueError):
            self.match(flow_id=other['flow_id'],stage_id=self.f['stage_ids'][0])


if __name__=='__main__':
    unittest.main()
