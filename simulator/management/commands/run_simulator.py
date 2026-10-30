import os
import signal
import threading
import uuid

from django.core.management.base import BaseCommand
from django.db import close_old_connections, connections

from simulator.services import finalize_pending, send_events
from work.services import heartbeat


class Command(BaseCommand):
    help = 'Finalize synthetic provider events and deliver signed hints over the local Compose network.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--limit', type=int, default=20)

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 20:
            raise ValueError('Batch must be between 1 and 20.')
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        identity = f'simulator:{os.getpid()}:{uuid.uuid4()}'
        finalized = delivered = 0
        try:
            while not stopping.is_set():
                close_old_connections()
                heartbeat(identity, kind='simulator')
                finalized += finalize_pending(options['limit'], stop_event=stopping)
                if not stopping.is_set():
                    delivered += send_events(options['limit'], stop_event=stopping)
                if options['once']:
                    break
                stopping.wait(1)
        finally:
            try:
                heartbeat(identity, kind='simulator', state='STOPPED')
            finally:
                connections.close_all()
        self.stdout.write(f'Simulator finalized {finalized}; delivered {delivered} event hints.')
