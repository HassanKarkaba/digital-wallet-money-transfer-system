import fcntl
import os
import tempfile
import time
import uuid
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.services import db_now, lock_users

from .models import Notification

MAIL_CAP = 100 * 1024 * 1024


def capture_mail(address, subject, body):
    """Only local files, with one shared lock for the bounded capture volume."""
    folder = Path(settings.EMAIL_FILE_PATH)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + 2
    with open(folder / '.capture.lock', 'a+b') as lock:
        os.chmod(folder / '.capture.lock', 0o600)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError('Local mail capture is busy.')
                time.sleep(0.01)
        try:
            return _capture_locked(folder, address, subject, body)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _capture_locked(folder, address, subject, body):
    size = sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
    message = EmailMessage()
    message['From'] = 'DWMT Demo <no-reply@dwmt.test>'
    message['To'] = address
    message['Subject'] = subject
    message.set_content(body)
    content = message.as_bytes()
    if size + len(content) > MAIL_CAP:
        raise OSError('Local mail capture capacity reached.')
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.pending-', dir=folder)
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        final = folder / f'{uuid.uuid4()}.eml'
        os.replace(temporary, final)
        return final.name
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def notify(user, event_key, title, body, mandatory=True, security=False):
    """Call in the business transaction: intent survives optional mail failure."""
    if len(str(event_key)) > 240 or len(title) > 160 or len(body) > 2000:
        raise ValidationError('Notification is too long.')
    event, created = Notification.objects.get_or_create(
        recipient=user,
        event_key=str(event_key),
        template_version=1,
        defaults={
            'title': title,
            'body': body,
            'mandatory': mandatory,
            'security': security,
        },
    )
    if created:
        from work.services import enqueue

        enqueue('NOTIFY', event.pk)
    return event


def notify_in_app(user, event_key, title, body, security=True):
    """Persist a mandatory inbox event when SecurityMail owns the email copy."""
    if len(str(event_key)) > 240 or len(title) > 160 or len(body) > 2000:
        raise ValidationError('Notification is too long.')
    event, _ = Notification.objects.get_or_create(
        recipient=user,
        event_key=str(event_key),
        template_version=1,
        defaults={
            'title': title,
            'body': body,
            'mandatory': True,
            'security': security,
            'delivery_state': 'SKIPPED',
        },
    )
    return event


def dispatch(notification_id):
    preliminary = Notification.objects.filter(pk=notification_id).values('recipient_id').first()
    if preliminary is None:
        return
    # Recheck current owner, verified local destination and preference at dispatch.
    with transaction.atomic():
        users = lock_users([preliminary['recipient_id']])
        user = users[0]
        event = Notification.objects.select_for_update().get(pk=notification_id, recipient=user)
        if event.delivery_state in ['DELIVERED', 'SKIPPED']:
            return
        if (
            not user.is_active
            or user.status != 'ACTIVE'
            or user.verified_at is None
            or (not event.security and not user.email_receipts)
        ):
            event.delivery_state = 'SKIPPED'
            event.save(update_fields=['delivery_state'])
            return
        event.attempts += 1
        try:
            capture_mail(
                user.email,
                event.title,
                event.body
                + '\n\nDemonstration money only. No cash value. Sign in locally to inspect your wallet.\n',
            )
        except OSError:
            event.delivery_state = 'FAILED'
            event.last_error = 'Local capture unavailable.'
            event.save(update_fields=['attempts', 'delivery_state', 'last_error'])
            # Persist failure, then signal retry outside this transaction.
            failure = True
        else:
            event.delivery_state = 'DELIVERED'
            event.delivered_at = db_now()
            event.last_error = ''
            event.save(update_fields=['attempts', 'delivery_state', 'delivered_at', 'last_error'])
            failure = False
    if failure:
        raise OSError('Local capture unavailable.')


def cleanup_private_files():
    now = db_now()
    # Generated files expire; retained ledger, notification and replay history do not.
    from reports.services import cleanup_exports

    cleanup_exports()
    from config.logging import cleanup_diagnostics

    cleanup_diagnostics(now=now)
    from accounts.models import RateBucket

    RateBucket.objects.filter(started_at__lt=now - timedelta(hours=24)).delete()
    # Unaccepted intents can expire without erasing accepted action identities.
    from django.db.models import Q

    from payments.models import Intent

    Intent.objects.filter(
        Q(consumed_at__lt=now - timedelta(days=7))
        | Q(consumed_at__isnull=True, expires_at__lt=now - timedelta(days=7))
    ).delete()
