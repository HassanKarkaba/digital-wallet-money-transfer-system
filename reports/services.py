"""Private, authorized snapshots derived from immutable posted entries."""

import csv
import io
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from datetime import timezone as utc_timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.models import Case, F, Q, Sum, When

from accounts.services import db_now, rate_limit
from audit.services import record
from payments.errors import DomainError

from .models import Export, Statement


def money(cents):
    cents = int(cents)
    sign = '-' if cents < 0 else ''
    return f'{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}'


def csv_safe(value):
    """Prevent a spreadsheet from evaluating user-authored labels as formulas."""
    text = str(value)
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


def private_root():
    return Path(settings.PRIVATE_ROOT)


def publish_file(folder, file_id, suffix, content):
    directory = private_root() / folder
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f'{file_id}.{suffix}'
    descriptor, temp_name = tempfile.mkstemp(prefix=f'{file_id}.', suffix='.pending', dir=directory)
    try:
        os.chmod(temp_name, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, directory / name)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return folder + '/' + name


def resolve_private(file_name):
    path = (private_root() / file_name).resolve()
    root = private_root().resolve()
    if root not in path.parents or not path.is_file():
        raise DomainError(
            'This private file is unavailable. Generate a fresh export.', 'FILE_UNAVAILABLE', 404
        )
    return path


def _statement_csv(statement, rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['DEMONSTRATION MONEY — NO CASH VALUE'])
    writer.writerow(['Currency', 'USD', 'Policy', 'DWMT-P1'])
    writer.writerow(
        [
            'Period start UTC',
            statement.start_at.isoformat(),
            'Period end UTC (exclusive)',
            statement.end_at.isoformat(),
        ]
    )
    writer.writerow(
        ['Display timezone', statement.timezone, 'Wallet sequence high water mark', statement.wallet_sequence]
    )
    writer.writerow(
        [
            'Opening available cents',
            statement.opening_available_cents,
            'Opening reserved cents',
            statement.opening_reserved_cents,
        ]
    )
    writer.writerow(
        [
            'Closing available cents',
            statement.closing_available_cents,
            'Closing reserved cents',
            statement.closing_reserved_cents,
        ]
    )
    writer.writerow([])
    writer.writerow(
        [
            'UTC timestamp',
            'Local timestamp with offset',
            'Operation reference',
            'Journal reference',
            'Type',
            'Effect',
            'Counterparty',
            'Note',
            'Available change cents',
            'Reserved change cents',
            'Available change USD',
            'Reserved change USD',
            'Available balance cents',
            'Reserved balance cents',
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row['timestamp'],
                row['local_timestamp'],
                row['operation_id'],
                row['journal_id'],
                row['kind'],
                row['effect'],
                csv_safe(row['counterparty']),
                csv_safe(row['note']),
                row['available_change'],
                row['reserved_change'],
                money(row['available_change']),
                money(row['reserved_change']),
                row['available_balance'],
                row['reserved_balance'],
            ]
        )
    return output.getvalue()


def _validate_period(start, end, display_timezone):
    if (
        not isinstance(start, datetime)
        or not isinstance(end, datetime)
        or start.tzinfo is None
        or end.tzinfo is None
        or not timedelta(0) < end - start <= timedelta(days=366)
    ):
        raise DomainError('Select a positive statement period no longer than 366 days.')
    try:
        return ZoneInfo(display_timezone)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise DomainError('Use a valid IANA timezone.') from None


