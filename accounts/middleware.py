import uuid
from datetime import timedelta

from django.contrib import auth, messages
from django.http import JsonResponse
from django.shortcuts import redirect

from .models import UserSession
from .services import db_now


class AccountSecurityMiddleware:
    """Authoritative idle, absolute and authority-version session revocation."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.correlation_id = uuid.uuid4()
        if request.user.is_authenticated:
            now = db_now()
            tracked = UserSession.objects.filter(
                user=request.user, session_key=request.session.session_key
            ).first()
            invalid = (
                not tracked
                or tracked.revoked_at
                or request.user.status != 'ACTIVE'
                or not request.user.is_active
                or tracked.authority_version != request.user.authority_version
                or now >= tracked.last_seen + timedelta(minutes=30)
                or now >= tracked.started_at + timedelta(hours=12)
            )
            if invalid:
                if tracked and not tracked.revoked_at:
                    UserSession.objects.filter(pk=tracked.pk, revoked_at__isnull=True).update(revoked_at=now)
                auth.logout(request)
                if 'application/json' in request.headers.get('Accept', ''):
                    response = JsonResponse(
                        {
                            'version': 1,
                            'code': 'SESSION_EXPIRED',
                            'message': 'Sign in again to continue.',
                            'field_errors': {},
                            'correlation_id': str(request.correlation_id),
                        },
                        status=401,
                    )
                else:
                    messages.info(request, 'Your session ended. Sign in again to continue.')
                    response = redirect('accounts:login')
            else:
                UserSession.objects.filter(pk=tracked.pk, revoked_at__isnull=True).update(last_seen=now)
                response = self.get_response(request)
        else:
            response = self.get_response(request)
        response['X-Content-Type-Options'] = 'nosniff'
        response['X-Frame-Options'] = 'DENY'
        response['Referrer-Policy'] = 'no-referrer'
        response['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
        )
        response['X-Correlation-ID'] = str(request.correlation_id)
        if request.user.is_authenticated or request.path.startswith('/accounts/'):
            response['Cache-Control'] = 'no-store, private, max-age=0'
            response['Pragma'] = 'no-cache'
        return response
