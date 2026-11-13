"""Conservative three-valued matching of user-authored behavior rules."""
from contextlib import closing
from datetime import datetime, timezone
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from behaviors import identity, decoded
from database import connect
from service_types import text

CONTEXT_FIELDS = {'flow_id','stage_id','location','states','trigger','local_datetime','timezone'}


def validate_context(value, *, frozen_at=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or not value.keys() <= CONTEXT_FIELDS:
        raise ValueError('Unsupported context fields')
    result = dict(value)
    zone_name = os.environ.get('ASSISTANT_TIMEZONE', 'Asia/Shanghai')
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('Invalid configured ASSISTANT_TIMEZONE') from None
    if value.get('timezone', zone_name) != zone_name:
        raise ValueError('Context timezone must match configured instance timezone')
    for field in ('flow_id','stage_id'):
        if field in result:
            identity(result[field])
    for field in ('location','trigger'):
        if field in result:
            text(result[field], field)
    if 'states' in result:
        if not isinstance(result['states'], list):
            raise ValueError('States must be an array')
        for state in result['states']:
            text(state, 'State')
    raw_time = value.get('local_datetime')
    if 'local_datetime' in value and not isinstance(raw_time, str):
        raise ValueError('local_datetime must be an offset ISO datetime')
    if raw_time is not None:
        try:
            moment = datetime.fromisoformat(raw_time)
        except ValueError:
            raise ValueError('Invalid local_datetime') from None
        if moment.tzinfo is None or moment.utcoffset() != moment.astimezone(zone).utcoffset():
            raise ValueError('local_datetime offset must match instance timezone')
        moment = moment.astimezone(zone)
    elif frozen_at is not None:
        moment = datetime.fromisoformat(frozen_at).astimezone(zone)
    else:
        moment = datetime.now(timezone.utc).astimezone(zone)
    return result, moment, zone_name


def minutes(value):
    hour, minute = value.split(':')
    return int(hour)*60 + int(minute)


def intervals(windows):
    if not windows:
        return [(0,1440)]
    result = []
    for window in windows:
        start, end = minutes(window['start']), minutes(window['end'])
        result.extend([(start,end)] if start < end else [(start,1440),(0,end)])
    return sorted(result)


def more_specific(a, b):
    """Prove containment; never rank arbitrary rules by number of conditions."""
    strict = False
    for key in ('flow_id','stage_id'):
        if b[key] is not None and a[key] != b[key]:
            return False
        strict |= a[key] != b[key]
    for key in ('locations','states','triggers','weekdays'):
        av, bv = set(a['conditions'].get(key, [])), set(b['conditions'].get(key, []))
        if bv and (not av or not av <= bv):
            return False
        strict |= bool(av) and (not bv or av < bv)
    ai, bi = intervals(a['conditions'].get('time_windows')), intervals(b['conditions'].get('time_windows'))
    if not all(any(bs <= start and end <= be for bs, be in bi) for start, end in ai):
        return False
    strict |= ai != bi
    return strict


class BehaviorMatching:
    def behavior_candidates(self, *, context=None, candidate_ids=None, _frozen_at=None):
        ctx, moment, zone_name = validate_context(context, frozen_at=_frozen_at)
        if candidate_ids is not None:
            if not isinstance(candidate_ids, list) or len(candidate_ids) > 100:
                raise ValueError('At most 100 candidate IDs')
            for rid in candidate_ids:
                identity(rid)
            if len(set(candidate_ids)) != len(candidate_ids):
                raise ValueError('Candidate IDs must be unique')
        with closing(connect(self.path)) as c:
            # A consistent read snapshot across rules, flows and stages.
            c.execute('BEGIN')
            flows = {r['id']:decoded(r) for r in c.execute('SELECT * FROM routine_flows')}
            stages = {r['id']:dict(r) for r in c.execute('SELECT * FROM routine_stages')}
            rules = [decoded(r) for r in c.execute('SELECT * FROM behavior_rules ORDER BY id')]
        for field, pool in [('flow_id',flows),('stage_id',stages)]:
            if field in ctx and (ctx[field] not in pool or pool[ctx[field]]['is_void']):
                raise ValueError('Context refers to a missing/removed '+field)
        if ctx.get('stage_id'):
            parent = stages[ctx['stage_id']]['flow_id']
            if ctx.get('flow_id', parent) != parent:
                raise ValueError('Context stage/flow mismatch')
            ctx['flow_id'] = parent
        if candidate_ids is not None:
            known = {r['id'] for r in rules}
            if not set(candidate_ids) <= known:
                raise ValueError('Candidate rule not found')
            rules = [r for r in rules if r['id'] in candidate_ids]
        result = dict(evaluated_at=moment.isoformat(), timezone=zone_name,
                      matched=[], possible=[], excluded=[], superseded=[], conflicts=[],
                      missing_context=[], evaluation_complete=True, has_more=False, next_cursor=None)
        minute = moment.hour*60 + moment.minute
        for rule in rules:
            fid, sid = rule['flow_id'], rule['stage_id']
            flow, stage = flows.get(fid), stages.get(sid)
            item = dict(rule, rule_id=rule['id'], flow_name=flow['name'] if flow else None,
                        stage_name=stage['name'] if stage else None,
                        checks={}, unresolved=[], semantic_review_required=False)
            checks = item['checks']
            if rule['is_void'] or not rule['is_active'] or (fid and (not flow or flow['is_void'] or not flow['is_active'])) or (sid and (not stage or stage['is_void'] or not stage['is_active'])):
                checks['enabled'] = 'contradicted'
            for key, expected in [('flow_id',fid),('stage_id',sid)]:
                if expected is not None:
                    dimension = key.removesuffix('_id')
                    checks[dimension] = ('unknown' if key not in ctx else
                                         'satisfied' if ctx[key] == expected else 'contradicted')
            if flow and not ctx.get('flow_id'):
                days = flow['weekdays']
                if days:
                    checks['flow_weekday'] = 'satisfied' if moment.isoweekday() in days else 'contradicted'
                if flow['applicability_text']:
                    checks['flow_applicability'] = 'unknown'
                    item['semantic_review_required'] = True
            conditions = rule['conditions']
            if conditions.get('weekdays'):
                checks['weekday'] = 'satisfied' if moment.isoweekday() in conditions['weekdays'] else 'contradicted'
            if conditions.get('time_windows'):
                checks['time'] = 'satisfied' if any(start <= minute < end for start,end in intervals(conditions['time_windows'])) else 'contradicted'
            for key, field in [('locations','location'),('states','states'),('triggers','trigger')]:
                expected = conditions.get(key, [])
                if not expected:
                    continue
                actual = ctx.get(field)
                observed = actual if isinstance(actual, list) else [actual] if actual is not None else []
                if set(expected) & set(observed):
                    checks[field] = 'satisfied'
                else:
                    # Different words are not proof of incompatibility (e.g. 床上/家里).
                    checks[field] = 'unknown'
                    if field in ctx:
                        item['semantic_review_required'] = True
            item['unresolved'] = [k for k,v in checks.items() if v == 'unknown']
            verdict = 'excluded' if 'contradicted' in checks.values() else 'possible' if item['unresolved'] else 'matched'
            result[verdict].append(item)
        matched = result['matched']
        for general in matched:
            stronger = [r['rule_id'] for r in matched if r['kind'] == general['kind'] and more_specific(r,general)]
            if stronger:
                result['superseded'].append(dict(general, superseded_by=stronger))
        suppressed = {r['rule_id'] for r in result['superseded']}
        result['matched'] = [r for r in matched if r['rule_id'] not in suppressed]
        for index, left in enumerate(result['matched']):
            for right in result['matched'][index+1:]:
                # Unordered alternatives in the same stage need review, not a guessed winner.
                same_scope = (left['flow_id'],left['stage_id']) == (right['flow_id'],right['stage_id'])
                same_behavior = left['behavior'].strip() == right['behavior'].strip()
                if ((same_scope and left['kind'] == right['kind'] and not same_behavior) or
                        (same_behavior and left['kind'] != right['kind'])):
                    result['conflicts'].append(dict(rule_ids=[left['rule_id'],right['rule_id']],
                                                   reason='overlapping_arrangements_require_review'))
        missing = {k for r in result['possible'] for k in r['unresolved']}
        order = ['stage','flow','states','location','trigger','flow_applicability']
        result['missing_context'] = sorted(missing, key=lambda k:(order.index(k) if k in order else len(order),k))
        result['status'] = ('conflict' if result['conflicts'] else 'needs_context' if result['possible'] else
                            'matched' if result['matched'] else 'no_match')
        result['response_instruction'] = 'status为matched时，只按已保存的behavior或alternative给当前一项行动提醒，最多两句；不得补充未保存的动作、例子、休息方案或下一阶段安排。needs_context或conflict时只问必要问题。'
        result['candidate_scope'] = 'all_rules' if candidate_ids is None else 'selected_rules'
        result['requires_stage_confirmation'] = 'stage' in missing and 'stage_id' not in ctx
        if result['requires_stage_confirmation']:
            # Do not hand the model actionable bedtime advice before a stage is known.
            result['status'] = 'needs_context'
            result['next_question'] = '你现在是刚回家、等饭、饭后，还是准备睡觉？'
            allowed = {'id','rule_id','version','flow_id','stage_id','flow_name','stage_name',
                       'checks','unresolved','semantic_review_required','superseded_by'}
            for group in ('matched','possible','excluded','superseded'):
                result[group] = [{k:v for k,v in row.items() if k in allowed} for row in result[group]]
            result['response_instruction'] = '只向用户询问 next_question 并等待回答；不要推荐行动或展示候选准则。'
        return result
