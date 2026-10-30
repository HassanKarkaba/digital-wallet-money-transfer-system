from django.core.management.base import BaseCommand, CommandError

from payments.reconciliation import reconcile


class Command(BaseCommand):
    help = 'Run recovery checks; never automatically reopen the integrity gate.'

    def handle(self, *args, **opts):
        run = reconcile()
        self.stdout.write(f'Recovery check {run.pk}: clean={run.clean}.')
        if not run.clean:
            raise CommandError('Recovery findings remain. Gate stays closed.')
        self.stdout.write(
            'A second eligible reviewer must acknowledge the clean result with fresh bound MFA in the staff workspace.'
        )
