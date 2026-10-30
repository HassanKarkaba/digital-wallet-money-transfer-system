"""Twenty real authenticated HTTP clients over the full isolated acceptance dataset.

Run inside the verification image after seed_workload. Sessions are explicit
performance fixtures; every measured monetary confirmation uses real bound TOTP.
No credentials, cookies, MFA seeds, full SQL or customer data are printed.
"""

import argparse
import gzip
import html.parser
import json
import math
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django

django.setup()

import pyotp
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import close_old_connections, connection, connections
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from accounts.models import User, UserSession
from accounts.services import begin_session, db_now, decrypt_secret
from payments.models import Operation, ReplayKey
from payments.reconciliation import verify_ledger
from payments.views import owned_operations


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class HiddenFields(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and values.get('type') == 'hidden' and values.get('name'):
            self.fields[values['name']] = values.get('value', '')


class HttpActor:
    def __init__(self, base, session_key):
        self.base = base
        self.cookies = {settings.SESSION_COOKIE_NAME: session_key}
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, data=None):
        headers = {
            'Cookie': '; '.join(f'{key}={value}' for key, value in self.cookies.items()),
            'Accept': 'text/html',
            'Referer': self.base + '/',
        }
        if data is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            headers['X-CSRFToken'] = self.cookies.get(settings.CSRF_COOKIE_NAME, '')
        payload = urllib.parse.urlencode(data).encode() if data is not None else None
        request = urllib.request.Request(
            self.base + path, data=payload, headers=headers, method='POST' if data is not None else 'GET'
        )
        started = time.monotonic()
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, OSError, TimeoutError):
            return 0, '', None, (time.monotonic() - started) * 1000
        try:
            try:
                body = response.read(256 * 1024).decode('utf-8', errors='replace')
            except (OSError, TimeoutError):
                return 0, '', None, (time.monotonic() - started) * 1000
            for value in response.headers.get_all('Set-Cookie', []):
                parsed = SimpleCookie()
                parsed.load(value)
                for key, cookie in parsed.items():
                    self.cookies[key] = cookie.value
            return (
                response.status,
                body,
                response.headers.get('Location'),
                (time.monotonic() - started) * 1000,
            )
        finally:
            response.close()


def tracked_session(user):
    request = RequestFactory().get('/')
    request.user = AnonymousUser()
    SessionMiddleware(lambda _: None).process_request(request)
    begin_session(request, user)
    request.session.save()
    return request.session.session_key


def percentile(values, proportion=0.95):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * proportion) - 1)], 3)


