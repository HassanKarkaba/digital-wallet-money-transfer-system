from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction

from accounts.services import SecurityError, assert_eligible, db_now, lock_users
from audit.services import record

from .models import Destination


@transaction.atomic
def create_destination(actor, label):
    locked = lock_users([actor.pk])
    user = locked[actor.pk] if isinstance(locked, dict) else locked[0]
    assert_eligible(user)
    label = str(label).strip()
    if not 1 <= len(label) <= 80 or any(ord(c) < 32 for c in label):
        raise SecurityError('Enter a nickname of 1 to 80 characters, without control characters.', 'INVALID')
    if Destination.objects.filter(owner=user, status='ACTIVE').count() >= 5:
        raise SecurityError('You may have at most five active synthetic destinations.', 'LIMIT')
    item = Destination.objects.create(owner=user, label=label, activation_at=db_now() + timedelta(minutes=10))
    record(
        user,
        'destination.link',
        str(item.pk),
        reason='Created an opaque synthetic token; no real bank information.',
    )
    return item


@transaction.atomic
def disable_destination(actor, destination_id, version=None):
    locked = lock_users([actor.pk])
    user = locked[actor.pk] if isinstance(locked, dict) else locked[0]
    assert_eligible(user)
    item = Destination.objects.select_for_update().filter(pk=destination_id, owner=user).first()
    if item is None:
        raise PermissionDenied('Destination unavailable.')
    if version is not None and item.version != int(version):
        raise SecurityError('This destination changed. Refresh and try again.', 'STALE')
    if item.status == 'ACTIVE':
        item.status = 'DISABLED'
        item.version += 1
        item.save(update_fields=['status', 'version'])
        record(user, 'destination.disable', str(item.pk))
    return item
