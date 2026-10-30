from django.core.management.base import BaseCommand, CommandError

from payments.reconciliation import verify_ledger


class Command(BaseCommand):
    def handle(self, *args, **options):
        issues = verify_ledger()
        if issues:
            raise CommandError(f'Ledger verification failed: {len(issues)} finding(s).')
        self.stdout.write('Ledger journals, projections, reservations and cash balance verified.')
