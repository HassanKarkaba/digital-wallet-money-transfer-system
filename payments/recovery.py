"""Reviewed reconstruction from retained independent, immutable rail facts.

This cannot manufacture a missing source or rewrite a posted journal. When the
retained instruction, owner, replay mapping or resources cannot be established,
the proposal remains blocked and admission stays closed.
"""

import uuid
from datetime import date, datetime

from django.db import transaction

from accounts.services import db_now
from audit.services import record
from ledger.models import IntegrityGate
from rails.models import Destination

from .errors import DomainError
from .models import Operation, ReplayKey


def recover_operation_locked(actor, proposal, reason):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Recovery requires protected primary transaction.')
    if (
        IntegrityGate.objects.get(pk=1).is_open
        or proposal.state != 'APPROVED'
        or proposal.reviewer_id != actor.pk
    ):
        raise DomainError('Reviewed recovery requires a closed integrity gate.', 'DENIED', 403)
    metadata = proposal.payload.get('recovery_metadata') or proposal.payload.get('metadata') or {}
    observation = proposal.payload.get('provider_observation') or proposal.payload.get('observation') or {}
    required = {
        'operation_id',
        'actor_id',
        'destination_id',
        'kind',
        'amount_cents',
        'currency',
        'created_at',
        'limit_day',
        'policy_version',
        'provider_key',
        'replays',
    }
    if (
        not required <= set(metadata)
        or metadata.get('schema_version') != 1
        or metadata['kind'] not in {'FUNDING', 'WITHDRAWAL'}
        or metadata['currency'] != 'USD'
        or metadata['policy_version'] != 'DWMT-P1'
    ):
        raise DomainError(
            'The independent store does not retain a provable original instruction. No reconstruction is permitted.',
            'CONFLICT',
            409,
        )
    if (
        observation.get('state') not in {'SUCCESS', 'DECLINED'}
        or observation.get('provider_key') != metadata['provider_key']
        or observation.get('amount_cents') != metadata['amount_cents']
        or observation.get('direction') != metadata['kind']
    ):
        raise DomainError('A matching immutable final provider result is required.', 'CONFLICT', 409)
    opid = uuid.UUID(metadata['operation_id'])
    if Operation.objects.filter(pk=opid).exists():
        raise DomainError('The operation already exists. Use ordinary reconciliation.', 'CONFLICT', 409)
    destination = Destination.objects.get(pk=metadata['destination_id'], owner_id=metadata['actor_id'])
    if str(destination.token_id) != str(observation.get('token_id')):
        raise DomainError('The retained token does not match.', 'CONFLICT', 409)
    replays = metadata['replays']
    if not isinstance(replays, list) or not replays or len(replays) > 10:
        raise DomainError('Original replay identities are not provable.', 'CONFLICT', 409)
    for replay in replays:
        if (
            str(replay.get('owner_id')) != metadata['actor_id']
            or replay.get('family') != metadata['kind']
            or len(replay.get('digest', '')) != 64
        ):
            raise DomainError('Invalid retained replay mapping.', 'CONFLICT', 409)
        if ReplayKey.objects.filter(
            owner_id=replay['owner_id'], family=replay['family'], action_key=replay['action_key']
        ).exists():
            raise DomainError(
                'A retained replay identity conflicts with the restored primary store.', 'CONFLICT', 409
            )
    stamp = db_now()
    op = Operation.objects.create(
        id=opid,
        actor_id=metadata['actor_id'],
        sender_id=metadata.get('sender_id'),
        recipient_id=metadata.get('recipient_id'),
        destination=destination,
        kind=metadata['kind'],
        amount_cents=metadata['amount_cents'],
        currency='USD',
        note=metadata.get('note', ''),
        state='READY',
        policy_version=metadata['policy_version'],
        limit_day=date.fromisoformat(metadata['limit_day']),
        provider_key=metadata['provider_key'],
        sender_label=metadata.get('sender_label', ''),
        recipient_label=metadata.get('recipient_label', ''),
        created_at=datetime.fromisoformat(metadata['created_at']),
        updated_at=stamp,
    )
    record(
        actor,
        'recovery.operation_reconstructed',
        str(op.pk),
        reason=reason,
        metadata={
            'proposal': str(proposal.pk),
            'source': 'immutable simulator effect',
            'provider_key': op.provider_key,
        },
    )
    with transaction.get_connection().cursor() as cursor:
        cursor.execute('SELECT public.dwmt_recover_reserve(%s,%s)', [op.pk, proposal.pk])
    for replay in replays:
        ReplayKey.objects.create(
            owner_id=replay['owner_id'],
            family=replay['family'],
            action_key=replay['action_key'],
            digest=replay['digest'],
            intent_id=replay.get('intent_id'),
            operation=op,
            target_id=op.pk,
            target_kind='OPERATION',
            created_at=datetime.fromisoformat(metadata['created_at']),
        )
    from .workflows import _transition, apply_provider_observation

    _transition(op, 'SUBMITTED')
    result = apply_provider_observation(op.pk, observation)
    record(
        actor,
        'recovery.settlement_applied',
        str(op.pk),
        reason=reason,
        metadata={'proposal': str(proposal.pk), 'state': result.state},
    )
    return result
