from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from work.models import Job
from work.services import retry_attention_job


class Command(BaseCommand):
    help = 'After investigation, retry a retained NEEDS_ATTENTION obligation under its original identity.'

    def add_arguments(self, parser):
        parser.add_argument('job_id')
        parser.add_argument('--reason', required=True)

    def handle(self, *args, **options):
        if settings.MODE != 'DEMO':
            raise CommandError('Local operator retry is available only in DEMO.')
        try:
            job = retry_attention_job(options['job_id'], options['reason'])
        except (ValidationError, Job.DoesNotExist, ValueError):
            raise CommandError('Retained job not found, ineligible, or reason invalid.')
        self.stdout.write(f'Controlled retry retained job {job.pk} and object {job.object_id}.')
