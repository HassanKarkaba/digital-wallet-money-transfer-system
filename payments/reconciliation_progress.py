"""Durable reconciliation keysets; accounting effects remain in the coordinator."""

import hashlib
import json
import time
from contextlib import ExitStack, contextmanager
from datetime import timedelta

from django.db import DatabaseError, connection, connections, transaction
from django.db.models import Max

from accounts.services import assert_eligible, db_now, lock_users
from audit.services import record
from ledger.models import IntegrityGate
from oversight.models import Finding, ReconciliationRun
from payments.errors import DomainError
from payments.models import Operation

NONSEVERE = {
    'STALE_UNKNOWN',
    'PROVIDER_UNAVAILABLE',
    'RUN_INCOMPLETE',
    'RECONCILIATION_BLOCKED',
    'AUTHORITY_CHANGED',
}


class BatchBudget(TimeoutError):
    pass


@contextmanager
def sql_budget(deadline):
    """Each SQL call receives the remaining batch budget, capped at runtime5s."""

    def bounded(execute, sql, params, many, context):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BatchBudget('Reconciliation step elapsed budget exhausted.')
        milliseconds = max(1, min(5000, int(remaining * 1000)))
        current = context['connection']
        context['cursor'].cursor.execute(
            f'SET {"LOCAL " if current.in_atomic_block else ""}statement_timeout = {milliseconds}'
        )
        return execute(sql, params, many, context)

    try:
        with ExitStack() as stack:
            for alias in ('default', 'simulator'):
                stack.enter_context(connections[alias].execute_wrapper(bounded))
            yield
    finally:
        for alias in ('default', 'simulator'):
            current = connections[alias]
            if current.connection is not None and not current.needs_rollback:
                try:
                    with current.connection.cursor() as cursor:
                        cursor.execute('SET statement_timeout = 5000')
                except Exception:
                    current.close()


def enqueue_step(run):
    from work.services import enqueue

    return enqueue('RECONCILE', run.pk, key=f'reconciliation:{run.pk}:{run.step}')


def finance_actor(actor, expected_version=None):
    if actor is None:
        return None
    current = lock_users([actor.pk])[0]
    from payments.workflows import _current_actor

    _current_actor(actor, current)
    assert_eligible(current, staff_role='FINANCE')
    if expected_version is not None and current.authority_version != expected_version:
        raise DomainError('The queued reconciliation authority changed.', 'DENIED', 403)
    return current


@transaction.atomic
def queue_reconciliation(actor=None, apply=True, limit=1000000, daily_key=None):
    """No provider scan or financial mutation runs in the requesting HTTP call."""
    if type(limit) is not int or not 1 <= limit <= 1000000 or type(apply) is not bool:
        raise DomainError('Use a reconciliation bound from 1 to 1,000,000 records.')
    if daily_key is not None and (not isinstance(daily_key, str) or not 1 <= len(daily_key) <= 80):
        raise DomainError('Invalid reconciliation repetition identity.')
    actor = finance_actor(actor)
    now = db_now()
    values = {
        'actor': actor,
        'actor_authority_version': getattr(actor, 'authority_version', None),
        'created_at': now,
        'cutoff_at': now,
        'state': 'PENDING',
        'phase': 'INIT',
        'apply_observations': apply,
        'max_records': limit,
        'summary': {
            'findings': 0,
            'scope': 'Fixed admission/provider creation cutoff; mutable outcomes observed at recorded step times.',
        },
    }
    if daily_key:
        run, created = ReconciliationRun.objects.get_or_create(daily_key=daily_key, defaults=values)
    else:
        run, created = ReconciliationRun.objects.create(**values), True
    if created:
        record(
            actor,
            'reconciliation.queued',
            str(run.pk),
            purpose='Bounded comparison of primary accounting and independent provider records',
            metadata={'cutoff_at': now.isoformat(), 'max_records': limit},
        )
    if run.state in {'PENDING', 'RUNNING'}:
        enqueue_step(run)
    return run


