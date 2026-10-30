from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase

from accounts.services import db_now
from testsupport import WalletFixtures
from work.models import Job
from work.services import claim_jobs, enqueue, fence_job, finish_job


class DurableJobTests(TransactionTestCase):
    def test_blocked_and_terminal_prefix_does_not_prevent_eligible_work(self):
        past = db_now() - timedelta(days=1)
        for i in range(25):
            enqueue('LOOKUP', f'blocked-{i}', key=f'blocked-{i}', due_at=past)
        Job.objects.update(state='NEEDS_ATTENTION')
        wanted = enqueue('LOOKUP', 'eligible', due_at=past)
        claimed = claim_jobs()
        self.assertEqual([job.pk for job in claimed], [wanted.pk])

    def test_expired_generation_cannot_record_completion(self):
        item = enqueue('LOOKUP', 'test')
        first = claim_jobs()[0]
        Job.objects.filter(pk=item.pk).update(lease_until=db_now() - timedelta(seconds=1))
        second = claim_jobs()[0]
        self.assertGreater(second.generation, first.generation)
        with transaction.atomic():
            self.assertFalse(fence_job(first.pk, first.generation))
            self.assertTrue(fence_job(second.pk, second.generation))
        self.assertFalse(finish_job(first.pk, first.generation))
        self.assertTrue(finish_job(second.pk, second.generation))
        item.refresh_from_db()
        self.assertEqual(item.state, 'DONE')

    def test_unique_job_identity_cannot_change_instruction(self):
        enqueue('LOOKUP', 'one', key='fixed')
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            enqueue('LOOKUP', 'two', key='fixed')

    def test_retry_burst_is_bounded_and_obligation_remains(self):
        item = enqueue('LOOKUP', 'test')
        for _ in range(6):
            Job.objects.filter(pk=item.pk).update(due_at=db_now() - timedelta(seconds=1))
            lease = claim_jobs()[0]
            finish_job(lease.pk, lease.generation, success=False, detail='Provider unavailable.')
        item.refresh_from_db()
        self.assertEqual(item.state, 'NEEDS_ATTENTION')
        self.assertEqual(claim_jobs(), [])

    def test_backward_clock_closes_gate_and_normal_observation_does_not_reopen_it(self):

        from django.conf import settings

        from ledger.models import IntegrityGate
        from work.services import observe_clock

        now = db_now()
        gate, _ = IntegrityGate.objects.update_or_create(
            pk=1,
            defaults={
                'environment_id': settings.ENVIRONMENT_ID,
                'recovery_generation': settings.RECOVERY_GENERATION,
                'is_open': True,
                'last_seen_at': now + timedelta(seconds=60),
            },
        )
        with patch('work.services.db_now', return_value=now):
            self.assertTrue(observe_clock())
        gate.last_seen_at = now + timedelta(seconds=61)
        gate.save(update_fields=['last_seen_at'])
        with patch('work.services.db_now', return_value=now):
            self.assertFalse(observe_clock())
        gate.refresh_from_db()
        self.assertFalse(gate.is_open)
        self.assertIn('clock moved backward', gate.reason)
        with patch('work.services.db_now', return_value=now + timedelta(minutes=2)):
            self.assertFalse(observe_clock())
        gate.refresh_from_db()
        self.assertFalse(gate.is_open)
        self.assertEqual(gate.last_seen_at, now + timedelta(minutes=2))

    def test_capability_loss_is_not_hidden_by_healthy_primary_database(self):

        from django.test import override_settings

        from payments.errors import DomainError
        from work.models import Heartbeat
        from work.services import require_progress

        with override_settings(VERIFY_SYNCHRONOUS_WORK=False):
            with self.assertRaises(DomainError):
                require_progress()
            Heartbeat.objects.create(identity='real-worker', kind='worker', state='RUNNING', seen_at=db_now())
            self.assertTrue(require_progress())
            with patch('rails.adapter.check_identity'):
                with self.assertRaises(DomainError):
                    require_progress(rails=True)
                Heartbeat.objects.create(
                    identity='stopped-simulator', kind='simulator', state='STOPPED', seen_at=db_now()
                )
                with self.assertRaises(DomainError):
                    require_progress(rails=True)
                Heartbeat.objects.create(
                    identity='stale-simulator',
                    kind='simulator',
                    state='RUNNING',
                    seen_at=db_now() - timedelta(seconds=31),
                )
                with self.assertRaises(DomainError):
                    require_progress(rails=True)
                Heartbeat.objects.create(
                    identity='live-simulator', kind='simulator', state='RUNNING', seen_at=db_now()
                )
                self.assertTrue(require_progress(rails=True))

    def test_explicit_synchronous_seed_waives_worker_but_not_provider_identity(self):

        from django.test import override_settings

        from payments.errors import DomainError
        from work.services import require_progress, synchronous_demo_bootstrap

        with override_settings(VERIFY_SYNCHRONOUS_WORK=False):
            with synchronous_demo_bootstrap():
                self.assertTrue(require_progress())
                with patch('rails.adapter.check_identity', side_effect=OSError):
                    with self.assertRaises(DomainError):
                        require_progress(rails=True)
            with self.assertRaises(DomainError):
                require_progress()


class EarlyScheduleClaimTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def test_early_claim_retries_instead_of_losing_the_schedule_obligation(self):
        from work.management.commands.run_worker import RetryWork, perform

        self.setup_wallet_environment()
        actor = self.customer('early_sender')
        self.customer('early_recipient')
        due = db_now() + timedelta(minutes=2)
        schedule = self.admit(
            actor,
            'SCHEDULE',
            {
                'recipient_handle': 'early_recipient',
                'amount': '0.01',
                'due_at': due.isoformat(),
                'timezone': 'UTC',
                'utc_offset': '+00:00',
            },
        )
        Job.objects.filter(kind='SCHEDULE', object_id=str(schedule.pk)).update(
            due_at=db_now() - timedelta(seconds=1)
        )
        job = next(item for item in claim_jobs() if item.kind == 'SCHEDULE')
        with self.assertRaises(RetryWork):
            perform(job)
        finish_job(job.pk, job.generation, success=False, detail='Schedule is not yet eligible.')
        job.refresh_from_db()
        schedule.refresh_from_db()
        self.assertEqual((job.state, schedule.state), ('RETRY', 'SCHEDULED'))
        self.assertIsNone(schedule.operation_id)
