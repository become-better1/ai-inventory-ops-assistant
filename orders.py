"""Conservative purchase identity matching; no screenshot/OCR processing here."""
from service_types import text

def normalized(value):
    return ''.join((value or '').casefold().split())

def identity(row):
    if row['merchant'] and row['external_order_id'] and row['external_line_id']:
        return (row['merchant'].strip().casefold(),row['external_order_id'],row['external_line_id'])
    return None

class PossibleOrderDuplicate(ValueError):
    def __init__(self,candidates):
        super().__init__('Possible repeated purchase; resolve identity before importing')
        self.candidates=candidates

class OrderConflict(ValueError):
    def __init__(self,purchase_id):
        super().__init__('Same order line has conflicting data; correct the existing record instead')
        self.purchase_id=purchase_id

def validate_order_input(merchant,order_id,line_id,source_kind,confirmed_distinct):
    for value in (merchant,order_id,line_id):
        if value is not None:
            text(value,'Order reference')
    if line_id is not None and order_id is None:
        raise ValueError('Order line ID requires an order ID')
    if source_kind not in ('natural_language','photo','order_image','correction'):
        raise ValueError('Invalid purchase source')
    if type(confirmed_distinct) is not bool:
        raise ValueError('Distinct-order flag must be boolean')

def match_purchase(c,payload):
    key=identity(payload)
    possible=[]
    for row in c.execute('SELECT * FROM purchase_records WHERE is_void=0'):
        prior_key=identity(row)
        if key is not None and key==prior_key:
            conflicts = (normalized(row['product_name'])!=normalized(payload['name']) or
                row['quantity']!=payload['quantity'] or row['unit']!=payload['unit'] or
                row['import_mode']!=('history_only' if payload['history_only'] else 'with_inventory'))
            for field in ('total_amount_minor','currency','purchase_date'):
                if row[field] is not None and payload[field] is not None and row[field]!=payload[field]:
                    conflicts=True
            if conflicts:
                raise OrderConflict(row['id'])
            ids=[r[0] for r in c.execute(
                'SELECT id FROM items WHERE purchase_record_id=? AND is_void=0 ORDER BY created_at,id',(row['id'],))]
            return dict(purchase_id=row['id'],item_id=ids[0] if ids else None,
                        item_ids=ids,duplicate=True)
        # Distinct reliable line IDs, orders, or merchants are separate purchases.
        if row['merchant'] and payload['merchant'] and row['merchant'].strip().casefold()!=payload['merchant'].strip().casefold():
            continue
        if row['external_order_id'] and payload['external_order_id'] and row['external_order_id']!=payload['external_order_id']:
            continue
        if key and prior_key and key!=prior_key:
            continue
        same_name=normalized(row['product_name'])==normalized(payload['name'])
        shared_order=bool(row['external_order_id'] and row['external_order_id']==payload['external_order_id'])
        image_candidate=payload['source_kind']=='order_image' and (
            row['quantity']==payload['quantity'] and row['unit']==payload['unit'] and
            (row['total_amount_minor'] is None or payload['total_amount_minor'] is None or
             row['total_amount_minor']==payload['total_amount_minor']) and
            (row['currency'] is None or payload['currency'] is None or row['currency']==payload['currency']) and
            (row['purchase_date'] is None or payload['purchase_date'] is None or
             row['purchase_date']==payload['purchase_date']))
        if same_name and (shared_order or image_candidate):
            possible.append(dict(purchase_id=row['id'],name=row['product_name'],
                quantity=row['quantity'],total_amount_minor=row['total_amount_minor'],
                currency=row['currency'],external_order_id=row['external_order_id'],
                external_line_id=row['external_line_id']))
    if possible and not payload['confirmed_distinct']:
        raise PossibleOrderDuplicate(possible)
    return None

def validate_unique_orders(c):
    seen=set()
    for row in c.execute('SELECT * FROM purchase_records WHERE is_void=0'):
        key=identity(row)
        if key is not None:
            if key in seen:
                raise ValueError('Duplicate active merchant/order/line identity')
            seen.add(key)

