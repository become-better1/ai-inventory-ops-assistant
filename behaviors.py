"""Persistent user-authored routines. No chat progress, matching or MCP exposure."""
from contextlib import closing
import json
import re
from uuid import UUID
from database import connect
from service_types import text, positive, DomainError


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def identity(value):
    if not isinstance(value, str):
        raise ValueError('ID must be a UUID')
    try:
        UUID(value)
    except ValueError:
        raise ValueError('ID must be a UUID') from None
    return value


def clock(value):
    if value is not None and (not isinstance(value, str) or
            not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value)):
        raise ValueError('Time must be HH:MM or null')


def weekdays(value):
    if not isinstance(value, list) or any(type(v) is not int or not 1 <= v <= 7 for v in value):
        raise ValueError('Weekdays must be an array of ISO weekdays')
    if len(set(value)) != len(value):
        raise ValueError('Weekdays must be unique')
    return sorted(value)


def conditions(value):
    keys = {'locations','states','triggers','weekdays','time_windows'}
    if not isinstance(value, dict) or not value.keys() <= keys:
        raise ValueError('Unsupported conditions')
    result = {}
    for key, values in value.items():
        if not isinstance(values, list):
            raise ValueError('Condition values must be arrays')
        if key == 'weekdays':
            result[key] = weekdays(values)
        elif key == 'time_windows':
            result[key] = []
            for window in values:
                if not isinstance(window, dict) or set(window) != {'start','end'}:
                    raise ValueError('Time window requires start and end')
                for v in window.values():
                    if v is None:
                        raise ValueError('Time window boundaries cannot be null')
                    clock(v)
                if window['start'] == window['end']:
                    raise ValueError('Empty/all-day window must be omitted')
                result[key].append(dict(window))
        else:
            for v in values:
                text(v, key)
            result[key] = list(dict.fromkeys(values))
    return result


FLOW_FIELDS = {'name','content','weekdays','applicability_text','is_active'}
STAGE_FIELDS = {'name','content','reference_time','is_active'}
RULE_FIELDS = {'content','flow_id','stage_id','kind','behavior','reason','alternative',
               'conditions','interpretation','is_active','inactive_reason','source_kind','source_label'}


def values_for(kind, values, *, partial=False):
    allowed = {'flow': FLOW_FIELDS, 'stage': STAGE_FIELDS, 'rule': RULE_FIELDS}[kind]
    if not isinstance(values, dict) or not values or not values.keys() <= allowed:
        raise ValueError('Unsupported or empty fields')
    required = {'flow': {'name','content'}, 'stage': {'name','content'},
                'rule': {'content','kind','behavior'}}[kind]
    if not partial and not required <= values.keys():
        raise ValueError('Missing required fields')
    result = dict(values)
    for k, v in values.items():
        if k in {'name','content','behavior'}:
            text(v, k)
        elif k == 'is_active':
            if type(v) is not bool:
                raise ValueError('Active flag must be boolean')
            result[k] = int(v)
        elif k in {'flow_id','stage_id'}:
            if v is not None:
                identity(v)
        elif k == 'kind':
            if v not in ('action','avoid'):
                raise ValueError('Unsupported rule kind')
        elif k == 'source_kind':
            if v not in ('natural_language','note_import'):
                raise ValueError('Unsupported source kind')
        elif k == 'weekdays':
            result.pop(k)
            result['weekdays_json'] = encode(weekdays(v))
        elif k == 'conditions':
            result.pop(k)
            result['conditions_json'] = encode(conditions(v))
        elif k == 'reference_time':
            clock(v)
        elif v is not None and not isinstance(v, str):
            raise ValueError(k + ' must be text or null')
    return result


def live(w, table, key, version=None):
    identity(key)
    if version is not None:
        positive(version, 'Version')
    try:
        row = w.get(table, key)
    except ValueError:
        raise DomainError('NOT_FOUND','Record not found') from None
    if row['is_void']:
        raise DomainError('NOT_FOUND','Record removed; refresh first')
    if version is not None and row['version'] != version:
        raise DomainError('VERSION_CONFLICT','Record changed; refresh first')
    return row


