"""Shared structured-input validation, independent of the business service."""
def positive(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(label + ' must be a positive integer')

def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' must be nonempty text')

def partial_date(value, precision):
    """Validate even when a duplicate import bypasses INSERT constraints."""
    import re
    from datetime import date
    if precision == 'unknown':
        if value is not None:
            raise ValueError('Unknown date precision requires a null date')
        return
    patterns = {'year':r'[0-9]{4}', 'month':r'[0-9]{4}-[0-9]{2}',
                'day':r'[0-9]{4}-[0-9]{2}-[0-9]{2}'}
    if not isinstance(precision,str) or precision not in patterns or not isinstance(value,str) or not re.fullmatch(patterns[precision],value):
        raise ValueError('Date does not match precision')
    parts = [int(p) for p in value.split('-')]
    date(*(parts+[1]*(3-len(parts))))


class DomainError(ValueError):
    """Stable structured refusal code, without exposing implementation details."""
    def __init__(self, code, message, *, candidates=None):
        super().__init__(message)
        self.code = code
        self.candidates = candidates
