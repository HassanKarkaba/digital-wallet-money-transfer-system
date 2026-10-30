"""Request credentials are context, never user-controlled business instruction."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.contrib.sessions.models import Session

from accounts.models import UserSession
from payments.errors import DomainError

request_authority = ContextVar('dwmt_request_authority', default=None)


@contextmanager
def bind_request(request):
    value = (
        {'actor_id': request.user.pk, 'session_key': request.session.session_key}
        if request.user.is_authenticated
        else None
    )
    token = request_authority.set(value)
    try:
        yield
    finally:
        request_authority.reset(token)


def assert_request_authority(actor, now):
    context = request_authority.get()
    if context is None:
        return  # Explicit workers/commands use their own authority.
    if context['actor_id'] != actor.pk:
        raise DomainError('Request authority does not match this actor.', 'DENIED', 403)
    tracked = UserSession.objects.filter(user=actor, session_key=context['session_key']).first()
    if (
        not tracked
        or tracked.revoked_at
        or tracked.authority_version != actor.authority_version
        or now >= tracked.started_at + timedelta(hours=12)
        or now >= tracked.last_seen + timedelta(minutes=30)
        or not Session.objects.filter(session_key=context['session_key']).exists()
    ):
        raise DomainError('Your session ended. Sign in again before continuing.', 'DENIED', 403)


def assert_request_deadline(actor, now):
    context = request_authority.get()
    if context and context.get('deadline') is not None and now >= context['deadline']:
        raise DomainError('Your session expired before admission. Sign in again.', 'DENIED', 403)
