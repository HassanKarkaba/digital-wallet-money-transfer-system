from collections import Counter
from html.parser import HTMLParser

from django.test import TransactionTestCase

from accounts.models import UserSession
from accounts.services import db_now
from payments.reconciliation import verify_ledger
from payments.workflows import open_dispute
from testsupport import WalletFixtures


class CaseFormMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = []
        self.controls = []
        self.forms = []
        self.details_depth = 0

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if values.get('id'):
            self.ids.append(values['id'])
        if tag == 'form':
            self.forms.append(values.get('action', ''))
        if tag == 'details':
            self.details_depth += 1
        action = self.forms[-1] if self.forms else None
        if tag == 'label' and values.get('for'):
            self.labels.append((action, values['for']))
        if tag in {'input', 'select', 'textarea'}:
            self.controls.append(
                {
                    'action': action,
                    'name': values.get('name'),
                    'id': values.get('id'),
                    'details_depth': self.details_depth,
                }
            )

    def handle_endtag(self, tag):
        if tag == 'form':
            self.forms.pop()
        if tag == 'details':
            self.details_depth -= 1


class CaseFormAccessibilityTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        self.setup_wallet_environment()
        self.owner = self.customer('case_form_owner')
        self.recipient = self.customer('case_form_recipient')
        self.staff = self.customer('case_form_staff', roles=('SUPPORT', 'REVIEWER', 'FINANCE'))
        self.fund(self.owner, '20.00')

    def tearDown(self):
        self.assertEqual(verify_ledger(), [])
        super().tearDown()

    def test_rendered_case_forms_have_unique_ids_and_correct_reason_labels(self):
        operation = self.admit(
            self.owner, 'TRANSFER', {'recipient_handle': self.recipient.handle, 'amount': '5.00'}
        )
        case = open_dispute(self.owner, operation.pk, 'Inspect this completed synthetic transfer.')
        self.client.force_login(self.staff)
        now = db_now()
        UserSession.objects.create(
            user=self.staff,
            session_key=self.client.session.session_key,
            started_at=now,
            last_seen=now,
            authority_version=self.staff.authority_version,
        )
        response = self.client.get(f'/staff/cases/{case.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['can_decide'])
        self.assertTrue(response.context['can_propose'])
        self.assertTrue(response.context['can_assign_finance'])
        markup = CaseFormMarkup()
        markup.feed(response.content.decode())
        counts = Counter(markup.ids)
        self.assertEqual(len(markup.ids), len(counts), 'Every rendered HTML id must be unique')
        for _, target in markup.labels:
            self.assertEqual(counts[target], 1, 'Each label must identify exactly one control')

        expected = {
            f'/staff/cases/{case.pk}/decide/': ('decision_reason', 0),
            f'/staff/disputes/{case.pk}/propose-reversal/': ('proposal_reason', 0),
            f'/staff/cases/{case.pk}/assign-finance/': ('assignment_reason', 1),
        }
        for action, (identifier, details_depth) in expected.items():
            with self.subTest(action=action):
                reasons = [
                    item for item in markup.controls if item['action'] == action and item['name'] == 'reason'
                ]
                self.assertEqual(len(reasons), 1)
                self.assertEqual(reasons[0]['id'], identifier)
                self.assertEqual(reasons[0]['details_depth'], details_depth)
                self.assertEqual(markup.labels.count((action, identifier)), 1)
