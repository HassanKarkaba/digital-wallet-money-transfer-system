"""Exercise request decisions through the actual browser HTTP routes."""

from urllib.parse import urlencode

from django.test import TransactionTestCase

from accounts.models import UserSession
from accounts.services import db_now
from ledger.models import DailyCounter, Entry, Journal, LedgerAccount, Reservation, Wallet
from testsupport import WalletFixtures

from .models import Operation
from .reconciliation import verify_ledger
from .workflows import create_request


class RequestActionHttpTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        self.setup_wallet_environment()
        self.requester = self.customer('request_action_owner')
        self.payer = self.customer('request_action_payer')
        self.fund(self.payer, '20.00')

    def tearDown(self):
        self.assertEqual(verify_ledger(), [])
        super().tearDown()

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

    def financial_state(self):
        models = (Operation, Journal, Entry, Reservation, Wallet, LedgerAccount, DailyCounter)
        return {model._meta.label: list(model.objects.order_by('pk').values()) for model in models}

    def post_action(self, item, action, version):
        return self.client.post(
            f'/requests/{item.pk}/{action}/',
            urlencode({'version': version}),
            content_type='application/x-www-form-urlencoded',
        )

    def test_requester_can_cancel_current_request_but_payer_and_stale_version_cannot(self):
        item = create_request(self.requester, self.payer.handle, '5.00', 'Cancel this exact request')
        original_version = item.version
        before = self.financial_state()

        self.login_as(self.payer)
        self.assertEqual(self.post_action(item, 'cancel', original_version).status_code, 403)
        item.refresh_from_db()
        self.assertEqual((item.state, item.version), ('OPEN', original_version))

        self.login_as(self.requester)
        self.assertEqual(self.post_action(item, 'cancel', original_version - 1).status_code, 409)
        item.refresh_from_db()
        self.assertEqual((item.state, item.version), ('OPEN', original_version))

        response = self.post_action(item, 'cancel', original_version)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response['Location'], f'/requests/{item.pk}/')
        item.refresh_from_db()
        self.assertEqual((item.state, item.version), ('CANCELLED', original_version + 1))
        self.assertIsNone(item.operation_id)
        self.assertEqual(self.financial_state(), before)

    def test_named_payer_can_decline_current_request_but_requester_and_stale_version_cannot(self):
        item = create_request(self.requester, self.payer.handle, '5.00', 'Decline this exact request')
        original_version = item.version
        before = self.financial_state()

        self.login_as(self.requester)
        self.assertEqual(self.post_action(item, 'decline', original_version).status_code, 403)
        item.refresh_from_db()
        self.assertEqual((item.state, item.version), ('OPEN', original_version))

        self.login_as(self.payer)
        self.assertEqual(self.post_action(item, 'decline', original_version - 1).status_code, 409)
        item.refresh_from_db()
        self.assertEqual((item.state, item.version), ('OPEN', original_version))

        response = self.post_action(item, 'decline', original_version)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response['Location'], f'/requests/{item.pk}/')
        item.refresh_from_db()
        self.assertEqual((item.state, item.version), ('DECLINED', original_version + 1))
        self.assertIsNone(item.operation_id)
        self.assertEqual(self.financial_state(), before)
