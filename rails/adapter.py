"""Simulation rail adapter. Provider commits use a separate database connection."""

import hashlib
import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import connections, transaction

from simulator.models import ProviderEffect, ProviderEvent, SimulatorEnvironment


class RailUnavailable(Exception):
    pass


class RailConflict(Exception):
    pass


def simulator_now():
    with connections['simulator'].cursor() as cursor:
        cursor.execute('SELECT clock_timestamp()')
        return cursor.fetchone()[0]


def check_identity():
    with connections['simulator'].cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='simulator_providereffect' AND t.tgname='dwmt_provider_immutable' AND t.tgenabled IN ('O','A')) AND EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='simulator_providereffect' AND column_name='recovery_metadata')"
        )
        if not cursor.fetchone()[0]:
            raise RailUnavailable('Protected simulator schema is unavailable.')
    with connections['simulator'].cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('public.simulator_providerevent') IS NOT NULL AND to_regclass('public.simulator_simulatorenvironment') IS NOT NULL"
        )
        if not cursor.fetchone()[0]:
            raise RailUnavailable('Simulator event storage is unavailable.')
    identity = SimulatorEnvironment.objects.using('simulator').filter(pk=1).first()
    if identity is None:
        raise RailUnavailable('Simulator identity is missing; reconciliation is required.')
    if str(identity.environment_id) != str(settings.ENVIRONMENT_ID) or identity.recovery_generation != int(
        settings.RECOVERY_GENERATION
    ):
        raise RailUnavailable('Simulator recovery identity does not match the declared environment.')
    return identity


def initialize_identity():
    """Explicit operator bootstrap, never a fallback for missing provider state."""
    return SimulatorEnvironment.objects.using('simulator').get_or_create(
        pk=1,
        defaults={
            'environment_id': settings.ENVIRONMENT_ID,
            'recovery_generation': settings.RECOVERY_GENERATION,
        },
    )[0]


def observation(effect):
    event = effect.events.using('simulator').filter(provider_sequence=effect.provider_sequence).first()
    return {
        'event_id': str(event.pk) if event else str(effect.pk),
        'provider_key': effect.provider_key,
        'direction': effect.direction,
        'token_id': str(effect.token_id),
        'amount_cents': effect.amount_cents,
        'currency': effect.currency,
        'state': effect.state,
        'provider_sequence': effect.provider_sequence,
        'occurred_at': effect.occurred_at.isoformat().replace('+00:00', 'Z'),
        'schema_version': effect.schema_version,
    }


def publish_event(effect):
    event_id = uuid.uuid4()
    body = observation(effect)
    body['event_id'] = str(event_id)
    return ProviderEvent.objects.using('simulator').create(
        id=event_id,
        effect=effect,
        provider_sequence=effect.provider_sequence,
        body=json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True),
        due_at=simulator_now(),
    )


def lookup(provider_key):
    """Read only; a missing record cannot establish certainty after a restore."""
    check_identity()
    effect = ProviderEffect.objects.using('simulator').filter(provider_key=provider_key).first()
    return observation(effect) if effect else None


def create_or_get(operation):
    if connections['default'].in_atomic_block:
        raise RailUnavailable('Provider work must run outside a primary transaction.')
    check_identity()
    if operation.kind not in ('FUNDING', 'WITHDRAWAL') or operation.currency != 'USD':
        raise RailConflict('Unsupported simulated rail instruction.')
    token = operation.destination
    expected = {
        'operation_id': operation.id,
        'environment_id': uuid.UUID(str(settings.ENVIRONMENT_ID)),
        'direction': operation.kind,
        'token_id': token.token_id,
        'owner_id': operation.actor_id,
        'amount_cents': operation.amount_cents,
        'currency': 'USD',
    }
    if hasattr(operation, 'limit_day'):
        from payments.models import ReplayKey

        expected['recovery_metadata'] = {
            'schema_version': 1,
            'operation_id': str(operation.pk),
            'actor_id': str(operation.actor_id),
            'sender_id': str(operation.sender_id) if operation.sender_id else None,
            'recipient_id': str(operation.recipient_id) if operation.recipient_id else None,
            'destination_id': str(operation.destination_id),
            'amount_cents': operation.amount_cents,
            'kind': operation.kind,
            'currency': operation.currency,
            'note': operation.note,
            'sender_label': operation.sender_label,
            'recipient_label': operation.recipient_label,
            'created_at': operation.created_at.isoformat(),
            'limit_day': operation.limit_day.isoformat(),
            'policy_version': operation.policy_version,
            'provider_key': operation.provider_key,
            'replays': list(
                ReplayKey.objects.filter(operation=operation).values(
                    'owner_id', 'family', 'action_key', 'digest', 'intent_id'
                )
            ),
        }
        expected['recovery_metadata'] = json.loads(json.dumps(expected['recovery_metadata'], default=str))
    else:
        expected['recovery_metadata'] = {}
    created = False
    with transaction.atomic(using='simulator'):
        lock_number = int.from_bytes(
            hashlib.sha256(operation.provider_key.encode()).digest()[:8], 'big', signed=True
        )
        with connections['simulator'].cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '2s'")
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [lock_number])
        effect = (
            ProviderEffect.objects.using('simulator')
            .select_for_update()
            .filter(provider_key=operation.provider_key)
            .first()
        )
        if effect:
            if any(getattr(effect, key) != value for key, value in expected.items()):
                raise RailConflict('The provider key already names a different instruction.')
        else:
            now = simulator_now()
            scenario = token.scenario
            if scenario not in (
                'SUCCESS',
                'DECLINE',
                'DELAY',
                'TIMEOUT_AFTER_COMMIT',
                'MALFORMED_AFTER_COMMIT',
            ):
                raise RailConflict('Unsupported simulator scenario.')
            state = 'DECLINED' if scenario == 'DECLINE' else ('PENDING' if scenario == 'DELAY' else 'SUCCESS')
            effect = ProviderEffect.objects.using('simulator').create(
                provider_key=operation.provider_key,
                **expected,
                state=state,
                scenario=scenario,
                occurred_at=now,
                finalize_at=now + timedelta(seconds=10) if scenario == 'DELAY' else None,
            )
            publish_event(effect)
            created = True
    # The independent transaction is committed before these reproducible failures.
    if created and effect.scenario == 'TIMEOUT_AFTER_COMMIT':
        raise TimeoutError('Synthetic response lost after provider commitment.')
    if created and effect.scenario == 'MALFORMED_AFTER_COMMIT':
        return {'schema_version': 1, 'state': 'INVALID'}
    return observation(effect)
