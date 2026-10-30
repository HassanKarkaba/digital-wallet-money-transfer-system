"""Private, bounded diagnostics that never format request or exception text.

These optional records are separate from mandatory append-only financial audit.
Only UTC event time, a validated correlation UUID and an HTTP status are stored.
"""

import fcntl
import json
import logging
import os
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_LOG_BYTES = 512 * 1024 * 1024
SEGMENT_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 4096
RETENTION = timedelta(days=14)


def _root(configured=None):
    if configured is None:
        from django.conf import settings

        configured = settings.PRIVATE_ROOT
    return Path(configured)


def _utc(value):
    if value.tzinfo is None:
        raise ValueError('An aware UTC instant is required.')
    return value.astimezone(timezone.utc)


def _instant(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('Invalid diagnostic timestamp.')
    return _utc(datetime.fromisoformat(value[:-1] + '+00:00'))


def _correlation(value):
    if type(value) not in (str, uuid.UUID):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def _encoded(timestamp, correlation, status):
    record = {
        'timestamp': _utc(timestamp).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
        'correlation_id': _correlation(correlation),
        'status': _status(status),
    }
    return json.dumps(record, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n'


def _files(folder):
    return sorted(
        (path for path in folder.glob('diagnostics-*.jsonl') if path.is_file() and not path.is_symlink()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )


def _size(folder):
    # Unknown files count against the cap but are never overwritten or deleted.
    return sum(path.lstat().st_size for path in folder.iterdir() if path.is_file() or path.is_symlink())


@contextmanager
def _locked(root, timeout=0.05):
    root.mkdir(parents=True, exist_ok=True)
    folder = root / 'logs'
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(folder / '.diagnostics.lock', flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError('Optional diagnostic lock budget elapsed.') from None
                time.sleep(0.002)
        yield folder
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _make_space(folder, maximum, reserve=0):
    size = _size(folder)
    removed = 0
    for path in _files(folder):
        if size + reserve <= maximum:
            break
        length = path.stat().st_size
        path.unlink()
        size -= length
        removed += 1
    if size + reserve > maximum:
        raise OSError('Optional diagnostic capacity unavailable.')
    return removed


def _retained_lines(handle, now, counts):
    cutoff = now - RETENTION
    while True:
        line = handle.readline(MAX_RECORD_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_RECORD_BYTES:
            while line and not line.endswith(b'\n'):
                line = handle.readline(MAX_RECORD_BYTES + 1)
            counts['discarded'] += 1
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError('Invalid record.')
            timestamp = _instant(item.get('timestamp'))
            # Equality is expired. A future timestamp beyond ordinary local skew
            # cannot establish a valid retained diagnostic event.
            if timestamp <= cutoff or timestamp > now + timedelta(seconds=60):
                counts['discarded'] += 1
                continue
            yield _encoded(timestamp, item.get('correlation_id'), item.get('status'))
            counts['retained'] += 1
        except (ValueError, TypeError, UnicodeError, OverflowError):
            counts['discarded'] += 1


def cleanup_diagnostics(now=None, *, root=None, max_bytes=MAX_LOG_BYTES):
    """Apply exact per-record expiry/redaction and capacity; sink failures stay optional.

    The cleanup coordinator calls this periodically. File modification time is
    deliberately not an expiry oracle: a new file can contain an expired row.
    Temporary rewrites live outside logs, so atomic replacement cannot transiently
    exceed the directory's selected cap.
    """
    counts = {'retained': 0, 'discarded': 0, 'removed_files': 0, 'failed': False}
    temporary = None
    try:
        if not 1 <= max_bytes <= MAX_LOG_BYTES:
            raise ValueError('Invalid diagnostic cap.')
        root_path = _root(root)
        instant = _utc(now or datetime.now(timezone.utc))
        with _locked(root_path, timeout=0.25) as folder:
            # With the shared lock held, a previous temporary file has no live
            # cleanup owner. Recover it after an interrupted rewrite.
            for abandoned in root_path.glob('.diagnostic-cleanup-*'):
                if abandoned.is_file() and not abandoned.is_symlink():
                    abandoned.unlink()
            for path in _files(folder):
                original = path.stat()
                descriptor, temporary = tempfile.mkstemp(prefix='.diagnostic-cleanup-', dir=root_path)
                with os.fdopen(descriptor, 'wb') as target:
                    os.fchmod(target.fileno(), 0o600)
                    with path.open('rb') as source:
                        for line in _retained_lines(source, instant, counts):
                            target.write(line)
                if Path(temporary).stat().st_size:
                    os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
                    os.replace(temporary, path)
                    temporary = None
                    os.chmod(path, 0o600)
                else:
                    Path(temporary).unlink()
                    temporary = None
                    path.unlink()
                    counts['removed_files'] += 1
            counts['removed_files'] += _make_space(folder, max_bytes)
    except Exception:
        # Do not send the failure to logging: that would recurse and could reveal
        # a private path, request, exception message or database statement.
        counts['failed'] = True
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
    return counts


class DiagnosticHandler(logging.Handler):
    """Fail-open optional sink. Mandatory business audit does not use this class."""

    def __init__(self, root=None, max_bytes=MAX_LOG_BYTES):
        super().__init__()
        if not 1 <= max_bytes <= MAX_LOG_BYTES:
            raise ValueError('Invalid diagnostic cap.')
        self.private_root = root
        self.max_bytes = max_bytes

    def emit(self, record):
        try:
            timestamp = datetime.fromtimestamp(record.created, timezone.utc)
            now = datetime.now(timezone.utc)
            if timestamp <= now - RETENTION or timestamp > now + timedelta(seconds=60):
                return
            request = getattr(record, 'request', None)
            correlation = getattr(record, 'correlation_id', None)
            if correlation is None and request is not None:
                correlation = getattr(request, 'correlation_id', None)
            content = _encoded(timestamp, correlation, getattr(record, 'status_code', None))
            with _locked(_root(self.private_root)) as folder:
                _make_space(folder, self.max_bytes, reserve=len(content))
                existing = _files(folder)
                path = existing[-1] if existing else None
                if path is None or path.stat().st_size + len(content) > min(SEGMENT_BYTES, self.max_bytes):
                    path = folder / (
                        'diagnostics-' + now.strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex + '.jsonl'
                    )
                flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
                descriptor = os.open(path, flags, 0o600)
                try:
                    os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
                    with os.fdopen(descriptor, 'ab', closefd=False) as stream:
                        stream.write(content)
                        stream.flush()
                finally:
                    os.close(descriptor)
        except Exception:
            # Formatting record.msg, request paths, bodies or exc_info is forbidden.
            # Never call handleError(), whose debug output can include those values.
            return
