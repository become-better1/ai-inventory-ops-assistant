"""User-authored consumption rules. These guide advice, never block purchases."""
from contextlib import closing
import json
from database import connect
from service_types import text, positive

def scope_value(value):
    if not isinstance(value,dict):
        raise ValueError('Rule scope must be a JSON object')
    def check(node):
        if isinstance(node,dict):
            if not all(isinstance(k,str) for k in node):
                raise ValueError('Scope keys must be text')
            for child in node.values():
                check(child)
        elif isinstance(node,list):
            for child in node:
                check(child)
        elif node is not None and type(node) not in (str,int,float,bool):
            raise ValueError('Scope must contain JSON values only')
    check(value)
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)

class RuleOperations:
    @staticmethod
    def _unique_rule(c,content,exclude=None):
        normalized=''.join(content.casefold().split())
        for row in c.execute('SELECT id,content FROM consumption_rules WHERE is_void=0'):
            if row['id']!=exclude and ''.join(row['content'].casefold().split())==normalized:
                raise ValueError('An identical rule already exists')

    def rules(self,*,active_only=False):
        if type(active_only) is not bool:
            raise ValueError('Active-only flag must be boolean')
        with closing(connect(self.path)) as c:
            query='SELECT * FROM consumption_rules WHERE is_void=0'
            if active_only:
                query+=' AND is_active=1'
            result=[]
            for row in c.execute(query+' ORDER BY created_at,id'):
                rule=dict(row)
                rule['scope']=json.loads(rule.pop('scope_json'))
                result.append(rule)
            return result

    def create_rule(self,request_key,*,content,scope=None,interpretation=None):
        # Caller must use this only for the user's explicit request to save a lasting rule.
        text(content,'Rule content')
        if interpretation is not None and not isinstance(interpretation,str):
            raise ValueError('Interpretation must be text or null')
        encoded=scope_value({} if scope is None else scope)
        def run(w):
            self._unique_rule(w.c,content)
            rid=w.insert('consumption_rules',dict(content=content,scope_json=encoded,
                                                 interpretation=interpretation))
            return dict(rule_id=rid)
        return self._write(request_key,'create_rule',dict(content=content,
            scope=json.loads(encoded),interpretation=interpretation),run)

    def update_rule(self,request_key,rule_id,*,expected_version,**changes):
        text(rule_id,'Rule ID')
        positive(expected_version,'Version')
        if not changes or not changes.keys()<={'content','scope','interpretation','is_active'}:
            raise ValueError('Unsupported or empty rule changes')
        if 'content' in changes:
            text(changes['content'],'Rule content')
        if 'is_active' in changes and type(changes['is_active']) is not bool:
            raise ValueError('Active flag must be boolean')
        if 'interpretation' in changes and changes['interpretation'] is not None and not isinstance(changes['interpretation'],str):
            raise ValueError('Interpretation must be text or null')
        values=dict(changes)
        if 'scope' in values:
            values['scope_json']=scope_value(values.pop('scope'))
        if 'is_active' in values:
            values['is_active']=int(values['is_active'])
        def run(w):
            rule=w.get('consumption_rules',rule_id)
            if rule['is_void'] or rule['version']!=expected_version:
                raise ValueError('Rule changed or removed; refresh first')
            applied=dict(values)
            if 'content' in applied:
                self._unique_rule(w.c,applied['content'],rule_id)
                if applied['content']!=rule['content']:
                    # Stale AI interpretations cannot silently survive a change in user intent.
                    applied.setdefault('scope_json','{}')
                    applied.setdefault('interpretation',None)
            w.update('consumption_rules',rule_id,applied)
            return dict(rule_id=rule_id)
        return self._write(request_key,'update_rule',
            dict(rule_id=rule_id,expected_version=expected_version,changes=values),run)

    def delete_rule(self,request_key,rule_id,*,expected_version):
        text(rule_id,'Rule ID')
        positive(expected_version,'Version')
        def run(w):
            rule=w.get('consumption_rules',rule_id)
            if rule['is_void'] or rule['version']!=expected_version:
                raise ValueError('Rule changed or removed; refresh first')
            w.update('consumption_rules',rule_id,dict(is_void=1))
            return dict(rule_id=rule_id)
        return self._write(request_key,'delete_rule',
            dict(rule_id=rule_id,expected_version=expected_version),run)

