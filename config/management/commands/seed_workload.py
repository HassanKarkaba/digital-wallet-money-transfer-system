"""Guarded acceptance fixture. Proof substitution here is not MFA evidence."""

import json
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pyotp
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connections, transaction

from accounts.models import User
from accounts.services import db_now, encrypt_secret
from ledger.posting import open_wallet
from payments.models import Intent, Operation, ReplayKey
from payments.reconciliation import verify_ledger
from payments.workflows import confirm_intent, create_intent, dispatch_operation
from rails.models import Destination
from work.models import Job, JobAttempt
from work.services import finish_job, synchronous_demo_bootstrap


def guard_test_stores():
    if (
        settings.MODE != 'DEMO'
        or settings.DATABASES['default']['NAME'] != 'wallet_test'
        or settings.DATABASES['simulator']['NAME'] != 'simulator_test'
    ):
        raise CommandError('Acceptance seeding refuses ordinary development databases.')


def grant_fixture(intent):
    """Issue an exact bound grant as an explicit load-fixture substitution only."""
    with transaction.atomic():
        User.objects.select_for_update().get(pk=intent.owner_id)
        item = Intent.objects.select_for_update().get(pk=intent.pk)
        item.verified_at = db_now()
        item.grant_id = uuid.uuid4()
        item.save(update_fields=['verified_at', 'grant_id'])


def fixture_payment(actor, kind, instruction, action_key):
    # Repeated runs recover the accepted identity rather than creating fresh money.
    accepted = (
        ReplayKey.objects.filter(owner=actor, family=kind, action_key=action_key)
        .select_related('operation')
        .first()
    )
    if accepted and accepted.operation_id:
        return accepted.operation
    intent = create_intent(actor, kind, instruction)
    grant_fixture(intent)
    return confirm_intent(actor, intent.pk, action_key)


def drain_fixture_job(business_key, handler):
    """Complete selected fixture outbox work with the ordinary generation fence."""
    with transaction.atomic():
        job = (
            Job.objects.select_for_update()
            .filter(business_key=business_key, state__in=['PENDING', 'RETRY'])
            .first()
        )
        if job is None:
            return  # A live worker may already own or have completed this job.
        now = db_now()
        if job.due_at > now or job.attempt >= 6:
            return
        job.state = 'RUNNING'
        job.generation += 1
        job.attempt += 1
        job.lease_until = now + timedelta(seconds=30)
        job.save(update_fields=['state', 'generation', 'attempt', 'lease_until'])
        JobAttempt.objects.create(
            job=job,
            generation=job.generation,
            outcome='CLAIMED',
            detail='Synchronous isolated acceptance fixture.',
            occurred_at=now,
        )
    try:
        handler(job)
    except Exception as exc:
        finish_job(job.pk, job.generation, success=False, detail=type(exc).__name__)
        raise
    finish_job(job.pk, job.generation)


def drain_fixture_outbox(operation):
    from notifications.models import Notification
    from notifications.services import dispatch

    participants = {operation.actor_id}
    if operation.sender_id:
        participants.add(operation.sender.owner_id)
    if operation.recipient_id:
        participants.add(operation.recipient.owner_id)
    for notification_id in Notification.objects.filter(
        recipient_id__in=participants, event_key__startswith=f'operation:{operation.pk}:'
    ).values_list('id', flat=True):
        drain_fixture_job('NOTIFY:' + str(notification_id), lambda job: dispatch(job.object_id))
    if operation.kind == 'FUNDING' and operation.state == 'COMPLETED':
        drain_fixture_job(
            'dispatch:' + str(operation.pk),
            lambda job: dispatch_operation(operation.pk, job_id=job.pk, generation=job.generation),
        )


