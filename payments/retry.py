import random
import time
from functools import wraps

from django.db import DatabaseError

from .errors import DomainError


def retry_rolled_back(function):
    """Retry only database-confirmed rollback, never an ambiguous commit loss."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        deadline = time.monotonic() + 5
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except DatabaseError as exc:
                code = getattr(exc.__cause__, 'sqlstate', None)
                if code in {'40P01', '40001'} and attempt < 2 and time.monotonic() < deadline - 0.1:
                    time.sleep(random.uniform(0.005, 0.025))
                    continue
                if code == '55P03':
                    raise DomainError(
                        'A conflicting action is still running. Check status, then retry the same action key.',
                        'UNAVAILABLE',
                        503,
                    ) from exc
                # No speculative re-execution follows an uncertain acknowledgment.
                raise

    return wrapped
