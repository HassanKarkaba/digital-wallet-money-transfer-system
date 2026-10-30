"""Rendered multi-session forms keep labels attached to their own controls."""

from html.parser import HTMLParser

from django.test import Client, TransactionTestCase
from django.urls import reverse

from testsupport import WalletFixtures

from .models import UserSession
from .services import db_now


class SessionFormMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = []
        self.forms = []
        self.current_form = None

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if attributes.get('id'):
            self.ids.append(attributes['id'])
        if tag == 'label' and attributes.get('for'):
            self.labels.append(attributes['for'])
        if tag == 'form':
            self.current_form = {'action': attributes.get('action'), 'controls': []}
            self.forms.append(self.current_form)
        elif self.current_form is not None and tag in {'input', 'textarea', 'select', 'button'}:
            self.current_form['controls'].append((tag, attributes))

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current_form = None


class SessionFormRenderingTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def test_two_owned_session_forms_have_unique_public_ids_and_unchanged_post_fields(self):
        self.setup_wallet_environment()
        user = self.customer('session_forms')
        other_client = Client()
        tracked = []
        for client in (self.client, other_client):
            client.force_login(user)
            now = db_now()
            tracked.append(
                UserSession.objects.create(
                    user=user,
                    session_key=client.session.session_key,
                    started_at=now,
                    last_seen=now,
                    authority_version=user.authority_version,
                )
            )
        response = self.client.get(reverse('accounts:sessions'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This session', count=1)
        self.assertContains(response, 'Another session', count=1)
        content = response.content.decode()
        markup = SessionFormMarkup()
        markup.feed(content)
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        self.assertTrue(all(markup.ids.count(target) == 1 for target in markup.labels))
        expected = {reverse('accounts:session_revoke', args=[item.pk]): item for item in tracked}
        forms = {form['action']: form for form in markup.forms if form['action'] in expected}
        self.assertEqual(set(forms), set(expected))
        for action, item in expected.items():
            controls = forms[action]['controls']
            fields = {
                attributes.get('name'): attributes
                for tag, attributes in controls
                if tag == 'input' and attributes.get('name') in {'password', 'otp'}
            }
            self.assertEqual(set(fields), {'password', 'otp'})
            for name, attributes in fields.items():
                self.assertEqual(attributes['id'], f'session_{item.pk}_{name}')
                self.assertEqual(markup.labels.count(attributes['id']), 1)
            self.assertEqual(
                sum(tag == 'button' and attributes.get('type') == 'submit' for tag, attributes in controls),
                1,
            )
        self.assertFalse(
            any(item.session_key in content for item in tracked),
            'Authentication session keys must not appear in rendered form identifiers.',
        )