class Command(BaseCommand):
    help = 'Build the isolated 10,000-customer/100,000-operation acceptance dataset through the normal coordinators and SQL posting boundary.'

    def add_arguments(self, parser):
        parser.add_argument('--customers', type=int, default=10000)
        parser.add_argument('--operations', type=int, default=100000)
        parser.add_argument('--workers', type=int, default=8)

    def handle(self, *args, **options):
        guard_test_stores()
        customers, operations, workers = options['customers'], options['operations'], options['workers']
        if not 2 <= customers <= 10000 or not customers <= operations <= 200000 or not 1 <= workers <= 16:
            raise CommandError('Use 2..10000 customers, customers..200000 operations, and 1..16 workers.')
        namespace = uuid.UUID(str(settings.ENVIRONMENT_ID))
        started = time.monotonic()
        output = Path(settings.PRIVATE_ROOT) / 'workload'
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        password_hash = make_password(secrets.token_urlsafe(36))
        now = db_now()
        rows = []
        for index in range(customers):
            handle = f'load_{index:05d}'
            rows.append(
                User(
                    id=uuid.uuid5(namespace, 'workload-user:' + handle),
                    handle=handle,
                    email=f'{handle}@dwmt.test',
                    display_name=f'Synthetic load customer {index + 1}',
                    password=password_hash,
                    verified_at=now,
                    mfa_confirmed=True,
                    mfa_secret=encrypt_secret(pyotp.random_base32()),
                    email_receipts=False,
                )
            )
            if len(rows) == 500 or index == customers - 1:
                User.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
                rows = []
        users = list(
            User.objects.filter(handle__in=[f'load_{i:05d}' for i in range(customers)]).order_by('handle')
        )
        if len(users) != customers:
            raise CommandError('Synthetic user identity reconstruction failed.')
        expected_ids = [uuid.uuid5(namespace, 'workload-user:' + f'load_{i:05d}') for i in range(customers)]
        if [user.pk for user in users] != expected_ids:
            raise CommandError('The requested fixture handle already belongs to another identity.')
        progress_lock = threading.Lock()
        progress = {'funding': 0, 'transfer': 0}

        def noted(kind):
            with progress_lock:
                progress[kind] += 1
                count = progress[kind]
                if options['verbosity'] and count % 1000 == 0:
                    self.stdout.write(
                        f'Acceptance seed {kind}: {count} protected operations checked.', ending='\n'
                    )
                    self.stdout.flush()

        def fund_chunk(indices):
            close_old_connections()
            try:
                with synchronous_demo_bootstrap():
                    for index in indices:
                        user = users[index]
                        open_wallet(user)
                        # The synthetic token and its elapsed activation are fixture
                        # inputs, after the separate browser case exercises linking.
                        destination, _ = Destination.objects.get_or_create(
                            pk=uuid.uuid5(namespace, f'workload-token:{user.pk}'),
                            defaults={
                                'owner': user,
                                'label': 'Acceptance synthetic token',
                                'activation_at': db_now() - timedelta(seconds=1),
                            },
                        )
                        if destination.owner_id != user.pk or destination.scenario != 'SUCCESS':
                            raise CommandError('Synthetic token identity or scenario changed.')
                        key = uuid.uuid5(namespace, f'workload-funding:{user.pk}')
                        operation = fixture_payment(
                            user,
                            'FUNDING',
                            {
                                'destination_id': str(destination.pk),
                                'amount': '750.00',
                                'note': 'Acceptance workload funding',
                            },
                            key,
                        )
                        if operation.state not in ['COMPLETED', 'FAILED', 'CANCELLED']:
                            operation = dispatch_operation(operation.pk)
                        if operation.state != 'COMPLETED':
                            raise CommandError(
                                f'Funding {operation.pk} needs investigation; the original key is retained.'
                            )
                        drain_fixture_outbox(operation)
                        noted('funding')
            finally:
                connections.close_all()

        remaining = operations - customers
        quotient, remainder = divmod(remaining, customers)

        def transfer_chunk(indices):
            close_old_connections()
            try:
                with synchronous_demo_bootstrap():
                    for index in indices:
                        actor = users[index]
                        recipient = users[(index + 1) % customers]
                        for number in range(quotient + (1 if index < remainder else 0)):
                            key = uuid.uuid5(namespace, f'workload-transfer:{actor.pk}:{number}')
                            operation = fixture_payment(
                                actor,
                                'TRANSFER',
                                {
                                    'recipient_handle': recipient.handle,
                                    'amount': '0.01',
                                    'note': 'Acceptance workload internal transfer',
                                },
                                key,
                            )
                            if operation.state != 'COMPLETED':
                                raise CommandError(f'Transfer {operation.pk} did not complete.')
                            drain_fixture_outbox(operation)
                            noted('transfer')
            finally:
                connections.close_all()

        chunks = [list(range(worker, customers, workers)) for worker in range(workers)]
        for runner in (fund_chunk, transfer_chunk):
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(runner, indices) for indices in chunks]
                for future in futures:
                    future.result()
        issues = verify_ledger()
        owned = Operation.objects.filter(actor__handle__startswith='load_')
        summary = {
            'target_customers': customers,
            'target_operations': operations,
            'fixture_customers': len(users),
            'fixture_operations': owned.count(),
            'dataset_customers': User.objects.count(),
            'dataset_operations': Operation.objects.count(),
            'dataset_journal_entries': __import__('ledger.models', fromlist=['Entry']).Entry.objects.count(),
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'workers': workers,
            'invariant_issues': issues,
            'environment_id': str(settings.ENVIRONMENT_ID),
            'configuration': 'wallet_test + simulator_test; DEMO; DWMT-P1',
            'substitutions': [
                'Synthetic verified accounts and elapsed destination activation.',
                'Bound per-intent proof grants are a load-fixture substitution; this is not authentication/MFA evidence.',
                'Seed funding dispatch and its optional-mail outbox are processed synchronously through the normal handlers with generation fences. Measured workload uses live worker/simulator and real bound TOTP.',
            ],
        }
        path = output / 'seed.json'
        path.write_text(json.dumps(summary, indent=2))
        path.chmod(0o600)
        if issues or owned.count() < operations:
            raise CommandError(
                'Acceptance dataset failed its count or accounting checks. Private summary retained.'
            )
        self.stdout.write(
            f'Acceptance dataset ready: {customers} fixture customers; {owned.count()} fixture operations; zero invariant issues. Private summary retained.'
        )
