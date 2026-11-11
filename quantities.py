"""Correct current totals without rewriting historical purchases."""
from contextlib import closing
from database import connect
from service_types import text, positive

def normalized(value):
    return ''.join(value.casefold().split())

class QuantityOperations:
    @staticmethod
    def _quantity_plan(c, *, name, unit, target_total, removals=None):
        text(name,'Item name')
        text(unit,'Unit')
        if type(target_total) is not int or target_total<0:
            raise ValueError('Target total must be a nonnegative integer')
        if removals is not None:
            if not isinstance(removals,dict):
                raise ValueError('Removals must map item IDs to quantities')
            for key,value in removals.items():
                text(key,'Item ID')
                positive(value,'Removal quantity')
        rows=[dict(r) for r in c.execute('SELECT * FROM current_inventory ORDER BY id')
              if normalized(r['name'])==normalized(name) and r['unit']==unit]
        current=sum(r['quantity'] for r in rows)
        delta=target_total-current
        allocation={} if removals is None else dict(removals)
        needs_allocation=False
        if delta>=0:
            if allocation:
                raise ValueError('An increase or unchanged total cannot remove items')
        else:
            if removals is None:
                if len(rows)==1:
                    allocation={rows[0]['id']:-delta}
                elif target_total==0:
                    allocation={r['id']:r['quantity'] for r in rows}
                else:
                    needs_allocation=True
            if not needs_allocation:
                available={r['id']:r['quantity'] for r in rows}
                if any(key not in available or value>available[key] for key,value in allocation.items()):
                    raise ValueError('Removal exceeds selected current inventory')
                if sum(allocation.values())!=-delta:
                    raise ValueError('Removal allocation does not match target total')
        return dict(kind='correct_quantity',name=name.strip(),unit=unit,target_total=target_total,
            current_total=current,delta=delta,items=rows,removals=allocation,
            needs_allocation=needs_allocation,unchanged=delta==0,
            requires_confirmation=len(allocation)>1)

    def preview_quantity_correction(self,*,name,unit='件',target_total,removals=None):
        """Read-only scope: normalized exact name + exact unit; not semantic category search."""
        with closing(connect(self.path)) as c:
            c.execute('BEGIN')
            return self._quantity_plan(c,name=name,unit=unit,target_total=target_total,removals=removals)

    def correct_quantity(self,request_key,*,plan,confirmed=False):
        if not isinstance(plan,dict) or plan.get('kind')!='correct_quantity':
            raise ValueError('Preview the quantity correction first')
        if type(confirmed) is not bool:
            raise ValueError('Confirmation must be boolean')
        if plan.get('needs_allocation'):
            raise ValueError('Select which groups to reduce; preview again with removals')
        if plan.get('requires_confirmation') and not confirmed:
            raise ValueError('Confirm the affected groups before a batch correction')
        if plan.get('unchanged'):
            raise ValueError('Current total already matches; no correction needed')
        try:
            args={key:plan[key] for key in ('name','unit','target_total','removals')}
        except KeyError:
            raise ValueError('Invalid quantity preview') from None
        def run(w):
            current=self._quantity_plan(w.c,**args)
            if current!=plan:
                raise ValueError('Inventory or correction scope changed; preview again')
            affected=[]
            if current['delta']>0:
                iid=w.insert('items',dict(name=current['name'],unit=current['unit'],
                    quantity=current['delta'],source_kind='correction'))
                w.event(iid,'quantity_corrected')
                affected.append(iid)
            else:
                for iid,quantity in sorted(current['removals'].items()):
                    original=w.get('items',iid)
                    target=iid
                    if quantity<original['quantity']:
                        w.update('items',iid,dict(quantity=original['quantity']-quantity))
                        child={k:v for k,v in original.items()
                            if k not in ('id','created_at','updated_at','version','is_void')}
                        child.update(quantity=quantity,parent_item_id=iid,source_kind='correction')
                        target=w.insert('items',child)
                        w.event(target,'split')
                    w.update('items',target,dict(ownership_status='count_corrected'))
                    w.event(target,'quantity_corrected')
                    affected.append(target)
            return dict(target_total=current['target_total'],unit=current['unit'],
                        affected_item_ids=affected)
        return self._write(request_key,'correct_quantity',
                           dict(plan=plan,confirmed=confirmed),run)

