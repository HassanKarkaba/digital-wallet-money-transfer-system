from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TransactionTestCase

from accounts.models import UserSession
from accounts.proof_budget import bound_proof_budget
from accounts.services import RateLimited, begin_session, db_now
from config.authority import bind_request
from payments import workflows
from payments.errors import DomainError
from payments.models import Operation
from testsupport import WalletFixtures


class FinalAuthorityTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        self.setup_wallet_environment()
        self.owner = self.customer('authorityowner')
        self.recipient = self.customer('authorityrecipient')

    def request(self):
        request = RequestFactory().post('/transfers/confirm/')
        SessionMiddleware(lambda _: None).process_request(request)
        request.user = AnonymousUser()
        begin_session(request, self.owner)
        request.session.save()
        return request

    def test_revoked_request_context_cannot_admit_with_previously_issued_proof(self):
        self.fund(self.owner, '100.00')
        intent = self.proof(
            self.owner, 'TRANSFER', {'recipient_handle': self.recipient.handle, 'amount': '10.00'}
        )
        request = self.request()
        UserSession.objects.filter(session_key=request.session.session_key).update(revoked_at=db_now())
        with bind_request(request), self.assertRaises(DomainError):
            workflows.confirm_intent(self.owner, intent.pk, __import__('uuid').uuid4())
        self.assertFalse(Operation.objects.filter(kind='TRANSFER').exists())
        intent.refresh_from_db()
        self.assertIsNone(intent.consumed_at)

    def test_successful_proof_attempts_do_not_exhaust_the_failed_proof_budget(self):
        for _ in range(10):
            with bound_proof_budget(self.owner.pk):
                pass

        class InvalidProof(Exception):
            keep_proof_attempt = True

        for _ in range(5):
            with self.assertRaises(InvalidProof), bound_proof_budget(self.owner.pk):
                raise InvalidProof()
        with self.assertRaises(RateLimited), bound_proof_budget(self.owner.pk):
            pass

    def test_invalid_bound_totp_attempts_persist_even_when_verification_rolls_back(self):
        import pyotp

        from accounts.services import decrypt_secret

        intent = workflows.create_intent(
            self.owner, 'TRANSFER', {'recipient_handle': self.recipient.handle, 'amount': '10.00'}
        )
        totp = pyotp.TOTP(decrypt_secret(self.owner.mfa_secret))
        counter = int(db_now().timestamp()) // 30
        valid = {totp.generate_otp(n) for n in (counter - 1, counter, counter + 1)}
        wrong = next(f'{n:06d}' for n in range(10) if f'{n:06d}' not in valid)
        for _ in range(5):
            with self.assertRaises(DomainError):
                workflows.verify_intent(self.owner, intent.pk, wrong)
        with self.assertRaises(RateLimited):
            workflows.verify_intent(self.owner, intent.pk, wrong)
        intent.refresh_from_db()
        self.assertIsNone(intent.verified_at)

    def test_actual_capability_loss_blocks_admission_despite_healthy_data(self):
        from django.db import connection

        intent = self.proof(
            self.owner, 'TRANSFER', {'recipient_handle': self.recipient.handle, 'amount': '1.00'}
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE ledger_entry DISABLE TRIGGER balanced_entries')
            with self.assertRaises(DomainError) as caught:
                workflows.confirm_intent(self.owner, intent.pk, __import__('uuid').uuid4())
            self.assertEqual(caught.exception.code, 'UNAVAILABLE')
            self.assertFalse(Operation.objects.exists())
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE ledger_entry ENABLE TRIGGER balanced_entries')