def owned_step(run_id, job_id=None, generation=None):
    from work.models import Job
    from work.services import fence_job

    with transaction.atomic():
        if job_id and not fence_job(job_id, generation):
            raise DomainError('Reconciliation worker lease is stale.', 'STALE', 409)
        preliminary = ReconciliationRun.objects.select_related('actor').get(pk=run_id)
        if preliminary.actor_id:
            finance_actor(preliminary.actor, preliminary.actor_authority_version)
        run = ReconciliationRun.objects.select_for_update().get(pk=run_id)
        if run.state in {'COMPLETED', 'FAILED'}:
            return None
        if job_id and Job.objects.get(pk=job_id).business_key != f'reconciliation:{run.pk}:{run.step}':
            return None
        if run.state == 'PENDING':
            run.state = 'RUNNING'
            run.save(update_fields=['state'])
            record(
                run.actor,
                'reconciliation.started',
                str(run.pk),
                purpose='Durable bounded operational delegation',
            )
        return run


def checkpoint(run, issues=(), fields=None, checked=0, job_id=None, generation=None, check_authority=True):
    """The first serious finding closes admission with its durable checkpoint."""
    from work.services import fence_job

    severe = any(issue['classification'] not in NONSEVERE for issue in issues)
    with transaction.atomic():
        if job_id and not fence_job(job_id, generation):
            raise DomainError('Reconciliation worker lease is stale.', 'STALE', 409)
        gate = IntegrityGate.objects.select_for_update().get(pk=1) if severe else None
        if run.actor_id and check_authority:
            finance_actor(run.actor, run.actor_authority_version)
        current = ReconciliationRun.objects.select_for_update().get(pk=run.pk)
        if current.step != run.step or current.state in {'COMPLETED', 'FAILED'}:
            return current
        for issue in issues:
            identity = {
                key: issue.get(key, '')
                for key in ('classification', 'provider_key', 'operation_id', 'reference')
            }
            evidence_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            operation_id = issue.get('operation_id')
            if not Operation.objects.filter(pk=operation_id).exists():
                operation_id = None
            Finding.objects.get_or_create(
                run=current,
                classification=issue['classification'],
                details__observation_key=evidence_key,
                defaults={
                    'operation_id': operation_id,
                    'provider_key': issue.get('provider_key', ''),
                    'details': {
                        **issue,
                        'observation_key': evidence_key,
                        'observed_at': db_now().isoformat(),
                    },
                    'created_at': db_now(),
                },
            )
        if gate:
            gate.is_open = False
            gate.version += 1
            gate.reason = 'Reconciliation detected an accounting or independent provider discrepancy.'
            gate.changed_at = db_now()
            gate.changed_by = current.actor
            gate.save(update_fields=['is_open', 'version', 'reason', 'changed_at', 'changed_by'])
            record(
                current.actor,
                'integrity.closed',
                'environment',
                reason=gate.reason,
                metadata={'run': str(current.pk)},
            )
        for name, value in (fields or {}).items():
            setattr(current, name, value)
        current.checked += checked
        current.step += 1
        current.failure_code = '' if current.state != 'FAILED' else current.failure_code
        current.summary = {
            **current.summary,
            'findings': current.findings.count(),
            'last_observed_at': db_now().isoformat(),
            'phase': current.phase,
            'cursor': current.cursor,
        }
        if current.state == 'COMPLETED':
            current.clean = not current.findings.exists()
            current.completed_at = db_now()
            record(
                current.actor,
                'reconciliation.completed',
                str(current.pk),
                metadata={
                    'checked': current.checked,
                    'findings': current.summary['findings'],
                    'clean': current.clean,
                },
            )
        elif current.state == 'FAILED':
            current.clean = False
            current.completed_at = db_now()
            record(
                current.actor,
                'reconciliation.failed',
                str(current.pk),
                outcome='FAILED',
                reason=current.failure_code,
            )
        else:
            record(
                current.actor,
                'reconciliation.progress',
                str(current.pk),
                metadata={
                    'step': current.step,
                    'phase': current.phase,
                    'checked': current.checked,
                    'findings': current.summary['findings'],
                },
            )
        current.save()
        if current.state in {'PENDING', 'RUNNING'}:
            enqueue_step(current)
        return current


