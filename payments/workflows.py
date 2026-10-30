"""Payment coordination. All money is posted through ledger's SQL boundary.

Lock order: optional job fence, action advisory, admission gate, users,
intent/destination/request/schedule/operation/case, wallets, accounts, day counters.
No primary transaction spans an independent simulator call.
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q

from accounts.models import User
from accounts.services import assert_eligible, db_now, lock_users, verify_totp
from audit.services import record
from ledger import posting
from ledger.models import DailyCounter, IntegrityGate, Wallet
from oversight.models import Case, Finding

from .errors import DomainError
from .models import Intent, MoneyRequest, Operation, ReplayKey, Schedule
from .money import DAY_LIMIT, POLICY, REVIEW_THRESHOLD, WALLET_CAP, parse_amount, validate_cents
from .retry import retry_rolled_back

TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED'}


def _digest(instruction):
    return hashlib.sha256(
        json.dumps(instruction, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    ).hexdigest()


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DomainError('Invalid reference.')


def _eligible(user, outgoing=False, role=None):
    try:
        assert_eligible(user, outgoing=outgoing, staff_role=role)
    except ValueError as exc:
        raise DomainError(str(exc), 'DENIED', 403) from exc


def _current_actor(actor, current):
    from config.authority import assert_request_authority

    assert_request_authority(current, db_now())
    if (
        actor.authority_version != current.authority_version
        or not current.is_active
        or current.status != 'ACTIVE'
    ):
        raise DomainError('Your authority changed. Sign in again.', 'DENIED', 403)


def _advisory(actor, family, key):
    raw = hashlib.blake2b(f'{actor}:{family}:{key}'.encode(), digest_size=8).digest()
    with connection.cursor() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)', [int.from_bytes(raw, 'big', signed=True)])


def _gate(require_open=True):
    with connection.cursor() as c:
        c.execute(
            'SELECT is_open,policy_version,environment_id,recovery_generation FROM ledger_integritygate WHERE id=1 FOR SHARE'
        )
        row = c.fetchone()
    if not require_open:
        return row
    from config.health import enforcement_ready

    if not enforcement_ready(check_runtime_role=not getattr(settings, 'VERIFY_SYNCHRONOUS_WORK', False)):
        raise DomainError(
            'Protected database capabilities are unavailable. Money activity is paused.', 'UNAVAILABLE', 503
        )
    if (
        not row
        or not row[0]
        or row[1] != POLICY
        or str(row[2]) != str(settings.ENVIRONMENT_ID)
        or row[3] != settings.RECOVERY_GENERATION
    ):
        raise DomainError(
            'Money activity is paused for an integrity or recovery check. You can still inspect your history.',
            'UNAVAILABLE',
            503,
        )
    last_seen = IntegrityGate.objects.filter(pk=1).values_list('last_seen_at', flat=True).first()
    if last_seen and db_now() < last_seen - timedelta(seconds=60):
        raise DomainError('Money activity is paused until the database clock is checked.', 'UNAVAILABLE', 503)
    try:
        marker = json.loads(settings.RECOVERY_MARKER_PATH.read_text())
        if (
            str(marker['environment_id']) != str(settings.ENVIRONMENT_ID)
            or int(marker['generation']) != settings.RECOVERY_GENERATION
        ):
            raise ValueError('marker')
    except (OSError, ValueError, KeyError):
        raise DomainError('Recovery marker is missing or inconsistent.', 'UNAVAILABLE', 503)


def _users(ids):
    return {u.pk: u for u in lock_users(sorted(set(i for i in ids if i), key=str))}


def _wallets(ids):
    return {
        w.pk: w
        for w in Wallet.objects.select_for_update().filter(pk__in=[i for i in ids if i]).order_by('pk')
    }


def _wallet_eligible(w, outgoing=False):
    if w.state not in (['OPEN'] if outgoing else ['OPEN', 'OUTGOING_RESTRICTED']):
        raise DomainError('The wallet is restricted for this action.', 'DENIED', 403)


def _note(value):
    if value is None:
        value = ''
    if (
        not isinstance(value, str)
        or len(value.strip()) > 280
        or any(ord(c) < 32 and c not in '\n\t' for c in value)
    ):
        raise DomainError('Notes may contain up to 280 plain text characters.')
    return value.strip()


def _reason(value):
    if not isinstance(value, str) or not 10 <= len(value.strip()) <= 1000:
        raise DomainError('Give a reason of 10 to 1,000 characters.')
    return value.strip()


def _recipient(handle, actor):
    normalized = str(handle).strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{3,30}', normalized):
        raise DomainError('Enter an exact recipient handle.')
    target = User.objects.filter(
        handle=normalized, status='ACTIVE', verified_at__isnull=False, mfa_confirmed=True
    ).first()
    if (
        not target
        or not Wallet.objects.filter(owner=target, state__in=['OPEN', 'OUTGOING_RESTRICTED']).exists()
    ):
        raise DomainError('No eligible recipient matches that handle.')
    if target.pk == actor.pk:
        raise DomainError('Choose a different customer. Self payments are not supported.')
    return target


def _schedule_time(ins, now):
    try:
        due = datetime.fromisoformat(str(ins['due_at']).replace('Z', '+00:00'))
        if due.tzinfo is None:
            raise ValueError()
        zone = ZoneInfo(ins.get('timezone', 'UTC'))
        local = due.astimezone(zone)
        supplied = ins.get('utc_offset')
        expected = local.strftime('%z')
        expected = expected[:3] + ':' + expected[3:]
        if supplied and supplied != expected:
            raise ValueError()
        due = due.astimezone(dt_timezone.utc)
        if due < now + timedelta(seconds=60) or due > now + timedelta(days=30):
            raise DomainError('Choose an instant at least 60 seconds and at most 30 days from confirmation.')
        return due, zone.key, expected
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        raise DomainError(
            'Use an explicit date, time, IANA timezone and matching UTC offset. Nonexistent local times are invalid.'
        )


def create_intent(actor, kind, instruction):
    if not isinstance(instruction, dict):
        raise DomainError('Invalid instruction.')
    kinds = {
        'TRANSFER',
        'FUNDING',
        'WITHDRAWAL',
        'REQUEST_PAY',
        'SCHEDULE',
        'DESTINATION',
        'CASE_DECISION',
        'RESTRICTION',
        'CLOSE',
        'EXPORT',
        'ROLE_CHANGE',
        'ROLE_INVITE',
        'GATE_REOPEN',
        'RECOVERY_APPROVAL',
    }
    if kind not in kinds:
        raise DomainError('Unsupported instruction.')
    common = {'policy_version', 'schema_version'}
    fields = {
        'TRANSFER': {'recipient_handle', 'recipient_id', 'amount', 'amount_cents', 'note'},
        'SCHEDULE': {
            'recipient_handle',
            'recipient_id',
            'amount',
            'amount_cents',
            'note',
            'due_at',
            'timezone',
            'utc_offset',
        },
        'FUNDING': {'destination_id', 'amount', 'amount_cents', 'note'},
        'WITHDRAWAL': {'destination_id', 'amount', 'amount_cents', 'note'},
        'REQUEST_PAY': {'request_id'},
        'DESTINATION': {'label'},
        'CASE_DECISION': {'case_id', 'decision', 'reason', 'version'},
        'RESTRICTION': {'wallet_id', 'state', 'reason', 'version'},
        'CLOSE': {'acknowledgement', 'version'},
        'EXPORT': set(),
        'ROLE_CHANGE': {'user_id', 'role', 'enabled', 'version'},
        'ROLE_INVITE': {'email', 'role'},
        'GATE_REOPEN': {'reason', 'version'},
        'RECOVERY_APPROVAL': {'proposal_id', 'reason', 'version'},
    }
    if set(instruction) - fields[kind] - common:
        raise DomainError('Unknown instruction fields.')
    if instruction.get('policy_version', POLICY) != POLICY or instruction.get('schema_version', 1) != 1:
        raise DomainError('This form is stale. Start a new review.', 'STALE', 409)
    with transaction.atomic():
        user = _users([actor.pk])[actor.pk]
        _current_actor(actor, user)
        _eligible(user, outgoing=kind in {'TRANSFER', 'WITHDRAWAL', 'SCHEDULE', 'REQUEST_PAY'})
        now = db_now()
        ins = {'kind': kind, 'currency': 'USD', 'policy_version': POLICY, 'schema_version': 1}
        if kind in {'TRANSFER', 'SCHEDULE'}:
            if instruction.get('recipient_id'):
                target = (
                    Wallet.objects.select_related('owner').get(pk=_uuid(instruction['recipient_id'])).owner
                )
                if target.pk == actor.pk:
                    raise DomainError('Self payments are not supported.')
            else:
                target = _recipient(instruction.get('recipient_handle', ''), user)
            ins['recipient_id'] = str(target.wallet.pk)
            ins['recipient_handle'] = target.handle
        if kind in {'TRANSFER', 'SCHEDULE', 'FUNDING', 'WITHDRAWAL'}:
            ins['amount_cents'] = (
                validate_cents(instruction['amount_cents'])
                if 'amount_cents' in instruction
                else parse_amount(instruction.get('amount', ''))
            )
            ins['note'] = _note(instruction.get('note', ''))
        if kind in {'FUNDING', 'WITHDRAWAL'}:
            from rails.models import Destination

            dest = Destination.objects.filter(
                pk=_uuid(instruction.get('destination_id')), owner=user, status='ACTIVE'
            ).first()
            if not dest:
                raise DomainError('Choose your own active synthetic destination.')
            ins['destination_id'] = str(dest.pk)
        if kind == 'SCHEDULE':
            due, zone, offset = _schedule_time(instruction, now)
            ins.update(due_at=due.isoformat(), timezone=zone, utc_offset=offset)
        if kind == 'REQUEST_PAY':
            req = (
                MoneyRequest.objects.filter(pk=_uuid(instruction.get('request_id')), payer=user)
                .select_related('requester__wallet')
                .first()
            )
            if not req:
                raise DomainError('Request not found.', 'DENIED', 404)
            ins.update(
                request_id=str(req.pk),
                recipient_id=str(req.requester.wallet.pk),
                amount_cents=req.amount_cents,
                note=req.note,
                request_version=req.version,
            )
        if kind == 'DESTINATION':
            label = str(instruction.get('label', '')).strip()
            if not 1 <= len(label) <= 80 or any(ord(c) < 32 for c in label):
                raise DomainError('Use a synthetic destination label up to 80 characters.')
            ins['label'] = label
        if kind in {
            'CASE_DECISION',
            'RESTRICTION',
            'ROLE_CHANGE',
            'ROLE_INVITE',
            'GATE_REOPEN',
            'RECOVERY_APPROVAL',
        }:
            ins.update({k: instruction.get(k) for k in fields[kind]})
            if kind in {'CASE_DECISION', 'RESTRICTION', 'GATE_REOPEN', 'RECOVERY_APPROVAL'}:
                ins['reason'] = _reason(ins.get('reason'))
        if kind == 'CLOSE':
            ins.update(acknowledgement=instruction.get('acknowledgement'), version=instruction.get('version'))
        return Intent.objects.create(
            owner=user,
            kind=kind,
            instruction=ins,
            digest=_digest(ins),
            authority_version=user.authority_version,
            created_at=now,
            expires_at=now + timedelta(seconds=120),
        )


class _BadProof(DomainError):
    keep_proof_attempt = True


def verify_intent(actor, intent_id, otp):
    from accounts.proof_budget import bound_proof_budget

    with bound_proof_budget(actor.pk):
        return _verify_intent_locked(actor, intent_id, otp)


def _verify_intent_locked(actor, intent_id, otp):
    with transaction.atomic():
        user = _users([actor.pk])[actor.pk]
        _current_actor(actor, user)
        _eligible(user)
        intent = Intent.objects.select_for_update().filter(pk=_uuid(intent_id), owner=user).first()
        now = db_now()
        if not intent:
            raise DomainError('Review not found.', 'DENIED', 404)
        if (
            intent.consumed_at
            or now >= intent.expires_at
            or intent.authority_version != user.authority_version
        ):
            raise DomainError(
                'This review expired or was already used. Start a fresh review.', 'EXPIRED', 409
            )
        try:
            verify_totp(user, otp)
        except ValueError as exc:
            if getattr(exc, 'code', None) == 'INVALID':
                raise _BadProof(str(exc), 'INVALID', 422) from exc
            raise DomainError(str(exc), getattr(exc, 'code', 'DENIED'), getattr(exc, 'status', 422)) from exc
        intent.verified_at = db_now()
        intent.grant_id = uuid.uuid4()
        intent.save(update_fields=['verified_at', 'grant_id'])
        return intent


def _proof(actor, intent):
    from config.authority import assert_request_authority, assert_request_deadline

    assert_request_authority(actor)
    now = db_now()
    assert_request_deadline(actor, now)
    if (
        intent.owner_id != actor.pk
        or intent.authority_version != actor.authority_version
        or intent.policy_version != POLICY
        or _digest(intent.instruction) != intent.digest
    ):
        raise DomainError('The review does not match current authority.', 'STALE', 409)
    if intent.consumed_at or not intent.verified_at or not intent.grant_id or now >= intent.expires_at:
        raise DomainError('A fresh, bound MFA confirmation is required. Start a new review.', 'EXPIRED', 409)
    return now


def _consume(intent, now):
    intent.consumed_at = now
    intent.save(update_fields=['consumed_at'])


def _notify(op, title=None):
    from notifications.services import notify

    text = f'{op.kind.title()} · USD {op.amount_cents // 100:,}.{op.amount_cents % 100:02d} · {op.state}. Reference {op.id}. Demonstration money; no cash value.'
    ids = set()
    if op.sender_id:
        ids.add(op.sender.owner_id)
    if op.recipient_id:
        ids.add(op.recipient.owner_id)
    for uid in ids:
        notify(
            User.objects.get(pk=uid),
            f'operation:{op.id}:{op.version}',
            title or op.state.replace('_', ' ').title(),
            text,
            mandatory=True,
        )


def _record_operation(op, action, acting=None):
    worker = getattr(op, '_audit_worker', '')
    record(
        None if worker else acting or op.actor,
        action,
        str(op.pk),
        worker=worker,
        metadata={
            'kind': op.kind,
            'amount_cents': op.amount_cents,
            'state': op.state,
            'policy': op.policy_version,
            'version': op.version,
            'initiating_actor': str(op.actor_id),
        },
    )


def _counters(owner, day, direction, amount):
    value = DailyCounter.objects.filter(owner=owner, day=day, direction=direction).first()
    if (value.reserved_cents + value.committed_cents if value else 0) + amount > DAY_LIMIT:
        raise DomainError('The $5,000.00 UTC daily admission limit would be exceeded.', 'LIMIT')


def _admit(user, ins, users, wallets, now, proof=None, request=None, schedule=None):
    from work.services import require_progress

    require_progress(rails=ins['kind'] in {'FUNDING', 'WITHDRAWAL'})
    kind = 'TRANSFER' if ins['kind'] in {'REQUEST_PAY', 'SCHEDULE'} else ins['kind']
    amount = ins['amount_cents']
    own = wallets[user.wallet.pk]
    sender = own if kind in {'TRANSFER', 'WITHDRAWAL'} else None
    recipient = (
        wallets[_uuid(ins['recipient_id'])] if kind == 'TRANSFER' else own if kind == 'FUNDING' else None
    )
    _eligible(user, outgoing=kind in {'TRANSFER', 'WITHDRAWAL'})
    _wallet_eligible(own, outgoing=kind in {'TRANSFER', 'WITHDRAWAL'})
    if recipient:
        _wallet_eligible(recipient)
        _eligible(users[recipient.owner_id])
        if (
            recipient.available_cents + recipient.reserved_cents + recipient.incoming_cents + amount
            > WALLET_CAP
        ):
            raise DomainError('Receiving wallet capacity would be exceeded.', 'CAPACITY')
    if sender and sender.available_cents < amount:
        raise DomainError('There are not enough available funds.', 'INSUFFICIENT_FUNDS')
    direction = 'INCOMING' if kind == 'FUNDING' else 'OUTGOING'
    review = amount >= REVIEW_THRESHOLD
    state = 'REVIEW_PENDING' if review else 'COMPLETED' if kind == 'TRANSFER' else 'READY'
    destination = None
    if kind in {'FUNDING', 'WITHDRAWAL'}:
        from rails.models import Destination

        destination = Destination.objects.get(pk=ins['destination_id'])
        if (
            destination.owner_id != user.pk
            or destination.status != 'ACTIVE'
            or now < destination.activation_at
        ):
            raise DomainError(
                'This synthetic destination is disabled or still in its 10 minute activation period.',
                'DENIED',
            )
        from rails.adapter import check_identity

        try:
            check_identity()
        except Exception as exc:
            raise DomainError(
                'The simulator is unavailable or requires recovery.', 'UNAVAILABLE', 503
            ) from exc
    # The final proof timestamp must belong to the day whose counter is
    # already locked. A midnight rollover requires preparing the new bucket.
    for _ in range(3):
        prepared = posting.lock_resources(
            [sender.pk if sender else None, recipient.pk if recipient else None], kind, user.pk
        )
        now = db_now()
        if now.date() != prepared.date():
            continue
        _counters(user, now.date(), direction, amount)
        now = _proof(user, proof) if proof is not None else db_now()
        if now.date() == prepared.date():
            break
    else:
        raise DomainError(
            'The database day changed while preparing admission. Retry the same action.', 'UNAVAILABLE', 503
        )
    if request is not None and now >= request.expires_at:
        raise DomainError('This money request expired before admission.', 'EXPIRED', 409)
    if schedule is not None and now >= schedule.due_at + timedelta(hours=24):
        raise DomainError('The schedule execution window elapsed before admission.', 'MISSED', 409)
    op_id = uuid.uuid4()
    op = Operation.objects.create(
        id=op_id,
        actor=user,
        sender=sender,
        recipient=recipient,
        destination=destination,
        kind=kind,
        amount_cents=amount,
        note=ins.get('note', ''),
        state=state,
        limit_day=now.date(),
        created_at=now,
        updated_at=now,
        completed_at=now if state == 'COMPLETED' else None,
        provider_key=f'{settings.ENVIRONMENT_ID}:{op_id}:{kind}'
        if kind in {'FUNDING', 'WITHDRAWAL'}
        else None,
        sender_label=user.handle if sender else 'Synthetic bank',
        recipient_label=users[recipient.owner_id].handle if recipient else destination.label,
    )
    _record_operation(op, 'payment.admitted')
    if state == 'COMPLETED':
        posting.post_effect(op, 'TRANSFER')
    else:
        posting.reserve(op)
    from work.services import enqueue

    if review:
        case = Case.objects.create(
            operation=op,
            type='RISK',
            creator=user,
            reason='DWMT-P1: amount is at least $1,000.00.',
            state='OPEN',
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        enqueue('REVIEW_EXPIRY', case.pk, key=f'review-expiry:{case.pk}', due_at=case.expires_at)
    elif state == 'READY':
        enqueue('DISPATCH', op.pk, key=f'dispatch:{op.pk}')
    _notify(op)
    return op


def _replay_target(replay):
    if replay.operation_id:
        return Operation.objects.get(pk=replay.operation_id)
    if replay.target_kind == 'SCHEDULE':
        return Schedule.objects.get(pk=replay.target_id)
    if replay.target_kind == 'DESTINATION':
        from rails.models import Destination

        return Destination.objects.get(pk=replay.target_id)
    if replay.target_kind == 'CASE':
        return Case.objects.get(pk=replay.target_id)
    if replay.target_kind == 'WALLET':
        return Wallet.objects.get(pk=replay.target_id)
    if replay.target_kind == 'USER':
        return User.objects.get(pk=replay.target_id)
    if replay.target_kind == 'INVITATION':
        from accounts.models import StaffInvitation

        return StaffInvitation.objects.get(pk=replay.target_id)
    if replay.target_kind == 'EXPORT':
        from reports.models import Export

        return Export.objects.get(pk=replay.target_id)
    if replay.target_kind == 'GATE':
        return IntegrityGate.objects.get(pk=1)
    if replay.target_kind == 'RECOVERY':
        from oversight.models import RecoveryProposal

        return RecoveryProposal.objects.get(pk=replay.target_id)
    return replay


@retry_rolled_back
def confirm_intent(actor, intent_id, action_key, expected_kind=None):
    from accounts.services import rate_limit

    key = _uuid(action_key)
    initial = Intent.objects.filter(pk=_uuid(intent_id), owner=actor).first()
    if not initial:
        return resolve_accepted_action(actor, key, intent_id=_uuid(intent_id), family=expected_kind)
    accepted = ReplayKey.objects.filter(owner=actor, family=initial.kind, action_key=key).exists()
    rate_limit('status' if accepted else 'money-admission', actor.pk, 60 if accepted else 20, 60)
    if expected_kind and initial.kind != expected_kind:
        raise DomainError('Wrong instruction type.', 'CONFLICT', 409)
    ins = initial.instruction
    ids = [actor.pk]
    recipient_id = ins.get('recipient_id')
    if recipient_id:
        ids.append(Wallet.objects.get(pk=recipient_id).owner_id)
    if initial.kind in {
        'CASE_DECISION',
        'RESTRICTION',
        'ROLE_CHANGE',
        'ROLE_INVITE',
        'GATE_REOPEN',
        'RECOVERY_APPROVAL',
    }:
        return _confirm_staff(actor, initial, key)
    with transaction.atomic():
        _advisory(actor.pk, initial.kind, key)
        _gate(False)
        users = _users(ids)
        user = users[actor.pk]
        _current_actor(actor, user)
        if user.status != 'ACTIVE' or not user.is_active:
            raise DomainError('Account unavailable.', 'DENIED', 403)
        replay = ReplayKey.objects.filter(owner=user, family=initial.kind, action_key=key).first()
        if replay:
            if replay.digest != initial.digest:
                raise DomainError(
                    'This action key already belongs to a different instruction.', 'CONFLICT', 409
                )
            return _replay_target(replay)
        if initial.kind not in {'GATE_REOPEN', 'RECOVERY_APPROVAL'}:
            _gate()
        intent = Intent.objects.select_for_update().get(pk=initial.pk)
        _eligible(user, outgoing=intent.kind in {'TRANSFER', 'SCHEDULE', 'REQUEST_PAY', 'WITHDRAWAL'})
        request = None
        if intent.kind == 'REQUEST_PAY':
            request = MoneyRequest.objects.select_for_update().get(pk=ins['request_id'])
            if request.payer_id != user.pk:
                raise DomainError('Request not found.', 'DENIED', 404)
            if request.operation_id:
                return request.operation
        if ins.get('destination_id'):
            from rails.models import Destination

            list(Destination.objects.select_for_update().filter(pk=ins['destination_id']))
        wallets = _wallets([user.wallet.pk, recipient_id])
        now = _proof(user, intent)
        if request and (
            request.state != 'OPEN' or now >= request.expires_at or request.version != ins['request_version']
        ):
            raise DomainError('The request is no longer open.', 'STALE', 409)
        if intent.kind == 'SCHEDULE':
            due, zone, offset = _schedule_time(ins, now)
            if Schedule.objects.filter(owner=user, state='SCHEDULED').count() >= 20:
                raise DomainError('At most 20 pending schedules are allowed.', 'LIMIT')
            _wallet_eligible(wallets[user.wallet.pk], True)
            _wallet_eligible(wallets[_uuid(recipient_id)])
            now = _proof(user, intent)
            due, zone, offset = _schedule_time(ins, now)
            target = Schedule.objects.create(
                owner=user,
                recipient=wallets[_uuid(recipient_id)],
                amount_cents=ins['amount_cents'],
                note=ins['note'],
                due_at=due,
                timezone=zone,
                utc_offset=offset,
                authority_version=user.authority_version,
                created_at=now,
            )
            from work.services import enqueue

            enqueue('SCHEDULE', target.pk, key=f'schedule:{target.pk}', due_at=due)
            record(user, 'schedule.created', str(target.pk), metadata={'due_at': due.isoformat()})
            target_kind = 'SCHEDULE'
        elif intent.kind == 'DESTINATION':
            from rails.services import create_destination

            target = create_destination(user, ins['label'])
            target_kind = 'DESTINATION'
        elif intent.kind in {'TRANSFER', 'REQUEST_PAY', 'FUNDING', 'WITHDRAWAL'}:
            target = _admit(user, ins, users, wallets, now, proof=intent, request=request)
            target_kind = 'OPERATION'
            if request:
                request.operation = target
                request.state = 'PAID' if target.state == 'COMPLETED' else 'PAYMENT_PENDING'
                request.version += 1
                request.save(update_fields=['operation', 'state', 'version'])
        elif intent.kind == 'EXPORT':
            from reports.services import generate_account_export

            target = generate_account_export(user, final_guard=lambda: _proof(user, intent))
            target_kind = 'EXPORT'
        elif intent.kind == 'CLOSE':
            return _close_account(user, intent, now)
        else:
            raise DomainError('Unsupported instruction.')
        _consume(intent, db_now())
        ReplayKey.objects.create(
            owner=user,
            family=intent.kind,
            action_key=key,
            digest=intent.digest,
            intent_id=intent.pk,
            operation=target if target_kind == 'OPERATION' else None,
            target_id=target.pk,
            target_kind=target_kind,
            created_at=now,
        )
        return target


def confirm_transfer(actor, intent_id, action_key):
    return confirm_intent(actor, intent_id, action_key, 'TRANSFER')


def confirm_rail_operation(actor, intent_id, action_key):
    return confirm_intent(actor, intent_id, action_key)


def _lock_operation(opid, actor_id=None, gate=False, job_id=None, generation=None):
    from work.services import fence_job

    if job_id and not fence_job(job_id, generation):
        raise DomainError('Worker lease is stale.', 'STALE', 409)
    initial = Operation.objects.select_related('sender', 'recipient').get(pk=opid)
    gate_row = None
    if gate in {'share', 'update'}:
        # Accepted obligations may settle while admission is closed. Lock the
        # gate before security and business rows, without treating it as proof
        # that a new provider effect is safe to create.
        mode = 'FOR UPDATE' if gate == 'update' else 'FOR SHARE'
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT is_open,policy_version,environment_id,recovery_generation FROM ledger_integritygate WHERE id=1 '
                + mode
            )
            gate_row = cursor.fetchone()
        if gate_row is None:
            raise DomainError('Integrity state is unavailable.', 'UNAVAILABLE', 503)
    elif gate:
        _gate()
    ids = [initial.actor_id, actor_id]
    ids += [w.owner_id for w in [initial.sender, initial.recipient] if w]
    users = _users(ids)
    if initial.destination_id:
        from rails.models import Destination

        list(Destination.objects.select_for_update().filter(pk=initial.destination_id))
    # A request is always locked before its operation.
    list(MoneyRequest.objects.select_for_update().filter(operation_id=opid).order_by('pk'))
    list(Schedule.objects.select_for_update().filter(operation_id=opid).order_by('pk'))
    op = Operation.objects.select_for_update().get(pk=opid)
    op._locked_gate = gate_row
    list(Case.objects.select_for_update().filter(operation=op).order_by('pk'))
    wallets = _wallets([op.sender_id, op.recipient_id])
    if actor_id is None:
        op._audit_worker = f'job:{job_id}' if job_id else 'payment-worker'
    return op, users, wallets


def _transition(op, state, reason=''):
    op.state = state
    op.updated_at = db_now()
    op.version += 1
    op.failure_reason = reason
    if state == 'COMPLETED':
        op.completed_at = op.updated_at
    op.save(update_fields=['state', 'updated_at', 'version', 'failure_reason', 'completed_at'])


def _linked_request(op):
    req = MoneyRequest.objects.filter(operation=op).first()
    if req:
        req.state = 'PAID' if op.state == 'COMPLETED' else 'CLOSED_UNPAID'
        req.version += 1
        req.save(update_fields=['state', 'version'])


def _cancel(op, reason, acting=None):
    if op.state not in {'REVIEW_PENDING', 'READY'}:
        raise DomainError(
            'Submission has already won. Settlement must finish or be investigated.', 'CONFLICT', 409
        )
    _transition(op, 'CANCELLED', reason)
    posting.release(op)
    Case.objects.filter(operation=op, type='RISK', state='OPEN').update(
        state='CANCELLED', decision_reason=reason, decided_at=db_now()
    )
    _linked_request(op)
    _record_operation(op, 'payment.cancelled', acting=acting)
    _notify(op)
    return op


def cancel_operation(actor, operation_id, version):
    with transaction.atomic():
        op, users, wallets = _lock_operation(_uuid(operation_id), actor.pk)
        if op.actor_id != actor.pk:
            raise DomainError('Payment not found.', 'DENIED', 404)
        _current_actor(actor, users[actor.pk])
        _eligible(users[actor.pk])
        if op.version != int(version):
            raise DomainError('This payment changed. Refresh its detail.', 'STALE', 409)
        return _cancel(op, 'Cancelled before provider submission.')


def dispatch_operation(operation_id, job_id=None, generation=None):
    from rails.adapter import create_or_get, lookup

    opid = _uuid(operation_id)
    with transaction.atomic():
        op, users, wallets = _lock_operation(opid, gate='share', job_id=job_id, generation=generation)
        if op.state in TERMINAL:
            return op
        if op.state == 'REVIEW_PENDING':
            return op
        if op.state == 'READY':
            from config.health import enforcement_ready

            if not enforcement_ready(
                check_runtime_role=not getattr(settings, 'VERIFY_SYNCHRONOUS_WORK', False)
            ):
                raise DomainError(
                    'Posting capabilities are unavailable; this queued operation remains reserved.',
                    'UNAVAILABLE',
                    503,
                )
            from rails.models import Destination

            dest = Destination.objects.get(pk=op.destination_id)
            owner = users[op.actor_id]
            own = wallets[op.sender_id or op.recipient_id]
            # A recovery/integrity pause retains the queued obligation. It is
            # different from loss of customer or destination eligibility.
            row = op._locked_gate
            if (
                not row[0]
                or row[1] != POLICY
                or str(row[2]) != str(settings.ENVIRONMENT_ID)
                or row[3] != settings.RECOVERY_GENERATION
            ):
                raise DomainError('Dispatch is paused for integrity or recovery checks.', 'UNAVAILABLE', 503)
            try:
                marker = json.loads(settings.RECOVERY_MARKER_PATH.read_text())
                if (
                    str(marker['environment_id']) != str(settings.ENVIRONMENT_ID)
                    or int(marker['generation']) != settings.RECOVERY_GENERATION
                ):
                    raise ValueError('marker')
            except (OSError, ValueError, KeyError):
                raise DomainError('Recovery marker is missing or inconsistent.', 'UNAVAILABLE', 503)
            try:
                _eligible(owner, outgoing=op.kind == 'WITHDRAWAL')
                _wallet_eligible(own, op.kind == 'WITHDRAWAL')
                if dest.status != 'ACTIVE' or db_now() < dest.activation_at:
                    raise DomainError('Destination unavailable.')
            except DomainError:
                return _cancel(op, 'Eligibility changed before submission.')
            _transition(op, 'SUBMITTED')
            _record_operation(op, 'payment.submitted')
        key = op.provider_key
    # Independent transaction may commit even if this call fails.
    try:
        observation = lookup(key)
        if observation is None:
            # A missing provider row only permits creation when the primary,
            # simulator and external recovery markers still agree. The
            # independent effect itself is deliberately outside this txn.
            with transaction.atomic():
                _gate()
            observation = create_or_get(Operation.objects.get(pk=opid))
    except Exception:
        with transaction.atomic():
            current, _, _ = _lock_operation(opid, job_id=job_id, generation=generation)
            if current.state in {'SUBMITTED', 'UNKNOWN'}:
                _transition(
                    current,
                    'UNKNOWN',
                    'Provider outcome is unknown. Funds remain reserved; staff reconciliation is responsible.',
                )
                _record_operation(current, 'payment.unknown')
                _notify(current)
        return Operation.objects.get(pk=opid)
    return apply_provider_observation(opid, observation, job_id, generation)


def apply_provider_observation(operation_id, observation, job_id=None, generation=None):
    with transaction.atomic():
        op, _, _ = _lock_operation(_uuid(operation_id), gate='update', job_id=job_id, generation=generation)
        from config.health import enforcement_ready

        if not enforcement_ready(check_runtime_role=not getattr(settings, 'VERIFY_SYNCHRONOUS_WORK', False)):
            raise DomainError(
                'Posting capabilities are unavailable; the accepted settlement remains due.',
                'UNAVAILABLE',
                503,
            )
        expected_fields = {
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
        structured = isinstance(observation, dict) and set(observation) == expected_fields
        if structured:
            try:
                _uuid(observation['event_id'])
                _uuid(observation['token_id'])
                occurred = datetime.fromisoformat(str(observation['occurred_at']).replace('Z', '+00:00'))
                structured = (
                    occurred.tzinfo is not None
                    and type(observation['provider_sequence']) is int
                    and observation['provider_sequence'] >= 1
                    and type(observation['amount_cents']) is int
                    and type(observation['schema_version']) is int
                    and observation['schema_version'] == 1
                    and observation['state'] in {'SUCCESS', 'DECLINED', 'PENDING'}
                )
            except (DomainError, ValueError, TypeError, KeyError):
                structured = False
        if not structured:
            if op.state in {'SUBMITTED', 'UNKNOWN'}:
                _transition(
                    op,
                    'UNKNOWN',
                    'The provider reply was malformed. Read-only reconciliation retains responsibility.',
                )
                from work.services import enqueue

                enqueue('LOOKUP', op.pk, key=f'invalid-reply:{op.pk}')
            _record_operation(op, 'provider.invalid_reply')
            return op
        expected_direction = op.kind
        valid = (
            isinstance(observation, dict)
            and observation.get('provider_key') == op.provider_key
            and observation.get('direction') == expected_direction
            and type(observation.get('amount_cents')) is int
            and observation.get('amount_cents') == op.amount_cents
            and observation.get('currency') == 'USD'
            and str(observation.get('token_id')) == str(op.destination.token_id)
            and type(observation.get('schema_version')) is int
            and observation.get('schema_version') == 1
        )
        state = observation.get('state') if isinstance(observation, dict) else None

        def incident(classification, details):
            Finding.objects.get_or_create(
                classification=classification,
                operation=op,
                provider_key=op.provider_key,
                state='OPEN',
                defaults={'details': details, 'created_at': db_now()},
            )
            gate = IntegrityGate.objects.get(pk=1)
            gate.is_open = False
            gate.version += 1
            gate.reason = f'{classification}: provider reconciliation is required for {op.pk}.'
            gate.changed_at = db_now()
            gate.changed_by = None
            gate.save(update_fields=['is_open', 'version', 'reason', 'changed_at', 'changed_by'])
            record(
                None,
                'integrity.closed',
                str(gate.pk),
                reason=gate.reason,
                metadata={'operation': str(op.pk)},
                worker=f'job:{job_id}' if job_id else 'provider-coordinator',
            )

        if not valid or state not in {'SUCCESS', 'DECLINED', 'PENDING'}:
            incident('MISMATCHED_EFFECT', {'reason': 'Untrusted or mismatched observation'})
            if op.state in {'SUBMITTED', 'UNKNOWN'}:
                _transition(op, 'UNKNOWN', 'Provider response requires investigation.')
            _record_operation(op, 'provider.mismatch')
            return op
        if op.state in TERMINAL:
            # A stale PENDING hint cannot contradict an already final result.
            # Opposite finals, or any provider work after cancellation, need
            # investigation and must never rewrite existing accounting.
            if (
                (op.state == 'COMPLETED' and state == 'DECLINED')
                or (op.state == 'FAILED' and state == 'SUCCESS')
                or op.state == 'CANCELLED'
            ):
                incident('CONTRADICTORY_TERMINAL', {'observed': state, 'local': op.state})
                _record_operation(op, 'provider.contradiction')
            return op
        if op.state not in {'SUBMITTED', 'UNKNOWN'}:
            raise DomainError('Provider result preceded submission.', 'CONFLICT', 409)
        if state == 'PENDING':
            if op.state != 'SUBMITTED':
                _transition(op, 'SUBMITTED')
                _record_operation(op, 'provider.pending')
            return op
        if state == 'SUCCESS':
            _transition(op, 'COMPLETED')
            posting.post_effect(op, op.kind)
        else:
            _transition(op, 'FAILED', 'The synthetic provider authoritatively declined this operation.')
            posting.release(op)
        _record_operation(op, 'payment.settled')
        _notify(op)
        return op


def create_request(actor, payer_handle, amount, note=''):
    cents = parse_amount(amount) if isinstance(amount, str) else validate_cents(amount)
    target = _recipient(payer_handle, actor)
    with transaction.atomic():
        _gate()
        users = _users([actor.pk, target.pk])
        user = users[actor.pk]
        _current_actor(actor, user)
        _eligible(user)
        _eligible(users[target.pk], outgoing=True)
        wallets = _wallets([user.wallet.pk, target.wallet.pk])
        _wallet_eligible(wallets[user.wallet.pk])
        _wallet_eligible(wallets[target.wallet.pk], True)
        if MoneyRequest.objects.filter(requester=user, state='OPEN').count() >= 20:
            raise DomainError('At most 20 open outgoing requests are allowed.', 'LIMIT')
        now = db_now()
        req = MoneyRequest.objects.create(
            requester=user,
            payer=target,
            amount_cents=cents,
            note=_note(note),
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        record(user, 'request.created', str(req.pk))
        from notifications.services import notify

        notify(
            target,
            f'request:{req.pk}',
            'Money requested',
            f'@{user.handle} requests USD {cents // 100}.{cents % 100:02d}. No money moves until you confirm. Reference {req.pk}. Demonstration money; no cash value.',
        )
        return req


def decide_request(actor, request_id, action, version):
    initial = MoneyRequest.objects.get(pk=_uuid(request_id))
    with transaction.atomic():
        users = _users([initial.payer_id, initial.requester_id])
        user = users.get(actor.pk)
        if not user:
            raise DomainError('Request not found.', 'DENIED', 404)
        _current_actor(actor, user)
        _eligible(user)
        req = MoneyRequest.objects.select_for_update().get(pk=initial.pk)
        if req.version != int(version):
            raise DomainError('This request changed. Refresh it.', 'STALE', 409)
        if req.state != 'OPEN':
            raise DomainError('This request is no longer open.', 'CONFLICT', 409)
        now = db_now()
        if now >= req.expires_at:
            req.state = 'EXPIRED'
        elif action == 'cancel' and req.requester_id == actor.pk:
            req.state = 'CANCELLED'
        elif action == 'decline' and req.payer_id == actor.pk:
            req.state = 'DECLINED'
        else:
            raise DomainError('This action is not permitted.', 'DENIED', 403)
        req.version += 1
        req.save(update_fields=['state', 'version'])
        record(user, 'request.' + req.state.lower(), str(req.pk))
        return req


def cancel_schedule(actor, schedule_id, version):
    with transaction.atomic():
        user = _users([actor.pk])[actor.pk]
        _current_actor(actor, user)
        _eligible(user)
        schedule = Schedule.objects.select_for_update().filter(pk=_uuid(schedule_id), owner=user).first()
        if not schedule:
            raise DomainError('Schedule not found.', 'DENIED', 404)
        if schedule.version != int(version) or schedule.state != 'SCHEDULED':
            raise DomainError('Execution or another change has already won.', 'STALE', 409)
        schedule.state = 'CANCELLED'
        schedule.version += 1
        schedule.save(update_fields=['state', 'version'])
        record(user, 'schedule.cancelled', str(schedule.pk))
        return schedule


def execute_schedule(schedule_id, job_id=None, generation=None):
    initial = Schedule.objects.select_related('owner__wallet', 'recipient').get(pk=_uuid(schedule_id))
    with transaction.atomic():
        from work.services import fence_job

        if job_id and not fence_job(job_id, generation):
            raise DomainError('Stale worker.', 'STALE', 409)
        _advisory(initial.owner_id, 'SCHEDULE_EXECUTE', initial.pk)
        _gate()
        users = _users([initial.owner_id, initial.recipient.owner_id])
        schedule = Schedule.objects.select_for_update().get(pk=initial.pk)
        if schedule.state != 'SCHEDULED':
            return schedule
        wallets = _wallets([initial.owner.wallet.pk, initial.recipient_id])
        now = db_now()
        if now < schedule.due_at:
            return schedule
        user = users[schedule.owner_id]
        if now >= schedule.due_at + timedelta(hours=24):
            schedule.state = 'MISSED'
            schedule.failure_reason = 'The 24 hour execution window elapsed.'
        elif schedule.authority_version != user.authority_version or schedule.policy_version != POLICY:
            schedule.state = 'FAILED'
            schedule.failure_reason = 'Security authority or policy changed. Create a new schedule.'
        else:
            try:
                with transaction.atomic():
                    ins = {
                        'kind': 'SCHEDULE',
                        'recipient_id': str(schedule.recipient_id),
                        'amount_cents': schedule.amount_cents,
                        'note': schedule.note,
                    }
                    op = _admit(user, ins, users, wallets, db_now(), schedule=schedule)
            except DomainError as exc:
                schedule.state = 'MISSED' if exc.code == 'MISSED' else 'FAILED'
                schedule.failure_reason = str(exc)
            else:
                schedule.operation = op
                schedule.state = 'EXECUTED'
        schedule.version += 1
        schedule.save(update_fields=['state', 'failure_reason', 'operation', 'version'])
        record(user, 'schedule.' + schedule.state.lower(), str(schedule.pk), reason=schedule.failure_reason)
        return schedule


def open_dispute(actor, operation_id, reason, evidence=''):
    reason = _reason(reason)
    if not isinstance(evidence, str) or len(evidence) > 4000:
        raise DomainError('Evidence must be plain text up to 4,000 characters.')
    with transaction.atomic():
        op, users, _ = _lock_operation(_uuid(operation_id), actor.pk)
        _current_actor(actor, users[actor.pk])
        _eligible(users[actor.pk])
        now = db_now()
        if not op.sender_id or op.sender.owner_id != actor.pk:
            raise DomainError('Payment not found.', 'DENIED', 404)
        if op.kind != 'TRANSFER' or op.state != 'COMPLETED' or now >= op.completed_at + timedelta(days=30):
            raise DomainError(
                'Disputes are available for completed outgoing internal transfers within 30 days.'
            )
        if Operation.objects.filter(original=op).exists():
            raise DomainError('This transfer was already reversed.', 'CONFLICT', 409)
        if Case.objects.filter(operation=op, type='DISPUTE', state='OPEN').exists():
            raise DomainError('There is already an open dispute.', 'CONFLICT', 409)
        case = Case.objects.create(
            operation=op, type='DISPUTE', creator=actor, reason=reason, evidence=evidence, created_at=now
        )
        record(actor, 'dispute.opened', str(case.pk), reason=reason)
        return case


def propose_reversal(actor, dispute_id, reason):
    reason = _reason(reason)
    initial = Case.objects.select_related('operation__sender', 'operation__recipient').get(
        pk=_uuid(dispute_id)
    )
    with transaction.atomic():
        op, users, _ = _lock_operation(initial.operation_id, actor.pk)
        _current_actor(actor, users[actor.pk])
        _eligible(users[actor.pk], role='FINANCE')
        dispute = Case.objects.select_for_update().get(pk=initial.pk)
        if dispute.type != 'DISPUTE' or dispute.state != 'OPEN':
            raise DomainError('This dispute is no longer open.', 'CONFLICT', 409)
        if actor.pk in {op.actor_id, op.recipient.owner_id}:
            raise DomainError('You cannot propose a reversal involving your own wallet.', 'DENIED', 403)
        if dispute.proposals.filter(state='OPEN').exists():
            raise DomainError('A proposal already awaits a second reviewer.', 'CONFLICT', 409)
        proposal = Case.objects.create(
            operation=op, type='REVERSAL', creator=actor, parent=dispute, reason=reason, created_at=db_now()
        )
        record(actor, 'reversal.proposed', str(proposal.pk), reason=reason)
        return proposal


class _DiscoveryChanged(Exception):
    pass


def _confirm_staff(actor, initial, key):
    for attempt in range(3):
        try:
            return _confirm_staff_once(actor, initial, key)
        except _DiscoveryChanged:
            continue
    raise DomainError('Related review work changed. Refresh and retry the same action.', 'UNAVAILABLE', 503)


def _confirm_staff_once(actor, initial, key):
    ins = initial.instruction
    ids = [actor.pk]
    wallet_ids = []
    restriction_reviews = []
    if initial.kind == 'CASE_DECISION':
        case0 = Case.objects.select_related('operation__sender', 'operation__recipient').get(
            pk=_uuid(ins['case_id'])
        )
        op0 = case0.operation
        ids += [
            op0.actor_id,
            op0.sender.owner_id if op0.sender_id else None,
            op0.recipient.owner_id if op0.recipient_id else None,
        ]
        wallet_ids = [op0.sender_id, op0.recipient_id]
    elif initial.kind == 'RESTRICTION':
        w0 = Wallet.objects.get(pk=_uuid(ins['wallet_id']))
        ids.append(w0.owner_id)
        wallet_ids = [w0.pk]
        if ins.get('state') != 'OPEN':
            restriction_reviews = list(
                Operation.objects.select_related('sender', 'recipient')
                .filter(Q(sender=w0) | Q(recipient=w0), kind='TRANSFER', state='REVIEW_PENDING')
                .order_by('pk')
            )
            for pending in restriction_reviews:
                ids.extend([pending.sender.owner_id, pending.recipient.owner_id])
                wallet_ids.extend([pending.sender_id, pending.recipient_id])
    elif initial.kind == 'ROLE_CHANGE':
        ids.append(_uuid(ins['user_id']))
    elif initial.kind == 'RECOVERY_APPROVAL':
        from oversight.models import RecoveryProposal

        proposal0 = RecoveryProposal.objects.select_related('finding').get(pk=_uuid(ins['proposal_id']))
        recovery_wallet = Wallet.objects.get(pk=proposal0.payload['wallet_id'])
        ids += [proposal0.creator_id, recovery_wallet.owner_id]
        wallet_ids = [recovery_wallet.pk]
        if proposal0.kind == 'RECOVER_OPERATION':
            from oversight.recovery import provider_recovery_payload

            current_source = provider_recovery_payload(proposal0.finding)
            if current_source != proposal0.payload:
                raise DomainError(
                    'The retained provider source changed. Create a fresh recovery proposal.', 'STALE', 409
                )
    with transaction.atomic():
        _advisory(actor.pk, initial.kind, key)
        if initial.kind in {'GATE_REOPEN', 'RECOVERY_APPROVAL'}:
            list(IntegrityGate.objects.select_for_update().filter(pk=1))
        else:
            _gate(False)
        users = _users(ids)
        user = users[actor.pk]
        _current_actor(actor, user)
        if initial.kind == 'RESTRICTION' and ins.get('state') != 'OPEN':
            current = set(
                Operation.objects.filter(
                    Q(sender=w0) | Q(recipient=w0), kind='TRANSFER', state='REVIEW_PENDING'
                ).values_list('pk', flat=True)
            )
            if current != {op.pk for op in restriction_reviews}:
                raise _DiscoveryChanged()
        _eligible(
            user,
            outgoing=initial.kind in {'CASE_DECISION', 'RECOVERY_APPROVAL'},
            role='ACCESS_ADMIN' if initial.kind in {'ROLE_CHANGE', 'ROLE_INVITE'} else 'REVIEWER',
        )
        replay = ReplayKey.objects.filter(owner=user, family=initial.kind, action_key=key).first()
        if replay:
            if replay.digest != initial.digest:
                raise DomainError('Action key conflict.', 'CONFLICT', 409)
            return _replay_target(replay)
        if initial.kind not in {'GATE_REOPEN', 'RECOVERY_APPROVAL'}:
            _gate()
        intent = Intent.objects.select_for_update().get(pk=initial.pk)
        if initial.kind == 'CASE_DECISION':
            list(MoneyRequest.objects.select_for_update().filter(operation=op0).order_by('pk'))
            list(Schedule.objects.select_for_update().filter(operation=op0).order_by('pk'))
            op = Operation.objects.select_for_update().get(pk=op0.pk)
            case = Case.objects.select_for_update().get(pk=case0.pk)
        if initial.kind == 'RESTRICTION' and restriction_reviews:
            opids = [op.pk for op in restriction_reviews]
            list(MoneyRequest.objects.select_for_update().filter(operation_id__in=opids).order_by('pk'))
            list(Schedule.objects.select_for_update().filter(operation_id__in=opids).order_by('pk'))
            restriction_reviews = list(
                Operation.objects.select_for_update().filter(pk__in=opids).order_by('pk')
            )
            list(Case.objects.select_for_update().filter(operation_id__in=opids).order_by('pk'))
        if initial.kind == 'RECOVERY_APPROVAL':
            if proposal0.kind == 'RECOVER_OPERATION':
                from rails.models import Destination

                list(
                    Destination.objects.select_for_update().filter(
                        pk=proposal0.payload['recovery_metadata']['destination_id']
                    )
                )
            proposal = RecoveryProposal.objects.select_for_update().get(pk=proposal0.pk)
            if proposal.version != int(ins['version']):
                raise DomainError('The recovery proposal changed.', 'STALE', 409)
        wallets = _wallets(wallet_ids)
        if initial.kind == 'RECOVERY_APPROVAL':
            metadata = proposal.payload.get('recovery_metadata', {})
            posting.lock_resources(
                wallet_ids,
                metadata.get('kind', 'TRANSFER'),
                metadata.get('actor_id'),
                metadata.get('limit_day'),
            )
        if initial.kind == 'RESTRICTION' and restriction_reviews:
            posting.lock_resources(wallet_ids, 'TRANSFER')
            for pending in sorted(restriction_reviews, key=lambda op: (str(op.actor_id), op.limit_day)):
                posting.lock_resources([], 'TRANSFER', pending.actor_id, pending.limit_day)
        if initial.kind == 'CASE_DECISION':
            posting.lock_resources(wallet_ids, op.kind, op.actor_id, op.limit_day)
        now = _proof(user, intent)
        if initial.kind == 'CASE_DECISION':
            target = _decide_case(
                user,
                case,
                op,
                users,
                wallets,
                ins['decision'],
                ins['reason'],
                ins['version'],
                now,
                final_guard=lambda: _proof(user, intent),
            )
            target_kind = 'CASE'
        elif initial.kind == 'RESTRICTION':
            target = wallets[w0.pk]
            if target.owner_id == user.pk:
                raise DomainError('A different reviewer must manage your wallet.', 'DENIED', 403)
            if target.version != int(ins['version']):
                raise DomainError('Wallet changed. Refresh.', 'STALE', 409)
            if ins['state'] not in {'OPEN', 'OUTGOING_RESTRICTED', 'FROZEN'} or target.state == 'CLOSED':
                raise DomainError('Invalid restriction state.')
            old = target.state
            target.state = ins['state']
            target.version += 1
            target.save(update_fields=['state', 'version'])
            record(
                user,
                'wallet.restriction',
                str(target.pk),
                reason=ins['reason'],
                metadata={'old': old, 'new': target.state},
            )
            for pending in restriction_reviews:
                if pending.state == 'REVIEW_PENDING' and (
                    target.state == 'FROZEN'
                    or target.state == 'OUTGOING_RESTRICTED'
                    and pending.sender_id == target.pk
                ):
                    _cancel(pending, 'Wallet eligibility changed during internal review.', acting=user)
            target_kind = 'WALLET'
        elif initial.kind == 'ROLE_CHANGE':
            from accounts.services import change_role

            target = change_role(user, ins['user_id'], ins['role'], ins['enabled'], ins['version'])
            target_kind = 'USER'
        elif initial.kind == 'ROLE_INVITE':
            from accounts.services import invite_staff_locked

            target = invite_staff_locked(user, ins['email'], ins['role'])
            target_kind = 'INVITATION'
        elif initial.kind == 'RECOVERY_APPROVAL':
            from oversight.recovery import approve_recovery_locked

            target = approve_recovery_locked(
                user, proposal, ins['reason'], final_guard=lambda: _proof(user, intent)
            )
            target_kind = 'RECOVERY'
        else:
            from .reconciliation import reopen_gate

            gate = IntegrityGate.objects.get(pk=1)
            if int(ins['version']) != gate.version:
                raise DomainError('The integrity gate changed. Review it again.', 'STALE', 409)
            target = reopen_gate(user, ins['reason'], final_guard=lambda: _proof(user, intent))
            target_kind = 'GATE'
        _consume(intent, db_now())
        ReplayKey.objects.create(
            owner=user,
            family=intent.kind,
            action_key=key,
            digest=intent.digest,
            intent_id=intent.pk,
            target_id=target.pk,
            target_kind=target_kind,
            created_at=now,
        )
        return target


def _decide_case(actor, case, op, users, wallets, decision, reason, version, now, final_guard=None):
    reason = _reason(reason)
    if case.version != int(version) or case.state != 'OPEN':
        raise DomainError('This case was already changed.', 'STALE', 409)
    parties = {op.actor_id}
    if op.sender_id:
        parties.add(op.sender.owner_id)
    if op.recipient_id:
        parties.add(op.recipient.owner_id)
    if actor.pk == case.creator_id or actor.pk in parties:
        raise DomainError('A different reviewer must decide this case.', 'DENIED', 403)
    if decision not in {'APPROVE', 'REJECT', 'CLOSE_NO_REFUND'}:
        raise DomainError('Choose a valid decision.')
    if final_guard:
        now = final_guard()
    if case.type == 'RISK':
        if op.state != 'REVIEW_PENDING':
            raise DomainError('The review is no longer pending.', 'STALE', 409)
        if now >= case.expires_at:
            _cancel(op, 'Risk review expired.', acting=actor)
            case.state = 'EXPIRED'
        elif decision != 'APPROVE':
            _cancel(op, 'Review rejected.', acting=actor)
            case.state = 'REJECTED'
        else:
            try:
                _eligible(users[op.actor_id], outgoing=op.kind != 'FUNDING')
                if op.sender_id:
                    _wallet_eligible(wallets[op.sender_id], True)
                if op.recipient_id:
                    _wallet_eligible(wallets[op.recipient_id])
                    _eligible(users[op.recipient.owner_id])
                if op.destination_id:
                    dest = op.destination
                    if dest.status != 'ACTIVE':
                        raise DomainError('Destination disabled.')
            except DomainError:
                _cancel(op, 'Eligibility changed during review.', acting=actor)
                case.state = 'CANCELLED'
            else:
                if final_guard:
                    now = final_guard()
                if now >= case.expires_at:
                    _cancel(op, 'Risk review expired.', acting=actor)
                    case.state = 'EXPIRED'
                elif op.kind == 'TRANSFER':
                    _transition(op, 'COMPLETED')
                    posting.post_effect(op, 'HELD_TRANSFER')
                    _linked_request(op)
                else:
                    _transition(op, 'READY')
                    from work.services import enqueue

                    enqueue('DISPATCH', op.pk, key=f'dispatch:{op.pk}')
                if case.state != 'EXPIRED':
                    case.state = 'APPROVED'
                    _record_operation(op, 'review.approved', acting=actor)
                    _notify(op)
    elif case.type == 'REVERSAL':
        if decision != 'APPROVE':
            case.state = 'REJECTED'
        elif Operation.objects.filter(original=op).exists():
            raise DomainError('Already reversed.', 'CONFLICT', 409)
        else:
            debit = wallets[op.recipient_id]
            credit = wallets[op.sender_id]
            _wallet_eligible(debit, True)
            _wallet_eligible(credit)
            if debit.available_cents < op.amount_cents:
                case.state = 'BLOCKED_FUNDS'
            elif (
                credit.available_cents + credit.reserved_cents + credit.incoming_cents + op.amount_cents
                > WALLET_CAP
            ):
                case.state = 'BLOCKED_CAPACITY'
            elif credit.state == 'CLOSED' or debit.state == 'CLOSED':
                case.state = 'BLOCKED_CAPACITY'
            else:
                if final_guard:
                    now = final_guard()
                reverse = Operation.objects.create(
                    actor=actor,
                    sender=debit,
                    recipient=credit,
                    kind='REVERSAL',
                    amount_cents=op.amount_cents,
                    state='COMPLETED',
                    limit_day=now.date(),
                    created_at=now,
                    updated_at=now,
                    completed_at=now,
                    original=op,
                    sender_label=op.recipient_label,
                    recipient_label=op.sender_label,
                    note='Reviewed whole transfer reversal',
                )
                posting.post_effect(reverse, 'REVERSAL')
                _record_operation(reverse, 'reversal.completed')
                _notify(reverse)
                case.state = 'REVERSED'
                if case.parent_id:
                    Case.objects.filter(pk=case.parent_id).update(
                        state='REVERSED', decision_reason=reason, decided_at=now
                    )
    elif case.type in {'DISPUTE', 'SUPPORT'}:
        if decision != 'CLOSE_NO_REFUND':
            raise DomainError(
                'Finance must propose a whole reversal; this case may otherwise close without refund.'
            )
        case.state = 'CLOSED_NO_REFUND'
    else:
        raise DomainError('Unknown case type.')
    case.reviewer = actor
    case.decision_reason = reason
    case.decided_at = now
    case.version += 1
    case.save(update_fields=['state', 'reviewer', 'decision_reason', 'decided_at', 'version'])
    record(
        actor,
        'case.decided',
        str(case.pk),
        reason=reason,
        metadata={'state': case.state, 'operation': str(op.pk)},
    )
    return case


def review_case(actor, case_id, decision, reason, version, intent_id=None):
    if not intent_id:
        raise DomainError('Review this decision and confirm its bound MFA proof.')
    intent = Intent.objects.get(pk=intent_id, owner=actor)
    expected = {'case_id': str(case_id), 'decision': decision, 'reason': reason, 'version': int(version)}
    if any(intent.instruction.get(k) != v for k, v in expected.items()):
        raise DomainError('Decision differs from confirmed instruction.', 'STALE', 409)
    return confirm_intent(actor, intent_id, uuid.uuid4(), 'CASE_DECISION')


def set_restriction(actor, wallet_id, state, reason, intent_id=None):
    if not intent_id:
        raise DomainError('Review the restriction and confirm its bound MFA proof.')
    intent = Intent.objects.get(pk=intent_id, owner=actor)
    if (
        intent.instruction.get('wallet_id') != str(wallet_id)
        or intent.instruction.get('state') != state
        or intent.instruction.get('reason') != reason
    ):
        raise DomainError('Restriction differs from confirmed instruction.', 'STALE', 409)
    return confirm_intent(actor, intent_id, uuid.uuid4(), 'RESTRICTION')


def expire_reviews(limit=100):
    count = 0
    for case_id in (
        Case.objects.filter(type='RISK', state='OPEN', expires_at__lte=db_now())
        .order_by('expires_at', 'id')
        .values_list('id', flat=True)[:limit]
    ):
        initial = Case.objects.get(pk=case_id)
        with transaction.atomic():
            op, _, _ = _lock_operation(initial.operation_id)
            case = Case.objects.select_for_update().get(pk=case_id)
            if case.state == 'OPEN' and op.state == 'REVIEW_PENDING' and db_now() >= case.expires_at:
                _cancel(op, 'Risk review expired.')
                case.state = 'EXPIRED'
                case.decided_at = db_now()
                case.version += 1
                case.save(update_fields=['state', 'decided_at', 'version'])
                count += 1
    MoneyRequest.objects.filter(state='OPEN', expires_at__lte=db_now()).update(state='EXPIRED')
    return count


def _close_account(user, intent, now):
    from accounts.services import close_account_locked, closure_blockers

    blockers = closure_blockers(user)
    if blockers:
        raise DomainError('Account closure is blocked: ' + '; '.join(blockers), 'CONFLICT', 409)
    if intent.instruction.get('acknowledgement') != 'CLOSE MY WALLET':
        raise DomainError('Acknowledge that closed login cannot retrieve statements. Export first.')
    target = close_account_locked(user, final_guard=lambda: _proof(user, intent))
    _consume(intent, now)
    return target


def resolve_accepted_action(actor, action_key, intent_id=None, family=None):
    """Authorized read through the lifetime mapping; proof files may have expired."""
    from accounts.services import rate_limit

    rate_limit('status', actor.pk, 60, 60)
    from .replay import match_accepted_reference

    key = _uuid(action_key)
    preliminary = match_accepted_reference(actor, key, intent_id=intent_id, family=family)
    with transaction.atomic():
        _advisory(actor.pk, preliminary.family, key)
        _gate(False)
        current = _users([actor.pk])[actor.pk]
        _current_actor(actor, current)
        role = (
            'ACCESS_ADMIN'
            if preliminary.family in {'ROLE_CHANGE', 'ROLE_INVITE'}
            else 'REVIEWER'
            if preliminary.family in {'CASE_DECISION', 'RESTRICTION', 'GATE_REOPEN', 'RECOVERY_APPROVAL'}
            else None
        )
        if role:
            _eligible(current, role=role)
        replay = match_accepted_reference(current, key, intent_id=intent_id, family=preliminary.family)
        if replay.pk != preliminary.pk:
            raise DomainError('The accepted action reference changed.', 'CONFLICT', 409)
        result = _replay_target(replay)
        if role:
            record(
                current,
                'staff.accepted_action.read',
                str(result.pk),
                purpose='Retrieve the current result of an accepted staff action.',
            )
        return result
