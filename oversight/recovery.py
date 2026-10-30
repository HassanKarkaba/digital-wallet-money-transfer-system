"""Reviewed local reconstruction; immutable history is never edited.

Approval is called only by the payment coordinator after its bound MFA proof,
exclusive integrity gate, sorted authority, business, wallet and account locks.
"""

import json
import uuid

from django.db import connection, transaction
from django.db.models import Sum

from accounts.services import assert_eligible, db_now, lock_users
from audit.services import record
from ledger.models import Entry, IntegrityGate, LedgerAccount, Reservation, Wallet
from payments.errors import DomainError

from .models import Finding, RecoveryProposal


def _reason(value):
    if not isinstance(value, str) or not 10 <= len(value.strip()) <= 1000:
        raise DomainError('Give a recovery reason of 10 to 1,000 characters.')
    return value.strip()


def projection_wallet(finding):
    if finding.classification not in {'PROJECTION_MISMATCH', 'RESERVATION_MISMATCH'}:
        raise DomainError('This finding does not identify a customer projection that can be rebuilt.')
    try:
        reference = uuid.UUID(str(finding.details.get('reference', '')))
    except ValueError:
        raise DomainError('The finding has no proven customer projection reference.') from None
    wallet = Wallet.objects.filter(pk=reference).first()
    if not wallet:
        account = (
            LedgerAccount.objects.select_related('wallet').filter(pk=reference, wallet__isnull=False).first()
        )
        wallet = account.wallet if account else None
    if not wallet:
        raise DomainError('A customer wallet cannot be proven from this finding. Keep the incident open.')
    return wallet


def projection_snapshot(wallet):
    accounts = list(LedgerAccount.objects.filter(wallet=wallet).order_by('id'))
    if {account.kind for account in accounts} != {'AVAILABLE', 'RESERVED'} or len(accounts) != 2:
        raise DomainError(
            'The account identities are inconsistent. This repair cannot invent missing accounts.'
        )
    calculated = {}
    for account in accounts:
        credits = (
            Entry.objects.filter(account=account, side='CREDIT').aggregate(total=Sum('amount_cents'))['total']
            or 0
        )
        debits = (
            Entry.objects.filter(account=account, side='DEBIT').aggregate(total=Sum('amount_cents'))['total']
            or 0
        )
        calculated[account.kind] = credits - debits
    funds = (
        Reservation.objects.filter(wallet=wallet, kind='FUNDS', state='ACTIVE').aggregate(
            total=Sum('amount_cents')
        )['total']
        or 0
    )
    incoming = (
        Reservation.objects.filter(wallet=wallet, kind='INCOMING', state='ACTIVE').aggregate(
            total=Sum('amount_cents')
        )['total']
        or 0
    )
    if (
        min(calculated.values()) < 0
        or calculated['RESERVED'] != funds
        or sum(calculated.values()) + incoming > 1000000
    ):
        raise DomainError(
            'Immutable entries and reservations do not prove a valid projection. The incident remains open.'
        )
    before = {
        'available_cents': wallet.available_cents,
        'reserved_cents': wallet.reserved_cents,
        'incoming_cents': wallet.incoming_cents,
        'sequence': wallet.sequence,
        'version': wallet.version,
        'accounts': {str(account.pk): account.balance_cents for account in accounts},
    }
    expected = {
        'available_cents': calculated['AVAILABLE'],
        'reserved_cents': calculated['RESERVED'],
        'incoming_cents': incoming,
        'accounts': {str(account.pk): calculated[account.kind] for account in accounts},
    }
    return {'wallet_id': str(wallet.pk), 'before': before, 'expected': expected, 'schema_version': 1}


def provider_recovery_payload(finding):
    if finding.classification != 'MISSING_LOCAL_OPERATION' or not finding.provider_key:
        raise DomainError('Only a proven missing local operation can use this recovery path.')
    from rails.adapter import check_identity, observation
    from simulator.models import ProviderEffect

    check_identity()
    effect = ProviderEffect.objects.using('simulator').filter(provider_key=finding.provider_key).first()
    if not effect or effect.state not in {'SUCCESS', 'DECLINED'} or not effect.recovery_metadata:
        raise DomainError('A terminal independent effect with retained admission metadata is required.')
    metadata = effect.recovery_metadata
    wallet_id = metadata.get('sender_id') or metadata.get('recipient_id')
    if not wallet_id or metadata.get('operation_id') != str(effect.operation_id):
        raise DomainError('The provider metadata cannot prove its original wallet and operation identity.')
    return {
        'schema_version': 1,
        'wallet_id': str(wallet_id),
        'provider_key': effect.provider_key,
        'operation_id': str(effect.operation_id),
        'owner_id': str(effect.owner_id),
        'provider_state': effect.state,
        'provider_sequence': effect.provider_sequence,
        'recovery_metadata': metadata,
        'provider_observation': observation(effect),
    }


