import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.services import db_now
from audit.services import record
from ledger.models import IntegrityGate
from rails.adapter import check_identity, initialize_identity


class Command(BaseCommand):
    help = 'Initialize a new isolated environment exactly once; never clear a recovery gate.'

    def handle(self, *args, **options):
        marker = settings.RECOVERY_MARKER_PATH
        with transaction.atomic():
            gate, created = IntegrityGate.objects.get_or_create(
                pk=1,
                defaults={
                    'environment_id': settings.ENVIRONMENT_ID,
                    'recovery_generation': settings.RECOVERY_GENERATION,
                    'last_seen_at': db_now(),
                },
            )
            if created:
                if marker.exists():
                    raise CommandError(
                        'A marker already exists for a new primary store. Use reviewed recovery.'
                    )
                identity = initialize_identity()
                if str(identity.environment_id) != str(settings.ENVIRONMENT_ID):
                    raise CommandError('Simulator identity mismatch.')
                marker.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as f:
                    json.dump(
                        {
                            'environment_id': str(settings.ENVIRONMENT_ID),
                            'generation': settings.RECOVERY_GENERATION,
                        },
                        f,
                    )
                gate.is_open = True
                gate.reason = 'Fresh isolated demonstration dataset.'
                gate.changed_at = db_now()
                gate.save()
                record(None, 'integrity.initialized', 'environment', reason=gate.reason)
            else:
                check_identity()
                if not marker.exists():
                    raise CommandError('Recovery marker missing. No automatic repair is allowed.')
        self.stdout.write('Environment identity checked; existing recovery decisions preserved.')
