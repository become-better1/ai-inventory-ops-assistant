"""Reproducible portfolio demo with synthetic data and a temporary database."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from tempfile import TemporaryDirectory
from service import Assistant
from maintenance import snapshot, verify_backup
from mcp_server import ToolAdapter

def main():
    with TemporaryDirectory(prefix='portfolio-demo-') as folder:
        root = Path(folder)
        api = Assistant(root / 'demo.sqlite3')
        purchase = api.record_purchase('demo-buy', name='演示雨伞', quantity=2,
                                       unit='把', received=True)
        def show(label):
            quantity = sum(i['quantity'] for i in api.inventory())
            amount = api.spending()['by_currency'].get('CNY', {}).get('net_minor')
            print(f'{label}: quantity={quantity}, net_minor={amount}')
        show('1. Purchase without amount')
        retried = api.record_purchase('demo-buy', name='演示雨伞', quantity=2,
                                     unit='把', received=True)
        assert retried == purchase
        assert sum(i['quantity'] for i in api.inventory()) == 2
        show('2. Retry same request: no duplicate inventory')
        api.correct_purchase_amount('demo-price', purchase['purchase_id'],
                                    expected_version=1, total_amount_minor=6000, currency='CNY')
        show('3. Correct purchase amount: inventory unchanged')
        item = api.inventory()[0]
        returned = api.return_item('demo-return', item['id'], quantity=1,
                                   expected_version=item['version'], infer_amount=True)
        show('4. Return one: deduct expense immediately')
        api.undo('demo-undo', returned['operation_id'])
        show('5. Undo return')
        # These schedules are fictional fixtures, not the author's routine.
        adapter = ToolAdapter(api.path)
        def call(name, **request):
            result = adapter.invoke(name, adapter.models[name].model_validate(request))
            if not result['ok']:
                raise RuntimeError(result['error'])
            return result['data']
        plan = call('preview_behavior_plan', actions=[
            dict(type='create_flow', client_ref='work',
                 flow=dict(name='演示工作流程', content='虚构样例：收工前整理资料，再检查清单。', weekdays=[1,2,3,4,5]),
                 stages=[dict(client_ref='tidy',name='收工前整理',content='整理演示资料',reference_time='17:00'),
                         dict(client_ref='check',name='离开前检查',content='核对演示清单')]),
            dict(type='create_rule',client_ref='tidy_rule',flow_ref='work',stage_ref='tidy',
                 rule=dict(content='在收工前整理阶段归档演示资料。',kind='action',behavior='归档演示资料。')),
            dict(type='create_rule',client_ref='paused',flow_ref='work',stage_ref='tidy',
                 rule=dict(content='演示用的已停用动作。',kind='action',behavior='重复整理资料。',
                           is_active=False,inactive_reason='仅用于验证停用行为'))])
        assert api.routine_flows() == []  # preview is not a database write
        imported = call('apply_behavior_plan',request_key='demo-plan',preview_id=plan['preview_id'],confirmed=True)
        refs = imported['new_refs']
        print('6. Preview then atomic import: one flow, two stages, two rules')
        rule_ids = [refs['tidy_rule']['id'],refs['paused']['id']]
        unknown = call('behavior_candidates',candidate_ids=rule_ids)
        assert unknown['requires_stage_confirmation']
        assert all('behavior' not in row for row in unknown['possible'])
        print('7. Unknown stage: ask before returning actionable suggestions')
        matched = call('behavior_candidates',context=dict(stage_id=refs['tidy']['id'],
                       local_datetime=datetime(2026,11,13,23,tzinfo=ZoneInfo(os.environ.get('ASSISTANT_TIMEZONE','Asia/Shanghai'))).isoformat()),candidate_ids=rule_ids)
        assert len(matched['matched']) == 1
        assert matched['matched'][0]['rule_id'] == refs['tidy_rule']['id']
        print('8. Actual stage beats reference time; paused rule excluded')
        changed = call('update_routine_flow',request_key='demo-weekdays',flow_id=refs['work']['id'],
                       expected_version=1,changes=dict(weekdays=[2,3,4,5,6]))
        assert api.routine_flows()[0]['weekdays'] == [2,3,4,5,6]
        api.undo('demo-undo-weekdays',changed['operation_id'])
        assert api.routine_flows()[0]['weekdays'] == [1,2,3,4,5]
        print('9. Weekday configuration updated and restored by undo')
        result = snapshot(api.path, root / 'backups')
        verify_backup(result['backup'])
        print('10. Consistent backup: checksum and database validation passed')
    print('Temporary synthetic data removed.')

if __name__ == '__main__':
    main()
