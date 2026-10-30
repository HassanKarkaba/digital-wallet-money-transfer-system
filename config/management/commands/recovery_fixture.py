"""Actual restore acceptance controls. Every operation targets isolated test stores."""

import json
import os
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pyotp
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Role, User
from accounts.services import db_now, decrypt_secret, encrypt_secret
from audit.services import record
from config.health import readiness
from ledger.models import DailyCounter, IntegrityGate, Journal, Reservation
from ledger.posting import open_wallet
from oversight.models import Finding
from oversight.recovery import propose_recovery
from payments.errors import DomainError
from payments.models import Operation, ReplayKey
from payments.reconciliation import reconcile, verify_ledger
from payments.workflows import _gate, confirm_intent, create_intent, dispatch_operation, verify_intent
from rails.adapter import lookup
from rails.models import Destination
from simulator.models import ProviderEffect
from work.services import synchronous_demo_bootstrap


def guard():
    if (
        settings.MODE != 'DEMO'
        or settings.DATABASES['default']['NAME'] != 'wallet_test'
        or settings.DATABASES['simulator']['NAME'] != 'simulator_test'
    ):
        raise CommandError('Actual restore fixtures refuse ordinary development databases.')


def fresh_code(user):
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        user.refresh_from_db()
        counter = int(db_now().timestamp()) // 30
        step = max(counter, user.mfa_last_counter + 1)
        if step <= counter + 1:
            return pyotp.TOTP(decrypt_secret(user.mfa_secret)).generate_otp(step)
        time.sleep(min(1, max(0.1, deadline - time.monotonic())))
    raise CommandError('Fresh TOTP did not become available within the bounded fixture wait.')


def confirmed(user, kind, instruction, action_key):
    intent = create_intent(user, kind, instruction)
    verify_intent(user, intent.pk, fresh_code(user))
    return confirm_intent(user, intent.pk, action_key), intent


