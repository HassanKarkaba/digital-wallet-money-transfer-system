from django.core.management import call_command
from django.test.runner import DiscoverRunner


class WalletTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        result = super().setup_databases(**kwargs)
        call_command('install_database_permissions', verbosity=0)
        return result
