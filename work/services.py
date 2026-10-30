"""Durable database dispatch; business effects stay in their owning coordinators."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import CharField, Exists, OuterRef, Q, Value
from django.db.models.functions import Cast, Concat

from accounts.services import db_now

from .models import Heartbeat, Job, JobAttempt

KINDS = {'DISPATCH', 'LOOKUP', 'SCHEDULE', 'REVIEW_EXPIRY', 'RECONCILE', 'NOTIFY', 'CLEANUP', 'STATEMENT'}
DELAYS = (1, 5, 15, 60, 300)
_bootstrap_progress = ContextVar('dwmt_synchronous_demo_bootstrap', default=False)


@contextmanager
def synchronous_demo_bootstrap():
    """Local seed executes dispatch synchronously; it does not waive identity."""
    if settings.MODE != 'DEMO':
        raise RuntimeError('Synchronous synthetic bootstrap requires DEMO mode.')
    token = _bootstrap_progress.set(True)
    try:
        yield
    finally:
        _bootstrap_progress.reset(token)


def require_progress(rails=False):
    from payments.errors import DomainError

    if rails:
        from rails.adapter import check_identity

        try:
            check_identity()
        except Exception as exc:
            raise DomainError(
                'The simulator is unavailable or requires recovery.', 'UNAVAILABLE', 503
            ) from exc
    # Only the explicit seed context or the isolated domain-test settings can
    # substitute synchronous progress. Normal web settings have no such bypass.
    if _bootstrap_progress.get() or getattr(settings, 'VERIFY_SYNCHRONOUS_WORK', False):
        return True
    cutoff = db_now() - timedelta(seconds=30)
    if not Heartbeat.objects.filter(kind='worker', state='RUNNING', seen_at__gt=cutoff).exists():
        raise DomainError(
            'Background processing is unavailable. Try again after the local worker is restored.',
            'UNAVAILABLE',
            503,
        )
    if rails and not Heartbeat.objects.filter(kind='simulator', state='RUNNING', seen_at__gt=cutoff).exists():
        raise DomainError(
            'The synthetic rail processor is unavailable. Try again after it is restored.', 'UNAVAILABLE', 503
        )
    return True


def enqueue(kind, object_id, key=None, due_at=None):
    if kind not in KINDS or len(str(object_id)) > 200:
        raise ValidationError('Unsupported durable job reference.')
    if len(json.dumps({'schema_version': 1, 'kind': kind, 'object_id': str(object_id)}).encode()) > 4096:
        raise ValidationError('Job payload exceeds 4 KiB.')
    business_key = key or f'{kind}:{object_id}'
    if len(business_key) > 240:
        raise ValidationError('Job identity is too long.')
    job, _ = Job.objects.get_or_create(
        business_key=business_key,
        defaults={'kind': kind, 'object_id': str(object_id), 'due_at': due_at or db_now()},
    )
    if job.kind != kind or job.object_id != str(object_id):
        raise ValidationError('Job identity conflicts with an existing obligation.')
    return job


def eligible(now):
    return Q(attempt__lt=6) & (
        Q(state__in=['PENDING', 'RETRY'], due_at__lte=now) | Q(state='RUNNING', lease_until__lte=now)
    )


@transaction.atomic
def claim_jobs(limit=20, reconciliation_first=False):
    """Claim and release the transaction before any provider or business work."""
    if not 1 <= limit <= 20:
        raise ValueError('Worker batch must be between one and twenty.')
    now = db_now()
    exhausted = list(
        Job.objects.select_for_update(skip_locked=True)
        .filter(state='RUNNING', lease_until__lte=now, attempt__gte=6)
        .order_by('due_at', 'created_at', 'id')[:20]
    )
    for job in exhausted:
        job.state = 'NEEDS_ATTENTION'
        job.lease_until = None
        job.last_error = 'Automated burst exhausted after interrupted ownership.'
        job.save(update_fields=['state', 'lease_until', 'last_error'])
        JobAttempt.objects.create(
            job=job,
            generation=job.generation,
            outcome='NEEDS_ATTENTION',
            detail=job.last_error,
            occurred_at=now,
        )
    queryset = Job.objects.select_for_update(skip_locked=True).filter(eligible(now))
    selected = []
    if reconciliation_first:
        selected = list(
            queryset.filter(kind__in=['RECONCILE', 'LOOKUP']).order_by('due_at', 'created_at', 'id')[:1]
        )
    rest = queryset.exclude(pk__in=[j.pk for j in selected]).order_by('due_at', 'created_at', 'id')[
        : limit - len(selected)
    ]
    selected.extend(list(rest))
    for job in selected:
        job.state = 'RUNNING'
        job.generation += 1
        job.attempt += 1
        job.lease_until = now + timedelta(seconds=30)
        job.save(update_fields=['state', 'generation', 'attempt', 'lease_until'])
        JobAttempt.objects.create(job=job, generation=job.generation, outcome='CLAIMED', occurred_at=now)
    return selected


def fence_job(job_id, generation):
    """Call first inside a primary business transaction, before all domain locks."""
    if job_id is None:
        return True
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('The lease fence requires an enclosing transaction.')
    job = Job.objects.select_for_update().filter(pk=job_id).first()
    return bool(
        job
        and job.state == 'RUNNING'
        and job.generation == generation
        and job.lease_until
        and db_now() < job.lease_until
    )


def is_current(job_id, generation):
    return Job.objects.filter(
        pk=job_id, state='RUNNING', generation=generation, lease_until__gt=db_now()
    ).exists()


def extend_lease(job_id, generation):
    now = db_now()
    return (
        Job.objects.filter(pk=job_id, state='RUNNING', generation=generation, lease_until__gt=now).update(
            lease_until=now + timedelta(seconds=30)
        )
        == 1
    )


@transaction.atomic
def finish_job(job_id, generation, success=True, detail=''):
    job = Job.objects.select_for_update().filter(pk=job_id).first()
    now = db_now()
    if (
        not job
        or job.generation != generation
        or job.state != 'RUNNING'
        or not job.lease_until
        or now >= job.lease_until
    ):
        return False
    if success:
        job.state = 'DONE'
        job.completed_at = now
        job.last_error = ''
    else:
        # Never persist raw exception text, provider payloads, or secrets.
        job.last_error = str(detail)[:120]
        if job.attempt >= 6:
            job.state = 'NEEDS_ATTENTION'
        else:
            job.state = 'RETRY'
            job.due_at = now + timedelta(seconds=DELAYS[min(job.attempt - 1, 4)])
    job.lease_until = None
    job.save(update_fields=['state', 'completed_at', 'last_error', 'due_at', 'lease_until'])
    JobAttempt.objects.create(
        job=job, generation=generation, outcome=job.state, detail=job.last_error, occurred_at=now
    )
    return True


@transaction.atomic
def retry_attention_job(job_id, reason, actor=None):
    """An explicit investigated retry retains the object and provider identity."""
    from audit.services import record

    if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 1000:
        raise ValidationError('Give an investigation reason of 10 to 1,000 characters.')
    job = Job.objects.select_for_update().get(pk=job_id)
    if job.state != 'NEEDS_ATTENTION':
        raise ValidationError('Only a job needing attention may start a controlled retry.')
    old_generation = job.generation
    job.generation += 1
    job.attempt = 0
    job.state = 'PENDING'
    job.due_at = db_now()
    job.lease_until = None
    job.last_error = ''
    job.save(update_fields=['generation', 'attempt', 'state', 'due_at', 'lease_until', 'last_error'])
    JobAttempt.objects.create(
        job=job, generation=job.generation, outcome='CONTROLLED_RETRY', occurred_at=job.due_at
    )
    record(
        actor,
        'work.controlled_retry',
        str(job.pk),
        reason=reason.strip(),
        metadata={
            'previous_generation': old_generation,
            'generation': job.generation,
            'kind': job.kind,
            'object_id': job.object_id,
        },
        worker='' if actor else 'local-operator',
    )
    return job


def heartbeat(identity, kind='worker', state='RUNNING'):
    Heartbeat.objects.update_or_create(
        identity=identity, defaults={'kind': kind, 'state': state, 'seen_at': db_now()}
    )


@transaction.atomic
def observe_clock():
    """Persist clock observations separately from every later domain lock chain."""
    from audit.services import record
    from ledger.models import IntegrityGate

    gate = IntegrityGate.objects.select_for_update().filter(pk=1).first()
    if gate is None:
        return False
    now = db_now()
    reason = 'Database clock moved backward more than 60 seconds. Operator time and reconciliation checks are required.'
    if gate.last_seen_at and now < gate.last_seen_at - timedelta(seconds=60):
        if gate.is_open or gate.reason != reason:
            gate.is_open = False
            gate.reason = reason
            gate.changed_at = now
            gate.changed_by = None
            gate.version += 1
            gate.save(update_fields=['is_open', 'reason', 'changed_at', 'changed_by', 'version'])
            record(None, 'integrity.clock_anomaly', str(gate.pk), reason=reason, worker='clock-observer')
        return False
    # Preserve the high observation, so a sequence of small backward movements
    # cannot reset the anomaly origin. A restored clock never reopens the gate.
    if gate.last_seen_at is None or now > gate.last_seen_at:
        gate.last_seen_at = now
        gate.save(update_fields=['last_seen_at'])
    return gate.is_open


def discover_work():
    """Reconstruct missing responsibility from durable business state after restart."""
    from payments.models import Operation, Schedule

    now = db_now()
    counts = 0
    dispatched = Job.objects.filter(kind='DISPATCH', object_id=Cast(OuterRef('id'), output_field=CharField()))
    for operation in (
        Operation.objects.filter(state='READY')
        .annotate(has_job=Exists(dispatched))
        .filter(has_job=False)
        .only('id')[:100]
    ):
        enqueue('DISPATCH', operation.pk)
        counts += 1
    bucket = now.strftime('%Y%m%d%H')
    recovered = Job.objects.filter(
        kind='LOOKUP',
        object_id=Cast(OuterRef('id'), output_field=CharField()),
        business_key__endswith=':' + bucket,
    )
    for operation in (
        Operation.objects.filter(state__in=['SUBMITTED', 'UNKNOWN'])
        .annotate(has_job=Exists(recovered))
        .filter(has_job=False)
        .only('id')[:100]
    ):
        # An hourly distinct lookup is read only; blocked main jobs do not starve it.
        enqueue('LOOKUP', operation.pk, key=f'recovery:{operation.pk}:{bucket}')
        counts += 1
    scheduled = Job.objects.filter(kind='SCHEDULE', object_id=Cast(OuterRef('id'), output_field=CharField()))
    for schedule in (
        Schedule.objects.filter(state='SCHEDULED')
        .annotate(has_job=Exists(scheduled))
        .filter(has_job=False)
        .only('id', 'due_at')[:100]
    ):
        enqueue('SCHEDULE', schedule.pk, due_at=schedule.due_at)
        counts += 1
    minute = now.strftime('%Y%m%d%H%M')
    enqueue('REVIEW_EXPIRY', 'global', key=f'review-expiry:{minute}')
    enqueue('CLEANUP', 'global', key=f'cleanup:{now:%Y%m%d%H}')
    from oversight.models import ReconciliationRun
    from payments.reconciliation import queue_reconciliation
    from payments.reconciliation_progress import enqueue_step

    queue_reconciliation(daily_key=f'daily:{now:%Y%m%d}')
    step_key = Concat(
        Value('reconciliation:'),
        Cast(OuterRef('id'), output_field=CharField()),
        Value(':'),
        Cast(OuterRef('step'), output_field=CharField()),
    )
    duties = Job.objects.filter(kind='RECONCILE', business_key=step_key)
    for run in (
        ReconciliationRun.objects.filter(state__in=['PENDING', 'RUNNING'])
        .annotate(has_job=Exists(duties))
        .filter(has_job=False)
        .order_by('created_at', 'id')[:100]
    ):
        enqueue_step(run)
    return counts
