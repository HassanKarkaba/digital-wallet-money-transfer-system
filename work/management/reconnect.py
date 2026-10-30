"""Transport-only daemon reconnect policy; business retries keep their owners."""

from django.db import InterfaceError, OperationalError, connections

TRANSPORT_ERRORS = (OperationalError, InterfaceError)
RECONNECT_DELAYS = (1, 2, 5, 10, 30)


def reconnect_delay(failures):
    return RECONNECT_DELAYS[min(max(failures - 1, 0), len(RECONNECT_DELAYS) - 1)]


def close_owned_connections(aliases=None):
    """Close this thread's initialized aliases; no business cleanup is implied."""
    for connection in connections.all(initialized_only=True):
        if aliases is not None and connection.alias not in aliases:
            continue
        try:
            connection.close()
        except TRANSPORT_ERRORS:
            # A failed socket close must not prevent other aliases closing.
            pass
