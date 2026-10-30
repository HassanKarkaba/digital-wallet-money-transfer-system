import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

from django.conf import settings
from django.db import connection, transaction
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts.services import RateLimited, db_now, rate_limit

from .models import CallbackInbox, CallbackIncident

FIELDS = {
    'event_id',
    'provider_key',
    'direction',
    'token_id',
    'amount_cents',
    'currency',
    'state',
    'provider_sequence',
    'occurred_at',
    'schema_version',
}


def _error(request, code, message, status, retry_after=None):
    payload = {
        'version': 1,
        'code': code,
        'message': message,
        'field_errors': {},
        'correlation_id': str(getattr(request, 'correlation_id', '')),
    }
    if retry_after is not None:
        payload['retry_after'] = retry_after
    return JsonResponse(payload, status=status)


def _quarantine(data, classification, digest):
    from audit.services import record
    from ledger.models import IntegrityGate

    gate = IntegrityGate.objects.select_for_update().filter(pk=1).first()
    CallbackIncident.objects.create(
        event_id=data['event_id'],
        provider_key=data['provider_key'],
        classification=classification,
        digest=digest,
    )
    if gate:
        gate.is_open = False
        gate.reason = f'{classification}: signed simulator event requires investigation.'
        gate.changed_at = db_now()
        gate.changed_by = None
        gate.version += 1
        gate.save(update_fields=['is_open', 'reason', 'changed_at', 'changed_by', 'version'])
        record(
            None,
            'integrity.closed',
            str(gate.pk),
            reason=gate.reason,
            metadata={'event_id': data['event_id']},
            worker='simulator-callback',
        )


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Repeated JSON property')
        result[key] = value
    return result


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Noncanonical UUID')
    return uuid.UUID(value)


def _validate(body):
    data = json.loads(body.decode('utf-8'), object_pairs_hook=_object)
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise ValueError('Invalid schema')
    _uuid(data['event_id'])
    _uuid(data['token_id'])
    if (
        data['direction'] not in ('FUNDING', 'WITHDRAWAL')
        or data['state'] not in ('PENDING', 'SUCCESS', 'DECLINED')
        or data['currency'] != 'USD'
    ):
        raise ValueError('Invalid enumerated value')
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ValueError('Unsupported version')
    if type(data['amount_cents']) is not int or not 1 <= data['amount_cents'] <= 250000:
        raise ValueError('Invalid cents')
    if type(data['provider_sequence']) is not int or not 1 <= data['provider_sequence'] <= 2147483647:
        raise ValueError('Invalid sequence')
    if (
        not isinstance(data['provider_key'], str)
        or not 1 <= len(data['provider_key']) <= 200
        or not data['provider_key'].isascii()
    ):
        raise ValueError('Invalid provider key')
    occurred = parse_datetime(data['occurred_at']) if isinstance(data['occurred_at'], str) else None
    if occurred is None or occurred.tzinfo is None or not data['occurred_at'].endswith('Z'):
        raise ValueError('Invalid event instant')
    return data


def _active_key(key_id, now):
    if key_id == getattr(settings, 'CALLBACK_KEY_ID', 'v1'):
        return str(settings.CALLBACK_HMAC_KEY).encode()
    previous_id = getattr(settings, 'CALLBACK_PREVIOUS_KEY_ID', None)
    expires = getattr(settings, 'CALLBACK_PREVIOUS_KEY_EXPIRES_AT', None)
    if isinstance(expires, str):
        expires = parse_datetime(expires)
    if key_id == previous_id and expires and expires > now:
        value = getattr(settings, 'CALLBACK_PREVIOUS_HMAC_KEY', None)
        return str(value).encode() if value else None
    return None


@csrf_exempt
@require_POST
def simulator_events(request):
    if len(request.body) > 32768:
        return _error(request, 'BODY_TOO_LARGE', 'Body too large.', 413)
    raw = request.body
    timestamp = request.headers.get('X-DWMT-Timestamp', '')
    signature = request.headers.get('X-DWMT-Signature', '')
    key_id = request.headers.get('X-DWMT-Key-Id', '')
    now = db_now()
    key = _active_key(key_id, now)
    try:
        if not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 12:
            raise ValueError()
        delivery_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return _error(request, 'UNAUTHENTICATED', 'Authentication failed.', 401)
    expected = hmac.new(key or b'invalid-key', timestamp.encode() + b'\n' + raw, hashlib.sha256).hexdigest()
    if (
        key is None
        or abs((now - delivery_at).total_seconds()) > 300
        or not hmac.compare_digest(signature, expected)
    ):
        return _error(request, 'UNAUTHENTICATED', 'Authentication failed.', 401)
    try:
        rate_limit('callback', 'simulator', 1000, 60)
    except RateLimited as exc:
        response = _error(request, 'RATE_LIMITED', 'Rate limit exceeded.', 429, retry_after=exc.retry_after)
        response['Retry-After'] = str(exc.retry_after)
        return response
    try:
        data = _validate(raw)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return _error(request, 'INVALID', 'Malformed event.', 400)
    digest = hashlib.sha256(raw).hexdigest()
    from payments.models import Operation
    from work.services import enqueue

    with transaction.atomic():
        number = int.from_bytes(hashlib.sha256(data['event_id'].encode()).digest()[:8], 'big', signed=True)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '1900ms'")
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [number])
        inbox = CallbackInbox.objects.filter(provider='simulator', event_id=data['event_id']).first()
        if inbox:
            if inbox.payload_digest == digest:
                return JsonResponse({'state': 'Already accepted.'}, status=200)
            _quarantine(data, 'EVENT_ID_BYTES_MISMATCH', digest)
            return _error(request, 'CONFLICT', 'Event conflict.', 409)
        operation = (
            Operation.objects.select_related('destination').filter(provider_key=data['provider_key']).first()
        )
        if operation is None or any(
            (
                operation.kind != data['direction'],
                operation.amount_cents != data['amount_cents'],
                operation.currency != data['currency'],
                operation.destination is None,
                operation.destination is not None and str(operation.destination.token_id) != data['token_id'],
            )
        ):
            _quarantine(data, 'INSTRUCTION_MISMATCH', digest)
            return _error(request, 'CONFLICT', 'Event conflict.', 409)
        CallbackInbox.objects.create(
            event_id=data['event_id'],
            provider_key=data['provider_key'],
            payload_digest=digest,
            key_id=key_id,
            received_at=now,
        )
        enqueue('LOOKUP', str(operation.pk), key='callback:' + data['event_id'])
    return JsonResponse({'state': 'Accepted for authoritative lookup.'}, status=202)