def queue_statement(
    owner,
    start,
    end,
    timezone='UTC',
    *,
    enqueue_job=True,
    allow_inactive_owner=False,
    audit_actor=None,
    purpose='',
):
    from accounts.models import User
    from ledger.models import Wallet
    from work.services import enqueue

    _validate_period(start, end, timezone)
    rate_limit('export', str(owner.id), 2, 3600)
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=owner.id)
        if audit_actor is None:
            from payments.workflows import _current_actor

            _current_actor(owner, current)
        if (not current.is_active or current.status != 'ACTIVE') and not allow_inactive_owner:
            raise DomainError('This account is inactive.', 'DENIED', 403)
        if not Wallet.objects.filter(owner=current).exists():
            raise DomainError('Verify your email to open a wallet before generating a statement.')
        if Statement.objects.filter(owner=current, state='PENDING').count() >= 3:
            raise DomainError('Three statement requests are already pending. Wait before creating another.')
        now = db_now()
        statement = Statement.objects.create(
            owner=current,
            start_at=start,
            end_at=end,
            timezone=timezone,
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        if enqueue_job:
            enqueue('STATEMENT', statement.id, key='statement:' + str(statement.id))
        record(
            audit_actor or current,
            'statement.queued',
            str(statement.id),
            reason=purpose,
            metadata={'start': start.isoformat(), 'end': end.isoformat()},
        )
        return statement


def _stage_statement_file(statement, content):
    directory = private_root() / 'statements'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=f'{statement.id}.', suffix='.pending', dir=directory)
    try:
        os.chmod(temp_name, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return Path(temp_name)


def _fail_statement(statement_id, error, job_id=None, generation=None):
    with transaction.atomic():
        if job_id:
            from work.services import fence_job

            if not fence_job(job_id, generation):
                raise DomainError('This statement worker lease is stale.', 'STALE', 409)
        item = Statement.objects.select_for_update().get(pk=statement_id)
        if item.state == 'PENDING':
            item.state = 'FAILED'
            item.failure_reason = str(error)[:1000]
            item.save(update_fields=['state', 'failure_reason'])
            record(None, 'statement.failed', str(item.id), reason=item.failure_reason)
        return item


def generate_statement(owner, start, end, timezone='UTC', *, statement_id=None, job_id=None, generation=None):
    """Capture one fresh read snapshot, then publish behind a current worker fence."""
    from accounts.models import User
    from ledger.models import Entry, Wallet

    if transaction.get_connection().in_atomic_block:
        raise DomainError(
            'Statements must start in a fresh database snapshot. Try again.', 'SNAPSHOT_BUSY', 409
        )
    zone = _validate_period(start, end, timezone)
    statement = (
        Statement.objects.get(pk=statement_id, owner=owner)
        if statement_id
        else queue_statement(owner, start, end, timezone, enqueue_job=False)
    )
    if statement.state != 'PENDING':
        return statement
    started = time.monotonic()
    staged = None
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                cursor.execute("SET LOCAL statement_timeout = '28000ms'")
            from .case_access import generation_delegation

            delegated = generation_delegation(statement.id)
            current = User.objects.get(pk=owner.id)
            if (not current.is_active or current.status != 'ACTIVE') and delegated is None:
                raise DomainError('This account is inactive.', 'DENIED', 403)
            wallet = Wallet.objects.get(owner=current)
            statement.wallet_sequence = wallet.sequence
            entries = Entry.objects.filter(
                account__wallet=wallet, journal__sealed=True, journal__committed_at__lt=end
            ).select_related('account', 'journal__operation')
            signed = Case(When(side='CREDIT', then=F('amount_cents')), default=-F('amount_cents'))
            opening = (
                entries.filter(journal__committed_at__lt=start)
                .values('account__kind')
                .annotate(total=Sum(signed))
            )
            totals = {row['account__kind']: row['total'] for row in opening}
            available = statement.opening_available_cents = totals.get('AVAILABLE', 0)
            reserved = statement.opening_reserved_cents = totals.get('RESERVED', 0)
            period = entries.filter(journal__committed_at__gte=start).order_by(
                'journal__committed_at', 'journal__id', 'account__kind', 'id'
            )
            if period.count() > 100000:
                raise DomainError('This statement exceeds 100,000 entry rows. Select a narrower period.')
            grouped = {}
            for entry in period.iterator(chunk_size=1000):
                if time.monotonic() - started >= 28:
                    raise DomainError(
                        'This statement reached its generation budget. Select a narrower period.',
                        'REPORT_BUDGET',
                        503,
                    )
                journal = entry.journal
                if int(journal.wallet_sequences.get(str(wallet.id), 0)) > wallet.sequence:
                    continue
                key = str(journal.id)
                op = journal.operation
                if key not in grouped:
                    counterparty = op.sender_label if op.recipient_id == wallet.id else op.recipient_label
                    if op.kind in ('FUNDING', 'WITHDRAWAL'):
                        counterparty = counterparty or 'Synthetic USD destination'
                    grouped[key] = {
                        'timestamp': journal.committed_at.astimezone(utc_timezone.utc)
                        .isoformat()
                        .replace('+00:00', 'Z'),
                        'local_timestamp': journal.committed_at.astimezone(zone).isoformat(),
                        'operation_id': str(op.id),
                        'journal_id': key,
                        'kind': op.kind,
                        'effect': journal.effect_kind,
                        'counterparty': counterparty,
                        'note': op.note,
                        'available_change': 0,
                        'reserved_change': 0,
                    }
                change = entry.amount_cents if entry.side == 'CREDIT' else -entry.amount_cents
                column = 'available_change' if entry.account.kind == 'AVAILABLE' else 'reserved_change'
                grouped[key][column] += change
            rows = list(grouped.values())
            for row in rows:
                available += row['available_change']
                reserved += row['reserved_change']
                row['available_balance'] = available
                row['reserved_balance'] = reserved
            statement.closing_available_cents = available
            statement.closing_reserved_cents = reserved
            statement.rows = rows
            statement.row_count = len(rows)
        # File construction and the repeatable-read transaction hold no job lease lock.
        staged = _stage_statement_file(statement, _statement_csv(statement, statement.rows))
        if time.monotonic() - started >= 29:
            raise DomainError(
                'This statement reached its generation budget. Select a narrower period.',
                'REPORT_BUDGET',
                503,
            )
        with transaction.atomic():
            if job_id:
                from work.services import fence_job

                if not fence_job(job_id, generation):
                    raise DomainError('This statement worker lease is stale.', 'STALE', 409)
            from oversight.models import CaseStatement

            from .case_access import generation_delegation

            delegated_actor_id = (
                CaseStatement.objects.filter(statement_id=statement.id)
                .values_list('requested_by_id', flat=True)
                .first()
            )
            users = {
                u.id: u
                for u in User.objects.select_for_update()
                .filter(id__in=[owner.id, delegated_actor_id] if delegated_actor_id else [owner.id])
                .order_by('id')
            }
            current = users[owner.id]
            delegated = generation_delegation(statement.id, lock_case=True)
            if (not current.is_active or current.status != 'ACTIVE') and delegated is None:
                raise DomainError('This account is inactive.', 'DENIED', 403)
            retained = Statement.objects.select_for_update().get(pk=statement.id, owner=current)
            if retained.state != 'PENDING':
                return retained
            if db_now() >= retained.expires_at:
                retained.state = 'EXPIRED'
                retained.save(update_fields=['state'])
                return retained
            complete = staged.with_suffix('.csv')
            os.replace(staged, complete)
            for field in (
                'wallet_sequence',
                'opening_available_cents',
                'opening_reserved_cents',
                'closing_available_cents',
                'closing_reserved_cents',
                'rows',
                'row_count',
            ):
                setattr(retained, field, getattr(statement, field))
            retained.file_name = 'statements/' + complete.name
            retained.state = 'READY'
            retained.save()
            record(
                None,
                'statement.generated',
                str(retained.id),
                reason=delegated.purpose if delegated else '',
                metadata={
                    'start': start.isoformat(),
                    'end': end.isoformat(),
                    'sequence': retained.wallet_sequence,
                    'rows': retained.row_count,
                    'case_id': str(delegated.case_id) if delegated else None,
                    'requested_by': str(delegated.requested_by_id) if delegated else str(current.id),
                },
            )
            from notifications.services import notify

            notify(
                delegated.requested_by if delegated else current,
                'statement:' + str(retained.id),
                'Your assigned case statement is ready' if delegated else 'Your statement is ready',
                'Open your assigned case to inspect its private statement.'
                if delegated
                else 'Your private demonstration statement is available in Statements.',
                mandatory=False,
            )
            return retained
    except DomainError as exc:
        if exc.code != 'STALE':
            _fail_statement(statement.id, exc, job_id, generation)
        raise
    finally:
        if staged:
            staged.unlink(missing_ok=True)


def generate_pending_statement(statement_id, job_id=None, generation=None):
    item = Statement.objects.select_related('owner').get(pk=statement_id)
    if item.state != 'PENDING':
        return item
    try:
        return generate_statement(
            item.owner,
            item.start_at,
            item.end_at,
            item.timezone,
            statement_id=item.id,
            job_id=job_id,
            generation=generation,
        )
    except DomainError as exc:
        if exc.code == 'STALE':
            raise
        return Statement.objects.get(pk=item.id)


def generate_account_export(owner, final_guard=None):
    """Called inside the bound MFA admission transaction for this owner."""
    from ledger.models import Wallet
    from oversight.models import Case
    from payments.models import MoneyRequest, Operation, Schedule

    rate_limit('export', str(owner.id), 2, 3600)
    now = db_now()
    wallet = Wallet.objects.filter(owner=owner).first()
    export = Export.objects.create(owner=owner, created_at=now, expires_at=now + timedelta(hours=24))
    operations = (
        Operation.objects.filter(Q(sender_id=wallet.id) | Q(recipient_id=wallet.id))
        if wallet
        else Operation.objects.none()
    ).values(
        'id',
        'kind',
        'amount_cents',
        'currency',
        'state',
        'note',
        'created_at',
        'completed_at',
        'sender_label',
        'recipient_label',
        'original_id',
    )
    requests = MoneyRequest.objects.filter(Q(requester=owner) | Q(payer=owner)).values(
        'id',
        'amount_cents',
        'note',
        'state',
        'expires_at',
        'operation_id',
        'requester__handle',
        'payer__handle',
    )
    schedules = Schedule.objects.filter(owner=owner).values(
        'id',
        'amount_cents',
        'note',
        'due_at',
        'timezone',
        'utc_offset',
        'state',
        'operation_id',
        'failure_reason',
        'recipient__owner__handle',
    )
    disputes = Case.objects.filter(creator=owner, type='DISPUTE').values(
        'id', 'operation_id', 'state', 'created_at', 'reason', 'evidence', 'decision_reason'
    )
    payload = {
        'notice': 'DEMONSTRATION MONEY — NO CASH VALUE',
        'generated_at': now,
        'profile': {
            'reference': owner.id,
            'display_name': owner.display_name,
            'handle': owner.handle,
            'email': owner.email,
            'timezone': owner.timezone,
            'verified_at': owner.verified_at,
        },
        'wallet': {
            'currency': 'USD',
            'available_cents': wallet.available_cents,
            'reserved_cents': wallet.reserved_cents,
            'incoming_cents': wallet.incoming_cents,
            'sequence': wallet.sequence,
        }
        if wallet
        else None,
        'transactions': list(operations),
        'requests': list(requests),
        'schedules': list(schedules),
        'disputes': list(disputes),
        'notification_preferences': {
            'optional_email_receipts': owner.email_receipts,
            'mandatory_in_app_security_and_money_updates': True,
        },
    }
    if final_guard:
        final_guard()
    export.file_name = publish_file(
        'exports', export.id, 'json', json.dumps(payload, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2)
    )
    if final_guard:
        final_guard()
    export.state = 'READY'
    export.save(update_fields=['file_name', 'state'])
    record(owner, 'account.exported', str(export.id))
    return export


def cleanup_exports():
    now = db_now()
    for model in (Statement, Export):
        for item in (
            model.objects.filter(expires_at__lte=now).exclude(state='EXPIRED').order_by('expires_at')[:200]
        ):
            if item.file_name:
                try:
                    resolve_private(item.file_name).unlink()
                except DomainError:
                    pass
            model.objects.filter(id=item.id).update(state='EXPIRED', file_name='')

    # A completed file whose metadata transaction rolled back is never advertised.
    # Remove only unreferenced generated files after the one-hour abandonment window.
    references = set(Statement.objects.exclude(file_name='').values_list('file_name', flat=True))
    references.update(Export.objects.exclude(file_name='').values_list('file_name', flat=True))
    cutoff = time.time() - 3600
    for folder in ('statements', 'exports'):
        directory = private_root() / folder
        if not directory.exists():
            continue
        removed = 0
        for path in directory.iterdir():
            if (
                path.is_file()
                and path.stat().st_mtime <= cutoff
                and folder + '/' + path.name not in references
            ):
                path.unlink(missing_ok=True)
                removed += 1
                if removed >= 200:
                    break
