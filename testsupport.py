"""Synthetic test setup only; never imported by runtime domain code.

proof() substitutes an already-issued bound MFA grant so financial interleavings
can be controlled without waiting for TOTP counters. Accounts tests exercise the
real cryptographic and replay boundary; seed_demo uses the real combined flow.
"""

import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

import pyotp
from django.conf import settings
from django.test import override_settings

from accounts.models import Role, User
from accounts.services import db_now, encrypt_secret
from ledger.models import IntegrityGate, LedgerAccount
from ledger.posting import open_wallet
from payments.models import Intent
from payments.workflows import confirm_intent, create_intent, dispatch_operation
from rails.adapter import initialize_identity
from rails.models import Destination


class WalletFixtures:
    def setup_wallet_environment(self):
        self.private = tempfile.TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        marker = Path(self.private.name) / 'marker.json'
        marker.write_text(
            json.dumps(
                {'environment_id': str(settings.ENVIRONMENT_ID), 'generation': settings.RECOVERY_GENERATION}
            )
        )
        override = override_settings(
            RECOVERY_MARKER_PATH=marker,
            PRIVATE_ROOT=Path(self.private.name),
            EMAIL_FILE_PATH=str(Path(self.private.name) / 'mail'),
        )
        override.enable()
        self.addCleanup(override.disable)
        IntegrityGate.objects.create(
            pk=1,
            is_open=True,
            environment_id=settings.ENVIRONMENT_ID,
            recovery_generation=settings.RECOVERY_GENERATION,
        )
        LedgerAccount.objects.get_or_create(kind='SYSTEM_CASH', defaults={'balance_cents': 0})
        initialize_identity()

    def customer(self, handle, roles=()):
        u = User.objects.create_user(
            email=handle + '@dwmt.test',
            handle=handle,
            display_name=handle.title(),
            password='A quiet synthetic river password 9076',
            verified_at=db_now(),
            mfa_confirmed=True,
            mfa_secret=encrypt_secret(pyotp.random_base32()),
        )
        open_wallet(u)
        for role in roles:
            Role.objects.create(user=u, role=role)
        return u

    def proof(self, user, kind, instruction):
        i = create_intent(user, kind, instruction)
        Intent.objects.filter(pk=i.pk).update(verified_at=db_now(), grant_id=uuid.uuid4())
        i.refresh_from_db()
        return i

    def admit(self, user, kind, instruction, key=None):
        i = self.proof(user, kind, instruction)
        return confirm_intent(user, i.pk, key or uuid.uuid4())

    def fund(self, user, amount='750.00', scenario='SUCCESS'):
        d = Destination.objects.create(
            owner=user,
            label='Test synthetic bank',
            activation_at=db_now() - timedelta(seconds=1),
            scenario=scenario,
        )
        op = self.admit(user, 'FUNDING', {'destination_id': str(d.pk), 'amount': amount})
        dispatch_operation(op.pk)
        op.refresh_from_db()
        return op