def propose_recovery(actor, finding_id, reason, kind):
    reason = _reason(reason)
    if kind not in {'REBUILD_PROJECTIONS', 'RECOVER_OPERATION'}:
        raise DomainError('Choose a supported reviewed recovery action.')
    preliminary = Finding.objects.get(pk=finding_id)
    if kind == 'REBUILD_PROJECTIONS':
        wallet = projection_wallet(preliminary)
        payload = None
    else:
        payload = provider_recovery_payload(preliminary)
        wallet = Wallet.objects.filter(pk=payload['wallet_id']).first()
        if not wallet:
            raise DomainError(
                'The retained customer wallet is missing. This reconstruction cannot invent it.'
            )
    with transaction.atomic():
        gate = IntegrityGate.objects.select_for_update().get(pk=1)
        if gate.is_open:
            raise DomainError(
                'Close and investigate the integrity gate before proposing reconstruction.', 'CONFLICT', 409
            )
        users = {user.pk: user for user in lock_users([actor.pk, wallet.owner_id])}
        current_actor = users[actor.pk]
        assert_eligible(current_actor, staff_role='FINANCE')
        finding = Finding.objects.select_for_update().get(pk=preliminary.pk)
        if finding.state != 'OPEN':
            raise DomainError('This finding was already resolved.', 'STALE', 409)
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        list(LedgerAccount.objects.select_for_update().filter(wallet=locked_wallet).order_by('pk'))
        if RecoveryProposal.objects.filter(finding=finding, state='OPEN').exists():
            raise DomainError('An open recovery proposal already exists for this finding.', 'CONFLICT', 409)
        if kind == 'REBUILD_PROJECTIONS':
            payload = projection_snapshot(locked_wallet)
        proposal = RecoveryProposal.objects.create(
            finding=finding,
            creator=current_actor,
            kind=kind,
            reason=reason,
            payload=payload,
            created_at=db_now(),
        )
        record(
            current_actor,
            'RECOVERY_PROPOSED',
            proposal.pk,
            reason=reason,
            purpose='Reviewed reconstruction while admission is gated',
            metadata={'kind': kind, 'diff': payload},
        )
        return proposal


def approve_recovery_locked(actor, proposal, reason, final_guard=None):
    """The caller owns bound proof, version validation and all earlier lock ranks."""
    if not connection.in_atomic_block:
        raise RuntimeError('Recovery approval requires the protected primary transaction.')
    reason = _reason(reason)
    assert_eligible(actor, staff_role='REVIEWER')
    gate = IntegrityGate.objects.get(pk=1)
    if gate.is_open or proposal.state != 'OPEN':
        raise DomainError('Recovery requires a closed gate and an open current proposal.', 'STALE', 409)
    wallet = Wallet.objects.get(pk=proposal.payload['wallet_id'])
    participants = {wallet.owner_id}
    if proposal.finding.operation_id:
        operation = proposal.finding.operation
        participants.add(operation.actor_id)
        if operation.sender_id:
            participants.add(operation.sender.owner_id)
        if operation.recipient_id:
            participants.add(operation.recipient.owner_id)
    if actor.pk == proposal.creator_id or actor.pk in participants:
        raise DomainError(
            'A different reviewer without customer involvement must approve this reconstruction.',
            'DENIED',
            403,
        )
    now = db_now()
    if proposal.kind == 'REBUILD_PROJECTIONS':
        current = projection_snapshot(wallet)
        if current != proposal.payload:
            proposal.state = 'BLOCKED'
            proposal.reviewer = actor
            proposal.decision_reason = (
                'The underlying state changed. Create a fresh proposal with a current before and after diff.'
            )
            proposal.decided_at = now
            proposal.version += 1
            proposal.save(update_fields=['state', 'reviewer', 'decision_reason', 'decided_at', 'version'])
            record(actor, 'RECOVERY_BLOCKED', proposal.pk, reason=reason, metadata={'reason': 'STALE_DIFF'})
            return proposal
    if final_guard:
        now = final_guard()
    proposal.state = 'APPROVED'
    proposal.reviewer = actor
    proposal.decision_reason = reason
    proposal.decided_at = now
    proposal.save(update_fields=['state', 'reviewer', 'decision_reason', 'decided_at'])
    record(
        actor,
        'RECOVERY_APPROVED',
        proposal.pk,
        reason=reason,
        purpose='Second reviewer approved exact reconstruction',
        metadata={'kind': proposal.kind, 'diff': proposal.payload},
    )
    if proposal.kind == 'REBUILD_PROJECTIONS':
        with connection.cursor() as cursor:
            cursor.execute('SELECT public.dwmt_rebuild_projection(%s)', [proposal.pk])
            actual = cursor.fetchone()[0]
        if isinstance(actual, str):
            actual = json.loads(actual)
        proposal.payload = {**proposal.payload, 'applied': actual}
    else:
        from payments.recovery import recover_operation_locked

        recover_operation_locked(actor, proposal, reason)
    proposal.state = 'APPLIED'
    proposal.applied_at = db_now()
    proposal.version += 1
    proposal.save(update_fields=['state', 'applied_at', 'version', 'payload'])
    record(
        actor,
        'RECOVERY_APPLIED',
        proposal.pk,
        reason=reason,
        metadata={'kind': proposal.kind, 'diff': proposal.payload},
    )
    # A clean global recheck and separate acknowledgement still own gate reopening.
    return proposal
