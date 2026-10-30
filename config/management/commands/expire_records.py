from django.core.management.base import BaseCommand

from notifications.services import cleanup_private_files
from payments.workflows import expire_reviews


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **opts):
        n = expire_reviews(min(opts['limit'], 1000))
        cleanup_private_files()
        self.stdout.write(f'Expired reviews: {n}. Private file retention checked.')