def persist(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.pending')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Command(BaseCommand):
    help = 'Create and inspect private synthetic fixtures for the actual pg_dump/primary-rollback acceptance script.'

    def add_arguments(self, parser):
        parser.add_argument(
            'phase', choices=['prepare', 'payout', 'mark-expectation', 'assert-mismatch', 'recover']
        )
        parser.add_argument('--file', default='/private/recovery-verification/fixture.json')
        parser.add_argument('--generation', type=int, default=2)

    def handle(self, *args, **options):
        guard()
        path = Path(options['file']).resolve()
        if settings.PRIVATE_ROOT.resolve() not in path.parents:
            raise CommandError('Restore fixture records must remain in designated private storage.')
        phase = options['phase']
        if phase == 'prepare':
            if path.exists():
                raise CommandError('Private restore fixture already exists; refusing a silent replacement.')
            data = self.prepare()
        else:
            if not path.is_file():
                raise CommandError('Private restore fixture is missing.')
            data = json.loads(path.read_text())
            if data['environment_id'] != str(settings.ENVIRONMENT_ID):
                raise CommandError('Restore fixture belongs to another environment.')
            if phase == 'payout':
                self.payout(data)
            elif phase == 'mark-expectation':
                marker = json.loads(settings.RECOVERY_MARKER_PATH.read_text())
                if options['generation'] <= int(marker['generation']):
                    raise CommandError('Expected restore generation must increase.')
                persist(
                    settings.RECOVERY_MARKER_PATH,
                    {'environment_id': str(settings.ENVIRONMENT_ID), 'generation': options['generation']},
                )
                data['expected_generation'] = options['generation']
                data['observations'].append(
                    {
                        'phase': phase,
                        'at': db_now().isoformat(),
                        'expected_generation': options['generation'],
                        'scope': 'External marker only; database generations deliberately remain stale for the next admission check.',
                    }
                )
            elif phase == 'assert-mismatch':
                self.assert_mismatch(data)
            elif phase == 'recover':
                self.recover(data)
        persist(path, data)
        self.stdout.write(
            f'Restore fixture phase {phase} passed. Private observations retained; no access secrets displayed.'
        )

    def prepare(self):
        run_id = uuid.uuid4().hex[:10]
        created = {}
        for name, roles in [('owner', []), ('finance', ['FINANCE']), ('reviewer', ['REVIEWER'])]:
            handle = f'restore_{name}_{run_id}'
            secret = pyotp.random_base32()
            with transaction.atomic():
                user = User.objects.create_user(
                    email=handle + '@dwmt.test',
                    password=secrets.token_urlsafe(32),
                    handle=handle,
                    display_name='Synthetic restore ' + name,
                    verified_at=db_now(),
                    mfa_confirmed=True,
                    mfa_secret=encrypt_secret(secret),
                    email_receipts=False,
                )
                open_wallet(user)
                for role in roles:
                    Role.objects.create(user=user, role=role)
                record(
                    None,
                    'fixture.restore_account',
                    str(user.pk),
                    reason='Explicit isolated actual-restore acceptance fixture.',
                    worker='local-operator',
                )
            created[name] = user
        owner = created['owner']
        destination = Destination.objects.create(
            owner=owner, label='Synthetic restore proof token', activation_at=db_now() - timedelta(seconds=1)
        )
        with synchronous_demo_bootstrap():
            funding, _ = confirmed(
                owner,
                'FUNDING',
                {
                    'destination_id': str(destination.pk),
                    'amount': '100.00',
                    'note': 'Actual restore funding ' + run_id,
                },
                uuid.uuid4(),
            )
            funding = dispatch_operation(funding.pk)
        if funding.state != 'COMPLETED' or verify_ledger():
            raise CommandError('Original funding did not establish a clean fixture.')
        owner.wallet.refresh_from_db()
        return {
            'run_id': run_id,
            'environment_id': str(settings.ENVIRONMENT_ID),
            'original_generation': settings.RECOVERY_GENERATION,
            'owner_id': str(owner.pk),
            'finance_id': str(created['finance'].pk),
            'reviewer_id': str(created['reviewer'].pk),
            'wallet_id': str(owner.wallet.pk),
            'destination_id': str(destination.pk),
            'funding_operation_id': str(funding.pk),
            'withdrawal_action_key': str(uuid.uuid4()),
            'withdrawal_note': 'Actual restore payout ' + run_id,
            'original_available_cents': owner.wallet.available_cents,
            'observations': [
                {
                    'phase': 'prepare',
                    'at': db_now().isoformat(),
                    'funding_state': funding.state,
                    'available_cents': owner.wallet.available_cents,
                    'boundary': 'Real bound TOTP and normal coordinator/independent provider funding. Synthetic verified accounts and elapsed destination activation are explicit fixture inputs.',
                }
            ],
        }

    def payout(self, data):
        if data.get('withdrawal_operation_id'):
            raise CommandError('Payout phase already recorded; original effect must not be replaced.')
        owner = User.objects.get(pk=data['owner_id'])
        with synchronous_demo_bootstrap():
            operation, intent = confirmed(
                owner,
                'WITHDRAWAL',
                {
                    'destination_id': data['destination_id'],
                    'amount': '30.00',
                    'note': data['withdrawal_note'],
                },
                uuid.UUID(data['withdrawal_action_key']),
            )
            operation = dispatch_operation(operation.pk)
        mapping = ReplayKey.objects.get(
            owner=owner, family='WITHDRAWAL', action_key=data['withdrawal_action_key']
        )
        effect = ProviderEffect.objects.using('simulator').get(provider_key=operation.provider_key)
        if (
            operation.state != 'COMPLETED'
            or effect.state != 'SUCCESS'
            or not effect.recovery_metadata.get('replays')
            or verify_ledger()
        ):
            raise CommandError('Independent payout or retained recovery identity was not established.')
        data.update(
            withdrawal_operation_id=str(operation.pk),
            withdrawal_intent_id=str(intent.pk),
            provider_key=operation.provider_key,
            original_replay_digest=mapping.digest,
        )
        data['observations'].append(
            {
                'phase': 'payout',
                'at': db_now().isoformat(),
                'operation_id': str(operation.pk),
                'provider_key': operation.provider_key,
                'provider_state': effect.state,
                'provider_effect_count': ProviderEffect.objects.using('simulator')
                .filter(provider_key=operation.provider_key)
                .count(),
                'boundary': 'Independent simulator payout committed after the primary snapshot; primary settlement also completed before physical rollback.',
            }
        )

    def assert_mismatch(self, data):
        owner = User.objects.get(pk=data['owner_id'])
        if Operation.objects.filter(pk=data['withdrawal_operation_id']).exists():
            raise CommandError('Primary snapshot did not actually remove the later payout operation.')
        result = lookup(data['provider_key'])
        if not result or result['state'] != 'SUCCESS':
            raise CommandError('Independent simulator did not retain the successful payout.')
        state = readiness()
        if state['recovery']:
            raise CommandError('External generation mismatch was not detected.')
        try:
            with transaction.atomic():
                _gate()
        except DomainError as error:
            if error.code != 'UNAVAILABLE':
                raise
        else:
            raise CommandError('Admission failed to close on recovery-generation mismatch.')
        run = reconcile(User.objects.get(pk=data['finance_id']))
        finding = (
            Finding.objects.filter(
                classification='MISSING_LOCAL_OPERATION', provider_key=data['provider_key'], state='OPEN'
            )
            .order_by('created_at')
            .first()
        )
        if run.clean or finding is None or IntegrityGate.objects.get(pk=1).is_open:
            raise CommandError(
                'Surviving orphan payout failed to create a durable finding and close the integrity gate.'
            )
        owner.wallet.refresh_from_db()
        if owner.wallet.available_cents != data['original_available_cents']:
            raise CommandError('Restored primary wallet does not match the actual pre-payout snapshot.')
        data['orphan_finding_id'] = str(finding.pk)
        data['observations'].append(
            {
                'phase': 'actual_primary_rollback',
                'at': db_now().isoformat(),
                'primary_operation_absent': True,
                'surviving_provider_success': True,
                'recovery_readiness': state['recovery'],
                'admission_rejected': True,
                'integrity_gate_open': False,
                'finding_id': str(finding.pk),
                'restored_available_cents': owner.wallet.available_cents,
            }
        )

    def recover(self, data):
        if settings.RECOVERY_GENERATION != data.get('expected_generation'):
            raise CommandError('Explicit runtime generation override does not match the external marker.')
        finance = User.objects.get(pk=data['finance_id'])
        reviewer = User.objects.get(pk=data['reviewer_id'])
        proposal = propose_recovery(
            finance,
            data['orphan_finding_id'],
            'Actual pg_dump primary rollback with independently retained successful synthetic payout.',
            'RECOVER_OPERATION',
        )
        key = uuid.uuid4()
        approved, intent = confirmed(
            reviewer,
            'RECOVERY_APPROVAL',
            {
                'proposal_id': str(proposal.pk),
                'reason': 'Independent reviewer confirms the exact retained operation and replay identity after physical primary restore.',
                'version': proposal.version,
            },
            key,
        )
        again = confirm_intent(reviewer, intent.pk, key)
        if approved.pk != again.pk or approved.state != 'APPLIED':
            raise CommandError('Reviewed recovery replay did not return one applied proposal.')
        operation = Operation.objects.get(pk=data['withdrawal_operation_id'])
        owner = User.objects.get(pk=data['owner_id'])
        mapping = ReplayKey.objects.get(
            owner=owner, family='WITHDRAWAL', action_key=data['withdrawal_action_key']
        )
        if (
            mapping.operation_id != operation.pk
            or mapping.digest != data['original_replay_digest']
            or operation.provider_key != data['provider_key']
            or operation.state != 'COMPLETED'
        ):
            raise CommandError('Original operation/provider/replay identity was not preserved.')
        # Rebuild the same preview while retaining the accepted action key. Even
        # under a closed gate it must retrieve the reconstructed accepted state.
        replay_intent = create_intent(
            owner,
            'WITHDRAWAL',
            {'destination_id': data['destination_id'], 'amount': '30.00', 'note': data['withdrawal_note']},
        )
        replayed = confirm_intent(owner, replay_intent.pk, data['withdrawal_action_key'])
        owner.wallet.refresh_from_db()
        counter = DailyCounter.objects.get(owner=owner, day=operation.limit_day, direction='OUTGOING')
        issues = verify_ledger()
        count = (
            ProviderEffect.objects.using('simulator')
            .filter(provider_key=data['provider_key'], state='SUCCESS')
            .count()
        )
        if (
            replayed.pk != operation.pk
            or count != 1
            or owner.wallet.available_cents != data['original_available_cents'] - 3000
            or owner.wallet.reserved_cents != 0
            or counter.committed_cents != 3000
            or counter.reserved_cents != 0
            or issues
        ):
            raise CommandError(
                'Restored payout failed balance, original-day, uniqueness or immutable-ledger checks.'
            )
        if (
            Journal.objects.filter(operation=operation, effect_kind='WITHDRAWAL').count() != 1
            or Reservation.objects.filter(operation=operation, state='ACTIVE').exists()
        ):
            raise CommandError('Reconstructed payout did not consume its exact hold once.')
        run = reconcile(finance)
        if not run.clean or IntegrityGate.objects.get(pk=1).is_open:
            raise CommandError('A clean recheck must preserve the explicit second-acknowledgement duty.')
        data['passed'] = True
        data['observations'].append(
            {
                'phase': 'reviewed_recovery',
                'at': db_now().isoformat(),
                'generation': settings.RECOVERY_GENERATION,
                'proposal_id': str(proposal.pk),
                'proposal_state': approved.state,
                'same_operation_uuid': True,
                'same_provider_key': True,
                'same_accepted_replay_digest': True,
                'provider_payout_count': count,
                'available_cents': owner.wallet.available_cents,
                'reserved_cents': owner.wallet.reserved_cents,
                'original_day_committed_cents': counter.committed_cents,
                'invariant_issues': issues,
                'reconciliation_clean': run.clean,
                'integrity_gate_open': False,
                'remaining_duty': 'Admission remains closed until a separately bound second reviewer acknowledgement of the clean recovery result.',
            }
        )
