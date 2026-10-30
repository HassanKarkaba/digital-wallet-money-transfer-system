import time
import uuid
from contextlib import ExitStack

from django.db import DatabaseError, connections
from django.http import HttpResponse, JsonResponse


class RequestBoundaryMiddleware:
    """Bound browser work and translate lock/availability errors without disclosure."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.correlation_id = str(uuid.uuid4())
        limit = 32768 if request.path.startswith('/integrations/') else 16384
        try:
            if int(request.META.get('CONTENT_LENGTH') or 0) > limit:
                return HttpResponse('Request too large.', status=413)
        except ValueError:
            return HttpResponse('Invalid request.', status=400)
        if request.META.get('CONTENT_TYPE', '').lower().startswith('multipart/'):
            return HttpResponse('File uploads are not supported.', status=415)
        if request.method in {'POST', 'PUT', 'PATCH'} and len(request.body) > limit:
            return HttpResponse('Request too large.', status=413)
        request.deadline = time.monotonic() + (2 if request.path.startswith('/integrations/') else 5)

        def budget(execute, sql, params, many, context):
            # Transaction cleanup must remain possible after a consumed budget.
            if str(sql).lstrip().upper().startswith(('ROLLBACK', 'RELEASE SAVEPOINT', 'SAVEPOINT')):
                return execute(sql, params, many, context)
            remaining = request.deadline - time.monotonic()
            if remaining <= 0:
                from payments.errors import DomainError

                raise DomainError(
                    'Request budget elapsed. Check the current operation before retrying.', 'UNAVAILABLE', 503
                )
            milliseconds = max(1, int(remaining * 1000))
            raw = context['cursor'].cursor
            raw.execute(f'SET statement_timeout={milliseconds}')
            raw.execute(f'SET lock_timeout={min(2000, milliseconds)}')
            return execute(sql, params, many, context)

        with ExitStack() as stack:
            for alias in ('default', 'simulator'):
                stack.enter_context(connections[alias].execute_wrapper(budget))
            from .authority import bind_request

            with bind_request(request):
                response = self.get_response(request)
        if (
            response.status_code == 302
            and not request.user.is_authenticated
            and 'application/json' in request.headers.get('Accept', '')
            and response.get('Location', '').startswith('/accounts/login/')
        ):
            response = JsonResponse(
                {
                    'version': 1,
                    'code': 'SESSION_EXPIRED',
                    'message': 'Sign in to continue.',
                    'field_errors': {},
                    'correlation_id': request.correlation_id,
                },
                status=401,
            )
        response['X-Correlation-ID'] = request.correlation_id
        return response

    def process_exception(self, request, exception):
        from payments.errors import DomainError

        if (
            isinstance(exception, DatabaseError)
            or isinstance(exception, DomainError)
            and exception.code == 'UNAVAILABLE'
        ):
            payload = {
                'version': 1,
                'code': 'UNAVAILABLE',
                'message': 'The operation may already be accepted. Check its current status before retrying with the same action key.',
                'field_errors': {},
                'correlation_id': str(request.correlation_id),
            }
            if 'application/json' in request.headers.get('Accept', ''):
                return JsonResponse(payload, status=503)
            return HttpResponse(
                '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Temporarily unavailable</title><main><h1>Temporarily unavailable</h1><p>'
                + payload['message']
                + '</p><p><a href="/activity/">Check your activity</a></p><p>Demonstration money. No cash value.</p></main></html>',
                status=503,
            )
