from django.core.management.base import CommandError
from django.core.management.commands.flush import Command as DjangoFlush
from django.db import connections


class Command(DjangoFlush):
    """Disposable test cleanup has a separate budget from browser operations."""

    def handle(self, **options):
        connection = connections[options['database']]
        if not connection.settings_dict['NAME'].startswith('case_'):
            raise CommandError(
                'Refusing dataset flush. Use the separately documented destructive DEMO reset procedure.'
            )
        with connection.cursor() as cursor:
            cursor.execute('SET statement_timeout=120000')
            cursor.execute('SET lock_timeout=10000')
        try:
            return super().handle(**options)
        finally:
            with connection.cursor() as cursor:
                cursor.execute('SET statement_timeout=5000')
                cursor.execute('SET lock_timeout=2000')
