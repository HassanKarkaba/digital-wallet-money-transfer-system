import hashlib
import hmac
import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.test import TransactionTestCase

from accounts.models import User
from accounts.services import db_now
from rails.adapter import RailConflict, create_or_get, initialize_identity, lookup
from rails.models import CallbackInbox, Destination
from simulator.models import ProviderEffect, ProviderEvent


class IndependentSimulatorTests(TransactionTestCase):
    databases = {'default', 'simulator'}

    def setUp(self):
        initialize_identity()
        self.owner = User.objects.create_user(
            'rail@test.test',
            password='A long synthetic password 2026',
            handle='railowner',
            display_name='Rail Owner',
            verified_at=db_now(),
            mfa_confirmed=True,
        )
        self.destination = Destination.objects.create(
            owner=self.owner, label='Test synthetic token', activation_at=db_now() - timedelta(seconds=1)
        )

    def operation(self, scenario='SUCCESS'):
        from types import SimpleNamespace

        self.destination.scenario = scenario
        self.destination.save(update_fields=['scenario'])
        key = f'{settings.ENVIRONMENT_ID}:{uuid.uuid4()}:FUNDING'
        return SimpleNamespace(
            id=uuid.uuid4(),
            provider_key=key,
            actor_id=self.owner.pk,
            destination=self.destination,
            kind='FUNDING',
            amount_cents=1234,
            currency='USD',
        )

    def test_provider_key_is_idempotent_and_conflicting_instruction_rejected(self):
        operation = self.operation()
        one = create_or_get(operation)
        two = create_or_get(operation)
        self.assertEqual(one, two)
        self.assertEqual(
            ProviderEffect.objects.using('simulator').filter(provider_key=operation.provider_key).count(), 1
        )
        self.assertEqual(ProviderEvent.objects.using('simulator').count(), 1)
        operation.amount_cents += 1
        with self.assertRaises(RailConflict):
            create_or_get(operation)

    def test_timeout_after_independent_commit_is_recoverable_by_read_only_lookup(self):
        operation = self.operation('TIMEOUT_AFTER_COMMIT')
        with self.assertRaises(TimeoutError):
            create_or_get(operation)
        self.assertEqual(lookup(operation.provider_key)['state'], 'SUCCESS')
        self.assertEqual(create_or_get(operation)['state'], 'SUCCESS')
        self.assertEqual(
            ProviderEffect.objects.using('simulator').filter(provider_key=operation.provider_key).count(), 1
        )

    def test_malformed_after_commit_keeps_authoritative_record(self):
        operation = self.operation('MALFORMED_AFTER_COMMIT')
        self.assertEqual(create_or_get(operation), {'schema_version': 1, 'state': 'INVALID'})
        self.assertEqual(lookup(operation.provider_key)['amount_cents'], 1234)

    def test_final_decline_never_becomes_success(self):
        operation = self.operation('DECLINE')
        self.assertEqual(create_or_get(operation)['state'], 'DECLINED')
        self.assertEqual(lookup(operation.provider_key)['state'], 'DECLINED')


