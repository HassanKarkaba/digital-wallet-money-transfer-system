import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.services import db_now
from audit.services import record
from ledger.models import IntegrityGate
from simulator.models import SimulatorEnvironment


class Command(BaseCommand):
    help = 'Declare restoration and close admission. Stop web/workers before invoking.'

    def add_arguments(self, p):
        p.add_argument('--generation', type=int, required=True)

    def handle(self, *args, **opts):
        generation = opts['generation']
        if generation <= settings.RECOVERY_GENERATION:
            raise CommandError('Generation must increase; update runtime configuration before restarting.')
        with transaction.atomic():
            gate = IntegrityGate.objects.select_for_update().get(pk=1)
            gate.is_open = False
            gate.version += 1
            gate.reason = 'Declared recovery: reconciliation and second reviewer required.'
            gate.recovery_generation = generation
            gate.changed_at = db_now()
            gate.save()
            record(
                None,
                'recovery.prepared',
                'environment',
                reason=gate.reason,
                metadata={'generation': generation},
            )
        # Independent store marker; an interruption fails closed by generation mismatch.
        SimulatorEnvironment.objects.using('simulator').filter(pk=1).update(recovery_generation=generation)
        path = settings.RECOVERY_MARKER_PATH
        temporary = path.with_suffix('.pending')
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'environment_id': str(settings.ENVIRONMENT_ID), 'generation': generation}, f)
        os.replace(temporary, path)
        self.stdout.write(
            'Recovery declared. Update RECOVERY_GENERATION, reconcile, and obtain a second reviewer acknowledgement before admission.'
        )
