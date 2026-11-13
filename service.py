"""Structured local business API. No natural-language parsing or network transport."""
from contextlib import closing
import hashlib
import json
from uuid import uuid4
from database import connect, initialize, DEFAULT_DB, now
from rooms import RoomOperations
from rules import RuleOperations
from behaviors import BehaviorOperations, validate_behaviors
from behavior_matching import BehaviorMatching
from behavior_plans import BehaviorPlanOperations
from quantities import QuantityOperations
from orders import validate_order_input, match_purchase, validate_unique_orders
from service_types import positive, text, partial_date, DomainError
from file_lock import file_lock, lock_path

TABLES = {'items', 'purchase_records', 'item_events', 'rooms', 'consumption_rules', 'routine_flows', 'routine_stages', 'behavior_rules'}

def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)

def money(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError('Amount must be a nonnegative integer in minor currency units')

class PossibleDuplicate(ValueError):
    """A separate record needs clarification; candidates are existing inventory IDs."""
    def __init__(self, candidates):
        super().__init__('Possible existing item; match it or confirm a distinct addition')
        self.candidates = candidates

class _Writer:
    def __init__(self, conn, op):
        self.c, self.op = conn, op
        self.changes = {}

    def get(self, table, key):
        if table not in TABLES:
            raise ValueError('Unsupported entity')
        row = self.c.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
        if not row:
            raise ValueError('Record not found')
        return dict(row)

    def change(self, table, key, before):
        token = table, key
        if token not in self.changes:
            self.changes[token] = before

    def new_id(self):
        return str(uuid4())

    def insert(self, table, values):
        if table not in TABLES:
            raise ValueError('Unsupported entity')
        values = dict(values, id=self.new_id(), created_at=now(), updated_at=now(),
                      version=1, is_void=0)
        self.c.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
        self.change(table, values['id'], None)
        return values['id']

    def update(self, table, key, values):
        before = self.get(table, key)
        self.change(table, key, before)
        values = dict(values, version=before['version']+1, updated_at=now())
        self.c.execute(f"UPDATE {table} SET {','.join(k+'=?' for k in values)} WHERE id=?",
                       (*values.values(), key))

    def event(self, item_id, kind, amount=None, currency=None):
        item = self.get('items', item_id)
        return self.insert('item_events', dict(operation_id=self.op, item_id=item_id,
            purchase_record_id=item['purchase_record_id'], event_type=kind,
            quantity=item['quantity'], amount_minor=amount, currency=currency))

    def validate(self):
        validate_unique_orders(self.c)
        validate_behaviors(self.c)
        rule_contents = set()
        for row in self.c.execute('SELECT content FROM consumption_rules WHERE is_void=0'):
            normalized = ''.join(row['content'].casefold().split())
            if normalized in rule_contents:
                raise ValueError('An identical rule already exists; resolve before restoring')
            rule_contents.add(normalized)
        items = {r['id']:dict(r) for r in self.c.execute('SELECT * FROM items WHERE is_void=0')}
        purchases = {r['id']:dict(r) for r in self.c.execute('SELECT * FROM purchase_records WHERE is_void=0')}
        rooms = {r[0] for r in self.c.execute('SELECT id FROM rooms WHERE is_void=0')}
        for item in items.values():
            if item['room_id'] is not None and item['room_id'] not in rooms:
                raise ValueError('Inactive room reference')
            if item['purchase_record_id'] is not None and item['purchase_record_id'] not in purchases:
                raise ValueError('Inactive purchase reference')
            seen = {item['id']}
            parent = item['parent_item_id']
            while parent:
                if parent not in items or parent in seen:
                    raise ValueError('Invalid/cyclic split ancestry')
                seen.add(parent)
                parent = items[parent]['parent_item_id']
        events = self.c.execute('SELECT * FROM item_events WHERE is_void=0').fetchall()
        for event in events:
            if event['item_id'] and event['item_id'] not in items:
                raise ValueError('Inactive item reference')
            if event['purchase_record_id'] and event['purchase_record_id'] not in purchases:
                raise ValueError('Inactive purchase reference')
            if event['item_id'] and event['purchase_record_id'] != items[event['item_id']]['purchase_record_id']:
                raise ValueError('Event purchase mismatch')
        for pid, p in purchases.items():
            groups = [i for i in items.values() if i['purchase_record_id']==pid]
            if sum(i['quantity'] for i in groups)>p['quantity'] or any(i['unit']!=p['unit'] for i in groups):
                raise ValueError('Purchase quantity/unit mismatch')
            deductions = [e for e in events if e['purchase_record_id']==pid and e['event_type'] in ('returned','cancelled')]
            if any(e['amount_minor'] is not None and e['currency']!=p['currency'] for e in deductions):
                raise ValueError('Currency mismatch')
            if p['total_amount_minor'] is not None and sum(e['amount_minor'] or 0 for e in deductions)>p['total_amount_minor']:
                raise ValueError('Deductions exceed purchase amount')

    def snapshots(self):
        for seq, ((table,key),before) in enumerate(self.changes.items(),1):
            after = self.get(table,key)
            self.c.execute('INSERT INTO operation_changes VALUES (?,?,?,?,?,?,?,?)',
                (self.op, seq, table, key, 'insert' if before is None else 'update',
                 None if before is None else dump(before), dump(after), after['version']))

class Assistant(RoomOperations, RuleOperations, QuantityOperations, BehaviorOperations, BehaviorMatching, BehaviorPlanOperations):
    def __init__(self, path=DEFAULT_DB):
        self.path = initialize(path)

    def _write(self, request_key, kind, payload, action, undo_of=None):
        text(request_key, 'Request key')
        digest = hashlib.sha256(dump({'kind':kind,'payload':payload}).encode()).hexdigest()
        with file_lock(lock_path(self.path,'write'),timeout=5), closing(connect(self.path)) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                old = c.execute('SELECT * FROM operations WHERE request_key=?',(request_key,)).fetchone()
                if old:
                    if old['request_hash'] != digest:
                        raise DomainError('KEY_CONFLICT','Request key reused with different content')
                    c.rollback()
                    return json.loads(old['response_json'])
                op = str(uuid4())
                c.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)',
                          (op,request_key,digest,kind,kind,'{}',undo_of,now()))
                writer = _Writer(c,op)
                result = dict(action(writer), operation_id=op)
                writer.validate()
                writer.snapshots()
                c.execute('UPDATE operations SET response_json=? WHERE id=?',(dump(result),op))
                c.commit()
                return result
            except BaseException:
                c.rollback()
                raise

    def record_purchase(self, request_key, *, name, quantity, total_amount_minor=None,
                        currency=None, unit='件', history_only=False, received=False,
                        purchase_date=None, date_precision='unknown', merchant=None,
                        external_order_id=None, external_line_id=None,
                        source_kind='natural_language', confirmed_distinct=False):
        text(name,'Name')
        text(unit,'Unit')
        positive(quantity,'Quantity')
        money(total_amount_minor)
        partial_date(purchase_date,date_precision)
        validate_order_input(merchant,external_order_id,external_line_id,source_kind,confirmed_distinct)
        merchant = None if merchant is None else merchant.strip()
        external_order_id = None if external_order_id is None else external_order_id.strip()
        external_line_id = None if external_line_id is None else external_line_id.strip()
        if type(history_only) is not bool or type(received) is not bool:
            raise ValueError('Flags must be booleans')
        if total_amount_minor is not None and currency is None:
            raise ValueError('Known amount needs a currency')
        if currency is not None and (not isinstance(currency,str) or len(currency)!=3 or not currency.isascii() or not currency.isalpha() or currency!=currency.upper()):
            raise ValueError('Currency must be three uppercase letters')
        payload = dict(name=name,quantity=quantity,total_amount_minor=total_amount_minor,
                       currency=currency,unit=unit,history_only=history_only,received=received,
                       purchase_date=purchase_date,date_precision=date_precision,
                       merchant=merchant,external_order_id=external_order_id,
                       external_line_id=external_line_id,source_kind=source_kind,
                       confirmed_distinct=confirmed_distinct)
        def run(w):
            matched = match_purchase(w.c,payload)
            if matched is not None:
                return matched
            pid = w.insert('purchase_records',dict(product_name=name,quantity=quantity,
                unit=unit,total_amount_minor=total_amount_minor,currency=currency,
                purchase_date=purchase_date,date_precision=date_precision,
                import_mode='history_only' if history_only else 'with_inventory',
                source_kind=source_kind,merchant=merchant,
                external_order_id=external_order_id,external_line_id=external_line_id))
            iid = None
            if not history_only:
                iid = w.insert('items',dict(name=name,quantity=quantity,unit=unit,
                    purchase_record_id=pid,source_kind=source_kind,
                    delivery_status='received' if received else 'unknown'))
                w.event(iid,'recorded')
            return dict(purchase_id=pid,item_id=iid,duplicate=False)
        return self._write(request_key,'record_purchase',payload,run)

    def record_owned(self, request_key, *, name, quantity=1, unit='件',
                     category=None, brand=None, model=None,
                     usage_status='unknown', condition_status='unknown',
                     location_detail=None, feedback=None, confirmed_distinct=False):
        """Record confirmed current possession, never a purchase or a total-count correction.

        If a similarly named held item exists, callers must resolve identity first.
        confirmed_distinct means the user clearly identified additional real items.
        """
        text(name, 'Name')
        text(unit, 'Unit')
        positive(quantity, 'Quantity')
        if type(confirmed_distinct) is not bool:
            raise ValueError('Distinct-addition flag must be boolean')
        for value in (category, brand, model, location_detail, feedback):
            if value is not None and not isinstance(value, str):
                raise ValueError('Optional attributes must be text or null')
        if usage_status not in ('in_use', 'not_in_use', 'unknown'):
            raise ValueError('Invalid usage status')
        if condition_status not in ('normal', 'damaged', 'unknown'):
            raise ValueError('Invalid condition status')
        name, unit = name.strip(), unit.strip()
        payload = dict(name=name, quantity=quantity, unit=unit, category=category,
            brand=brand, model=model, usage_status=usage_status,
            condition_status=condition_status, location_detail=location_detail,
            feedback=feedback, confirmed_distinct=confirmed_distinct)
        def normalized(value):
            return ''.join((value or '').casefold().split())
        def run(w):
            candidates = []
            for row in w.c.execute('SELECT * FROM current_inventory'):
                same_name = normalized(row['name']) == normalized(name)
                same_model = bool(model and normalized(model) == normalized(row['model'])
                    and (not brand or not row['brand'] or normalized(brand) == normalized(row['brand'])))
                if same_name or same_model:
                    candidates.append(dict(item_id=row['id'], name=row['name'],
                        quantity=row['quantity'], unit=row['unit'], version=row['version']))
            if candidates and not confirmed_distinct:
                raise PossibleDuplicate(candidates)
            iid = w.insert('items', dict(name=name, quantity=quantity, unit=unit,
                category=category, brand=brand, model=model,
                usage_status=usage_status, condition_status=condition_status,
                location_detail=location_detail, feedback=feedback,
                source_kind='natural_language'))
            w.event(iid, 'recorded')
            return dict(item_id=iid, quantity=quantity, unit=unit, purchase_id=None)
        return self._write(request_key, 'record_owned', payload, run)

    def inventory(self, *, room_id=None, unclassified=False):
        if type(unclassified) is not bool or (unclassified and room_id is not None):
            raise ValueError('Choose a room or unclassified inventory')
        if room_id is not None:
            text(room_id, 'Room ID')
        with closing(connect(self.path)) as c:
            query, args = 'SELECT * FROM current_inventory', ()
            if room_id is not None:
                self._active_room(c, room_id)
                query += ' WHERE room_id=?'
                args = (room_id,)
            elif unclassified:
                query += ' WHERE room_id IS NULL'
            return [dict(r) for r in c.execute(query+' ORDER BY created_at,id', args)]

    def update_item(self, request_key, item_id, *, expected_version, **changes):
        allowed = {'usage_status','condition_status','feedback','location_detail'}
        if not changes or not changes.keys() <= allowed:
            raise ValueError('Only usage, condition, feedback and location detail supported')
        positive(expected_version,'Version')
        if any(v is not None and not isinstance(v,str) for v in changes.values()):
            raise ValueError('Attribute values must be text or null')
        def run(w):
            item = w.get('items',item_id)
            if item['is_void'] or item['version']!=expected_version:
                raise ValueError('Item changed; refresh before editing')
            w.update('items',item_id,changes)
            w.event(item_id,'history_corrected')
            return dict(item_id=item_id)
        return self._write(request_key,'update_item',
            dict(item_id=item_id,expected_version=expected_version,changes=changes),run)

    def return_item(self, request_key, item_id, *, quantity, expected_version,
                    amount_minor=None, infer_amount=False):
        """No arrival confirmation. Inference requires caller to confirm equal-price allocation."""
        positive(quantity,'Quantity')
        positive(expected_version,'Version')
        money(amount_minor)
        if type(infer_amount) is not bool or (infer_amount and amount_minor is not None):
            raise ValueError('Choose explicit amount or equal-price inference')
        def run(w):
            item = w.get('items',item_id)
            if item['is_void'] or item['ownership_status']!='held' or item['version']!=expected_version:
                raise ValueError('Item changed or no longer held')
            if quantity>item['quantity']:
                raise ValueError('Return exceeds held quantity')
            amount, currency = amount_minor, None
            pid = item['purchase_record_id']
            if pid:
                p = w.get('purchase_records',pid)
                currency = p['currency']
                if infer_amount:
                    if p['total_amount_minor'] is None:
                        raise ValueError('Purchase amount unknown; provide amount or leave unknown')
                    prior = w.c.execute("""SELECT quantity,amount_minor FROM item_events
                        WHERE purchase_record_id=? AND is_void=0 AND event_type IN ('returned','cancelled')""",(pid,)).fetchall()
                    if any(e['amount_minor'] is None for e in prior):
                        raise ValueError('Previous deduction unknown; provide amount or leave unknown')
                    remaining_qty = p['quantity']-sum(e['quantity'] or 0 for e in prior)
                    remaining_money = p['total_amount_minor']-sum(e['amount_minor'] for e in prior)
                    amount = remaining_money if quantity==remaining_qty else p['total_amount_minor']*quantity//p['quantity']
                    if amount>remaining_money:
                        raise ValueError('Allocation ambiguous; provide amount')
            elif infer_amount:
                raise ValueError('No purchase record for allocation')
            if amount is not None and currency is None:
                raise ValueError('Return currency unknown')
            target = item_id
            if quantity<item['quantity']:
                w.update('items',item_id,{'quantity':item['quantity']-quantity})
                child = {k:v for k,v in item.items() if k not in {'id','created_at','updated_at','version','is_void'}}
                child.update(quantity=quantity,parent_item_id=item_id)
                target = w.insert('items',child)
                w.event(target,'split')
            w.update('items',target,{'ownership_status':'returned'})
            event = w.event(target,'returned',amount,currency)
            return dict(item_id=target,event_id=event,deduction_minor=amount,
                        amount_incomplete=amount is None)
        return self._write(request_key,'return_item',dict(item_id=item_id,quantity=quantity,
            expected_version=expected_version,amount_minor=amount_minor,infer_amount=infer_amount),run)

    def purchase_records(self, *, purchase_id=None):
        if purchase_id is not None:
            text(purchase_id, 'Purchase ID')
        with closing(connect(self.path)) as c:
            query='SELECT * FROM purchase_records WHERE is_void=0'
            args=()
            if purchase_id is not None:
                query+=' AND id=?'
                args=(purchase_id,)
            return [dict(r) for r in c.execute(query+' ORDER BY created_at,id',args)]

    def correct_purchase_amount(self, request_key, purchase_id, *, expected_version,
                                total_amount_minor, currency=None):
        text(purchase_id, 'Purchase ID')
        positive(expected_version, 'Version')
        money(total_amount_minor)
        if currency is not None and (not isinstance(currency,str) or len(currency)!=3
                or not currency.isascii() or not currency.isalpha() or currency!=currency.upper()):
            raise ValueError('Currency must be three uppercase letters')
        def run(w):
            purchase=w.get('purchase_records',purchase_id)
            if purchase['is_void'] or purchase['version']!=expected_version:
                raise ValueError('Purchase changed or removed; query current version')
            selected=purchase['currency'] if currency is None else currency
            if purchase['currency'] is not None and selected!=purchase['currency']:
                raise ValueError('Amount correction cannot change a known currency')
            if total_amount_minor is not None and selected is None:
                raise ValueError('Known purchase amount requires currency')
            w.update('purchase_records',purchase_id,
                     dict(total_amount_minor=total_amount_minor,currency=selected))
            w.insert('item_events',dict(operation_id=w.op,purchase_record_id=purchase_id,
                event_type='history_corrected',details_json=dump(dict(
                    corrected_purchase_id=purchase_id,old_amount_minor=purchase['total_amount_minor'],
                    new_amount_minor=total_amount_minor))))
            return dict(purchase_id=purchase_id,purchase_version=expected_version+1,
                        total_amount_minor=total_amount_minor,currency=selected)
        return self._write(request_key,'correct_purchase_amount',dict(purchase_id=purchase_id,
            expected_version=expected_version,total_amount_minor=total_amount_minor,currency=currency),run)

    def return_events(self, *, purchase_id=None):
        """Read effective return records, including current versions for corrections."""
        if purchase_id is not None:
            text(purchase_id, 'Purchase ID')
        with closing(connect(self.path)) as c:
            query = "SELECT * FROM item_events WHERE is_void=0 AND event_type='returned'"
            args = ()
            if purchase_id is not None:
                query += ' AND purchase_record_id=?'
                args = (purchase_id,)
            return [dict(r) for r in c.execute(query+' ORDER BY created_at,id', args)]

    def correct_return_amount(self, request_key, event_id, *, expected_version,
                              amount_minor, currency=None):
        """Correct the original deduction; never create another return or change stock."""
        text(event_id, 'Return event ID')
        positive(expected_version, 'Version')
        money(amount_minor)
        if currency is not None and (not isinstance(currency,str) or len(currency)!=3
                or not currency.isascii() or not currency.isalpha() or currency!=currency.upper()):
            raise ValueError('Currency must be three uppercase letters')
        def run(w):
            event = w.get('item_events', event_id)
            if event['is_void'] or event['event_type']!='returned' or event['version']!=expected_version:
                raise ValueError('Return record changed, removed or not a return')
            selected_currency = event['currency'] if currency is None else currency
            if event['currency'] is not None and selected_currency!=event['currency']:
                raise ValueError('Correcting an amount cannot change its currency')
            if amount_minor is not None and selected_currency is None:
                raise ValueError('Known return amount needs a currency')
            w.update('item_events', event_id,
                     dict(amount_minor=amount_minor, currency=selected_currency))
            w.insert('item_events', dict(operation_id=w.op, item_id=event['item_id'],
                purchase_record_id=event['purchase_record_id'], event_type='history_corrected',
                details_json=dump(dict(corrected_event_id=event_id,
                    old_amount_minor=event['amount_minor'],new_amount_minor=amount_minor))))
            return dict(event_id=event_id,event_version=expected_version+1,
                        deduction_minor=amount_minor,amount_incomplete=amount_minor is None)
        return self._write(request_key,'correct_return_amount',dict(event_id=event_id,
            expected_version=expected_version,amount_minor=amount_minor,currency=currency),run)

    def undo(self, request_key, operation_id):
        def run(w):
            original = w.c.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
            if not original or original['undo_of_operation_id']:
                raise ValueError('Operation missing or is itself an undo')
            if w.c.execute('SELECT 1 FROM operations WHERE undo_of_operation_id=? AND id<>?',
                           (operation_id,w.op)).fetchone():
                raise ValueError('Operation already undone')
            changes = w.c.execute('SELECT * FROM operation_changes WHERE operation_id=? ORDER BY sequence_no DESC',
                                   (operation_id,)).fetchall()
            if not changes:
                raise ValueError('No reversible changes')
            for change in changes:
                current = w.get(change['entity_type'],change['entity_id'])
                if current['version']!=change['expected_version'] or dump(current)!=change['after_json']:
                    raise ValueError('Later edits prevent automatic undo')
            # Vacate touched unique stage positions before restoring an entire order.
            stage_changes = [ch for ch in changes if ch['entity_type']=='routine_stages']
            maximum = w.c.execute('SELECT coalesce(max(position),0) FROM routine_stages').fetchone()[0]
            for index, change in enumerate(stage_changes,1):
                current = w.get('routine_stages',change['entity_id'])
                w.change('routine_stages',current['id'],current)
                w.c.execute('UPDATE routine_stages SET position=? WHERE id=?',
                            (maximum+index,current['id']))
            for change in changes:
                if change['before_json'] is None:
                    w.update(change['entity_type'],change['entity_id'],{'is_void':1})
                else:
                    before = json.loads(change['before_json'])
                    for k in ('id','created_at','updated_at','version'):
                        before.pop(k)
                    w.update(change['entity_type'],change['entity_id'],before)
            # Validation rejects remaining live references to any newly voided entity.
            return dict(undone_operation_id=operation_id)
        return self._write(request_key,'undo',{'operation_id':operation_id},run,undo_of=operation_id)

    def spending(self):
        with closing(connect(self.path)) as c:
            purchases = c.execute('SELECT * FROM purchase_records WHERE is_void=0').fetchall()
            events = c.execute("""SELECT * FROM item_events WHERE is_void=0
                AND event_type IN ('returned','cancelled','sale_income_received')""").fetchall()
            result = {}
            unknown_currency = 0
            for row, kind in [(p,'purchase') for p in purchases]+[(e,'event') for e in events]:
                currency = row['currency']
                if currency is None:
                    unknown_currency += 1
                    continue
                group = result.setdefault(currency,dict(purchases_minor=0,deductions_minor=0,
                    sale_income_minor=0,unknown_amount_count=0))
                amount = row['total_amount_minor'] if kind=='purchase' else row['amount_minor']
                if amount is None:
                    group['unknown_amount_count'] += 1
                else:
                    key = 'purchases_minor' if kind=='purchase' else (
                        'sale_income_minor' if row['event_type']=='sale_income_received' else 'deductions_minor')
                    group[key] += amount
            for group in result.values():
                group['known_net_minor'] = group['purchases_minor']-group['deductions_minor']-group['sale_income_minor']
                group['complete'] = group['unknown_amount_count']==0 and unknown_currency==0
                group['net_minor'] = group['known_net_minor'] if group['complete'] else None
            return dict(by_currency=result,unknown_currency_count=unknown_currency,
                        complete=unknown_currency==0 and all(g['complete'] for g in result.values()))

