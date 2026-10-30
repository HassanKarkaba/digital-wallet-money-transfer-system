"""Filesystem checks for privacy, exact event-age expiry and shared sink bounds."""

import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from config.logging import DiagnosticHandler, cleanup_diagnostics


class PrivateDiagnosticTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def records(self):
        return [
            json.loads(line)
            for path in sorted((self.root / 'logs').glob('diagnostics-*.jsonl'))
            for line in path.read_text().splitlines()
        ]

    def test_message_request_and_exception_secrets_never_enter_the_sink(self):
        secret = 'private-otp-password-and-recovery-token-728391'
        correlation = uuid.uuid4()
        record = logging.LogRecord(
            'django.request', logging.ERROR, '/private/source.py', 10, secret, (), None
        )
        record.exc_info = (ValueError, ValueError(secret), None)
        record.request = SimpleNamespace(correlation_id=correlation, path='/recover/' + secret, body=secret)
        record.status_code = 403
        handler = DiagnosticHandler(root=self.root)
        handler.emit(record)
        saved = self.records()
        self.assertEqual(len(saved), 1)
        self.assertEqual(set(saved[0]), {'timestamp', 'correlation_id', 'status'})
        self.assertEqual(saved[0]['correlation_id'], str(correlation))
        self.assertEqual(saved[0]['status'], 403)
        content = ''.join(path.read_text() for path in (self.root / 'logs').iterdir())
        self.assertNotIn(secret, content)
        self.assertNotIn('/recover/', content)
        for path in (self.root / 'logs').iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / 'logs').stat().st_mode), 0o700)

    def test_expiry_uses_each_event_timestamp_and_expires_at_equality(self):
        folder = self.root / 'logs'
        folder.mkdir()
        now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        boundary = now - timedelta(days=14)
        path = folder / 'diagnostics-fixture.jsonl'
        events = [
            {
                'timestamp': (boundary - timedelta(microseconds=1)).isoformat().replace('+00:00', 'Z'),
                'status': 400,
            },
            {'timestamp': boundary.isoformat().replace('+00:00', 'Z'), 'status': 401},
            {
                'timestamp': (boundary + timedelta(microseconds=1)).isoformat().replace('+00:00', 'Z'),
                'status': 402,
            },
            {
                'timestamp': now.isoformat().replace('+00:00', 'Z'),
                'status': 403,
                'message': 'must-be-redacted',
            },
        ]
        path.write_text('\n'.join(json.dumps(event) for event in events) + '\nmalformed-private-data\n')
        # A fresh file mtime must not prolong the expired records inside it.
        os.utime(path, (now.timestamp(), now.timestamp()))
        (self.root / '.diagnostic-cleanup-interrupted').write_text('An abandoned sanitized rewrite.')
        outcome = cleanup_diagnostics(now=now, root=self.root)
        self.assertFalse(outcome['failed'])
        self.assertEqual([event['status'] for event in self.records()], [402, 403])
        self.assertTrue(
            all(set(event) == {'timestamp', 'correlation_id', 'status'} for event in self.records())
        )
        self.assertNotIn('must-be-redacted', path.read_text())
        self.assertNotIn('malformed-private-data', path.read_text())
        self.assertEqual(outcome['discarded'], 3)
        self.assertFalse(list(self.root.glob('.diagnostic-cleanup-*')))

    def test_independent_process_writers_preserve_the_shared_capacity_cap(self):
        program = """
import logging,sys,uuid
from config.logging import DiagnosticHandler
handler=DiagnosticHandler(root=sys.argv[1],max_bytes=2048)
for index in range(50):
    record=logging.LogRecord('django.request',logging.WARNING,'',0,'unretained-private-text',(),None)
    record.correlation_id=str(uuid.uuid4())
    record.status_code=429
    handler.emit(record)
"""
        writers = [
            subprocess.Popen(
                [sys.executable, '-c', program, str(self.root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        for writer in writers:
            _, error = writer.communicate(timeout=15)
            self.assertEqual(writer.returncode, 0, error.decode())
        files = list((self.root / 'logs').iterdir())
        self.assertLessEqual(sum(path.stat().st_size for path in files), 2048)
        self.assertTrue(self.records())
        self.assertTrue(all(event['status'] == 429 for event in self.records()))
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files))

    def test_optional_unwritable_sink_cannot_raise_into_application_logging(self):
        invalid_root = self.root / 'not-a-directory'
        invalid_root.write_text('existing user data')
        logger = logging.Logger('isolated-optional-sink', level=logging.WARNING)
        logger.addHandler(DiagnosticHandler(root=invalid_root))
        logger.error('A secret-bearing message must not be formatted or rethrown.')
        self.assertEqual(invalid_root.read_text(), 'existing user data')
        result = cleanup_diagnostics(root=invalid_root)
        self.assertTrue(result['failed'])