class CallbackAuthenticationTests(TransactionTestCase):
    def test_invalid_hmac_and_stale_delivery_are_rejected_before_schema_lookup(self):
        body = b'{}'
        timestamp = str(int(db_now().timestamp()))
        signature = hmac.new(
            str(settings.CALLBACK_HMAC_KEY).encode(), timestamp.encode() + b'\n' + body, hashlib.sha256
        ).hexdigest()
        response = self.client.post(
            '/integrations/simulator/events/',
            data=body,
            content_type='application/json',
            HTTP_X_DWMT_KEY_ID='v1',
            HTTP_X_DWMT_TIMESTAMP=timestamp,
            HTTP_X_DWMT_SIGNATURE='0' * 64,
        )
        self.assertEqual(response.status_code, 401)
        stale = str(int(db_now().timestamp()) - 301)
        stale_signature = hmac.new(
            str(settings.CALLBACK_HMAC_KEY).encode(), stale.encode() + b'\n' + body, hashlib.sha256
        ).hexdigest()
        response = self.client.post(
            '/integrations/simulator/events/',
            data=body,
            content_type='application/json',
            HTTP_X_DWMT_KEY_ID='v1',
            HTTP_X_DWMT_TIMESTAMP=stale,
            HTTP_X_DWMT_SIGNATURE=stale_signature,
        )
        self.assertEqual(response.status_code, 401)
        response = self.client.post(
            '/integrations/simulator/events/',
            data=body,
            content_type='application/json',
            HTTP_X_DWMT_KEY_ID='v1',
            HTTP_X_DWMT_TIMESTAMP=timestamp,
            HTTP_X_DWMT_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(CallbackInbox.objects.count(), 0)


class RailAccountingBoundaryTests(TransactionTestCase):
    """Exercise independent provider commitment and protected primary settlement."""

    databases = {'default', 'simulator'}

    def setUp(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        import pyotp
        from django.test import override_settings

        from accounts.services import encrypt_secret
        from ledger.models import IntegrityGate, LedgerAccount
        from ledger.posting import open_wallet

        self.private = TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        marker = Path(self.private.name) / 'marker.json'
        marker.write_text(
            json.dumps(
                {'environment_id': str(settings.ENVIRONMENT_ID), 'generation': settings.RECOVERY_GENERATION}
            )
        )
        self.override = override_settings(RECOVERY_MARKER_PATH=marker)
        self.override.enable()
        self.addCleanup(self.override.disable)
        IntegrityGate.objects.update_or_create(
            pk=1,
            defaults={
                'environment_id': settings.ENVIRONMENT_ID,
                'recovery_generation': settings.RECOVERY_GENERATION,
                'is_open': True,
                'reason': 'Isolated test environment.',
            },
        )
        LedgerAccount.objects.get_or_create(kind='SYSTEM_CASH', defaults={'balance_cents': 0})
        initialize_identity()
        self.secret = pyotp.random_base32()
        self.owner = User.objects.create_user(
            'boundary@rail.test',
            password='A long synthetic password 2026',
            handle='railboundary',
            display_name='Rail Boundary',
            verified_at=db_now(),
            mfa_confirmed=True,
            mfa_secret=encrypt_secret(self.secret),
        )
        self.wallet = open_wallet(self.owner)
        self.destination = Destination.objects.create(
            owner=self.owner, label='Synthetic boundary token', activation_at=db_now() - timedelta(seconds=1)
        )

    def admit_funding(self, scenario='SUCCESS'):
        import pyotp

        from payments.workflows import confirm_intent, create_intent, verify_intent

        self.destination.scenario = scenario
        self.destination.save(update_fields=['scenario'])
        intent = create_intent(
            self.owner, 'FUNDING', {'destination_id': str(self.destination.pk), 'amount': '12.34'}
        )
        verify_intent(self.owner, intent.pk, pyotp.TOTP(self.secret).now())
        return confirm_intent(self.owner, intent.pk, uuid.uuid4())

    def post_event(self, data):
        body = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        timestamp = str(int(db_now().timestamp()))
        signature = hmac.new(
            str(settings.CALLBACK_HMAC_KEY).encode(), timestamp.encode() + b'\n' + body, hashlib.sha256
        ).hexdigest()
        return self.client.post(
            '/integrations/simulator/events/',
            data=body,
            content_type='application/json',
            HTTP_X_DWMT_KEY_ID='v1',
            HTTP_X_DWMT_TIMESTAMP=timestamp,
            HTTP_X_DWMT_SIGNATURE=signature,
        )

    def test_lost_provider_reply_keeps_capacity_and_duplicate_observation_posts_once(self):
        from ledger.models import Journal, Reservation
        from payments.workflows import apply_provider_observation, dispatch_operation
        from work.models import Job

        operation = self.admit_funding('TIMEOUT_AFTER_COMMIT')
        dispatch_operation(operation.pk)
        operation.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(operation.state, 'UNKNOWN')
        self.assertEqual((self.wallet.available_cents, self.wallet.incoming_cents), (0, 1234))
        self.assertEqual(
            ProviderEffect.objects.using('simulator')
            .filter(provider_key=operation.provider_key, state='SUCCESS')
            .count(),
            1,
        )
        result = lookup(operation.provider_key)
        self.assertEqual(self.post_event(result).status_code, 202)
        self.assertEqual(self.post_event(result).status_code, 200)
        self.assertEqual(Job.objects.filter(business_key='callback:' + result['event_id']).count(), 1)
        apply_provider_observation(operation.pk, result)
        apply_provider_observation(operation.pk, result)
        operation.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(operation.state, 'COMPLETED')
        self.assertEqual((self.wallet.available_cents, self.wallet.incoming_cents), (1234, 0))
        self.assertEqual(Journal.objects.filter(operation=operation, effect_kind='FUNDING').count(), 1)
        self.assertEqual(Reservation.objects.get(operation=operation, kind='INCOMING').state, 'CONSUMED')

    def test_stale_pending_after_success_does_not_regress_or_close_gate(self):
        from ledger.models import IntegrityGate, Journal
        from oversight.models import Finding
        from payments.workflows import apply_provider_observation, dispatch_operation

        operation = self.admit_funding()
        dispatch_operation(operation.pk)
        pending = lookup(operation.provider_key)
        pending['state'] = 'PENDING'
        apply_provider_observation(operation.pk, pending)
        operation.refresh_from_db()
        self.assertEqual(operation.state, 'COMPLETED')
        self.assertTrue(IntegrityGate.objects.get(pk=1).is_open)
        self.assertFalse(Finding.objects.filter(operation=operation).exists())
        self.assertEqual(Journal.objects.filter(operation=operation).count(), 1)

    def test_opposite_final_closes_gate_without_rewriting_committed_money(self):
        from ledger.models import IntegrityGate, Journal
        from oversight.models import Finding
        from payments.workflows import apply_provider_observation, dispatch_operation

        operation = self.admit_funding()
        dispatch_operation(operation.pk)
        contradiction = lookup(operation.provider_key)
        contradiction['state'] = 'DECLINED'
        apply_provider_observation(operation.pk, contradiction)
        self.wallet.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual((operation.state, self.wallet.available_cents), ('COMPLETED', 1234))
        self.assertFalse(IntegrityGate.objects.get(pk=1).is_open)
        self.assertTrue(
            Finding.objects.filter(
                operation=operation, classification='CONTRADICTORY_TERMINAL', state='OPEN'
            ).exists()
        )
        self.assertEqual(Journal.objects.filter(operation=operation).count(), 1)

    def test_trustworthy_mismatched_callback_closes_gate_without_posting(self):
        from ledger.models import IntegrityGate, Journal
        from payments.workflows import dispatch_operation

        operation = self.admit_funding('TIMEOUT_AFTER_COMMIT')
        dispatch_operation(operation.pk)
        mismatch = lookup(operation.provider_key)
        mismatch['amount_cents'] += 1
        self.assertEqual(self.post_event(mismatch).status_code, 409)
        self.assertFalse(IntegrityGate.objects.get(pk=1).is_open)
        self.assertFalse(Journal.objects.filter(operation=operation).exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.incoming_cents, 1234)

    def test_expired_worker_is_fenced_before_independent_provider_call(self):
        from payments.errors import DomainError
        from payments.workflows import dispatch_operation
        from work.models import Job
        from work.services import claim_jobs

        operation = self.admit_funding()
        first = next(
            job for job in claim_jobs() if job.object_id == str(operation.pk) and job.kind == 'DISPATCH'
        )
        Job.objects.filter(pk=first.pk).update(lease_until=db_now() - timedelta(seconds=1))
        second = next(job for job in claim_jobs() if job.pk == first.pk)
        with self.assertRaises(DomainError) as caught:
            dispatch_operation(operation.pk, job_id=first.pk, generation=first.generation)
        self.assertEqual(caught.exception.code, 'STALE')
        self.assertFalse(
            ProviderEffect.objects.using('simulator').filter(provider_key=operation.provider_key).exists()
        )
        dispatch_operation(operation.pk, job_id=second.pk, generation=second.generation)
        operation.refresh_from_db()
        self.assertEqual(operation.state, 'COMPLETED')
