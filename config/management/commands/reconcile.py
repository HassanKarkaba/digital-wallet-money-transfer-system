from django.core.management.base import BaseCommand, CommandError

from payments.reconciliation import reconcile


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument('--limit', type=int, default=1000000)

    def handle(self, *args, **opts):
        run = reconcile(limit=opts['limit'])
        self.stdout.write(
            f'Reconciliation {run.id}: checked={run.checked}; clean={run.clean}; findings={run.summary.get("findings", 0)}'
        )
        if not run.clean:
            raise CommandError('Reconciliation requires investigation.')
