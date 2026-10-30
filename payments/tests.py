import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase

from accounts.models import User
from accounts.services import db_now
from ledger.models import DailyCounter, Entry, IntegrityGate, Journal, LedgerAccount, Reservation, Wallet
from oversight.models import Case
from rails.adapter import lookup
from rails.models import Destination
from testsupport import WalletFixtures

from . import workflows as wf
from .errors import DomainError
from .models import Intent, Operation, ReplayKey
from .money import parse_amount
from .reconciliation import reconcile, verify_ledger


class FinancialBoundaryTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        self.setup_wallet_environment()
        self.alice = self.customer('alice')
        self.bob = self.customer('bob')
        self.carol = self.customer('carol')

    def tearDown(self):
        self.assertEqual(verify_ledger(), [])
        super().tearDown()

    def test_exact_money_normalization_and_rejection(self):
        for value in ['1', '1.0', '1.00', ' 1.00 ']:
            self.assertEqual(parse_amount(value), 100)
        for value in ['0', '-1', '+1', '1e2', '1.001', 'NaN', '1,000', '１', '1 0', '2500.01', None, 1.0]:
            with self.subTest(value=value), self.assertRaises(DomainError):
                parse_amount(value)

    def test_funding_transfer_and_balanced_whole_effect(self):
        self.fund(self.alice, '100.00')
        op = self.admit(
            self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '30.00', 'note': 'Lunch'}
        )
        self.alice.wallet.refresh_from_db()
        self.bob.wallet.refresh_from_db()
        self.assertEqual((self.alice.wallet.available_cents, self.bob.wallet.available_cents), (7000, 3000))
        entries = list(Entry.objects.filter(journal__operation=op).values_list('side', 'amount_cents'))
        self.assertCountEqual(entries, [('DEBIT', 3000), ('CREDIT', 3000)])
        self.assertEqual(op.state, 'COMPLETED')

    def test_rejected_admission_does_not_poison_key_or_consume_grant(self):
        i = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '30.00'})
        key = uuid.uuid4()
        with self.assertRaises(DomainError):
            wf.confirm_intent(self.alice, i.pk, key)
        self.assertFalse(ReplayKey.objects.filter(action_key=key).exists())
        self.assertFalse(Operation.objects.exists())
        self.assertFalse(Reservation.objects.exists())
        i.refresh_from_db()
        self.assertIsNone(i.consumed_at)
        self.fund(self.alice, '50.00')
        op = wf.confirm_intent(self.alice, i.pk, key)
        self.assertEqual(op.state, 'COMPLETED')

    def test_accepted_replay_survives_expiry_and_gate_pause_conflict_cannot_mutate(self):
        self.fund(self.alice)
        i = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '30.00'})
        key = uuid.uuid4()
        op = wf.confirm_intent(self.alice, i.pk, key)
        Intent.objects.filter(pk=i.pk).update(expires_at=db_now() - timedelta(seconds=1))
        IntegrityGate.objects.filter(pk=1).update(is_open=False)
        self.assertEqual(wf.confirm_intent(self.alice, i.pk, key).pk, op.pk)
        IntegrityGate.objects.filter(pk=1).update(is_open=True)
        other = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '31.00'})
        with self.assertRaises(DomainError) as exc:
            wf.confirm_intent(self.alice, other.pk, key)
        self.assertEqual(exc.exception.status, 409)
        self.assertEqual(Operation.objects.filter(kind='TRANSFER').count(), 1)

    def test_concurrent_overspend_from_independent_connections_has_one_winner(self):
        self.fund(self.alice, '100.00')
        i1 = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '80.00'})
        i2 = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'carol', 'amount': '80.00'})
        barrier = threading.Barrier(2)

        def run(intent):
            close_old_connections()
            user = User.objects.get(pk=self.alice.pk)
            barrier.wait(5)
            try:
                return wf.confirm_intent(user, intent.pk, uuid.uuid4()).state
            except DomainError:
                return 'REJECTED'
            finally:
                close_old_connections()

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, [i1, i2]))
        self.assertCountEqual(results, ['COMPLETED', 'REJECTED'])
        self.assertEqual(Operation.objects.filter(kind='TRANSFER').count(), 1)

    def test_concurrent_duplicate_identity_returns_one_operation(self):
        self.fund(self.alice, '100.00')
        i = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '30.00'})
        key = uuid.uuid4()
        barrier = threading.Barrier(2)

        def run(_):
            close_old_connections()
            user = User.objects.get(pk=self.alice.pk)
            barrier.wait(5)
            try:
                return wf.confirm_intent(user, i.pk, key).pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, [0, 1]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(Journal.objects.filter(operation_id=results[0]).count(), 1)

    def test_opposing_transfers_follow_shared_sorted_lock_order(self):
        self.fund(self.alice)
        self.fund(self.bob)
        ia = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '10.00'})
        ib = self.proof(self.bob, 'TRANSFER', {'recipient_handle': 'alice', 'amount': '10.00'})
        barrier = threading.Barrier(2)

        def run(pair):
            user, intent = pair
            close_old_connections()
            user = User.objects.get(pk=user.pk)
            barrier.wait(5)
            try:
                return wf.confirm_intent(user, intent.pk, uuid.uuid4()).state
            finally:
                close_old_connections()

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, [(self.alice, ia), (self.bob, ib)]))
        self.assertEqual(results, ['COMPLETED', 'COMPLETED'])

    def test_risk_review_reserves_both_sides_and_reject_releases_original_day(self):
        self.fund(self.alice, '999.99')
        self.fund(self.alice, '1.00')
        op = self.admit(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '1000.00'})
        self.assertEqual(op.state, 'REVIEW_PENDING')
        self.alice.wallet.refresh_from_db()
        self.bob.wallet.refresh_from_db()
        self.assertEqual(
            (
                self.alice.wallet.available_cents,
                self.alice.wallet.reserved_cents,
                self.bob.wallet.incoming_cents,
            ),
            (99, 100000, 100000),
        )
        reviewer = self.customer('reviewer', ('REVIEWER',))
        case = Case.objects.get(operation=op)
        result = self.admit(
            reviewer,
            'CASE_DECISION',
            {
                'case_id': str(case.pk),
                'decision': 'REJECT',
                'reason': 'Synthetic documented rejection.',
                'version': case.version,
            },
        )
        op.refresh_from_db()
        self.assertEqual(op.state, 'CANCELLED')
        self.assertEqual(result.state, 'REJECTED')
        self.assertEqual(Reservation.objects.filter(operation=op, state='ACTIVE').count(), 0)
        self.assertEqual(
            DailyCounter.objects.get(owner=self.alice, day=op.limit_day, direction='OUTGOING').reserved_cents,
            0,
        )

    def test_review_approval_posts_once_and_participant_cannot_approve(self):
        self.fund(self.alice, '999.99')
        self.fund(self.alice, '1.00')
        op = self.admit(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '1000.00'})
        case = Case.objects.get(operation=op)
        reviewer = self.customer('reviewer', ('REVIEWER',))
        self.admit(
            reviewer,
            'CASE_DECISION',
            {
                'case_id': str(case.pk),
                'decision': 'APPROVE',
                'reason': 'Eligible second reviewer approval.',
                'version': case.version,
            },
        )
        op.refresh_from_db()
        self.assertEqual(op.state, 'COMPLETED')
        self.assertEqual(Journal.objects.filter(operation=op, effect_kind='HELD_TRANSFER').count(), 1)
        self.bob.wallet.refresh_from_db()
        self.assertEqual(self.bob.wallet.available_cents, 100000)

    def test_deadline_is_rechecked_after_decisive_locks(self):
        self.fund(self.alice)
        i = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '10.00'})
        with patch('payments.workflows.db_now', return_value=i.expires_at):
            with self.assertRaises(DomainError):
                wf.confirm_intent(self.alice, i.pk, uuid.uuid4())
        self.assertFalse(Operation.objects.filter(kind='TRANSFER').exists())

    def test_tampered_intent_digest_and_policy_are_rejected(self):
        self.fund(self.alice)
        i = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '10.00'})
        data = i.instruction
        data['amount_cents'] = 99999
        Intent.objects.filter(pk=i.pk).update(instruction=data)
        with self.assertRaises(DomainError):
            wf.confirm_intent(self.alice, i.pk, uuid.uuid4())
        self.assertFalse(Operation.objects.filter(kind='TRANSFER').exists())

    def test_unknown_withdrawal_keeps_its_hold_and_late_success_settles_when_frozen(self):
        self.fund(self.alice, '100.00')
        d = Destination.objects.create(
            owner=self.alice,
            label='Unknown payout token',
            activation_at=db_now() - timedelta(seconds=1),
            scenario='TIMEOUT_AFTER_COMMIT',
        )
        op = self.admit(self.alice, 'WITHDRAWAL', {'destination_id': str(d.pk), 'amount': '30.00'})
        wf.dispatch_operation(op.pk)
        op.refresh_from_db()
        self.assertEqual(op.state, 'UNKNOWN')
        with self.assertRaises(DomainError):
            wf.cancel_operation(self.alice, op.pk, op.version)
        self.alice.wallet.refresh_from_db()
        self.assertEqual(self.alice.wallet.reserved_cents, 3000)
        Wallet.objects.filter(owner=self.alice).update(state='FROZEN')
        wf.apply_provider_observation(op.pk, lookup(op.provider_key))
        op.refresh_from_db()
        self.alice.wallet.refresh_from_db()
        self.assertEqual(
            (op.state, self.alice.wallet.reserved_cents, self.alice.wallet.available_cents),
            ('COMPLETED', 0, 7000),
        )

    def test_queued_cancellation_never_creates_provider_effect(self):
        self.fund(self.alice, '100.00')
        d = Destination.objects.create(
            owner=self.alice, label='Cancelable payout', activation_at=db_now() - timedelta(seconds=1)
        )
        op = self.admit(self.alice, 'WITHDRAWAL', {'destination_id': str(d.pk), 'amount': '30.00'})
        wf.cancel_operation(self.alice, op.pk, op.version)
        wf.dispatch_operation(op.pk)
        self.assertIsNone(lookup(op.provider_key))
        self.assertEqual(Reservation.objects.get(operation=op).state, 'RELEASED')

    def test_request_has_one_named_payer_and_one_linked_transfer(self):
        self.fund(self.alice, '100.00')
        req = wf.create_request(self.bob, 'alice', '10.00', 'Test request')
        self.assertFalse(Reservation.objects.filter(operation__money_request=req).exists())
        with self.assertRaises(DomainError):
            self.proof(self.carol, 'REQUEST_PAY', {'request_id': str(req.pk)})
        op = self.admit(self.alice, 'REQUEST_PAY', {'request_id': str(req.pk)})
        req.refresh_from_db()
        self.assertEqual(req.operation_id, op.pk)
        self.assertEqual(req.state, 'PAID')

    def test_schedule_reserves_nothing_then_executes_once_and_security_change_invalidates(self):
        self.fund(self.alice, '100.00')
        due = db_now() + timedelta(minutes=2)
        schedule = self.admit(
            self.alice,
            'SCHEDULE',
            {
                'recipient_handle': 'bob',
                'amount': '10.00',
                'due_at': due.isoformat(),
                'timezone': 'UTC',
                'utc_offset': '+00:00',
            },
        )
        self.assertFalse(Reservation.objects.filter(operation__schedule=schedule).exists())
        self.alice.wallet.refresh_from_db()
        self.assertEqual(self.alice.wallet.available_cents, 10000)
        with patch('payments.workflows.db_now', return_value=due):
            one = wf.execute_schedule(schedule.pk)
            two = wf.execute_schedule(schedule.pk)
        self.assertEqual(one.operation_id, two.operation_id)
        self.assertEqual(Operation.objects.filter(schedule=schedule).count(), 1)

    def test_whole_reversal_uses_available_only_and_original_is_immutable(self):
        self.fund(self.alice, '100.00')
        original = self.admit(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '30.00'})
        dispute = wf.open_dispute(
            self.alice, original.pk, 'Dispute with clear evidence.', 'Synthetic evidence'
        )
        finance = self.customer('finance', ('FINANCE',))
        reviewer = self.customer('reviewer', ('REVIEWER',))
        proposal = wf.propose_reversal(finance, dispute.pk, 'Investigation supports a whole reversal.')
        self.admit(
            reviewer,
            'CASE_DECISION',
            {
                'case_id': str(proposal.pk),
                'decision': 'APPROVE',
                'reason': 'Independent whole reversal approval.',
                'version': proposal.version,
            },
        )
        original.refresh_from_db()
        self.assertEqual(original.state, 'COMPLETED')
        self.assertTrue(Operation.objects.filter(original=original).exists())
        self.alice.wallet.refresh_from_db()
        self.assertEqual(self.alice.wallet.available_cents, 10000)
        self.assertEqual(
            DailyCounter.objects.get(owner=self.alice, direction='OUTGOING').committed_cents, 3000
        )

    def test_mandatory_audit_failure_rolls_back_money_and_key(self):
        self.fund(self.alice)
        i = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '10.00'})
        key = uuid.uuid4()
        with patch('payments.workflows.record', side_effect=RuntimeError('synthetic audit unavailable')):
            with self.assertRaises(RuntimeError):
                wf.confirm_intent(self.alice, i.pk, key)
        self.assertFalse(ReplayKey.objects.filter(action_key=key).exists())
        self.assertFalse(Operation.objects.filter(kind='TRANSFER').exists())

    def test_runtime_database_role_cannot_change_ledger_or_audit(self):
        self.fund(self.alice)
        import psycopg

        runtime = settings.TEST_RUNTIME_DATABASES['default']
        with psycopg.connect(
            dbname=connection.settings_dict['NAME'],
            host=runtime['HOST'],
            port=runtime['PORT'],
            user=runtime['USER'],
            password=runtime['PASSWORD'],
            autocommit=True,
        ) as c:
            for sql in [
                'UPDATE ledger_wallet SET available_cents=999999',
                'DELETE FROM ledger_entry',
                'UPDATE ledger_journal SET sealed=false',
                'DELETE FROM audit_auditevent',
                "UPDATE ledger_reservation SET state='RELEASED'",
            ]:
                with self.subTest(sql=sql), self.assertRaises(psycopg.Error):
                    c.execute(sql)

    def test_sealed_journal_rejects_late_balanced_insert_even_with_migration_credentials(self):
        self.fund(self.alice)
        journal = Journal.objects.first()
        account = LedgerAccount.objects.get(wallet=self.alice.wallet, kind='AVAILABLE')
        with self.assertRaises(DatabaseError), transaction.atomic():
            Entry.objects.create(journal=journal, account=account, side='CREDIT', amount_cents=1)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Journal.objects.filter(pk=journal.pk).update(committed_at=db_now())

    def test_reconciliation_recovers_missing_local_settlement(self):
        op = self.fund(self.alice, scenario='TIMEOUT_AFTER_COMMIT')
        self.assertEqual(op.state, 'UNKNOWN')
        run = reconcile()
        self.assertTrue(run.clean)
        op.refresh_from_db()
        self.assertEqual(op.state, 'COMPLETED')

    def test_freezing_wallet_releases_undisbursed_internal_reviews(self):
        self.fund(self.alice, '999.99')
        self.fund(self.alice, '1.00')
        op = self.admit(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '1000.00'})
        reviewer = self.customer('reviewer', ('REVIEWER',))
        self.bob.wallet.refresh_from_db()
        self.admit(
            reviewer,
            'RESTRICTION',
            {
                'wallet_id': str(self.bob.wallet.pk),
                'state': 'FROZEN',
                'reason': 'Synthetic documented wallet freeze.',
                'version': self.bob.wallet.version,
            },
        )
        op.refresh_from_db()
        self.alice.wallet.refresh_from_db()
        self.bob.wallet.refresh_from_db()
        self.assertEqual(op.state, 'CANCELLED')
        self.assertEqual(self.alice.wallet.reserved_cents, 0)
        self.assertEqual(self.bob.wallet.incoming_cents, 0)

    def test_recovery_metadata_preserves_original_identity_and_replay_mapping(self):
        from simulator.models import ProviderEffect

        op = self.fund(self.alice)
        effect = ProviderEffect.objects.using('simulator').get(provider_key=op.provider_key)
        self.assertEqual(effect.recovery_metadata['operation_id'], str(op.pk))
        self.assertEqual(effect.recovery_metadata['limit_day'], op.limit_day.isoformat())
        self.assertEqual(
            effect.recovery_metadata['replays'][0]['digest'], ReplayKey.objects.get(operation=op).digest
        )

    def test_primary_rollback_with_surviving_provider_payout_is_recovered_only_after_review(self):
        # Emulate restoring primary to before admission while preserving the
        # independently committed provider record. Migration-only fixture DML
        # bypasses immutable triggers exactly as a volume restore would.
        from oversight.recovery import propose_recovery
        from simulator.models import ProviderEffect

        funded = self.fund(self.alice, '100.00')
        self.assertEqual(funded.state, 'COMPLETED')
        dest = Destination.objects.create(
            owner=self.alice, label='Restore payout token', activation_at=db_now() - timedelta(seconds=1)
        )
        before_wallet = self.alice.wallet
        before_wallet.refresh_from_db()
        op = self.admit(self.alice, 'WITHDRAWAL', {'destination_id': str(dest.pk), 'amount': '30.00'})
        wf.dispatch_operation(op.pk)
        effect = ProviderEffect.objects.using('simulator').get(provider_key=op.provider_key)
        self.assertEqual(effect.state, 'SUCCESS')
        with transaction.atomic(), connection.cursor() as c:
            c.execute('ALTER TABLE payments_operation DISABLE TRIGGER operation_instruction')
            c.execute('ALTER TABLE payments_replaykey DISABLE TRIGGER immutable_replay')
            c.execute('ALTER TABLE ledger_journal DISABLE TRIGGER immutable_journal')
            c.execute('ALTER TABLE ledger_entry DISABLE TRIGGER immutable_entry')
            # Restoration removes the operation's post-snapshot dependent data.
            c.execute(
                'DELETE FROM notifications_notification WHERE event_key LIKE %s', [f'operation:{op.pk}:%']
            )
            c.execute('DELETE FROM work_job WHERE object_id=%s', [str(op.pk)])
            c.execute(
                'DELETE FROM ledger_entry WHERE journal_id IN (SELECT id FROM ledger_journal WHERE operation_id=%s)',
                [op.pk],
            )
            c.execute('DELETE FROM ledger_journal WHERE operation_id=%s', [op.pk])
            c.execute('DELETE FROM ledger_reservation WHERE operation_id=%s', [op.pk])
            c.execute('DELETE FROM payments_replaykey WHERE operation_id=%s', [op.pk])
            c.execute('DELETE FROM payments_operation WHERE id=%s', [op.pk])
            c.execute(
                "UPDATE ledger_ledgeraccount SET balance_cents=10000 WHERE wallet_id=%s AND kind='AVAILABLE'",
                [before_wallet.pk],
            )
            c.execute(
                "UPDATE ledger_ledgeraccount SET balance_cents=0 WHERE wallet_id=%s AND kind='RESERVED'",
                [before_wallet.pk],
            )
            c.execute("UPDATE ledger_ledgeraccount SET balance_cents=10000 WHERE kind='SYSTEM_CASH'")
            c.execute(
                'UPDATE ledger_wallet SET available_cents=10000,reserved_cents=0,sequence=%s,version=%s WHERE id=%s',
                [before_wallet.sequence, before_wallet.version, before_wallet.pk],
            )
            c.execute(
                "DELETE FROM ledger_dailycounter WHERE owner_id=%s AND direction='OUTGOING'", [self.alice.pk]
            )
            c.execute('SET CONSTRAINTS ALL IMMEDIATE')
            c.execute('ALTER TABLE payments_operation ENABLE TRIGGER operation_instruction')
            c.execute('ALTER TABLE payments_replaykey ENABLE TRIGGER immutable_replay')
            c.execute('ALTER TABLE ledger_journal ENABLE TRIGGER immutable_journal')
            c.execute('ALTER TABLE ledger_entry ENABLE TRIGGER immutable_entry')
        run = reconcile()
        self.assertFalse(run.clean)
        self.assertFalse(IntegrityGate.objects.get(pk=1).is_open)
        from oversight.models import Finding

        finding = Finding.objects.get(classification='MISSING_LOCAL_OPERATION', run=run)
        finance = self.customer('finance', ('FINANCE',))
        reviewer = self.customer('reviewer', ('REVIEWER',))
        proposal = propose_recovery(
            finance, finding.pk, 'Recover the surviving immutable payout.', 'RECOVER_OPERATION'
        )
        result = self.admit(
            reviewer,
            'RECOVERY_APPROVAL',
            {
                'proposal_id': str(proposal.pk),
                'reason': 'Independent review of retained payout.',
                'version': proposal.version,
            },
        )
        self.assertEqual(result.state, 'APPLIED')
        restored = Operation.objects.get(pk=op.pk)
        self.alice.wallet.refresh_from_db()
        self.assertEqual(restored.provider_key, op.provider_key)
        self.assertEqual(restored.state, 'COMPLETED')
        self.assertEqual(self.alice.wallet.available_cents, 7000)
        self.assertEqual(
            ProviderEffect.objects.using('simulator').filter(provider_key=op.provider_key).count(), 1
        )
        self.assertEqual(
            ReplayKey.objects.get(operation=restored).action_key,
            uuid.UUID(effect.recovery_metadata['replays'][0]['action_key']),
        )
        self.assertFalse(IntegrityGate.objects.get(pk=1).is_open)

    def test_real_database_clock_rejects_intent_that_expires_while_waiting_for_account_lock(self):
        import time

        self.fund(self.alice, '100.00')
        intent = self.proof(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '10.00'})
        expiry = db_now() + timedelta(milliseconds=1200)
        Intent.objects.filter(pk=intent.pk).update(
            created_at=expiry - timedelta(seconds=120), expires_at=expiry
        )
        account = LedgerAccount.objects.get(wallet=self.alice.wallet, kind='AVAILABLE')
        ready = threading.Event()

        def attempt():
            close_old_connections()
            with connection.cursor() as cursor:
                cursor.execute("SET application_name='dwmt-intent-lock-case'")
            ready.set()
            try:
                wf.confirm_intent(User.objects.get(pk=self.alice.pk), intent.pk, uuid.uuid4())
                return 'ADMITTED'
            except DomainError as exc:
                return exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(1) as pool:
            with transaction.atomic():
                LedgerAccount.objects.select_for_update().get(pk=account.pk)
                future = pool.submit(attempt)
                self.assertTrue(ready.wait(5))
                reached = False
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_stat_clear_snapshot()')
                        cursor.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name='dwmt-intent-lock-case' AND wait_event_type='Lock')"
                        )
                        reached = cursor.fetchone()[0]
                    if reached:
                        break
                    time.sleep(0.01)
                self.assertTrue(reached, 'The independent admission did not reach its account lock barrier.')
                while db_now() < expiry:
                    time.sleep(0.01)
            self.assertEqual(future.result(timeout=5), 'EXPIRED')
        self.assertFalse(Operation.objects.filter(kind='TRANSFER').exists())

    def test_malformed_response_after_provider_commit_keeps_capacity_then_lookup_converges(self):
        op = self.fund(self.alice, '12.34', scenario='MALFORMED_AFTER_COMMIT')
        self.assertEqual(op.state, 'UNKNOWN')
        self.assertTrue(IntegrityGate.objects.get(pk=1).is_open)
        self.alice.wallet.refresh_from_db()
        self.assertEqual(self.alice.wallet.incoming_cents, 1234)
        wf.apply_provider_observation(op.pk, lookup(op.provider_key))
        op.refresh_from_db()
        self.alice.wallet.refresh_from_db()
        self.assertEqual(
            (op.state, self.alice.wallet.available_cents, self.alice.wallet.incoming_cents),
            ('COMPLETED', 1234, 0),
        )
        self.assertEqual(Journal.objects.filter(operation=op, effect_kind='FUNDING').count(), 1)

    def test_new_reversal_does_not_override_a_current_wallet_freeze(self):
        self.fund(self.alice, '100.00')
        original = self.admit(self.alice, 'TRANSFER', {'recipient_handle': 'bob', 'amount': '30.00'})
        dispute = wf.open_dispute(self.alice, original.pk, 'Investigate this completed transfer.')
        finance = self.customer('finance', ('FINANCE',))
        reviewer = self.customer('reviewer', ('REVIEWER',))
        proposal = wf.propose_reversal(finance, dispute.pk, 'Propose a reviewed whole reversal.')
        Wallet.objects.filter(owner=self.bob).update(state='FROZEN')
        with self.assertRaises(DomainError):
            self.admit(
                reviewer,
                'CASE_DECISION',
                {
                    'case_id': str(proposal.pk),
                    'decision': 'APPROVE',
                    'reason': 'Independent proposed reversal review.',
                    'version': proposal.version,
                },
            )
        self.assertFalse(Operation.objects.filter(original=original).exists())
        self.bob.wallet.refresh_from_db()
        self.assertEqual(self.bob.wallet.available_cents, 3000)
