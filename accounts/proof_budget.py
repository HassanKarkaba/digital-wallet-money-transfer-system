"""Persist failed bound-proof attempts without charging successful proofs."""

from contextlib import contextmanager

from django.db import transaction

from .models import RateBucket
from .services import _bucket_key, rate_limit


@contextmanager
def bound_proof_budget(owner):
    scope = 'bound-mfa-proof'
    # Reserve an attempt before acquiring authority. Concurrent invalid guesses
    # cannot all pass an uncharged precheck; success refunds only its own slot.
    with transaction.atomic():
        rate_limit(scope, owner, 5, 900)
        bucket = RateBucket.objects.get(pk=_bucket_key(scope, owner))
        origin = bucket.started_at
    keep = False
    try:
        yield
    except Exception as exc:
        keep = bool(getattr(exc, 'keep_proof_attempt', False))
        raise
    finally:
        if not keep:
            with transaction.atomic():
                current = RateBucket.objects.select_for_update().get(pk=bucket.pk)
                if current.started_at == origin and current.count > 0:
                    current.count -= 1
                    current.save(update_fields=['count'])
