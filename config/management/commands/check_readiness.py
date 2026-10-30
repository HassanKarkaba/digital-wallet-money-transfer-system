import time

from django.core.management.base import BaseCommand, CommandError

from config.health import readiness


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument('--require-all', action='store_true')
        p.add_argument('--timeout', type=int, default=0)

    def handle(self, *args, **opts):
        deadline = time.monotonic() + min(opts['timeout'], 120)
        while True:
            state = readiness()
            if all(state.values()) or time.monotonic() >= deadline:
                break
            time.sleep(1)
        self.stdout.write(' '.join(f'{k}={"ready" if v else "unready"}' for k, v in state.items()))
        if opts['require_all'] and not all(state.values()):
            raise CommandError('Required local capabilities unavailable.')
