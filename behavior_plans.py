"""Preview F13 edits on an in-memory copy; replay one confirmed plan atomically."""
from collections import deque
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from behaviors import live
from database import connect
from service_types import DomainError, text

TABLES = ('routine_flows','routine_stages','behavior_rules')
FIELDS = {
 'create_flow': {'type','client_ref','flow','stages','confirmed_distinct'},
 'create_rule': {'type','client_ref','rule','flow_ref','stage_ref','confirmed_distinct'},
 'update_rule': {'type','rule_id','expected_version','changes'},
 'delete_rule': {'type','rule_id','expected_version'},
 'edit_stage': {'type','stage_id','expected_version','changes'},
 'insert_stage': {'type','client_ref','flow_id','expected_flow_version','before_stage_id','stage'},
 'reorder_stages': {'type','flow_id','expected_flow_version','ordered_stage_ids'},
 'delete_stage': {'type','stage_id','expected_version','cascade_rules'},
 'delete_flow': {'type','flow_id','expected_version','cascade_dependents'},
}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def state(c):
    return digest({t:[dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY id')] for t in TABLES})


def normalize(actions):
    if not isinstance(actions,list) or not 1 <= len(actions) <= 200:
        raise ValueError('A plan requires 1 to 200 actions')
    try:
        result=json.loads(json.dumps(actions,ensure_ascii=False,allow_nan=False))
    except (TypeError,ValueError):
        raise ValueError('Actions must be JSON values') from None
    for a in result:
        if not isinstance(a,dict) or not isinstance(a.get('type'),str) or a['type'] not in FIELDS or not a.keys()<=FIELDS[a['type']]:
            raise ValueError('Unsupported action or fields')
        for flag in ('cascade_rules','cascade_dependents','confirmed_distinct'):
            if flag in a and type(a[flag]) is not bool:
                raise ValueError('Action flags must be boolean')
    return result


def replay(api, writer, actions, reserved=None):
    # Import lazily: service imports this mixin, and remains independent of MCP/Pydantic.
    from service import _Writer
    class PlanWriter(_Writer):
        def __init__(self):
            super().__init__(writer.c,writer.op)
            self.changes=writer.changes
            self.reserved=None if reserved is None else deque(reserved)
            self.allocated=[]
            self.action_touched=set()

        def new_id(self):
            if self.reserved is not None and not self.reserved:
                raise DomainError('PREVIEW_STALE','Plan allocation changed; preview again')
            value=super().new_id() if self.reserved is None else self.reserved.popleft()
            self.allocated.append(value)
            return value

        def change(self,table,key,before):
            self.action_touched.add((table,key))
            return super().change(table,key,before)
    w=PlanWriter()
    runner=object.__new__(type(api))
    runner._write=lambda request_key,kind,payload,action,**kwargs:action(w)
    refs={}
    touched=set()
    structural=set()
    warnings=[]
    def ref(name,kind):
        if name not in refs or refs[name]['kind']!=kind:
            raise ValueError('Unknown, forward or wrong-kind client_ref')
        return refs[name]['id']
    def register(name,kind,key):
        text(name,'client_ref')
        if name in refs:
            raise ValueError('client_ref must be unique in a plan')
        refs[name]=dict(kind=kind,id=key)
    for index,a in enumerate(actions):
        kind=a['type']
        w.action_touched=set()
        try:
            if kind=='create_flow':
                stages=[]
                stage_refs=[]
                source=a.get('stages',[])
                if not isinstance(source,list):
                    raise ValueError('Stages must be an array')
                for s in source:
                    if not isinstance(s,dict):
                        raise ValueError('Stage must be an object')
                    copied=dict(s)
                    stage_refs.append(copied.pop('client_ref',None))
                    stages.append(copied)
                result=runner.create_routine_flow('plan',flow=a['flow'],stages=stages,
                                                 confirmed_distinct=a.get('confirmed_distinct',False))
                register(a['client_ref'],'flow',result['flow_id'])
                for label,key in zip(stage_refs,result['stage_ids']):
                    if label is not None:
                        register(label,'stage',key)
                if a.get('confirmed_distinct'):
                    warnings.append('User confirmed a distinct flow addition')
            elif kind=='create_rule':
                if not isinstance(a['rule'],dict):
                    raise ValueError('Rule must be an object')
                rule=dict(a['rule'])
                for field,label,entity in [('flow_id','flow_ref','flow'),('stage_id','stage_ref','stage')]:
                    if label in a:
                        if field in rule:
                            raise ValueError('Use either ID or client_ref, not both')
                        rule[field]=ref(a[label],entity)
                result=runner.create_behavior_rule('plan',rule=rule,confirmed_distinct=a.get('confirmed_distinct',False))
                register(a['client_ref'],'rule',result['rule_id'])
                if a.get('confirmed_distinct'):
                    warnings.append('User confirmed a distinct rule addition')
            elif kind in ('update_rule','delete_rule'):
                method=runner.update_behavior_rule if kind=='update_rule' else runner.delete_behavior_rule
                kwargs=dict(expected_version=a['expected_version'])
                if kind=='update_rule':
                    kwargs['changes']=a['changes']
                method('plan',a['rule_id'],**kwargs)
            elif kind=='edit_stage':
                runner.update_routine_stage('plan',a['stage_id'],expected_version=a['expected_version'],changes=a['changes'])
            elif kind in ('insert_stage','reorder_stages','delete_stage'):
                fid=live(w,'routine_stages',a['stage_id'],a['expected_version'])['flow_id'] if kind=='delete_stage' else a['flow_id']
                if fid in structural:
                    raise ValueError('Only one structural action per flow per plan')
                structural.add(fid)
                if kind=='insert_stage':
                    result=runner.insert_routine_stage('plan',fid,expected_flow_version=a['expected_flow_version'],
                             stage=a['stage'],before_stage_id=a.get('before_stage_id'))
                    register(a['client_ref'],'stage',result['stage_id'])
                elif kind=='reorder_stages':
                    runner.reorder_routine_stages('plan',fid,expected_flow_version=a['expected_flow_version'],ordered_stage_ids=a['ordered_stage_ids'])
                else:
                    dependencies=w.c.execute('SELECT id FROM behavior_rules WHERE stage_id=? AND is_void=0',(a['stage_id'],)).fetchall()
                    if dependencies and not a.get('cascade_rules',False):
                        raise DomainError('REFERENCE_CONFLICT','Stage has rules; explicitly preview cascade_rules')
                    for row in dependencies:
                        w.update('behavior_rules',row['id'],dict(is_void=1))
                    runner.delete_routine_stage('plan',a['stage_id'],expected_version=a['expected_version'])
            elif kind=='delete_flow':
                fid=a['flow_id']
                live(w,'routine_flows',fid,a['expected_version'])
                stages=w.c.execute('SELECT id FROM routine_stages WHERE flow_id=? AND is_void=0',(fid,)).fetchall()
                rules=w.c.execute('SELECT id FROM behavior_rules WHERE flow_id=? AND is_void=0',(fid,)).fetchall()
                if (stages or rules) and not a.get('cascade_dependents',False):
                    raise DomainError('REFERENCE_CONFLICT','Flow has dependencies; explicitly preview cascade_dependents')
                for row in rules:
                    w.update('behavior_rules',row['id'],dict(is_void=1))
                for row in stages:
                    w.update('routine_stages',row['id'],dict(is_void=1))
                runner.delete_routine_flow('plan',fid,expected_version=a['expected_version'])
        except KeyError as error:
            raise ValueError('Missing action field: '+str(error.args[0])) from None
        overlap=touched & w.action_touched
        # Multiple independent stage edits share a parent revision, not a stage edit.
        allowed_parents={key for key in overlap if key[0]=='routine_flows' and kind in ('edit_stage','insert_stage','reorder_stages','delete_stage')}
        if overlap-allowed_parents:
            raise ValueError('Plan modifies the same entity more than once; split and preview again')
        touched |= w.action_touched
        w.validate()
    if w.reserved:
        raise DomainError('PREVIEW_STALE','Plan allocation changed; preview again')
    return dict(new_refs=refs,allocated_ids=w.allocated,warnings=warnings)


def plan_digest(plan):
    return digest({k:plan[k] for k in ('database','state_digest','normalized_actions','allocated_ids','requires_confirmation')})


class BehaviorPlanOperations:
    def preview_behavior_plan(self, *, actions):
        from service import _Writer
        actions=normalize(actions)
        # SQLite backup gives a consistent snapshot; simulation never writes the source.
        with closing(connect(self.path)) as source, closing(sqlite3.connect(':memory:')) as simulation:
            source.backup(simulation)
            simulation.row_factory=sqlite3.Row
            simulation.execute('PRAGMA foreign_keys=ON')
            before=state(simulation)
            simulation.execute('BEGIN')
            writer=_Writer(simulation,'preview')
            output=replay(self,writer,actions)
            affected=[dict(entity_type=table,entity_id=key,before=old,after=writer.get(table,key))
                      for (table,key),old in writer.changes.items()]
            requires=(len(actions)>1 or any(a.get('cascade_rules') or a.get('cascade_dependents') for a in actions)
                      or (len(affected)>1 and actions[0]['type']!='edit_stage'))
            plan=dict(database=str(Path(self.path).resolve()),state_digest=before,
                      normalized_actions=actions,affected=affected,new_refs=output['new_refs'],
                      allocated_ids=output['allocated_ids'],warnings=output['warnings'],
                      duplicate_candidates=[],requires_confirmation=requires)
            plan['plan_digest']=plan_digest(plan)
            return plan

    def apply_behavior_plan(self, request_key, *, preview_id, confirmed=False, _load_plan=None):
        text(preview_id,'Preview ID')
        if type(confirmed) is not bool:
            raise ValueError('Confirmed must be boolean')
        def run(w):
            if _load_plan is None:
                raise DomainError('PREVIEW_EXPIRED','Preview unavailable; preview again')
            plan=_load_plan(preview_id)
            if plan['database']!=str(Path(self.path).resolve()) or plan_digest(plan)!=plan['plan_digest']:
                raise DomainError('PREVIEW_STALE','Plan identity changed; preview again')
            if plan['requires_confirmation'] and not confirmed:
                raise DomainError('CONFIRMATION_REQUIRED','Review the plan and obtain user confirmation')
            if state(w.c)!=plan['state_digest']:
                raise DomainError('PREVIEW_STALE','Behavior data changed; review a fresh preview')
            result=replay(self,w,plan['normalized_actions'],plan['allocated_ids'])
            affected=[dict(entity_type=table,entity_id=key,version=w.get(table,key)['version'])
                      for table,key in w.changes]
            return dict(new_refs=result['new_refs'],affected=affected,plan_digest=plan['plan_digest'])
        # _write checks persisted idempotency before calling run or loading a preview.
        return self._write(request_key,'apply_behavior_plan',dict(preview_id=preview_id,confirmed=confirmed),run)
