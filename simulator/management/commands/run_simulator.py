import os
import signal
import threading
import time
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from simulator.services import finalize_pending, send_events
from work.management.reconnect import TRANSPORT_ERRORS, close_owned_connections, reconnect_delay
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
        try:
            finalized, delivered = self.run_loop(stopping, options, identity)
        finally:
            try:
                heartbeat(identity, kind='simulator', state='STOPPED')
            except TRANSPORT_ERRORS:
                pass
            finally:
                close_owned_connections()
        self.stdout.write(f'Simulator finalized {finalized}; delivered {delivered} event hints.')

    def run_loop(self, stopping, options, identity):
        finalized = delivered = failures = 0
        heartbeat_failures = 0
        next_heartbeat = 0.0
        while not stopping.is_set():
            try:
                close_old_connections()
                if time.monotonic() >= next_heartbeat:
                    try:
                        heartbeat(identity, kind='simulator')
                    except TRANSPORT_ERRORS:
                        # Primary health publication cannot stop independently
                        # committed provider effects or their durable hints.
                        close_owned_connections(aliases=('default',))
                        heartbeat_failures += 1
                        delay = reconnect_delay(heartbeat_failures)
                        next_heartbeat = time.monotonic() + delay
                        self.stderr.write(
                            f'Simulator primary heartbeat unavailable; retrying publication in {delay}s.'
                        )
                    else:
                        heartbeat_failures = 0
                        next_heartbeat = 0.0
                finalized += finalize_pending(options['limit'], stop_event=stopping)
                if not stopping.is_set():
                    delivered += send_events(options['limit'], stop_event=stopping)
            except TRANSPORT_ERRORS:
                close_owned_connections()
                failures += 1
                if options['once'] and failures >= 6:
                    raise CommandError(
                        'Database unavailable after six bounded single-pass reconnect attempts.'
                    ) from None
                delay = reconnect_delay(failures)
                self.stderr.write(
                    f'Simulator database transport unavailable; retrying in {delay}s. Independent effects and events are retained.'
                )
                if stopping.wait(delay):
                    break
                continue
            failures = 0
            if options['once']:
                break
            stopping.wait(1)
        return finalized, delivered