def resource_observation():
    result = {}
    for key, path in [
        ('memory_current', '/sys/fs/cgroup/memory.current'),
        ('memory_max', '/sys/fs/cgroup/memory.max'),
        ('cpu_max', '/sys/fs/cgroup/cpu.max'),
        ('cpu_set', '/sys/fs/cgroup/cpuset.cpus.effective'),
    ]:
        try:
            result[key] = Path(path).read_text().strip()
        except OSError:
            result[key] = None
    for alias in ('default', 'simulator'):
        with connections[alias].cursor() as cursor:
            cursor.execute('SELECT pg_database_size(current_database())')
            result[alias + '_database_bytes'] = cursor.fetchone()[0]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=int, default=600)
    parser.add_argument('--clients', type=int, default=20)
    parser.add_argument('--think-ms', type=int, default=250)
    parser.add_argument('--minimum-customers', type=int, default=10000)
    parser.add_argument('--minimum-operations', type=int, default=100000)
    parser.add_argument('--base-url', default=os.environ.get('DWMT_BROWSER_URL', 'http://web:8000'))
    args = parser.parse_args()
    if (
        settings.MODE != 'DEMO'
        or settings.DATABASES['default']['NAME'] != 'wallet_test'
        or settings.DATABASES['simulator']['NAME'] != 'simulator_test'
    ):
        raise SystemExit('Refusing ordinary development resources.')
    if getattr(settings, 'VERIFY_SYNCHRONOUS_WORK', False):
        raise SystemExit(
            'Measured HTTP workload requires normal runtime settings and live progress services.'
        )
    if not 1 <= args.clients <= 20 or not 10 <= args.duration <= 3600 or not 0 <= args.think_ms <= 5000:
        raise SystemExit('Use 1..20 clients, 10..3600 seconds and bounded think time.')
    base = args.base_url.rstrip('/')
    parsed_base = urllib.parse.urlparse(base)
    if (
        parsed_base.scheme != 'http'
        or parsed_base.hostname not in ['web', 'localhost', '127.0.0.1']
        or parsed_base.username
        or parsed_base.path
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise SystemExit('Use the explicit local verification HTTP service.')
    users = list(
        User.objects.filter(
            handle__startswith='load_', status='ACTIVE', verified_at__isnull=False, mfa_confirmed=True
        ).order_by('handle')[: args.clients]
    )
    dataset = {'customers': User.objects.count(), 'operations': Operation.objects.count()}
    from ledger.models import Entry

    dataset['entries'] = Entry.objects.count()
    if (
        len(users) < args.clients
        or dataset['customers'] < args.minimum_customers
        or dataset['operations'] < args.minimum_operations
        or dataset['entries'] > 500000
    ):
        raise SystemExit(
            'Required isolated acceptance dataset is absent or exceeds the selected entry bound.'
        )
    initial_issues = verify_ledger()
    if initial_issues:
        raise SystemExit('Initial accounting verification failed; refusing to load a corrupt fixture.')
    output = Path(settings.PRIVATE_ROOT) / 'workload'
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    resources_before = resource_observation()
    sessions = [tracked_session(user) for user in users]
    secrets = [decrypt_secret(user.mfa_secret) for user in users]
    barrier = threading.Barrier(args.clients + 1)
    run_id = uuid.uuid4().hex
    start_box = {}
    sample_lock = threading.Lock()
    samples = {'dashboard': [], 'history': [], 'internal_admission': []}
    failures = []
    counters = {
        'http_requests': 0,
        'unexpected_errors': 0,
        'transfer_attempts': 0,
        'completed_transfers': 0,
        'clients_finished': 0,
    }
    client_seconds = {}

    def observed(kind, status, milliseconds, expected, reference=''):
        with sample_lock:
            counters['http_requests'] += 1
            if kind in samples:
                samples[kind].append(milliseconds)
            if status not in expected:
                counters['unexpected_errors'] += 1
                if len(failures) < 100:
                    failures.append({'kind': kind, 'status': status, 'reference': reference})

    def actor_client(index):
        close_old_connections()
        actor = users[index]
        recipient = (
            users[(index + 1) % len(users)]
            if len(users) > 1
            else User.objects.filter(handle__startswith='load_').exclude(pk=actor.pk).first()
        )
        client = HttpActor(base, sessions[index])
        last_counter = actor.mfa_last_counter
        barrier.wait(timeout=60)
        start = start_box['start']
        deadline = start + args.duration
        next_transfer = start + (index / args.clients) * min(31, args.duration / 2)
        toggle = False
        try:
            while time.monotonic() < deadline:
                if time.monotonic() >= next_transfer:
                    with sample_lock:
                        counters['transfer_attempts'] += 1
                    # Read, prepare, review and verify the exact bound instruction
                    # through ordinary HTTP forms; only decisive POST is timed as
                    # monetary admission, separately from review preparation.
                    status, body, _, milliseconds = client.request('/send/')
                    observed('prepare_form', status, milliseconds, {200})
                    if status != 200:
                        next_transfer += 31
                        continue
                    payload = {
                        'csrfmiddlewaretoken': client.cookies.get(settings.CSRF_COOKIE_NAME, ''),
                        'kind': 'TRANSFER',
                        'recipient_handle': recipient.handle,
                        'amount': '0.01',
                        'note': 'Measured acceptance workload ' + run_id,
                        'policy_version': 'DWMT-P1',
                    }
                    status, _, location, milliseconds = client.request('/payments/intents/', payload)
                    observed('prepare_intent', status, milliseconds, {303})
                    if status != 303 or not location or not location.startswith('/payments/intents/'):
                        next_transfer += 31
                        continue
                    status, body, _, milliseconds = client.request(location)
                    observed('review', status, milliseconds, {200})
                    if status != 200:
                        next_transfer += 31
                        continue
                    hidden = HiddenFields()
                    hidden.feed(body)
                    fields = hidden.fields
                    if not {'intent_id', 'action_key', 'schema_version', 'policy_version'} <= set(fields):
                        observed('invalid_review', 0, 0, {200})
                        next_transfer += 31
                        continue
                    step = max(int(db_now().timestamp()) // 30, last_counter + 1)
                    current = int(db_now().timestamp()) // 30
                    if step > current + 1:
                        next_transfer = max(next_transfer + 31, time.monotonic() + 1)
                        continue
                    fields['otp'] = pyotp.TOTP(secrets[index]).generate_otp(step)
                    fields['csrfmiddlewaretoken'] = client.cookies.get(
                        settings.CSRF_COOKIE_NAME, fields.get('csrfmiddlewaretoken', '')
                    )
                    intent_id = fields['intent_id']
                    status, _, _, milliseconds = client.request(
                        f'/payments/intents/{intent_id}/verify/', fields
                    )
                    observed('internal_admission', status, milliseconds, {303}, reference=intent_id)
                    if status == 303:
                        last_counter = step
                        with sample_lock:
                            counters['completed_transfers'] += 1
                    else:
                        # An uncertain reply never creates a replacement key.
                        # This is an authoritative fixture-side observation only.
                        mapping = (
                            ReplayKey.objects.filter(
                                owner=actor, family='TRANSFER', action_key=fields['action_key']
                            )
                            .select_related('operation')
                            .first()
                        )
                        if mapping and mapping.operation_id and mapping.operation.state == 'COMPLETED':
                            with sample_lock:
                                counters['completed_transfers'] += 1
                            last_counter = step
                    next_transfer += 31
                else:
                    path, kind = ('/activity/', 'history') if toggle else ('/wallet/', 'dashboard')
                    toggle = not toggle
                    status, _, _, milliseconds = client.request(path)
                    observed(kind, status, milliseconds, {200})
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(args.think_ms / 1000, remaining))
            with sample_lock:
                counters['clients_finished'] += 1
        except Exception as exc:
            # No exception body can leak a session, MFA seed or full response.
            observed('client_exception', 0, 5000, {200}, reference=type(exc).__name__)
        finally:
            with sample_lock:
                client_seconds[str(index)] = round(time.monotonic() - start, 3)
            connections.close_all()

    started_at = db_now().isoformat()
    with ThreadPoolExecutor(max_workers=args.clients) as executor:
        futures = [executor.submit(actor_client, index) for index in range(args.clients)]
        start_box['start'] = time.monotonic()
        barrier.wait(timeout=60)
        for future in futures:
            future.result()
    elapsed = time.monotonic() - start_box['start']
    final_issues = verify_ledger()
    measured_operations = Operation.objects.filter(
        kind='TRANSFER',
        actor_id__in=[user.pk for user in users],
        note='Measured acceptance workload ' + run_id,
    )
    measured_count = measured_operations.count()
    invalid_measured_records = measured_operations.exclude(amount_cents=1, state='COMPLETED').count()
    query_client = Client()
    query_client.cookies[settings.SESSION_COOKIE_NAME] = sessions[0]
    query_counts = {}
    for name, path in [('dashboard', '/wallet/'), ('history', '/activity/')]:
        with CaptureQueriesContext(connection) as captured:
            response = query_client.get(path)
        query_counts[name] = {
            'queries': len(captured),
            'status': response.status_code,
            'boundary': 'same Django view/middleware under local test client; latency is measured separately over real HTTP',
        }
    history_plan = json.loads(
        owned_operations(users[0]).order_by('-created_at', '-id')[:50].explain(format='json')
    )
    p95 = {name: percentile(values) for name, values in samples.items()}
    unexpected_ratio = counters['unexpected_errors'] / max(1, counters['http_requests'])
    minimum_transfers = max(args.clients, args.clients * max(0, args.duration // 31 - 1))
    passed = (
        not final_issues
        and p95['dashboard'] is not None
        and p95['dashboard'] < 1000
        and p95['history'] is not None
        and p95['history'] < 1000
        and p95['internal_admission'] is not None
        and p95['internal_admission'] < 2000
        and unexpected_ratio < 0.01
        and counters['completed_transfers'] >= minimum_transfers
        and counters['clients_finished'] == args.clients
        and measured_count == counters['completed_transfers']
        and invalid_measured_records == 0
        and elapsed >= args.duration
        and all(item['status'] == 200 for item in query_counts.values())
    )
    report = {
        'passed': passed,
        'full_acceptance_shape': args.clients == 20
        and args.duration == 600
        and args.minimum_customers >= 10000
        and args.minimum_operations >= 100000,
        'run_id': run_id,
        'started_at': started_at,
        'completed_at': db_now().isoformat(),
        'requested_duration_seconds': args.duration,
        'elapsed_seconds': round(elapsed, 3),
        'clients': args.clients,
        'think_time_ms': args.think_ms,
        'client_elapsed_seconds': client_seconds,
        'dataset_before': dataset,
        'dataset_operations_after': Operation.objects.count(),
        'dataset_entries_after': Entry.objects.count(),
        'samples': {name: len(values) for name, values in samples.items()},
        'p95_ms': p95,
        'p99_ms': {name: percentile(values, 0.99) for name, values in samples.items()},
        'max_ms': {name: round(max(values), 3) if values else None for name, values in samples.items()},
        'authoritative_measured_operation_count': measured_count,
        'invalid_measured_operation_records': invalid_measured_records,
        'mean_ms': {
            name: round(statistics.mean(values), 3) if values else None for name, values in samples.items()
        },
        'counters': counters,
        'unexpected_error_fraction': round(unexpected_ratio, 8),
        'minimum_completed_transfers': minimum_transfers,
        'initial_invariant_issues': initial_issues,
        'final_invariant_issues': final_issues,
        'query_counts': query_counts,
        'history_explain_plan': history_plan,
        'resource_before': resources_before,
        'resource_after': resource_observation(),
        'failures': failures,
        'boundaries': [
            'Real authenticated HTTP to the isolated web container.',
            'Measured money review and bound TOTP confirmation use normal runtime settings, CSRF, authority and SQL ledger enforcement.',
            'Fixtures establish tracked sessions directly, so this is not a login benchmark.',
            'Admission latency measures the decisive confirmation POST, excluding human review and OTP wait.',
            'No intentional fault injection or lock waits in this workload.',
        ],
        'limitations': [
            'Docker VM CPU/RAM allocation and container memory peaks must be recorded by the external operator alongside this report.',
            'One selected think-time distribution is measured; this is local capacity evidence only.',
            'Query counts use the same Django code in the verification client; full SQL is intentionally omitted to protect sessions.',
        ],
    }
    path = output / ('result-' + run_id + '.json')
    sample_path = output / ('latency-samples-' + run_id + '.json.gz')
    with gzip.open(sample_path, 'wt', encoding='utf-8') as sample_file:
        json.dump(
            {name: [round(value, 3) for value in values] for name, values in samples.items()}, sample_file
        )
    sample_path.chmod(0o600)
    report['raw_latency_samples'] = sample_path.name
    path.write_text(json.dumps(report, indent=2))
    path.chmod(0o600)
    # Revoke only the performance fixture sessions created by this process.
    from django.contrib.sessions.models import Session

    UserSession.objects.filter(session_key__in=sessions).update(revoked_at=db_now())
    Session.objects.filter(session_key__in=sessions).delete()
    print(
        json.dumps(
            {
                'passed': passed,
                'full_acceptance_shape': report['full_acceptance_shape'],
                'elapsed_seconds': report['elapsed_seconds'],
                'p95_ms': p95,
                'unexpected_error_fraction': report['unexpected_error_fraction'],
                'completed_transfers': counters['completed_transfers'],
                'invariant_violations': len(final_issues),
                'report': str(path),
            },
            indent=2,
        ),
        flush=True,
    )
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
