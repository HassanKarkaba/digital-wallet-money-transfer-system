import csv
import io
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.test import TransactionTestCase

from accounts.models import UserSession
from accounts.services import db_now
from ledger.models import Journal
from payments.errors import DomainError
from payments.workflows import cancel_operation
from testsupport import WalletFixtures

from .models import Statement
from .services import csv_safe, generate_statement, resolve_private


def post_form(client, path, payload):
    return client.post(path, urlencode(payload, doseq=True), content_type='application/x-www-form-urlencoded')


class StatementSnapshotTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        self.setup_wallet_environment()
        self.owner = self.customer('statement_owner')
        self.other = self.customer('statement_other')

    def login_as(self, user):
        self.client.force_login(user)
        now = db_now()
        UserSession.objects.create(
            user=user,
            session_key=self.client.session.session_key,
            started_at=now,
            last_seen=now,
            authority_version=user.authority_version,
        )

    def test_reservations_releases_and_later_effect_do_not_change_snapshot(self):
        start = db_now() - timedelta(hours=1)
        self.fund(self.owner)
        self.fund(self.owner)
        held = self.admit(
            self.owner,
            'TRANSFER',
            {'recipient_handle': self.other.handle, 'amount': '1000.00', 'note': '=1+1'},
        )
        self.assertEqual(held.state, 'REVIEW_PENDING')
        cancel_operation(self.owner, held.id, held.version)
        statement = generate_statement(self.owner, start, db_now() + timedelta(hours=1), 'America/Detroit')
        self.assertEqual((statement.opening_available_cents, statement.opening_reserved_cents), (0, 0))
        self.assertEqual((statement.closing_available_cents, statement.closing_reserved_cents), (150000, 0))
        self.assertTrue(any(row['reserved_change'] == 100000 for row in statement.rows))
        self.assertTrue(any(row['reserved_change'] == -100000 for row in statement.rows))
        retained_rows = statement.rows
        self.admit(self.owner, 'TRANSFER', {'recipient_handle': self.other.handle, 'amount': '10.00'})
        statement.refresh_from_db()
        self.assertEqual(statement.rows, retained_rows)
        self.assertEqual(statement.closing_available_cents, 150000)
        output = resolve_private(statement.file_name).read_text()
        csv_rows = list(csv.reader(io.StringIO(output)))
        self.assertIn('DEMONSTRATION MONEY', csv_rows[0][0])
        self.assertTrue(any("'=1+1" in row for row in csv_rows))
        self.assertTrue(any(row['local_timestamp'].endswith(('-04:00', '-05:00')) for row in statement.rows))

    def test_inclusive_start_and_exclusive_end_derive_opening_from_entries(self):
        funding = self.fund(self.owner, amount='10.00')
        committed = Journal.objects.get(operation=funding, effect_kind='FUNDING').committed_at
        before = generate_statement(self.owner, committed - timedelta(hours=1), committed)
        self.assertEqual(before.row_count, 0)
        self.assertEqual(before.closing_available_cents, 0)
        on = generate_statement(self.owner, committed, committed + timedelta(seconds=1))
        self.assertEqual(on.opening_available_cents, 0)
        self.assertEqual(on.closing_available_cents, 1000)
        self.assertEqual(on.row_count, 1)

    def test_private_download_checks_owner_and_expiry(self):
        self.fund(self.owner, amount='10.00')
        statement = generate_statement(self.owner, db_now() - timedelta(days=1), db_now() + timedelta(days=1))
        self.login_as(self.other)
        self.assertEqual(self.client.get(f'/statements/{statement.id}/download/').status_code, 404)
        self.client.logout()
        self.login_as(self.owner)
        response = self.client.get(f'/statements/{statement.id}/download/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn('no-store', response['Cache-Control'])
        response.close()
        Statement.objects.filter(pk=statement.pk).update(expires_at=db_now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(f'/statements/{statement.id}/download/').status_code, 410)

    def test_range_rejection_produces_no_report(self):
        with self.assertRaises(DomainError):
            generate_statement(self.owner, db_now() - timedelta(days=367), db_now())
        self.assertEqual(Statement.objects.count(), 0)

    def test_formula_escaping_covers_leading_whitespace(self):
        self.assertEqual(csv_safe(' =SUM(A1:A2)'), "' =SUM(A1:A2)")
        self.assertEqual(csv_safe('@recipient'), "'@recipient")
        self.assertEqual(csv_safe('ordinary note'), 'ordinary note')

    def test_staff_reversal_actor_gains_no_customer_ownership(self):
        from payments.models import Operation
        from payments.workflows import open_dispute, propose_reversal

        self.fund(self.owner, amount='20.00')
        transfer = self.admit(
            self.owner, 'TRANSFER', {'recipient_handle': self.other.handle, 'amount': '10.00'}
        )
        finance = self.customer('statement_finance', roles=('FINANCE',))
        reviewer = self.customer('statement_reviewer', roles=('REVIEWER',))
        dispute = open_dispute(self.owner, transfer.id, 'Investigate this synthetic completed transfer.')
        proposal = propose_reversal(finance, dispute.id, 'Propose the whole synthetic transfer reversal.')
        self.admit(
            reviewer,
            'CASE_DECISION',
            {
                'case_id': str(proposal.id),
                'decision': 'APPROVE',
                'reason': 'Approve this independently investigated whole reversal.',
                'version': proposal.version,
            },
        )
        reversal = Operation.objects.get(original=transfer)
        self.assertEqual(reversal.actor_id, reviewer.id)
        self.login_as(reviewer)
        self.assertEqual(self.client.get(f'/payments/{reversal.id}/').status_code, 404)
        self.assertNotContains(self.client.get('/activity/'), str(reversal.id))

    def test_statement_admission_commits_a_durable_job_before_private_publication(self):
        from work.models import Job

        from .services import generate_pending_statement, queue_statement

        self.fund(self.owner, amount='10.00')
        statement = queue_statement(self.owner, db_now() - timedelta(days=1), db_now() + timedelta(days=1))
        self.assertEqual(statement.state, 'PENDING')
        self.assertEqual(statement.file_name, '')
        self.assertTrue(Job.objects.filter(kind='STATEMENT', object_id=str(statement.id)).exists())
        self.login_as(self.owner)
        self.assertContains(self.client.get(f'/statements/{statement.id}/'), 'Your statement is queued')
        self.assertEqual(self.client.get(f'/statements/{statement.id}/download/').status_code, 409)
        ready = generate_pending_statement(statement.id)
        self.assertEqual((ready.state, ready.closing_available_cents), ('READY', 1000))
        self.assertTrue(resolve_private(ready.file_name).is_file())

    def test_expired_statement_worker_cannot_publish_over_new_generation(self):
        from work.models import Job
        from work.services import claim_jobs

        from .services import generate_pending_statement, queue_statement

        self.fund(self.owner, amount='10.00')
        statement = queue_statement(self.owner, db_now() - timedelta(days=1), db_now() + timedelta(days=1))
        first = next(
            job for job in claim_jobs() if job.kind == 'STATEMENT' and job.object_id == str(statement.id)
        )
        Job.objects.filter(pk=first.pk).update(lease_until=db_now() - timedelta(seconds=1))
        second = next(job for job in claim_jobs() if job.pk == first.pk)
        with self.assertRaises(DomainError) as caught:
            generate_pending_statement(statement.id, job_id=first.id, generation=first.generation)
        self.assertEqual(caught.exception.code, 'STALE')
        statement.refresh_from_db()
        self.assertEqual((statement.state, statement.file_name), ('PENDING', ''))
        ready = generate_pending_statement(statement.id, job_id=second.id, generation=second.generation)
        self.assertEqual(ready.state, 'READY')

    def test_private_cleanup_skips_fresh_prefix_and_preserves_referenced_snapshot(self):
        import os
        import time
        from pathlib import Path

        from .services import cleanup_exports

        statement = generate_statement(self.owner, db_now() - timedelta(days=1), db_now() + timedelta(days=1))
        retained = resolve_private(statement.file_name)
        directory = Path(settings.PRIVATE_ROOT) / 'statements'
        for index in range(510):
            (directory / f'fresh-{index}.pending').write_text('new unfinished fixture')
        orphan = directory / 'old-unreferenced.csv'
        orphan.write_text('complete but never advertised')
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        os.utime(retained, (old, old))
        cleanup_exports()
        self.assertFalse(orphan.exists())
        self.assertTrue(retained.exists())
        self.assertTrue((directory / 'fresh-0.pending').exists())

    def test_activity_cursor_uses_posting_order_for_late_settlement(self):
        from payments.workflows import dispatch_operation
        from rails.models import Destination

        self.fund(self.owner, amount='20.00')
        destination = Destination.objects.filter(owner=self.owner).first()
        pending = self.admit(self.owner, 'FUNDING', {'destination_id': str(destination.id), 'amount': '1.00'})
        transfer = self.admit(
            self.owner, 'TRANSFER', {'recipient_handle': self.other.handle, 'amount': '1.00'}
        )
        self.assertLess(pending.created_at, transfer.created_at)
        dispatch_operation(pending.id)
        self.login_as(self.owner)
        response = self.client.get('/activity/', {'page_size': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['operations'][0].id, pending.id)
        cursor = response.context['next_cursor']
        next_page = self.client.get('/activity/', {'page_size': '1', 'cursor': cursor})
        self.assertEqual(next_page.context['operations'][0].id, transfer.id)

    def test_accepted_protocol_replay_and_status_survive_proof_row_expiry(self):
        import uuid

        from ledger.models import Journal
        from payments.models import Intent, Operation, ReplayKey
        from payments.workflows import confirm_intent

        self.fund(self.owner, amount='20.00')
        intent = self.proof(self.owner, 'TRANSFER', {'recipient_handle': self.other.handle, 'amount': '5.00'})
        key = uuid.uuid4()
        operation = confirm_intent(self.owner, intent.id, key)
        mapping = ReplayKey.objects.get(owner=self.owner, family='TRANSFER', action_key=key)
        self.assertEqual(mapping.intent_id, intent.id)
        Intent.objects.filter(pk=intent.id).delete()
        self.login_as(self.owner)
        data = {
            'intent_id': str(intent.id),
            'action_key': str(key),
            'policy_version': 'DWMT-P1',
            'schema_version': '1',
        }
        response = post_form(self.client, '/transfers/confirm/', data)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response['Location'], f'/payments/{operation.id}/')
        status = self.client.get(f'/payments/actions/TRANSFER/{key}/', HTTP_ACCEPT='application/json')
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['reference'], str(operation.id))
        self.assertEqual(status.json()['state'], 'COMPLETED')
        changed = post_form(self.client, '/transfers/confirm/', {**data, 'intent_id': str(uuid.uuid4())})
        self.assertIn(changed.status_code, (404, 409))
        self.assertEqual(Operation.objects.filter(kind='TRANSFER').count(), 1)
        self.assertEqual(Journal.objects.filter(operation=operation).count(), 1)
        self.assertEqual(self.client.get(f'/payments/intents/{intent.id}/').status_code, 410)
        self.client.logout()
        self.login_as(self.other)
        self.assertEqual(
            self.client.get(f'/payments/actions/TRANSFER/{key}/', HTTP_ACCEPT='application/json').status_code,
            404,
        )
        self.assertEqual(self.client.get(f'/payments/intents/{intent.id}/').status_code, 404)

    def test_http_upload_rejection_does_not_consume_bound_proof_or_post_money(self):
        import uuid

        from django.core.files.uploadedfile import SimpleUploadedFile

        from payments.models import Operation, ReplayKey

        self.fund(self.owner, amount='20.00')
        intent = self.proof(self.owner, 'TRANSFER', {'recipient_handle': self.other.handle, 'amount': '1.00'})
        key = uuid.uuid4()
        self.login_as(self.owner)
        response = self.client.post(
            '/transfers/confirm/',
            {
                'intent_id': str(intent.id),
                'action_key': str(key),
                'policy_version': 'DWMT-P1',
                'schema_version': '1',
                'unexpected_file': SimpleUploadedFile('synthetic.txt', b'test'),
            },
        )
        self.assertEqual(response.status_code, 415)
        intent.refresh_from_db()
        self.assertIsNone(intent.consumed_at)
        self.assertFalse(Operation.objects.filter(kind='TRANSFER').exists())
        self.assertFalse(
            ReplayKey.objects.filter(owner=self.owner, family='TRANSFER', action_key=key).exists()
        )

    def test_recipient_and_destination_routes_handle_authenticated_lazy_user(self):
        from payments.models import Recipient
        from rails.models import Destination

        self.login_as(self.owner)
        response = post_form(
            self.client, '/recipients/', {'handle': self.other.handle, 'label': 'Saved synthetic recipient'}
        )
        self.assertEqual(response.status_code, 303)
        saved = Recipient.objects.get(owner=self.owner, target=self.other)
        self.assertEqual(saved.label, 'Saved synthetic recipient')
        response = post_form(self.client, f'/recipients/{saved.id}/remove/', {})
        self.assertEqual(response.status_code, 303)
        self.assertFalse(Recipient.objects.filter(pk=saved.id).exists())
        self.fund(self.owner, amount='10.00')
        destination = Destination.objects.filter(owner=self.owner).first()
        response = post_form(self.client, f'/destinations/{destination.id}/disable/', {})
        self.assertEqual(response.status_code, 303)
        destination.refresh_from_db()
        self.assertEqual(destination.status, 'DISABLED')


class CaseStatementAccessTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        self.setup_wallet_environment()
        self.owner = self.customer('case_customer')
        self.outsider = self.customer('case_outsider')
        self.support = self.customer('case_support', roles=('SUPPORT',))
        self.finance = self.customer('case_finance', roles=('FINANCE',))
        self.funding = self.fund(self.owner, amount='10.00')
        self.login_as(self.support)
        response = post_form(
            self.client,
            f'/staff/operations/{self.funding.id}/support/',
            {'reason': 'Investigate the exact synthetic funding record.'},
        )
        self.assertEqual(response.status_code, 303)
        from oversight.models import Case

        self.case = Case.objects.get(operation=self.funding, type='SUPPORT')
        response = post_form(
            self.client,
            f'/staff/cases/{self.case.id}/assign-finance/',
            {
                'finance_email': self.finance.email,
                'reason': 'Assign an accountable Finance investigation.',
                'version': self.case.version,
            },
        )
        self.assertEqual(response.status_code, 303)
        self.case.refresh_from_db()

    def login_as(self, user):
        self.client.logout()
        self.client.force_login(user)
        now = db_now()
        UserSession.objects.create(
            user=user,
            session_key=self.client.session.session_key,
            started_at=now,
            last_seen=now,
            authority_version=user.authority_version,
        )

    def queue_for_case(self):
        from oversight.models import CaseStatement

        self.login_as(self.finance)
        response = post_form(
            self.client,
            f'/staff/cases/{self.case.id}/statements/',
            {
                'owner_id': str(self.owner.id),
                'start': (db_now() - timedelta(days=1)).date().isoformat(),
                'end': (db_now() + timedelta(days=1)).date().isoformat(),
                'timezone': 'UTC',
                'purpose': 'Inspect this assigned funding investigation.',
                'version': self.case.version,
            },
        )
        self.assertEqual(response.status_code, 303)
        return CaseStatement.objects.get(case=self.case)

    def test_assigned_finance_inspects_only_through_audited_case_route(self):
        from .services import generate_pending_statement

        link = self.queue_for_case()
        ready = generate_pending_statement(link.statement_id)
        self.assertEqual(ready.state, 'READY')
        url = f'/staff/cases/{self.case.id}/statements/{link.id}/'
        self.assertContains(self.client.get(url), 'Wallet statement')
        response = self.client.get(url + 'download/')
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.client.get(f'/statements/{ready.id}/download/').status_code, 404)
        second = self.customer('case_other_finance', roles=('FINANCE',))
        self.login_as(second)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(url + 'download/').status_code, 404)

    def test_finance_role_loss_blocks_pending_generation_and_known_file_read(self):
        from accounts.models import Role

        from .services import generate_pending_statement

        link = self.queue_for_case()
        Role.objects.filter(user=self.finance, role='FINANCE').delete()
        result = generate_pending_statement(link.statement_id)
        self.assertEqual((result.state, result.file_name), ('FAILED', ''))
        self.assertEqual(
            self.client.get(f'/staff/cases/{self.case.id}/statements/{link.id}/').status_code, 404
        )

    def test_case_reassignment_removes_old_finance_file_access(self):
        from .services import generate_pending_statement

        link = self.queue_for_case()
        ready = generate_pending_statement(link.statement_id)
        self.assertEqual(ready.state, 'READY')
        replacement = self.customer('case_replacement', roles=('FINANCE',))
        self.login_as(self.support)
        response = post_form(
            self.client,
            f'/staff/cases/{self.case.id}/assign-finance/',
            {
                'finance_email': replacement.email,
                'reason': 'Transfer the explicit investigation assignment.',
                'version': self.case.version,
            },
        )
        self.assertEqual(response.status_code, 303)
        self.login_as(self.finance)
        url = f'/staff/cases/{self.case.id}/statements/{link.id}/download/'
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login_as(replacement)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_case_scope_rejects_a_customer_outside_the_operation(self):
        from .case_access import queue_case_statement

        with self.assertRaises(DomainError) as caught:
            queue_case_statement(
                self.finance,
                self.case.id,
                self.outsider.id,
                db_now() - timedelta(days=1),
                db_now() + timedelta(days=1),
                'UTC',
                'This must not grant an unrelated customer export.',
                self.case.version,
            )
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(Statement.objects.count(), 0)

    def test_retained_closed_customer_history_is_available_only_to_assigned_finance(self):
        from payments.workflows import dispatch_operation
        from rails.models import Destination

        from .services import generate_pending_statement

        self.case.state = 'CLOSED_NO_REFUND'
        self.case.save(update_fields=['state'])
        destination = Destination.objects.filter(owner=self.owner).first()
        withdrawal = self.admit(
            self.owner, 'WITHDRAWAL', {'destination_id': str(destination.id), 'amount': '10.00'}
        )
        dispatch_operation(withdrawal.id)
        self.admit(
            self.owner,
            'CLOSE',
            {'acknowledgement': 'CLOSE MY WALLET', 'version': self.owner.authority_version},
        )
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        link = self.queue_for_case()
        ready = generate_pending_statement(link.statement_id)
        self.assertEqual((ready.state, ready.closing_available_cents), ('READY', 0))
        response = self.client.get(f'/staff/cases/{self.case.id}/statements/{link.id}/download/')
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.client.get(f'/statements/{ready.id}/download/').status_code, 404)

    def test_access_administrator_role_does_not_disclose_financial_case_queue(self):
        admin = self.customer('case_access_admin', roles=('ACCESS_ADMIN',))
        self.login_as(admin)
        response = self.client.get('/staff/')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response['Location'], '/staff/access/')
        self.assertEqual(self.client.get(f'/staff/cases/{self.case.id}/').status_code, 403)
