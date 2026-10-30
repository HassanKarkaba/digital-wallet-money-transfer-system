import hashlib
import hmac
import http.client
from datetime import timedelta

from django.conf import settings
from django.db import transaction

from rails.adapter import check_identity, publish_event, simulator_now

from .models import ProviderEffect, ProviderEvent


@transaction.atomic(using='simulator')
def finalize_pending(limit=20, stop_event=None):
    check_identity()
    now = simulator_now()
    effects = list(
        ProviderEffect.objects.using('simulator')
        .select_for_update(skip_locked=True)
        .filter(state='PENDING', finalize_at__lte=now)
        .order_by('finalize_at', 'id')[:limit]
    )
    finalized = 0
    for effect in effects:
        if stop_event is not None and stop_event.is_set():
            break
        effect.state = 'SUCCESS'
        effect.provider_sequence += 1
        effect.occurred_at = now
        effect.finalize_at = None
        effect.save(
            using='simulator', update_fields=['state', 'provider_sequence', 'occurred_at', 'finalize_at']
        )
        publish_event(effect)
        finalized += 1
    return finalized


def send_events(limit=20, stop_event=None):
    check_identity()
    with transaction.atomic(using='simulator'):
        now = simulator_now()
        events = list(
            ProviderEvent.objects.using('simulator')
            .select_for_update(skip_locked=True)
            .filter(state__in=['PENDING', 'RETRY', 'SENDING'], due_at__lte=now)
            .order_by('due_at', 'id')[:limit]
        )
        for event in events:
            event.state = 'SENDING'
            event.attempts += 1
            event.due_at = now + timedelta(seconds=30)
            event.save(using='simulator', update_fields=['state', 'attempts', 'due_at'])
    delivered = 0
    for event in events:
        if stop_event is not None and stop_event.is_set():
            break
        body = event.body.encode()
        timestamp = str(int(simulator_now().timestamp()))
        signature = hmac.new(
            str(settings.CALLBACK_HMAC_KEY).encode(), timestamp.encode() + b'\n' + body, hashlib.sha256
        ).hexdigest()
        connection = http.client.HTTPConnection('web', 8000, timeout=2)
        status = None
        try:
            connection.request(
                'POST',
                '/integrations/simulator/events/',
                body=body,
                headers={
                    'Host': 'localhost',
                    'Content-Type': 'application/json',
                    'X-DWMT-Key-Id': getattr(settings, 'CALLBACK_KEY_ID', 'v1'),
                    'X-DWMT-Timestamp': timestamp,
                    'X-DWMT-Signature': signature,
                },
            )
            response = connection.getresponse()
            status = response.status
            response.read(1024)
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        now = simulator_now()
        if status in (200, 202):
            ProviderEvent.objects.using('simulator').filter(
                pk=event.pk, attempts=event.attempts, state='SENDING'
            ).update(state='DELIVERED', delivered_at=now)
            delivered += 1
        else:
            delays = (1, 5, 15, 60, 300)
            state = 'NEEDS_ATTENTION' if event.attempts >= 6 else 'RETRY'
            ProviderEvent.objects.using('simulator').filter(
                pk=event.pk, attempts=event.attempts, state='SENDING'
            ).update(state=state, due_at=now + timedelta(seconds=delays[min(event.attempts - 1, 4)]))
    return delivered
