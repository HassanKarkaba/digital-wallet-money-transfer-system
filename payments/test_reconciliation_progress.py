import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import TransactionTestCase

from accounts.models import Role, User
from accounts.services import db_now
from ledger.models import IntegrityGate
from oversight.models import Finding
from payments.errors import DomainError
from payments.reconciliation import advance_reconciliation, queue_reconciliation
from rails.adapter import create_or_get
from rails.models import Destination
from testsupport import WalletFixtures
from work.models import Job
from work.services import claim_jobs


class ReconciliationProgressTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        self.setup_wallet_environment()
        self.owner = self.customer('recon_owner')
        self.finance = self.customer('recon_finance', ('FINANCE',))

    def finish(self, run, batch=1):
        for _ in range(30):
            run = advance_reconciliation(run.pk, batch=batch)
            if run.state in {'COMPLETED', 'FAILED'}:
                return run
        self.fail('A bounded finite fixture did not reach a terminal reconciliation state.')

    def test_http_admission_does_not_scan_and_daily_identity_is_unique(self):
        with patch(
            'rails.adapter.check_identity', side_effect=AssertionError('No provider work in admission.')
        ):
            run = queue_reconciliation(self.finance, daily_key='test-daily')
            same = queue_reconciliation(self.finance, daily_key='test-daily')
        self.assertEqual(run.pk, same.pk)
        self.assertEqual((run.state, run.phase, run.checked), ('PENDING', 'INIT', 0))
        self.assertEqual(Job.objects.filter(business_key=f'reconciliation:{run.pk}:0').count(), 1)

    def test_bounded_keysets_complete_the_fixed_range_and_exclude_later_members(self):
        self.fund(self.owner, '10.00')
        self.fund(self.finance, '20.00')
        run = queue_reconciliation(self.finance)
        self.fund(self.owner, '1.00')  # This new member is beyond the retained cutoff.
        first = advance_reconciliation(run.pk, batch=1)
        self.assertEqual((first.phase, first.checked, first.clean), ('PROVIDER', 0, False))
        first = advance_reconciliation(run.pk, batch=1)
        self.assertEqual(first.checked, 1)
        self.assertTrue(first.cursor)
        self.assertIsNone(first.completed_at)
        finished = self.finish(first)
        self.assertEqual(
            (finished.state, finished.phase, finished.checked, finished.clean), ('COMPLETED', 'DONE', 4, True)
        )
        self.assertIsNotNone(finished.completed_at)

    def test_an_orphan_closes_gate_and_does_not_starve_a_later_healthy_record(self):
        self.fund(self.owner, '10.00')
        destination = Destination.objects.filter(owner=self.owner).first()
        if destination is None:
            destination = Destination.objects.create(
                owner=self.owner,
                label='Synthetic reconcile fixture',
                activation_at=db_now() - timedelta(seconds=1),
            )
        zero = uuid.UUID(int=0)
        create_or_get(
            SimpleNamespace(
                id=zero,
                actor_id=self.owner.pk,
                provider_key=f'{settings.ENVIRONMENT_ID}:{zero}:FUNDING',
                destination=destination,
                kind='FUNDING',
                amount_cents=5,
                currency='USD',
            )
        )
        run = queue_reconciliation(self.finance)
        run = advance_reconciliation(run.pk)
        run = advance_reconciliation(run.pk)
        self.assertFalse(IntegrityGate.objects.get(pk=1).is_open)
        self.assertEqual(run.checked, 1)
        self.assertEqual(run.phase, 'PROVIDER')
        finished = self.finish(run)
        self.assertEqual((finished.state, finished.clean, finished.checked), ('COMPLETED', False, 3))
        self.assertEqual(Finding.objects.filter(run=run, classification='MISSING_LOCAL_OPERATION').count(), 1)

    def test_revoked_queued_authority_finishes_failed_before_provider_inspection(self):
        run = queue_reconciliation(self.finance)
        Role.objects.filter(user=self.finance, role='FINANCE').delete()
        User.objects.filter(pk=self.finance.pk).update(authority_version=self.finance.authority_version + 1)
        with patch(
            'rails.adapter.check_identity',
            side_effect=AssertionError('Revoked delegation must not inspect provider.'),
        ):
            result = advance_reconciliation(run.pk)
        self.assertEqual((result.state, result.clean, result.checked), ('FAILED', False, 0))
        self.assertEqual(result.failure_code, 'AUTHORITY_CHANGED')

    def test_old_job_generation_cannot_advance_a_reconciliation_cursor(self):
        run = queue_reconciliation(self.finance)
        first = next(job for job in claim_jobs() if job.object_id == str(run.pk))
        Job.objects.filter(pk=first.pk).update(lease_until=db_now() - timedelta(seconds=1))
        second = next(job for job in claim_jobs() if job.pk == first.pk)
        with self.assertRaises(DomainError):
            advance_reconciliation(run.pk, job_id=first.pk, generation=first.generation)
        run.refresh_from_db()
        self.assertEqual((run.state, run.step, run.cursor), ('PENDING', 0, ''))
        result = advance_reconciliation(run.pk, job_id=second.pk, generation=second.generation)
        self.assertEqual((result.state, result.step, result.phase), ('RUNNING', 1, 'PROVIDER'))
