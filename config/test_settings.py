from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F403
from .settings import DATABASES, database

VERIFY_SYNCHRONOUS_WORK = True
# Test runner creates disposable databases with migration credentials. The web
# service and explicit privilege-attack connections retain runtime credentials.
TEST_RUNTIME_DATABASES = {k: dict(v) for k, v in DATABASES.items()}
for alias, var in [('default', 'MIGRATION_DATABASE_URL'), ('simulator', 'SIMULATOR_MIGRATION_DATABASE_URL')]:
    DATABASES[alias] = database(var)
    if not DATABASES[alias]['NAME'].endswith('_test'):
        raise ImproperlyConfigured('Refusing development database in verification runner.')
    DATABASES[alias]['TEST']['NAME'] = 'case_' + DATABASES[alias]['NAME']
TEST_RUNNER = 'config.test_runner.WalletTestRunner'
