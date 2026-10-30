from django import template

register = template.Library()


@register.filter
def cents(value):
    try:
        value = int(value or 0)
        sign = '-' if value < 0 else ''
        value = abs(value)
        return f'{sign}${value // 100:,}.{value % 100:02d}'
    except (ValueError, TypeError):
        return '$0.00'


@register.filter
def state_label(value):
    return {
        'REVIEW_PENDING': 'Under review',
        'READY': 'Queued',
        'SUBMITTED': 'Processing',
        'UNKNOWN': 'Outcome unknown',
        'COMPLETED': 'Completed',
        'FAILED': 'Failed',
        'CANCELLED': 'Cancelled',
        'PAYMENT_PENDING': 'Payment pending',
        'CLOSED_UNPAID': 'Closed unpaid',
        'OPEN': 'Open',
        'PAID': 'Paid',
        'PENDING': 'Pending',
        'SCHEDULED': 'Scheduled',
        'MISSED': 'Missed',
        'BLOCKED_FUNDS': 'Blocked: funds',
        'BLOCKED_CAPACITY': 'Blocked: capacity',
        'OUTGOING_RESTRICTED': 'Outgoing restricted',
    }.get(value, str(value).replace('_', ' ').title())


@register.filter
def initial(value):
    return (str(value or '?')[0]).upper()


@register.filter
def masked(value):
    value = str(value or '')
    return value[:1] + '•••' if value else 'Wallet customer'
