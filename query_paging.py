"""Opaque, filter-bound keyset cursors with data-change detection."""
import base64
import hashlib
import hmac
import json
import secrets
from time import time


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


class QueryPager:
    def __init__(self):
        self.key = secrets.token_bytes(32)

    def decode(self, token, query):
        if token is None:
            return None
        try:
            if not isinstance(token,str) or len(token)>4096:
                raise ValueError()
            body, signature = token.split('.')
            expected = hmac.new(self.key,body.encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature,expected):
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)))
            if value['query'] != digest(query) or time()-value['issued'] > 900:
                raise ValueError()
            return value
        except (ValueError,KeyError,TypeError,UnicodeError):
            raise ValueError('Query cursor invalid, expired or for different filters; restart query') from None

    def page(self, rows, query, limit, *, decoded=None, anchor=None):
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError('Limit must be between 1 and 100')
        ordered = sorted(rows,key=lambda r:r['id'])
        fingerprint = digest(ordered)
        if decoded is not None and decoded['data'] != fingerprint:
            raise ValueError('Query results changed; restart query')
        remaining = ordered if decoded is None else [r for r in ordered if r['id']>decoded['after']]
        selected = remaining[:limit]
        more = len(remaining)>limit
        token = None
        if more:
            value = dict(query=digest(query),data=fingerprint,after=selected[-1]['id'],anchor=anchor,
                         issued=time() if decoded is None else decoded['issued'])
            body = base64.urlsafe_b64encode(json.dumps(value,sort_keys=True).encode()).decode().rstrip('=')
            token = body+'.'+hmac.new(self.key,body.encode(),hashlib.sha256).hexdigest()
        return dict(items=selected,has_more=more,next_cursor=token)
