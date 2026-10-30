"""Real-PostgreSQL account checks; financial outcomes are tested by their owners."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pyotp
from cryptography.fernet import Fernet
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings

from . import services
from .forms import RegistrationForm
from .models import AccountToken, RateBucket, User, UserSession


@override_settings(MFA_ENCRYPTION_KEYS={'v1': Fernet.generate_key().decode()}, MFA_KEY_VERSION='v1')
class AccountSecurityTests(TransactionTestCase):
    def setUp(self):
        self.secret = pyotp.random_base32()
        self.user = User.objects.create_user(
            email='alice@wallet.test',
            handle='alice',
            display_name='Alice',
            password='A quiet river has many stones 8047',
            verified_at=services.db_now(),
            mfa_confirmed=True,
            mfa_secret=services.encrypt_secret(self.secret),
        )

    def test_totp_counter_is_single_use_across_independent_connections(self):
        barrier = threading.Barrier(2)
        instant = datetime(2026, 10, 30, 12, 0, 1, tzinfo=timezone.utc)
        code = pyotp.TOTP(self.secret).at(instant)
        prefix = 'dwmt-totp-' + uuid.uuid4().hex

        def attempt(index):
            close_old_connections()
            candidate = User.objects.get(pk=self.user.pk)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('application_name', %s, false)", [prefix + '-' + str(index)]
                )
            barrier.wait(timeout=5)
            try:
                services.verify_totp(candidate, code)
                return 'accepted'
            except services.SecurityError:
                return 'rejected'
            finally:
                close_old_connections()

        with (
            patch('accounts.services.db_now', return_value=instant),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            with transaction.atomic():
                User.objects.select_for_update().get(pk=self.user.pk)
                futures = [pool.submit(attempt, index) for index in range(2)]
                deadline = time.monotonic() + 1.5
                blocked = 0
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_stat_clear_snapshot()')
                        cursor.execute(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE %s AND wait_event_type='Lock'",
                            [prefix + '%'],
                        )
                        blocked = cursor.fetchone()[0]
                    if blocked == 2:
                        break
                    time.sleep(0.01)
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(blocked, 2, 'Both independent proof attempts must reach the held security-row lock.')
        self.assertCountEqual(results, ['accepted', 'rejected'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.mfa_last_counter, int(instant.timestamp()) // 30)
        self.assertNotIn(self.secret, self.user.mfa_secret)

    def test_reset_is_single_use_preserves_mfa_and_revokes_authority(self):
        token = services.create_token(self.user, 'RESET')
        now = services.db_now()
        tracked = UserSession.objects.create(
            user=self.user,
            session_key='synthetic-session',
            authority_version=1,
            started_at=now,
            last_seen=now,
        )
        updated = services.reset_password(token, 'Another secure passphrase with 4930')
        self.assertEqual(updated.authority_version, 2)
        self.assertTrue(updated.mfa_confirmed)
        self.assertEqual(updated.mfa_secret, self.user.mfa_secret)
        tracked.refresh_from_db()
        self.assertIsNotNone(tracked.revoked_at)
        with self.assertRaises(services.SecurityError):
            services.reset_password(token, 'Yet another valid passphrase 2490')
        self.assertEqual(AccountToken.objects.filter(used_at__isnull=False).count(), 1)

    def test_recovery_hash_consumed_and_cooldown_starts_at_recovery(self):
        recovery = 'random-test-recovery-value'
        self.user.recovery_hashes = [make_password(recovery)]
        self.user.save(update_fields=['recovery_hashes'])
        instant = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        with patch('accounts.services.db_now', return_value=instant):
            user = services.recover_mfa(self.user, 'A quiet river has many stones 8047', recovery)
        self.assertFalse(user.mfa_confirmed)
        self.assertEqual(user.mfa_secret, '')
        self.assertEqual(user.recovery_hashes, [])
        self.assertEqual(user.outgoing_cooldown_until, instant + timedelta(hours=24))
        with self.assertRaises(services.SecurityError):
            services.recover_mfa(user, 'A quiet river has many stones 8047', recovery)

    def test_rate_window_expires_at_equality_and_not_before(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        with patch('accounts.services.db_now', return_value=start):
            services.rate_limit('test', 'alice', 2, 60)
            services.rate_limit('test', 'alice', 2, 60)
        with patch(
            'accounts.services.db_now', return_value=start + timedelta(seconds=59, microseconds=999999)
        ):
            with self.assertRaises(services.RateLimited):
                services.rate_limit('test', 'alice', 2, 60)
        with patch('accounts.services.db_now', return_value=start + timedelta(seconds=60)):
            self.assertEqual(services.rate_limit('test', 'alice', 2, 60), 1)
        self.assertEqual(RateBucket.objects.get().started_at, start + timedelta(seconds=60))

    def test_case_insensitive_email_unique_at_database_boundary(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(email='ALICE@wallet.test', handle='alice_other', display_name='Other')

    def test_password_and_field_boundaries(self):
        values = {
            'display_name': 'New User',
            'email': ' new@wallet.test ',
            'handle': 'NEW_USER',
            'password': 'A long password with spaces and café 7032',
        }
        form = RegistrationForm(values)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['email'], 'new@wallet.test')
        self.assertEqual(form.cleaned_data['handle'], 'new_user')
        self.assertEqual(form.cleaned_data['password'], values['password'])
        invalid = RegistrationForm({**values, 'owner_id': str(self.user.pk)})
        self.assertFalse(invalid.is_valid())
        too_long = RegistrationForm({**values, 'password': 'a' * 129})
        self.assertFalse(too_long.is_valid())

    def test_role_change_revokes_sessions_and_invitation_grants_no_authority(self):
        from .models import Role, SecurityMail, StaffInvitation

        admin = User.objects.create_user(
            email='administrator@wallet.test',
            handle='administrator',
            display_name='Administrator',
            password='A separate administrator passphrase 8031',
            verified_at=services.db_now(),
            mfa_confirmed=True,
            mfa_secret=services.encrypt_secret(pyotp.random_base32()),
        )
        Role.objects.create(user=admin, role='ACCESS_ADMIN')
        now = services.db_now()
        tracked = UserSession.objects.create(
            user=self.user,
            session_key='role-test-session',
            authority_version=1,
            started_at=now,
            last_seen=now,
        )
        with transaction.atomic():
            changed = services.change_role(admin, self.user.pk, 'SUPPORT', True, 1)
        self.assertEqual(changed.authority_version, 2)
        tracked.refresh_from_db()
        self.assertIsNotNone(tracked.revoked_at)
        with self.assertRaises(services.SecurityError), transaction.atomic():
            services.change_role(admin, self.user.pk, 'SUPPORT', False, 1)
        self.assertTrue(Role.objects.filter(user=self.user, role='SUPPORT').exists())
        with transaction.atomic():
            first = services.invite_staff_locked(admin, 'future@wallet.test', 'REVIEWER')
            second = services.invite_staff_locked(admin, 'future@wallet.test', 'REVIEWER')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(StaffInvitation.objects.filter(email='future@wallet.test').count(), 1)
        self.assertFalse(User.objects.filter(email='future@wallet.test').exists())
        self.assertEqual(SecurityMail.objects.filter(purpose='STAFF_INVITATION').count(), 1)

    def test_outgoing_recovery_cooldown_ends_at_equality(self):
        end = services.db_now() + timedelta(hours=24)
        self.user.outgoing_cooldown_until = end
        self.user.save(update_fields=['outgoing_cooldown_until'])
        with patch('accounts.services.db_now', return_value=end - timedelta(microseconds=1)):
            with self.assertRaises(services.SecurityError):
                services.assert_eligible(self.user, outgoing=True)
        with patch('accounts.services.db_now', return_value=end):
            self.assertEqual(services.assert_eligible(self.user, outgoing=True), self.user)

    def test_authority_version_invalidates_a_previously_permitted_session(self):
        self.client.force_login(self.user)
        now = services.db_now()
        UserSession.objects.create(
            user=self.user,
            session_key=self.client.session.session_key,
            authority_version=1,
            started_at=now,
            last_seen=now,
        )
        self.assertEqual(self.client.get('/accounts/profile/').status_code, 200)
        User.objects.filter(pk=self.user.pk).update(authority_version=2)
        response = self.client.get('/accounts/profile/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/accounts/login/'))
        self.assertFalse(UserSession.objects.filter(user=self.user, revoked_at__isnull=True).exists())
        self.assertEqual(response['Cache-Control'], 'no-store, private, max-age=0')

    def test_key_rotation_reseals_seeds_and_durable_security_mail_without_disclosure(self):
        from io import StringIO

        from django.core.management import call_command

        from notifications.models import Notification

        old_key = services._keyring()['v1']
        private_body = 'Synthetic private recovery link payload 17b3e9'
        mail = services.queue_security_mail(
            self.user, self.user.email, 'Security reset requested', private_body, 'RESET'
        )
        event = Notification.objects.get(event_key='security-mail:' + str(mail.pk))
        self.assertNotIn(private_body, event.body)
        with override_settings(
            MFA_KEY_VERSION='v2', MFA_ENCRYPTION_KEYS={'v1': old_key, 'v2': Fernet.generate_key().decode()}
        ):
            output = StringIO()
            call_command('rotate_mfa_secrets', batch=100, stdout=output)
            self.user.refresh_from_db()
            mail.refresh_from_db()
            self.assertTrue(self.user.mfa_secret.startswith('v2:'))
            self.assertTrue(mail.encrypted_body.startswith('v2:'))
            self.assertEqual(services.decrypt_secret(self.user.mfa_secret), self.secret)
            self.assertEqual(services.decrypt_secret(mail.encrypted_body), private_body)
            self.assertNotIn(self.secret, output.getvalue())
            self.assertNotIn(private_body, output.getvalue())

    def test_rejected_session_reproof_cannot_refund_its_persistent_attempt_budget(self):
        self.client.force_login(self.user)
        now = services.db_now()
        tracked = UserSession.objects.create(
            user=self.user,
            session_key=self.client.session.session_key,
            authority_version=1,
            started_at=now,
            last_seen=now,
        )
        counter = int(now.timestamp()) // 30
        possible = {
            pyotp.TOTP(self.secret).generate_otp(step)
            for step in (counter - 1, counter, counter + 1, counter + 2)
        }
        invalid = next(f'{value:06d}' for value in range(100) if f'{value:06d}' not in possible)
        payload = {'password': 'A quiet river has many stones 8047', 'otp': invalid}
        route = f'/accounts/sessions/{tracked.pk}/revoke/'
        for _ in range(5):
            self.assertEqual(
                self.client.post(
                    route,
                    __import__('urllib.parse', fromlist=['urlencode']).urlencode(payload),
                    content_type='application/x-www-form-urlencoded',
                ).status_code,
                422,
            )
        limited = self.client.post(
            route,
            __import__('urllib.parse', fromlist=['urlencode']).urlencode(payload),
            content_type='application/x-www-form-urlencoded',
        )
        self.assertEqual(limited.status_code, 429)
        self.assertGreater(int(limited['Retry-After']), 0)
        tracked.refresh_from_db()
        self.assertIsNone(tracked.revoked_at)
        self.assertEqual(
            RateBucket.objects.get(key=services._bucket_key('session-revoke-proof', self.user.pk)).count, 5
        )
