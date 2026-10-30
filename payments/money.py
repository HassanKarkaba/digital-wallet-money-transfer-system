import re

from .errors import DomainError

POLICY = 'DWMT-P1'
OP_LIMIT = 250000
DAY_LIMIT = 500000
WALLET_CAP = 1000000
REVIEW_THRESHOLD = 100000


def parse_amount(value):
    if not isinstance(value, str) or not re.fullmatch(
        r'[0-9]+(?:\.[0-9]{1,2})?', value.strip(), flags=re.ASCII
    ):
        raise DomainError('Enter a USD amount using digits and at most two decimal places.')
    text = value.strip()
    if len(text) > 12:
        raise DomainError('Amount exceeds the $2,500.00 operation limit.', 'LIMIT')
    whole, _, fraction = text.partition('.')
    cents = int(whole) * 100 + int((fraction + '00')[:2])
    if not 1 <= cents <= OP_LIMIT:
        raise DomainError('Amount must be $0.01 to $2,500.00.', 'LIMIT')
    return cents


def validate_cents(value):
    if type(value) is not int or not 1 <= value <= OP_LIMIT:
        raise DomainError('Invalid amount in cents.', 'INVALID')
    return value
