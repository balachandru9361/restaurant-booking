from decimal import Decimal, InvalidOperation
from django import template

register = template.Library()


@register.filter
def inr(value, decimals=2):
    """1651859 -> 16,51,859.00  (Indian digit grouping)"""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return value
    decimals = int(decimals)
    whole, _, frac = f"{abs(d):.{decimals}f}".partition('.')
    if len(whole) > 3:
        head, tail, parts = whole[:-3], whole[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ','.join(parts + [tail])
    return ('-' if d < 0 else '') + whole + ('.' + frac if frac else '')
