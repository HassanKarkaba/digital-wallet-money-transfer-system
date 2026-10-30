"""A resolved dispute cannot leave a historical blocked attempt as a live duty."""

from datetime import timedelta

from django.test import TransactionTestCase

from accounts.services import closure_blockers, db_now
from oversight.models import Case
from payments import workflows
from rails.models import Destination
from testsupport import WalletFixtures


class ResolvedDisputeClosureTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def test_blocked_reversal_then_success_preserves_history_without_stranding_closure(self):
        self.setup_wallet_environment()
        sender = self.customer('closure_sender')
        recipient = self.customer('closure_recipient')
        third = self.customer('closure_third')
        finance = self.customer('closure_finance', ('FINANCE',))
        reviewer = self.customer('closure_reviewer', ('REVIEWER',))
        self.fund(sender, '100.00')
        original = self.admit(sender, 'TRANSFER', {'recipient_handle': recipient.handle, 'amount': '100.00'})
        self.admit(recipient, 'TRANSFER', {'recipient_handle': third.handle, 'amount': '1.00'})
        dispute = workflows.open_dispute(sender, original.pk, 'Synthetic dispute with a documented reason.')
        first = workflows.propose_reversal(
            finance, dispute.pk, 'The investigation supports a complete reversal.'
        )
        self.admit(
            reviewer,
            'CASE_DECISION',
            {
                'case_id': str(first.pk),
                'decision': 'APPROVE',
                'reason': 'Independent review of the original complete amount.',
                'version': first.version,
            },
        )
        first.refresh_from_db()
        self.assertEqual(first.state, 'BLOCKED_FUNDS')
        self.admit(third, 'TRANSFER', {'recipient_handle': recipient.handle, 'amount': '1.00'})
        second = workflows.propose_reversal(
            finance, dispute.pk, 'Funds changed; explicitly retry the reviewed whole reversal.'
        )
        self.admit(
            reviewer,
            'CASE_DECISION',
            {
                'case_id': str(second.pk),
                'decision': 'APPROVE',
                'reason': 'A separate current review approves the complete amount.',
                'version': second.version,
            },
        )
        destination = Destination.objects.create(
            owner=sender, label='Closure synthetic withdrawal', activation_at=db_now() - timedelta(seconds=1)
        )
        withdrawal = self.admit(
            sender, 'WITHDRAWAL', {'destination_id': str(destination.pk), 'amount': '100.00'}
        )
        workflows.dispatch_operation(withdrawal.pk)
        sender.wallet.refresh_from_db()
        self.assertEqual(
            (sender.wallet.available_cents, sender.wallet.reserved_cents, sender.wallet.incoming_cents),
            (0, 0, 0),
        )
        self.assertEqual(Case.objects.get(pk=dispute.pk).state, 'REVERSED')
        self.assertEqual(Case.objects.get(pk=first.pk).state, 'BLOCKED_FUNDS')
        self.assertEqual(closure_blockers(sender), [])
