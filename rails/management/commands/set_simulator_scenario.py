from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.services import lock_users
from audit.services import record
from rails.models import Destination


class Command(BaseCommand):
    help = 'Select a reproducible local-only scenario on a synthetic token with no unresolved operations.'

    def add_arguments(self, parser):
        parser.add_argument('destination_id')
        parser.add_argument('scenario', choices=[value for value, _ in Destination.Scenario.choices])
        parser.add_argument('--reason', required=True)

    def handle(self, *args, **options):
        if settings.MODE != 'DEMO':
            raise CommandError('Scenario controls require DEMO mode.')
        if not 10 <= len(options['reason'].strip()) <= 1000:
            raise CommandError('Provide an investigation or test purpose of 10 to 1,000 characters.')
        initial = Destination.objects.filter(pk=options['destination_id']).first()
        if initial is None:
            raise CommandError('Synthetic destination not found.')
        from payments.models import Operation

        with transaction.atomic():
            lock_users([initial.owner_id])
            destination = Destination.objects.select_for_update().get(pk=initial.pk)
            if Operation.objects.filter(
                destination=destination, state__in=['REVIEW_PENDING', 'READY', 'SUBMITTED', 'UNKNOWN']
            ).exists():
                raise CommandError('Resolve existing operations before changing the token scenario.')
            old = destination.scenario
            destination.scenario = options['scenario']
            destination.version += 1
            destination.save(update_fields=['scenario', 'version'])
            record(
                None,
                'simulator.scenario',
                str(destination.pk),
                reason=options['reason'].strip(),
                metadata={'old': old, 'new': destination.scenario},
                worker='local-operator',
            )
        self.stdout.write(f'Scenario selected for synthetic destination {initial.pk}.')