def inspect_provider(row, run, job_id, generation):
    from payments.workflows import apply_provider_observation
    from rails.adapter import observation

    op = Operation.objects.select_related('destination').filter(provider_key=row.provider_key).first()
    if op is None:
        return [
            {
                'classification': 'MISSING_LOCAL_OPERATION',
                'provider_key': row.provider_key,
                'operation_id': str(row.operation_id),
            }
        ]
    if not op.destination_id or (row.direction, row.amount_cents, row.currency, row.token_id) != (
        op.kind,
        op.amount_cents,
        op.currency,
        op.destination.token_id,
    ):
        return [
            {
                'classification': 'MISMATCHED_EFFECT',
                'operation_id': str(op.pk),
                'provider_key': row.provider_key,
            }
        ]
    if row.state not in {'SUCCESS', 'DECLINED'}:
        return []
    desired = 'COMPLETED' if row.state == 'SUCCESS' else 'FAILED'
    if op.state in {'SUBMITTED', 'UNKNOWN'} and run.apply_observations:
        apply_provider_observation(op.pk, observation(row), job_id=job_id, generation=generation)
        op.refresh_from_db()
    if op.state != desired:
        return [
            {
                'classification': 'CONTRADICTORY_TERMINAL'
                if op.state in {'COMPLETED', 'FAILED', 'CANCELLED'}
                else 'MISSING_LOCAL_SETTLEMENT',
                'operation_id': str(op.pk),
                'provider_key': row.provider_key,
            }
        ]
    return []


def advance_reconciliation(run_id, job_id=None, generation=None, batch=200):
    started = time.monotonic()
    with sql_budget(started + 19):
        return advance_step(run_id, job_id, generation, batch, started)