def decoded(row):
    result = dict(row)
    for field in ('conditions','weekdays'):
        if field + '_json' in result:
            result[field] = json.loads(result.pop(field + '_json'))
    return result


def validate_behaviors(c):
    flows = {r['id']: dict(r) for r in c.execute('SELECT * FROM routine_flows WHERE is_void=0')}
    stages = {r['id']: dict(r) for r in c.execute('SELECT * FROM routine_stages WHERE is_void=0')}
    for f in flows.values():
        weekdays(json.loads(f['weekdays_json']))
    for stage in stages.values():
        if stage['flow_id'] not in flows:
            raise DomainError('REFERENCE_CONFLICT', 'Live stage references a removed flow')
        clock(stage['reference_time'])
    for row in c.execute('SELECT * FROM behavior_rules WHERE is_void=0'):
        conditions(json.loads(row['conditions_json']))
        if row['flow_id'] is not None and row['flow_id'] not in flows:
            raise DomainError('REFERENCE_CONFLICT', 'Live rule references a removed flow')
        if row['stage_id'] is not None:
            stage = stages.get(row['stage_id'])
            if stage is None or stage['flow_id'] != row['flow_id']:
                raise DomainError('REFERENCE_CONFLICT', 'Rule stage/flow mismatch or removed stage')


def reposition(w, flow_id, ordered_ids):
    """Move live rows to a disjoint range before any final position assignments."""
    rows = w.c.execute('SELECT * FROM routine_stages WHERE flow_id=? AND is_void=0', (flow_id,)).fetchall()
    if len(ordered_ids) != len(rows) or set(ordered_ids) != {r['id'] for r in rows}:
        raise ValueError('Reorder must include all live stages exactly once')
    offset = max((r['position'] for r in rows), default=0) + len(rows) + 1
    for index, row in enumerate(rows):
        w.change('routine_stages', row['id'], dict(row))
        w.c.execute('UPDATE routine_stages SET position=? WHERE id=?', (offset+index, row['id']))
    for position, sid in enumerate(ordered_ids, 1):
        w.update('routine_stages', sid, {'position': position})


