import base64
import json
import os
import secrets
import uuid
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Generate private DEMO configuration without loading runtime settings.'

    def add_arguments(self, p):
        p.add_argument('--output', required=True)
        p.add_argument('--force', action='store_true')
        p.add_argument('--test', action='store_true')
        p.add_argument('--reuse-test', action='store_true')

    def handle(self, *args, **opts):
        target = Path(opts['output'])
        if target.exists() and opts['reuse_test'] and opts['test']:
            values = {}
            for line in target.read_text().splitlines():
                if '=' in line:
                    key, value = line.split('=', 1)
                    values[key] = value.strip().strip("'")
            self._scoped_test_files(target, values)
            self.stdout.write('Existing isolated test configuration scopes refreshed.')
            return
        if target.exists() and not opts['force']:
            raise CommandError('Configuration already exists; refusing overwrite.')
        primary = secrets.token_hex(24)
        simulator = secrets.token_hex(24)
        migration = secrets.token_hex(24)
        values = {
            'DWMT_MODE': 'DEMO',
            'DJANGO_SECRET_KEY': secrets.token_urlsafe(64),
            'MFA_ENCRYPTION_KEYS': json.dumps(
                {'v1': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}, separators=(',', ':')
            ),
            'MFA_KEY_VERSION': 'v1',
            'CALLBACK_HMAC_KEY': secrets.token_hex(32),
            'ENVIRONMENT_ID': str(uuid.uuid4()),
            'RECOVERY_GENERATION': '1',
            'POSTGRES_PASSWORD': secrets.token_hex(24),
            'WALLET_PASSWORD': primary,
            'SIMULATOR_PASSWORD': simulator,
            'MIGRATION_PASSWORD': migration,
            'DATABASE_URL': f'postgresql://wallet_runtime:{primary}@db:5432/wallet',
            'SIMULATOR_DATABASE_URL': f'postgresql://simulator_runtime:{simulator}@db:5432/simulator',
            'MIGRATION_DATABASE_URL': f'postgresql://wallet_migrate:{migration}@db:5432/wallet',
            'SIMULATOR_MIGRATION_DATABASE_URL': f'postgresql://simulator_migrate:{migration}@db:5432/simulator',
        }
        if opts['test']:
            values['WALLET_DB'] = 'wallet_test'
            values['SIMULATOR_DB'] = 'simulator_test'
            for key in ['DATABASE_URL', 'MIGRATION_DATABASE_URL']:
                values[key] = values[key].rsplit('/', 1)[0] + '/wallet_test'
            for key in ['SIMULATOR_DATABASE_URL', 'SIMULATOR_MIGRATION_DATABASE_URL']:
                values[key] = values[key].rsplit('/', 1)[0] + '/simulator_test'
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            for k, v in values.items():
                f.write(f"{k}='{v}'\n")
        os.chmod(target, 0o600)
        if opts['test']:
            self._scoped_test_files(target, values)
        self.stdout.write('Private DEMO configuration written. No secrets displayed.')

    def _scoped_test_files(self, target, values):
        admin = {
            'MIGRATION_DATABASE_URL',
            'SIMULATOR_MIGRATION_DATABASE_URL',
            'MIGRATION_PASSWORD',
            'POSTGRES_PASSWORD',
            'WALLET_PASSWORD',
            'SIMULATOR_PASSWORD',
        }
        database = {
            'POSTGRES_PASSWORD',
            'WALLET_PASSWORD',
            'SIMULATOR_PASSWORD',
            'MIGRATION_PASSWORD',
            'WALLET_DB',
            'SIMULATOR_DB',
        }
        for scope, selected in [
            ('app', {k: v for k, v in values.items() if k not in admin}),
            ('db', {k: v for k, v in values.items() if k in database}),
        ]:
            folder = target.parent / scope
            folder.mkdir(exist_ok=True, mode=0o700)
            path = folder / 'runtime.env'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                for key, value in selected.items():
                    stream.write(f"{key}='{value}'\n")
