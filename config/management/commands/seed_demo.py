import json
import os
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

import pyotp
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Role, User
from accounts.services import db_now, encrypt_secret
from audit.services import record
from ledger.posting import open_wallet
from payments.workflows import confirm_intent, create_intent, dispatch_operation, verify_intent
from rails.models import Destination


class Command(BaseCommand):
    help = 'Idempotent synthetic seeding through the normal funding coordinator.'

    def add_arguments(self, p):
        p.add_argument('--access-file', required=True)

    def handle(self, *args, **opts):
        from work.services import synchronous_demo_bootstrap

        with synchronous_demo_bootstrap():
            return self._seed(*args, **opts)

    def _seed(self, *args, **opts):
        if settings.MODE != 'DEMO':
            raise CommandError('Synthetic seeding is DEMO only.')
        path = Path(opts['access_file']).resolve()
        if settings.PRIVATE_ROOT.resolve() not in path.parents:
            raise CommandError('Access details must stay in private storage.')
        if path.exists():
            self.stdout.write('Demo access file already exists; preserving accounts and credentials.')
            return
        access = []
        for handle, name, roles in [
            ('alex', 'Alex Morgan', []),
            ('jordan', 'Jordan Lee', []),
            ('reviewer', 'Taylor Brooks', ['REVIEWER']),
            ('finance', 'Sam Rivera', ['FINANCE', 'SUPPORT']),
            ('access_admin', 'Casey Patel', ['ACCESS_ADMIN']),
        ]:
            email = f'{handle}@dwmt.test'
            if User.objects.filter(email=email).exists():
                raise CommandError(
                    'Synthetic accounts exist without retained access file; no credential overwrite performed.'
                )
            password = secrets.token_urlsafe(24)
            secret = pyotp.random_base32()
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email,
                    password=password,
                    handle=handle,
                    display_name=name,
                    verified_at=db_now(),
                    mfa_confirmed=True,
                    mfa_secret=encrypt_secret(secret),
                )
                open_wallet(user)
                for role in roles:
                    Role.objects.get_or_create(user=user, role=role)
                # An explicit local seed control; ordinary customers always wait ten minutes.
                dest = Destination.objects.create(
                    owner=user,
                    label='Demo checking · synthetic',
                    activation_at=db_now() - timedelta(seconds=1),
                )
                record(
                    None, 'demo.account_seeded', str(user.pk), reason='Explicit local synthetic seed command.'
                )
            intent = create_intent(user, 'FUNDING', {'destination_id': str(dest.pk), 'amount': '750.00'})
            verify_intent(user, intent.pk, pyotp.TOTP(secret).now())
            op = confirm_intent(user, intent.pk, uuid.uuid4())
            dispatch_operation(op.pk)
            access.append(
                {
                    'email': email,
                    'password': password,
                    'totp_secret': secret,
                    'handle': handle,
                    'roles': roles,
                }
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(
                {
                    'notice': 'PRIVATE SYNTHETIC DEMO ACCESS. Do not commit or share. TOTP secrets may be imported into an authenticator.',
                    'accounts': access,
                },
                f,
                indent=2,
            )
        self.stdout.write(
            'Synthetic accounts funded through normal simulated rails. Private access file written; no credentials displayed.'
        )