class BehaviorOperations:
    def routine_flows(self, *, active_only=False):
        if type(active_only) is not bool:
            raise ValueError('Active flag must be boolean')
        with closing(connect(self.path)) as c:
            return [decoded(r) for r in c.execute('SELECT * FROM routine_flows WHERE is_void=0' +
                    (' AND is_active=1' if active_only else '') + ' ORDER BY id')]

    def routine_stages(self, flow_id, *, include_inactive=False):
        identity(flow_id)
        if type(include_inactive) is not bool:
            raise ValueError('Include inactive must be boolean')
        with closing(connect(self.path)) as c:
            f = c.execute('SELECT * FROM routine_flows WHERE id=? AND is_void=0', (flow_id,)).fetchone()
            if f is None:
                raise ValueError('Flow not found')
            rows = c.execute('SELECT * FROM routine_stages WHERE flow_id=? AND is_void=0' +
                  ('' if include_inactive else ' AND is_active=1') + ' ORDER BY position', (flow_id,))
            return dict(flow_id=flow_id, flow_version=f['version'], stages=[dict(r) for r in rows])

    def behavior_rules(self, *, rule_id=None, flow_id=None, stage_id=None, active_only=False):
        if type(active_only) is not bool:
            raise ValueError('Active flag must be boolean')
        query = 'SELECT r.* FROM behavior_rules r WHERE r.is_void=0'
        params = []
        for field, val in [('id',rule_id),('flow_id',flow_id),('stage_id',stage_id)]:
            if val is not None:
                identity(val)
                query += ' AND r.' + field + '=?'
                params.append(val)
        if active_only:
            query += ''' AND r.is_active=1
                AND (r.flow_id IS NULL OR EXISTS(SELECT 1 FROM routine_flows f
                  WHERE f.id=r.flow_id AND f.is_void=0 AND f.is_active=1))
                AND (r.stage_id IS NULL OR EXISTS(SELECT 1 FROM routine_stages s
                  WHERE s.id=r.stage_id AND s.is_void=0 AND s.is_active=1))'''
        with closing(connect(self.path)) as c:
            return [decoded(r) for r in c.execute(query+' ORDER BY r.id', params)]

    def create_routine_flow(self, request_key, *, flow, stages=None, confirmed_distinct=False):
        values = values_for('flow', flow)
        if type(confirmed_distinct) is not bool:
            raise ValueError('Distinct flag must be boolean')
        stages = [] if stages is None else stages
        if not isinstance(stages, list) or len(stages) > 100:
            raise ValueError('At most 100 stages per flow')
        stage_values = [values_for('stage', s) for s in stages]
        def run(w):
            if not confirmed_distinct:
                normalized = ''.join(values['content'].casefold().split())
                for row in w.c.execute('SELECT id,name,content FROM routine_flows WHERE is_void=0'):
                    if (''.join(row['content'].casefold().split()) == normalized
                            and ''.join(row['name'].casefold().split()) == ''.join(values['name'].casefold().split())):
                        raise DomainError('POSSIBLE_DUPLICATE','Similar flow already exists',candidates=[row['id']])
            fid = w.insert('routine_flows', values)
            ids = [w.insert('routine_stages', dict(v, flow_id=fid, position=i))
                   for i, v in enumerate(stage_values, 1)]
            return dict(flow_id=fid, flow_version=1, stage_ids=ids, stage_versions=[1]*len(ids))
        return self._write(request_key, 'create_routine_flow', dict(flow=values, stages=stage_values, confirmed_distinct=confirmed_distinct), run)

    def update_routine_flow(self, request_key, flow_id, *, expected_version, changes):
        values = values_for('flow', changes, partial=True)
        def run(w):
            live(w, 'routine_flows', flow_id, expected_version)
            w.update('routine_flows', flow_id, values)
            return dict(flow_id=flow_id, flow_version=expected_version+1)
        return self._write(request_key, 'update_routine_flow',
                           dict(flow_id=flow_id, expected_version=expected_version, changes=values), run)

    def update_routine_stage(self, request_key, stage_id, *, expected_version, changes):
        values = values_for('stage', changes, partial=True)
        def run(w):
            row = live(w, 'routine_stages', stage_id, expected_version)
            w.update('routine_stages', stage_id, values)
            # Parent version also detects stage metadata edits in dependent plans.
            f = live(w, 'routine_flows', row['flow_id'])
            w.update('routine_flows', f['id'], {})
            return dict(stage_id=stage_id, stage_version=expected_version+1, flow_version=f['version']+1)
        return self._write(request_key, 'update_routine_stage',
                           dict(stage_id=stage_id, expected_version=expected_version, changes=values), run)

    def insert_routine_stage(self, request_key, flow_id, *, expected_flow_version, stage, before_stage_id=None):
        values = values_for('stage', stage)
        if before_stage_id is not None:
            identity(before_stage_id)
        def run(w):
            live(w, 'routine_flows', flow_id, expected_flow_version)
            rows = w.c.execute('SELECT id,position FROM routine_stages WHERE flow_id=? AND is_void=0 ORDER BY position', (flow_id,)).fetchall()
            ids = [r['id'] for r in rows]
            if len(ids) >= 100 or (before_stage_id is not None and before_stage_id not in ids):
                raise ValueError('Stage limit or invalid insertion target')
            sid = w.insert('routine_stages', dict(values, flow_id=flow_id, position=max([r['position'] for r in rows], default=0)+1))
            ids.insert(len(ids) if before_stage_id is None else ids.index(before_stage_id), sid)
            reposition(w, flow_id, ids)
            w.update('routine_flows', flow_id, {})
            return dict(stage_id=sid, stage_version=w.get('routine_stages', sid)['version'], flow_version=expected_flow_version+1)
        return self._write(request_key, 'insert_routine_stage', dict(flow_id=flow_id,
                 expected_flow_version=expected_flow_version, stage=values, before_stage_id=before_stage_id), run)

    def reorder_routine_stages(self, request_key, flow_id, *, expected_flow_version, ordered_stage_ids):
        if not isinstance(ordered_stage_ids, list):
            raise ValueError('Stage order must be an array')
        for sid in ordered_stage_ids:
            identity(sid)
        def run(w):
            live(w, 'routine_flows', flow_id, expected_flow_version)
            reposition(w, flow_id, ordered_stage_ids)
            w.update('routine_flows', flow_id, {})
            return dict(flow_id=flow_id, flow_version=expected_flow_version+1)
        return self._write(request_key, 'reorder_routine_stages', dict(flow_id=flow_id,
                 expected_flow_version=expected_flow_version, ordered_stage_ids=ordered_stage_ids), run)

    def delete_routine_stage(self, request_key, stage_id, *, expected_version):
        def run(w):
            row = live(w, 'routine_stages', stage_id, expected_version)
            w.update('routine_stages', stage_id, dict(is_void=1))
            ids = [r[0] for r in w.c.execute('SELECT id FROM routine_stages WHERE flow_id=? AND is_void=0 ORDER BY position', (row['flow_id'],))]
            reposition(w, row['flow_id'], ids)
            w.update('routine_flows', row['flow_id'], {})
            return dict(stage_id=stage_id, stage_version=expected_version+1)
        return self._write(request_key, 'delete_routine_stage', dict(stage_id=stage_id, expected_version=expected_version), run)

    def delete_routine_flow(self, request_key, flow_id, *, expected_version):
        # Referenced deletion deliberately fails validation; cascading requires future previews.
        def run(w):
            live(w, 'routine_flows', flow_id, expected_version)
            w.update('routine_flows', flow_id, dict(is_void=1))
            return dict(flow_id=flow_id, flow_version=expected_version+1)
        return self._write(request_key, 'delete_routine_flow', dict(flow_id=flow_id, expected_version=expected_version), run)

    def create_behavior_rule(self, request_key, *, rule, confirmed_distinct=False):
        values = values_for('rule', rule)
        if type(confirmed_distinct) is not bool:
            raise ValueError('Distinct flag must be boolean')
        def run(w):
            if not confirmed_distinct:
                for row in w.c.execute('SELECT * FROM behavior_rules WHERE is_void=0'):
                    if (''.join(row['content'].casefold().split()) == ''.join(values['content'].casefold().split())
                            and row['flow_id'] == values.get('flow_id') and row['stage_id'] == values.get('stage_id')
                            and row['conditions_json'] == values.get('conditions_json', '{}')):
                        raise DomainError('POSSIBLE_DUPLICATE','Similar behavior rule already exists',candidates=[row['id']])
            rid = w.insert('behavior_rules', values)
            return dict(rule_id=rid, version=1)
        return self._write(request_key, 'create_behavior_rule', dict(rule=values, confirmed_distinct=confirmed_distinct), run)

    def update_behavior_rule(self, request_key, rule_id, *, expected_version, changes):
        values = values_for('rule', changes, partial=True)
        if set(changes) & {'content','behavior','flow_id','stage_id','conditions'}:
            values.setdefault('interpretation', None)
        def run(w):
            live(w, 'behavior_rules', rule_id, expected_version)
            w.update('behavior_rules', rule_id, values)
            return dict(rule_id=rule_id, version=expected_version+1)
        return self._write(request_key, 'update_behavior_rule',
                          dict(rule_id=rule_id, expected_version=expected_version, changes=values), run)

    def delete_behavior_rule(self, request_key, rule_id, *, expected_version):
        def run(w):
            live(w, 'behavior_rules', rule_id, expected_version)
            w.update('behavior_rules', rule_id, dict(is_void=1))
            return dict(rule_id=rule_id, version=expected_version+1)
        return self._write(request_key, 'delete_behavior_rule', dict(rule_id=rule_id, expected_version=expected_version), run)
