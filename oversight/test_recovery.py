"""Reviewed projection repair over real immutable PostgreSQL journal data."""

from django.db import transaction
from django.test import TransactionTestCase

from accounts.services import db_now, lock_users
from audit.models import AuditEvent
from ledger.models import Entry, IntegrityGate, LedgerAccount, Wallet
from payments.errors import DomainError
from testsupport import WalletFixtures

from .models import Finding, RecoveryProposal
from .recovery import approve_recovery_locked, propose_recovery


class ProjectionRecoveryTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        self.setup_wallet_environment()
        self.owner = self.customer('projection_owner')
        self.finance = self.customer('projection_finance', ('FINANCE',))
        self.reviewer = self.customer('projection_reviewer', ('REVIEWER',))
        self.fund(self.owner, '100.00')
        self.account = LedgerAccount.objects.get(wallet=self.owner.wallet, kind='AVAILABLE')
        # Explicit privileged corruption control. Production runtime lacks this DML.
        LedgerAccount.objects.filter(pk=self.account.pk).update(balance_cents=12345)
        IntegrityGate.objects.filter(pk=1).update(is_open=False)
        self.finding = Finding.objects.create(
            classification='PROJECTION_MISMATCH',
            details={'reference': str(self.account.pk)},
            created_at=db_now(),
        )

    def approval(self, actor, proposal):
        # This helper substitutes the already-issued bound proof, while retaining
        # all real primary lock and definer-function boundaries. Root workflow
        # acceptance separately exercises RECOVERY_APPROVAL intent proof.
        with transaction.atomic():
            IntegrityGate.objects.select_for_update().get(pk=1)
            users = {user.pk: user for user in lock_users([actor.pk, proposal.creator_id, self.owner.pk])}
            locked = RecoveryProposal.objects.select_for_update().get(pk=proposal.pk)
            Wallet.objects.select_for_update().get(pk=self.owner.wallet.pk)
            list(LedgerAccount.objects.select_for_update().filter(wallet=self.owner.wallet).order_by('pk'))
            return approve_recovery_locked(
                users[actor.pk], locked, 'Independent exact projection reconstruction approval.'
            )

    def test_repair_rebuilds_projection_only_and_preserves_closed_gate(self):
        prior_entries = list(
            Entry.objects.order_by('pk').values('id', 'journal_id', 'account_id', 'side', 'amount_cents')
        )
        proposal = propose_recovery(
            self.finance,
            self.finding.pk,
            'Investigated projection mismatch against immutable entries.',
            'REBUILD_PROJECTIONS',
        )
        self.assertEqual(proposal.payload['before']['accounts'][str(self.account.pk)], 12345)
        self.assertEqual(proposal.payload['expected']['accounts'][str(self.account.pk)], 10000)
        result = self.approval(self.reviewer, proposal)
        self.assertEqual(result.state, 'APPLIED')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_cents, 10000)
        self.assertEqual(
            list(
                Entry.objects.order_by('pk').values('id', 'journal_id', 'account_id', 'side', 'amount_cents')
            ),
            prior_entries,
        )
        self.assertFalse(IntegrityGate.objects.get(pk=1).is_open)
        self.assertEqual(
            AuditEvent.objects.filter(resource=str(proposal.pk), action='RECOVERY_APPLIED').count(), 1
        )

    def test_proposer_and_customer_participant_cannot_approve(self):
        from accounts.models import Role

        Role.objects.get_or_create(user=self.finance, role='REVIEWER')
        Role.objects.get_or_create(user=self.owner, role='REVIEWER')
        proposal = propose_recovery(
            self.finance,
            self.finding.pk,
            'Investigated immutable accounting mismatch.',
            'REBUILD_PROJECTIONS',
        )
        for forbidden in [self.finance, self.owner]:
            with self.subTest(actor=forbidden.pk), self.assertRaises(DomainError):
                self.approval(forbidden, proposal)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_cents, 12345)
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, 'OPEN')

    def test_changed_diff_blocks_without_applying_a_silent_new_repair(self):
        proposal = propose_recovery(
            self.finance,
            self.finding.pk,
            'Investigated immutable accounting mismatch.',
            'REBUILD_PROJECTIONS',
        )
        LedgerAccount.objects.filter(pk=self.account.pk).update(balance_cents=12346)
        result = self.approval(self.reviewer, proposal)
        self.assertEqual(result.state, 'BLOCKED')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_cents, 12346)
        self.assertFalse(
            AuditEvent.objects.filter(resource=str(proposal.pk), action='RECOVERY_APPLIED').exists()
        )
