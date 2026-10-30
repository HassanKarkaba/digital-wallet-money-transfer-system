"""Real PostgreSQL regressions for accepted keys from before intent-ID retention."""

import importlib
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import psycopg
from django.conf import settings
from django.db import connection, connections
from django.db.migrations.loader import MigrationLoader
from django.test import TransactionTestCase

from accounts.models import UserSession
from accounts.services import db_now
from config.health import enforcement_ready
from ledger.models import DailyCounter, Entry, Journal, LedgerAccount, Reservation, Wallet
from testsupport import WalletFixtures
from work.models import Job

from . import workflows
from .errors import DomainError
from .models import Intent, LegacyIntentReference, MoneyRequest, Operation, ReplayKey
from .reconciliation import verify_ledger
from .replay import capture_legacy_references, match_accepted_reference, purge_intent_batch


class LegacyAcceptedReferenceTests(WalletFixtures, TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        self.setup_wallet_environment()
        self.owner = self.customer('legacy_owner')
        self.other = self.customer('legacy_other')
        self.fund(self.owner, '30.00')

    def tearDown(self):
        self.assertEqual(verify_ledger(), [])
        super().tearDown()

    def accepted(self, kind='TRANSFER', instruction=None, *, owner=None, key=None, legacy=True):
        owner = owner or self.owner
        if instruction is None:
            instruction = {'recipient_handle': self.other.handle, 'amount': '2.00'}
        intent = self.proof(owner, kind, instruction)
        key = key or uuid.uuid4()
        if legacy:
            # Reproduce the old coordinator's initial NULL insert. The lifetime
            # mapping remains immutable; no trigger is disabled or row rebound.
            create = ReplayKey.objects.create

            def create_legacy(**values):
                values['intent_id'] = None
                return create(**values)

            with patch('payments.workflows.ReplayKey.objects.create', side_effect=create_legacy):
                target = workflows.confirm_intent(owner, intent.pk, key)
        else:
            target = workflows.confirm_intent(owner, intent.pk, key)
        intent.refresh_from_db()
        self.assertIsNotNone(intent.consumed_at)
        mapping = ReplayKey.objects.get(owner=owner, family=kind, action_key=key)
        self.assertEqual(mapping.intent_id, None if legacy else intent.pk)
        return intent, key, target, mapping

    def age(self, intent, now):
        cutoff = now - timedelta(days=7)
        Intent.objects.filter(pk=intent.pk).update(
            created_at=cutoff - timedelta(seconds=100),
            expires_at=cutoff + timedelta(seconds=20),
            consumed_at=cutoff,
        )
        intent.refresh_from_db()

    def financial_state(self):
        models = (
            Operation,
            Journal,
            Entry,
            Wallet,
            LedgerAccount,
            DailyCounter,
            Reservation,
            Job,
            MoneyRequest,
            ReplayKey,
            LegacyIntentReference,
        )
        return {model._meta.label: list(model.objects.order_by('pk').values()) for model in models}

    def assert_reference_error(self, status, owner, key, intent_id=None, family=None):
        with self.assertRaises(DomainError) as error:
            match_accepted_reference(owner, key, intent_id=intent_id, family=family)
        self.assertEqual(error.exception.status, status)

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

    def post_confirmation(self, path, intent, key):
        return self.client.post(
            path,
            urlencode(
                {
                    'intent_id': str(intent.pk),
                    'action_key': str(key),
                    'policy_version': 'DWMT-P1',
                    'schema_version': '1',
                }
            ),
            content_type='application/x-www-form-urlencoded',
        )

    def test_historical_migration_and_seven_day_purge_keep_original_protocol_reference(self):
        intent, key, operation, mapping = self.accepted()
        module = importlib.import_module('payments.migrations.0003_legacy_intent_reference')
        historical_apps = (
            MigrationLoader(connections['default'])
            .project_state([('payments', '0003_legacy_intent_reference')])
            .apps
        )
        with self.assertNumQueries(0, using='default'), self.assertNumQueries(0, using='simulator'):
            module.preserve_legacy_references(
                historical_apps, SimpleNamespace(connection=connections['simulator'])
            )
        self.assertFalse(LegacyIntentReference.objects.exists())
        with self.assertNumQueries(0, using='simulator'):
            module.preserve_legacy_references(
                historical_apps, SimpleNamespace(connection=connections['default'])
            )
        reference = LegacyIntentReference.objects.get(pk=intent.pk)
        self.assertEqual(
            (reference.owner_id, reference.family, reference.digest),
            (self.owner.pk, intent.kind, intent.digest),
        )
        now = db_now()
        self.age(intent, now)
        self.assertEqual(purge_intent_batch(now), 1)
        self.assertFalse(Intent.objects.filter(pk=intent.pk).exists())
        mapping.refresh_from_db()
        self.assertIsNone(mapping.intent_id)

        before = self.financial_state()
        self.assertEqual(workflows.confirm_intent(self.owner, intent.pk, key).pk, operation.pk)
        self.login_as(self.owner)
        response = self.post_confirmation('/transfers/confirm/', intent, key)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response['Location'], f'/payments/{operation.pk}/')
        status = self.client.get(f'/payments/actions/TRANSFER/{key}/', HTTP_ACCEPT='application/json')
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['reference'], str(operation.pk))
        self.assertEqual(status.json()['state'], 'COMPLETED')
        old_review = self.client.get(f'/payments/intents/{intent.pk}/')
        self.assertEqual(old_review.status_code, 410)
        self.assertNotContains(old_review, 'name="action_key"', status_code=410)
        self.assertEqual(self.financial_state(), before)

    def test_owned_fingerprint_does_not_authorize_foreign_or_changed_instructions(self):
        first, key, _, _ = self.accepted()
        changed, _, _, _ = self.accepted(
            instruction={'recipient_handle': self.other.handle, 'amount': '3.00'}
        )
        destination, _, _, _ = self.accepted('DESTINATION', {'label': 'Legacy synthetic destination'})
        self.fund(self.other, '10.00')
        foreign, _, _, _ = self.accepted(
            instruction={'recipient_handle': self.owner.handle, 'amount': '1.00'}, owner=self.other
        )
        self.assertEqual(capture_legacy_references([first, changed, destination, foreign]), 4)
        self.assertNotEqual(first.digest, changed.digest)
        before = self.financial_state()
        self.assert_reference_error(404, self.other, key, first.pk, 'TRANSFER')
        self.assert_reference_error(404, self.owner, key, foreign.pk, 'TRANSFER')
        self.assert_reference_error(404, self.owner, uuid.uuid4(), first.pk, 'TRANSFER')
        self.assert_reference_error(404, self.owner, key, uuid.uuid4(), 'TRANSFER')
        self.assert_reference_error(409, self.owner, key, changed.pk, 'TRANSFER')
        self.assert_reference_error(409, self.owner, key, first.pk, 'DESTINATION')
        self.assertEqual(self.financial_state(), before)

    def test_one_action_key_in_two_families_uses_owned_fingerprint_to_disambiguate(self):
        key = uuid.uuid4()
        transfer, _, operation, transfer_map = self.accepted(key=key)
        destination, _, target, destination_map = self.accepted(
            'DESTINATION', {'label': 'Same key separate family'}, key=key
        )
        self.assertEqual(capture_legacy_references([transfer, destination]), 2)
        self.assertEqual(match_accepted_reference(self.owner, key, transfer.pk).pk, transfer_map.pk)
        self.assertEqual(match_accepted_reference(self.owner, key, destination.pk).pk, destination_map.pk)
        self.assert_reference_error(409, self.owner, key)
        now = db_now()
        self.age(transfer, now)
        self.age(destination, now)
        self.assertEqual(purge_intent_batch(now), 2)
        before = self.financial_state()
        self.assertEqual(workflows.confirm_intent(self.owner, transfer.pk, key).pk, operation.pk)
        self.assertEqual(workflows.confirm_intent(self.owner, destination.pk, key).pk, target.pk)
        self.assertEqual(self.financial_state(), before)

    def test_identical_consumed_candidates_are_both_retained_without_rebinding_null_maps(self):
        first, first_key, first_operation, first_map = self.accepted()
        second, second_key, second_operation, second_map = self.accepted()
        self.assertEqual(first.digest, second.digest)
        self.assertNotEqual(first_operation.pk, second_operation.pk)
        self.assertEqual(capture_legacy_references([first, second]), 2)
        self.assertEqual(capture_legacy_references([second, first]), 0)
        self.assertCountEqual(
            LegacyIntentReference.objects.values_list('pk', flat=True), [first.pk, second.pk]
        )
        now = db_now()
        self.age(first, now)
        self.age(second, now)
        self.assertEqual(purge_intent_batch(now, batch_size=1), 1)
        self.assertEqual(Intent.objects.filter(pk__in=[first.pk, second.pk]).count(), 1)
        self.assertEqual(purge_intent_batch(now, batch_size=1), 1)
        before = self.financial_state()
        self.assertEqual(workflows.confirm_intent(self.owner, first.pk, first_key).pk, first_operation.pk)
        self.assertEqual(workflows.confirm_intent(self.owner, second.pk, second_key).pk, second_operation.pk)
        first_map.refresh_from_db()
        second_map.refresh_from_db()
        self.assertIsNone(first_map.intent_id)
        self.assertIsNone(second_map.intent_id)
        self.assertEqual(self.financial_state(), before)

    def test_explicit_new_mapping_refuses_an_alternate_legacy_uuid_even_with_same_digest(self):
        legacy, _, _, _ = self.accepted()
        normal, key, operation, mapping = self.accepted(legacy=False)
        self.assertEqual(legacy.digest, normal.digest)
        self.assertEqual(capture_legacy_references([legacy]), 1)
        now = db_now()
        self.age(legacy, now)
        self.age(normal, now)
        self.assertEqual(purge_intent_batch(now), 2)
        before = self.financial_state()
        self.assert_reference_error(409, self.owner, key, legacy.pk, 'TRANSFER')
        with self.assertRaises(DomainError) as error:
            workflows.confirm_intent(self.owner, legacy.pk, key, expected_kind='TRANSFER')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(workflows.confirm_intent(self.owner, normal.pk, key).pk, operation.pk)
        mapping.refresh_from_db()
        self.assertEqual(mapping.intent_id, normal.pk)
        self.assertEqual(self.financial_state(), before)

    def test_request_payment_replay_stays_bound_to_exact_linked_request_after_purge(self):
        request = workflows.create_request(self.other, self.owner.handle, '2.00', 'Legacy request payment')
        different = workflows.create_request(self.other, self.owner.handle, '2.00', 'Another exact request')
        intent, key, operation, _ = self.accepted('REQUEST_PAY', {'request_id': str(request.pk)})
        request.refresh_from_db()
        self.assertEqual(request.operation_id, operation.pk)
        now = db_now()
        self.age(intent, now)
        self.assertEqual(purge_intent_batch(now), 1)
        before = self.financial_state()
        self.login_as(self.owner)
        response = self.post_confirmation(f'/requests/{request.pk}/pay/', intent, key)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response['Location'], f'/payments/{operation.pk}/')
        altered = self.post_confirmation(f'/requests/{different.pk}/pay/', intent, key)
        self.assertEqual(altered.status_code, 409)
        request.refresh_from_db()
        different.refresh_from_db()
        self.assertEqual(request.operation_id, operation.pk)
        self.assertIsNone(different.operation_id)
        self.assertEqual(self.financial_state(), before)

    def test_capture_ignores_unconsumed_or_unmatched_candidates_and_nondefault_alias(self):
        consumed, _, _, _ = self.accepted()
        pending = self.proof(
            self.owner, 'TRANSFER', {'recipient_handle': self.other.handle, 'amount': '2.00'}
        )
        normal, _, _, _ = self.accepted(
            instruction={'recipient_handle': self.other.handle, 'amount': '4.00'}, legacy=False
        )
        self.assertEqual(capture_legacy_references([pending, normal]), 0)
        self.assertFalse(LegacyIntentReference.objects.exists())
        with self.assertNumQueries(0, using='default'), self.assertNumQueries(0, using='simulator'):
            self.assertEqual(capture_legacy_references([consumed], using='simulator'), 0)
            self.assertEqual(purge_intent_batch(consumed.consumed_at, using='simulator'), 0)
        self.assertEqual(capture_legacy_references([consumed]), 1)
        self.assertFalse(LegacyIntentReference.objects.filter(pk__in=[pending.pk, normal.pk]).exists())

    def test_conflicting_retained_fingerprint_blocks_capture_and_atomic_purge(self):
        intent, _, _, _ = self.accepted()
        wrong_digest = '0' * 64 if intent.digest != '0' * 64 else '1' * 64
        reference = LegacyIntentReference.objects.create(
            id=intent.pk, owner=self.owner, family=intent.kind, digest=wrong_digest
        )
        now = db_now()
        self.age(intent, now)
        before = self.financial_state()
        with self.assertRaises(DomainError) as capture_error:
            capture_legacy_references([intent])
        self.assertEqual(capture_error.exception.status, 409)
        with self.assertRaises(DomainError) as purge_error:
            purge_intent_batch(now)
        self.assertEqual(purge_error.exception.status, 409)
        self.assertTrue(Intent.objects.filter(pk=intent.pk).exists())
        reference.refresh_from_db()
        self.assertEqual(reference.digest, wrong_digest)
        self.assertEqual(self.financial_state(), before)

    def test_actual_runtime_role_can_append_and_read_but_cannot_change_legacy_references(self):
        intent, _, _, _ = self.accepted()
        runtime = settings.TEST_RUNTIME_DATABASES['default']
        with psycopg.connect(
            dbname=connection.settings_dict['NAME'],
            host=runtime['HOST'],
            port=runtime['PORT'],
            user=runtime['USER'],
            password=runtime['PASSWORD'],
            autocommit=True,
            connect_timeout=2,
        ) as runtime_connection:
            privileges = runtime_connection.execute(
                """SELECT current_user,
                has_table_privilege('public.payments_legacyintentreference','SELECT'),
                has_table_privilege('public.payments_legacyintentreference','INSERT'),
                has_table_privilege('public.payments_legacyintentreference','UPDATE'),
                has_table_privilege('public.payments_legacyintentreference','DELETE')"""
            ).fetchone()
            self.assertEqual(privileges, ('wallet_runtime', True, True, False, False))
            runtime_connection.execute(
                'INSERT INTO public.payments_legacyintentreference(id,owner_id,family,digest) '
                'VALUES (%s,%s,%s,%s)',
                [intent.pk, intent.owner_id, intent.kind, intent.digest],
            )
            retained = runtime_connection.execute(
                'SELECT owner_id,family,digest FROM public.payments_legacyintentreference WHERE id=%s',
                [intent.pk],
            ).fetchone()
            self.assertEqual(retained, (intent.owner_id, intent.kind, intent.digest))
            before = self.financial_state()
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                runtime_connection.execute(
                    'UPDATE public.payments_legacyintentreference SET digest=%s WHERE id=%s',
                    ['0' * 64, intent.pk],
                )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                runtime_connection.execute(
                    'DELETE FROM public.payments_legacyintentreference WHERE id=%s', [intent.pk]
                )
        self.assertTrue(runtime_connection.closed)
        self.assertEqual(self.financial_state(), before)

    def test_readiness_detects_disabled_legacy_reference_immutability_trigger(self):
        self.assertTrue(enforcement_ready(check_runtime_role=False))
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'ALTER TABLE public.payments_legacyintentreference '
                    'DISABLE TRIGGER immutable_legacy_intent_reference'
                )
            self.assertFalse(enforcement_ready(check_runtime_role=False))
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    'ALTER TABLE public.payments_legacyintentreference '
                    'ENABLE TRIGGER immutable_legacy_intent_reference'
                )
            self.assertTrue(enforcement_ready(check_runtime_role=False))
