from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from accounts.models import User
from accounts.services import db_now
from notifications.models import Notification
from notifications.services import dispatch, notify


class NotificationPersistenceTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            'notify@test.test',
            password='A long synthetic password 2026',
            handle='notified',
            display_name='Notified',
            verified_at=db_now(),
        )

    def test_event_identity_is_unique_and_dispatch_observes_current_preferences(self):
        one = notify(self.user, 'event-one', 'Completed', 'Demonstration money only.')
        two = notify(self.user, 'event-one', 'Completed', 'Demonstration money only.')
        self.assertEqual(one.pk, two.pk)
        self.user.email_receipts = False
        self.user.save(update_fields=['email_receipts'])
        dispatch(one.pk)
        one.refresh_from_db()
        self.assertEqual(one.delivery_state, 'SKIPPED')
        self.assertEqual(Notification.objects.count(), 1)

    def test_optional_capture_failure_leaves_durable_event(self):
        event = notify(self.user, 'event-failure', 'Completed', 'Demonstration money only.')
        with patch('notifications.services.capture_mail', side_effect=OSError):
            with self.assertRaises(OSError):
                dispatch(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.delivery_state, 'FAILED')
        self.assertEqual(event.attempts, 1)
        with TemporaryDirectory() as folder:
            with override_settings(EMAIL_FILE_PATH=folder):
                dispatch(event.pk)
            self.assertEqual(len(list(Path(folder).glob('*.eml'))), 1)
        event.refresh_from_db()
        self.assertEqual(event.delivery_state, 'DELIVERED')
