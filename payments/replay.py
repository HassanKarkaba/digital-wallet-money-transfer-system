"""Accepted reference matching and minimal legacy proof retention."""

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from .errors import DomainError
from .models import Intent, LegacyIntentReference, ReplayKey

MAX_PURGE_BATCH = 500


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise DomainError('Invalid reference.') from error


def _lock_reference_owners(intent_ids, using):
    owner_ids = (
        Intent.objects.using(using)
        .filter(pk__in=intent_ids)
        .order_by('owner_id')
        .values_list('owner_id', flat=True)
        .distinct()
    )
    return list(
        get_user_model()
        .objects.using(using)
        .select_for_update()
        .filter(pk__in=owner_ids)
        .order_by('id')
        .values_list('id', flat=True)
    )


def match_accepted_reference(owner, action_key, intent_id=None, family=None):
    """Match exact owned evidence; never infer an original proof association."""
    key = _uuid(action_key)
    query = ReplayKey.objects.filter(owner=owner, action_key=key)
    reference = None
    if intent_id is not None:
        intent_id = _uuid(intent_id)
        reference = LegacyIntentReference.objects.filter(pk=intent_id, owner=owner).first()
        if reference is not None and family is not None and family != reference.family:
            raise DomainError('This reference belongs to a different instruction family.', 'CONFLICT', 409)
        identity = Q(intent_id=intent_id)
        if reference is not None:
            # A fingerprint proves the old input, not which identical proof was
            # originally paired with an accepted key. New explicit links stay strict.
            query = query.filter(family=reference.family, digest=reference.digest)
            identity |= Q(intent_id__isnull=True)
        query = query.filter(identity)
    if family is not None:
        query = query.filter(family=family)
    matches = list(query.order_by('pk')[:2])
    if len(matches) > 1:
        raise DomainError('Use the exact accepted action family.', 'CONFLICT', 409)
    if not matches:
        if (
            reference is not None
            and ReplayKey.objects.filter(owner=owner, family=reference.family, action_key=key).exists()
        ):
            raise DomainError('This action key belongs to a different instruction.', 'CONFLICT', 409)
        raise DomainError('No accepted action matches this reference.', 'DENIED', 404)
    return matches[0]


def capture_legacy_references(intents, using='default'):
    """Capture actual consumed legacy candidates without retaining proof material."""
    if using != 'default':
        return 0
    ids = [intent.pk for intent in intents]
    if len(ids) > MAX_PURGE_BATCH:
        raise ValueError('Use a bounded legacy reference batch.')
    if not ids:
        return 0
    with transaction.atomic(using=using):
        owner_ids = _lock_reference_owners(ids, using)
        legacy = ReplayKey.objects.using(using).filter(
            owner_id=OuterRef('owner_id'),
            family=OuterRef('kind'),
            digest=OuterRef('digest'),
            intent_id__isnull=True,
        )
        candidates = list(
            Intent.objects.using(using)
            .select_for_update()
            .filter(pk__in=ids, owner_id__in=owner_ids, consumed_at__isnull=False)
            .annotate(has_legacy_key=Exists(legacy))
            .filter(has_legacy_key=True)
            .order_by('id')
            .values('id', 'owner_id', 'kind', 'digest')
        )
        expected = {row['id']: (row['owner_id'], row['kind'], row['digest']) for row in candidates}
        existing = dict(
            (
                row['id'],
                (row['owner_id'], row['family'], row['digest']),
            )
            for row in LegacyIntentReference.objects.using(using)
            .filter(pk__in=expected)
            .values('id', 'owner_id', 'family', 'digest')
        )
        if any(existing[key] != expected[key] for key in existing):
            raise DomainError(
                'Retained instruction identity conflicts with stored evidence.', 'CONFLICT', 409
            )
        additions = [
            LegacyIntentReference(id=key, owner_id=value[0], family=value[1], digest=value[2])
            for key, value in expected.items()
            if key not in existing
        ]
        LegacyIntentReference.objects.using(using).bulk_create(additions, ignore_conflicts=True)
        retained = {
            row['id']: (row['owner_id'], row['family'], row['digest'])
            for row in LegacyIntentReference.objects.using(using)
            .filter(pk__in=expected)
            .values('id', 'owner_id', 'family', 'digest')
        }
        if retained != expected:
            raise DomainError('Retained instruction identity could not be preserved.', 'CONFLICT', 409)
        return len(additions)


def purge_intent_batch(now, batch_size=MAX_PURGE_BATCH, using='default'):
    """Copy minimal legacy evidence and remove aged full proofs in one transaction."""
    if using != 'default':
        return 0
    if not 1 <= batch_size <= MAX_PURGE_BATCH:
        raise ValueError('Use a bounded intent cleanup batch.')
    cutoff = now - timedelta(days=7)
    with transaction.atomic(using=using):
        aged = Q(consumed_at__lte=cutoff) | Q(consumed_at__isnull=True, expires_at__lte=cutoff)
        ids = list(
            Intent.objects.using(using).filter(aged).order_by('id').values_list('id', flat=True)[:batch_size]
        )
        # FK insertion needs an owner KEY SHARE lock. Match confirmation and
        # account closure by locking sorted owners before their proof rows.
        owner_ids = _lock_reference_owners(ids, using)
        intents = list(
            Intent.objects.using(using)
            .select_for_update(skip_locked=True)
            .filter(aged, pk__in=ids, owner_id__in=owner_ids)
            .order_by('id')[:batch_size]
        )
        capture_legacy_references(intents, using=using)
        count, _ = Intent.objects.using(using).filter(pk__in=[intent.pk for intent in intents]).delete()
        return count
