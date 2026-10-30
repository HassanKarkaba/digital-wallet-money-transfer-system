import os
import signal
import threading
import time
import uuid

from django.core.management.base import BaseCommand
from django.db import close_old_connections, connections

from work.services import claim_jobs, discover_work, extend_lease, finish_job, heartbeat, observe_clock


class RetryWork(Exception):
    pass


def perform(job):
    if job.schema_version != 1:
        raise RetryWork('Unsupported job schema.')
    if job.kind == 'DISPATCH':
        from payments.workflows import dispatch_operation

        dispatch_operation(job.object_id, job_id=job.pk, generation=job.generation)
        from payments.models import Operation

        if Operation.objects.filter(pk=job.object_id, state__in=['SUBMITTED', 'UNKNOWN']).exists():
            raise RetryWork('Awaiting authoritative provider result.')
    elif job.kind == 'LOOKUP':
        from payments.models import Operation
        from payments.workflows import apply_provider_observation
        from rails.adapter import lookup

        operation = Operation.objects.filter(pk=job.object_id).first()
        if operation is None:
            raise RetryWork('Missing operation requires reconciliation.')
        result = lookup(operation.provider_key)
        if result is None:
            raise RetryWork('No authoritative provider record; investigation retained.')
        apply_provider_observation(operation.pk, result, job_id=job.pk, generation=job.generation)
        if Operation.objects.filter(pk=operation.pk, state__in=['SUBMITTED', 'UNKNOWN']).exists():
            raise RetryWork('Provider result remains pending.')
    elif job.kind == 'SCHEDULE':
        from payments.workflows import execute_schedule

        result = execute_schedule(job.object_id, job_id=job.pk, generation=job.generation)
        if result.state == 'SCHEDULED':
            raise RetryWork('Schedule is not yet eligible.')
    elif job.kind == 'REVIEW_EXPIRY':
        from payments.workflows import expire_reviews

        expire_reviews()
    elif job.kind == 'RECONCILE':
        from payments.reconciliation import advance_reconciliation, queue_reconciliation

        if job.object_id == 'global':
            from accounts.services import db_now

            queue_reconciliation(daily_key=f'daily:{db_now():%Y%m%d}')
        else:
            advance_reconciliation(job.object_id, job_id=job.pk, generation=job.generation)
    elif job.kind == 'NOTIFY':
        from notifications.services import dispatch

        dispatch(job.object_id)
    elif job.kind == 'STATEMENT':
        from reports.services import generate_pending_statement

        generate_pending_statement(job.object_id, job_id=job.pk, generation=job.generation)
    elif job.kind == 'CLEANUP':
        from notifications.services import cleanup_private_files

        cleanup_private_files()
    else:
        raise RetryWork('Unsupported job kind.')


def lease_heartbeat(jobs, stop_event, identity):
    close_old_connections()
    try:
        while not stop_event.wait(10):
            heartbeat(identity)
            for job in jobs:
                extend_lease(job.pk, job.generation)
    finally:
        connections.close_all()


class Command(BaseCommand):
    help = 'Process bounded durable jobs. SIGTERM stops claims without releasing financial reservations.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--limit', type=int, default=20)
        parser.add_argument('--max-batches', type=int)

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 20:
            raise ValueError('--limit must be between 1 and 20.')
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        identity = f'worker:{os.getpid()}:{uuid.uuid4()}'
        batches = 0
        last_discovery = 0.0
        try:
            while not stopping.is_set():
                close_old_connections()
                heartbeat(identity)
                observe_clock()
                if time.monotonic() - last_discovery >= 10:
                    discover_work()
                    last_discovery = time.monotonic()
                from accounts.services import process_security_mail

                process_security_mail(limit=20)
                jobs = claim_jobs(options['limit'], reconciliation_first=(batches % 5 == 4))
                if jobs:
                    batches += 1
                stop_lease = threading.Event()
                thread = threading.Thread(
                    target=lease_heartbeat, args=(jobs, stop_lease, identity), daemon=True
                )
                thread.start()
                for job in jobs:
                    if stopping.is_set():
                        break
                    # Unstarted claims safely become eligible again when their lease expires.
                    started = time.monotonic()
                    try:
                        perform(job)
                        budget = 30 if job.kind == 'STATEMENT' else 20
                        if time.monotonic() - started > budget:
                            finish_job(
                                job.pk,
                                job.generation,
                                success=False,
                                detail='Worker elapsed budget exceeded; inspect current authoritative state.',
                            )
                        else:
                            finish_job(job.pk, job.generation)
                    except Exception as exc:
                        # Type only: never log secrets from exception text or provider payloads.
                        detail = (
                            'Awaiting authoritative result.'
                            if isinstance(exc, RetryWork)
                            else type(exc).__name__
                        )
                        finish_job(job.pk, job.generation, success=False, detail=detail)
                        self.stderr.write(f'job {job.pk}: {detail}')
                    heartbeat(identity)
                stop_lease.set()
                thread.join(timeout=1)
                if options['once'] or (options['max_batches'] and batches >= options['max_batches']):
                    break
                if not jobs:
                    stopping.wait(1)
        finally:
            try:
                heartbeat(identity, state='STOPPED')
            finally:
                connections.close_all()
        self.stdout.write(f'Worker completed {batches} nonempty batches.')