def advance_step(run_id, job_id=None, generation=None, batch=200, started=None):
    from payments.reconciliation import verify_ledger
    from rails.adapter import check_identity
    from simulator.models import ProviderEffect

    if type(batch) is not int or not 1 <= batch <= 200:
        raise DomainError('Reconciliation batches contain 1 to 200 records.')
    if connection.in_atomic_block:
        raise RuntimeError('Reconciliation must run outside an existing primary transaction.')
    try:
        run = owned_step(run_id, job_id, generation)
    except ValueError as error:
        if getattr(error, 'code', '') != 'DENIED':
            raise
        run = ReconciliationRun.objects.select_related('actor').get(pk=run_id)
        return checkpoint(
            run,
            [
                {
                    'classification': 'AUTHORITY_CHANGED',
                    'reason': 'The queued Finance authority was revoked or changed before this step.',
                }
            ],
            {'state': 'FAILED', 'failure_code': 'AUTHORITY_CHANGED'},
            job_id=job_id,
            generation=generation,
            check_authority=False,
        )
    if run is None:
        return ReconciliationRun.objects.get(pk=run_id)
    cutoff = run.cutoff_at or run.created_at
    check_identity()
    if run.phase == 'INIT':
        provider_max = (
            ProviderEffect.objects.using('simulator')
            .filter(created_at__lte=cutoff)
            .aggregate(value=Max('provider_key'))['value']
            or ''
        )
        local_max = (
            Operation.objects.filter(kind__in=['FUNDING', 'WITHDRAWAL'], created_at__lte=cutoff).aggregate(
                value=Max('provider_key')
            )['value']
            or ''
        )
        return checkpoint(
            run,
            fields={
                'cutoff_at': cutoff,
                'provider_high_water': provider_max,
                'local_high_water': local_max,
                'phase': 'PROVIDER',
                'cursor': '',
            },
            job_id=job_id,
            generation=generation,
        )
    if run.phase == 'LEDGER':
        return checkpoint(
            run,
            verify_ledger(),
            fields={'phase': 'DONE', 'state': 'COMPLETED', 'cursor': ''},
            job_id=job_id,
            generation=generation,
        )
    if run.phase not in {'PROVIDER', 'LOCAL'}:
        return checkpoint(
            run,
            [{'classification': 'RUN_INCOMPLETE', 'reason': 'Unknown persisted reconciliation phase.'}],
            fields={'state': 'FAILED', 'failure_code': 'UNSUPPORTED_PHASE'},
            job_id=job_id,
            generation=generation,
        )
    remaining = run.max_records - run.checked
    if remaining <= 0:
        return checkpoint(
            run,
            [
                {
                    'classification': 'RUN_INCOMPLETE',
                    'reason': 'The selected total observation bound was exhausted.',
                }
            ],
            fields={'state': 'FAILED', 'failure_code': 'OBSERVATION_LIMIT'},
            job_id=job_id,
            generation=generation,
        )
    count = min(batch, remaining)
    if run.phase == 'PROVIDER':
        rows = (
            list(
                ProviderEffect.objects.using('simulator')
                .filter(
                    created_at__lte=cutoff,
                    provider_key__gt=run.cursor,
                    provider_key__lte=run.provider_high_water,
                )
                .order_by('provider_key')[: count + 1]
            )
            if run.provider_high_water
            else []
        )
    else:
        rows = (
            list(
                Operation.objects.select_related('destination')
                .filter(
                    kind__in=['FUNDING', 'WITHDRAWAL'],
                    created_at__lte=cutoff,
                    provider_key__gt=run.cursor,
                    provider_key__lte=run.local_high_water,
                )
                .order_by('provider_key')[: count + 1]
            )
            if run.local_high_water
            else []
        )
    issues, processed, cursor = [], 0, run.cursor
    deadline = (started or time.monotonic()) + 14
    provider_keys = set()
    if run.phase == 'LOCAL':
        provider_keys = set(
            ProviderEffect.objects.using('simulator')
            .filter(provider_key__in=[row.provider_key for row in rows[:count]])
            .values_list('provider_key', flat=True)
        )
    for row in rows[:count]:
        if processed and time.monotonic() >= deadline:
            break
        try:
            with sql_budget(deadline):
                if run.phase == 'PROVIDER':
                    issues.extend(inspect_provider(row, run, job_id, generation))
                elif row.provider_key not in provider_keys and row.state in {'COMPLETED', 'FAILED'}:
                    issues.append(
                        {
                            'classification': 'MISSING_PROVIDER_EFFECT',
                            'operation_id': str(row.pk),
                            'provider_key': row.provider_key,
                        }
                    )
                elif row.state == 'UNKNOWN' and cutoff - row.updated_at > timedelta(hours=1):
                    issues.append(
                        {
                            'classification': 'STALE_UNKNOWN',
                            'operation_id': str(row.pk),
                            'provider_key': row.provider_key,
                        }
                    )
        except (DatabaseError, BatchBudget):
            issues.append(
                {
                    'classification': 'RECONCILIATION_BLOCKED',
                    'operation_id': str(row.pk) if run.phase == 'LOCAL' else str(row.operation_id),
                    'provider_key': row.provider_key,
                    'reason': 'This record exceeded bounded resources. Its retained identity and existing obligations require a later recheck.',
                }
            )
        processed += 1
        cursor = row.provider_key
        if issues and issues[-1]['classification'] not in NONSEVERE:
            break
    exhausted = processed == len(rows) and len(rows) <= count
    fields = {'cursor': cursor}
    if exhausted:
        fields.update(phase='LOCAL' if run.phase == 'PROVIDER' else 'LEDGER', cursor='')
    return checkpoint(run, issues, fields, processed, job_id, generation)


def reconcile_inline(actor=None, apply=True, limit=1000000):
    run = queue_reconciliation(actor, apply, limit)
    while run.state in {'PENDING', 'RUNNING'}:
        try:
            run = advance_reconciliation(run.pk)
        except Exception as error:
            if isinstance(error, DomainError) and error.code == 'STALE':
                run.refresh_from_db()
                continue
            run.refresh_from_db()
            run = checkpoint(
                run,
                [
                    {
                        'classification': 'PROVIDER_UNAVAILABLE',
                        'reason': 'A required store could not be inspected within the bounded operational step.',
                    }
                ],
                {'state': 'FAILED', 'failure_code': type(error).__name__[:80]},
            )
    return run
