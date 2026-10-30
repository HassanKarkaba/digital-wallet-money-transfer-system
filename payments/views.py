import uuid
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.db.models import DateTimeField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.shortcuts import redirect as django_redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.services import SecurityError, assert_eligible
from payments import forms, workflows
from payments.errors import DomainError
from payments.models import (
    Intent,
    LegacyIntentReference,
    MoneyRequest,
    Operation,
    Recipient,
    ReplayKey,
    Schedule,
)
from payments.replay import match_accepted_reference
from rails.models import Destination


def redirect(*args, **kwargs):
    response = django_redirect(*args, **kwargs)
    response.status_code = 303
    return response


def domain_boundary(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except (DomainError, SecurityError) as exc:
            status_code = 429 if hasattr(exc, 'retry_after') else getattr(exc, 'status', 422)
            if 'application/json' in request.headers.get('Accept', ''):
                payload = {
                    'version': 1,
                    'code': getattr(exc, 'code', 'REJECTED'),
                    'message': str(exc),
                    'field_errors': {},
                    'correlation_id': str(getattr(request, 'correlation_id', '')),
                }
                if hasattr(exc, 'retry_after'):
                    payload['retry_after'] = exc.retry_after
                response = JsonResponse(payload, status=status_code)
            else:
                response = render(
                    request,
                    'payments/error.html',
                    {'error': str(exc), 'code': getattr(exc, 'code', 'REJECTED')},
                    status=status_code,
                )
            if hasattr(exc, 'retry_after'):
                response['Retry-After'] = str(exc.retry_after)
            return response

    return wrapped


def wallet_for(user):
    from ledger.models import Wallet

    return Wallet.objects.filter(owner=user).first()


def owned_operations(user, wallet=None):
    from ledger.models import Wallet

    wallet_id = (
        wallet.id if wallet else Wallet.objects.filter(owner=user).values_list('id', flat=True).first()
    )
    query = Operation.objects.select_related('sender__owner', 'recipient__owner', 'destination', 'actor')
    if not wallet_id:
        return query.none()
    return query.filter(Q(sender_id=wallet_id) | Q(recipient_id=wallet_id))


def activity_operations(user, wallet=None):
    from ledger.models import Journal

    latest = (
        Journal.objects.filter(operation_id=OuterRef('pk'), sealed=True)
        .order_by('-committed_at', '-id')
        .values('committed_at')[:1]
    )
    return owned_operations(user, wallet).annotate(
        activity_at=Coalesce(Subquery(latest, output_field=DateTimeField()), 'created_at')
    )


def decorate_operations(rows, user):
    for op in rows:
        incoming = bool(op.recipient_id and op.recipient.owner_id == user.id)
        op.ui_direction = 'incoming' if incoming else 'outgoing'
        if op.kind == 'FUNDING':
            op.ui_direction = 'incoming'
            op.ui_counterparty = op.destination.label if op.destination_id else 'Synthetic funding'
        elif op.kind == 'WITHDRAWAL':
            op.ui_counterparty = op.destination.label if op.destination_id else 'Synthetic withdrawal'
        elif incoming:
            op.ui_counterparty = '@' + op.sender.owner.handle if op.sender_id else 'Internal transfer'
        else:
            op.ui_counterparty = '@' + op.recipient.owner.handle if op.recipient_id else 'Internal transfer'
    return rows


@require_GET
def landing(request):
    if request.user.is_authenticated:
        return redirect('/wallet/')
    return render(request, 'payments/landing.html')


@login_required
@require_GET
def dashboard(request):
    wallet = wallet_for(request.user)
    rows = decorate_operations(
        list(activity_operations(request.user, wallet).order_by('-activity_at', '-id')[:6]), request.user
    )
    requests_count = MoneyRequest.objects.filter(
        payer=request.user, state='OPEN', expires_at__gt=timezone.now()
    ).count()
    schedules_count = Schedule.objects.filter(
        owner=request.user,
        state='SCHEDULED',
        authority_version=request.user.authority_version,
        policy_version='DWMT-P1',
    ).count()
    return render(
        request,
        'payments/dashboard.html',
        {
            'wallet': wallet,
            'operations': rows,
            'as_of': timezone.now(),
            'requests_count': requests_count,
            'schedules_count': schedules_count,
            'page': 'wallet',
        },
    )


@login_required
@require_GET
@domain_boundary
def activity(request):
    query = activity_operations(request.user)
    allowed = {'kind', 'state', 'direction', 'start', 'end', 'cursor', 'page_size'}
    if set(request.GET) - allowed:
        raise DomainError('Unsupported activity filters.')
    try:
        page_size = int(request.GET.get('page_size', '50'))
        if not 1 <= page_size <= 50:
            raise ValueError
    except ValueError:
        raise DomainError('Choose a page size from 1 to 50.')
    filters = {k: request.GET.get(k, '') for k in ('kind', 'state', 'direction', 'start', 'end')}
    filters['page_size'] = str(page_size)
    if filters['state'] and filters['state'] not in (
        'COMPLETED',
        'REVIEW_PENDING',
        'READY',
        'SUBMITTED',
        'UNKNOWN',
        'FAILED',
        'CANCELLED',
    ):
        raise DomainError('Choose a supported activity state.')
    if filters['direction'] not in ('', 'incoming', 'outgoing'):
        raise DomainError('Choose incoming, outgoing, or both directions.')
    if filters['kind']:
        if filters['kind'] not in ('TRANSFER', 'FUNDING', 'WITHDRAWAL', 'REVERSAL'):
            raise DomainError('Choose a supported activity type.')
        query = query.filter(kind=filters['kind'])
    if filters['state']:
        query = query.filter(state=filters['state'])
    if filters['direction'] == 'incoming':
        query = query.filter(Q(recipient__owner=request.user) | Q(kind='FUNDING', actor=request.user))
    elif filters['direction'] == 'outgoing':
        query = query.filter(Q(sender__owner=request.user) & ~Q(kind='FUNDING'))
    from datetime import date, datetime, time
    from datetime import timezone as dt_timezone

    for key, lookup in (('start', 'activity_at__gte'), ('end', 'activity_at__lt')):
        if filters[key]:
            try:
                value = datetime.combine(date.fromisoformat(filters[key]), time.min, tzinfo=dt_timezone.utc)
            except ValueError:
                raise DomainError('Enter dates in YYYY-MM-DD format.')
            query = query.filter(**{lookup: value})
    if filters['start'] and filters['end']:
        days = (date.fromisoformat(filters['end']) - date.fromisoformat(filters['start'])).days
        if not 0 < days <= 366:
            raise DomainError('Select an activity period no longer than 366 days.')
    cursor = request.GET.get('cursor')
    if cursor:
        try:
            value = signing.loads(cursor, salt='wallet-history')
            if value['filters'] != filters or value['owner'] != str(request.user.id):
                raise ValueError
            point = datetime.fromisoformat(value['time'])
            query = query.filter(Q(activity_at__lt=point) | Q(activity_at=point, id__lt=value['id']))
        except (signing.BadSignature, KeyError, ValueError, TypeError):
            raise DomainError('This activity cursor is no longer valid. Start from the first page.')
    rows = list(query.order_by('-activity_at', '-id')[: page_size + 1])
    next_cursor = None
    if len(rows) > page_size:
        last = rows[page_size - 1]
        next_cursor = signing.dumps(
            {
                'time': last.activity_at.isoformat(),
                'id': str(last.id),
                'owner': str(request.user.id),
                'filters': filters,
            },
            salt='wallet-history',
        )
    querystring = request.GET.copy()
    querystring.pop('cursor', None)
    return render(
        request,
        'payments/activity.html',
        {
            'operations': decorate_operations(rows[:page_size], request.user),
            'filters': filters,
            'next_cursor': next_cursor,
            'filter_query': querystring.urlencode(),
            'page': 'activity',
        },
    )


@login_required
@require_GET
@domain_boundary
def payment_entry(request, kind):
    kind = kind.upper()
    if kind not in ('TRANSFER', 'FUNDING', 'WITHDRAWAL'):
        raise Http404
    destinations = Destination.objects.filter(owner=request.user, status='ACTIVE').order_by('created_at')
    form = (
        forms.PaymentForm(initial={'recipient_handle': request.GET.get('to', '')})
        if kind == 'TRANSFER'
        else forms.RailForm(destinations=destinations)
    )
    return render(
        request,
        'payments/entry.html',
        {
            'form': form,
            'kind': kind,
            'wallet': wallet_for(request.user),
            'destinations': destinations,
            'page': 'money',
        },
    )


@login_required
@require_POST
@domain_boundary
def prepare_intent(request):
    kind = request.POST.get('kind', '').upper()
    data = request.POST.copy()
    data.pop('kind', None)
    if kind == 'TRANSFER':
        form = forms.PaymentForm(data)
    elif kind in ('FUNDING', 'WITHDRAWAL'):
        form = forms.RailForm(
            data, destinations=Destination.objects.filter(owner=request.user, status='ACTIVE')
        )
    elif kind == 'SCHEDULE':
        form = forms.ScheduleForm(data)
    elif kind == 'DESTINATION':
        form = forms.DestinationForm(data)
    elif kind == 'REQUEST_PAY':
        try:
            request_id = uuid.UUID(data.get('request_id', ''))
        except (ValueError, TypeError, AttributeError):
            raise DomainError('Use a valid money request reference.')
        if set(data) - {'csrfmiddlewaretoken', 'request_id', 'policy_version'}:
            raise DomainError('Unsupported request payment fields.')
        item = get_object_or_404(MoneyRequest, id=request_id, payer=request.user)
        intent = workflows.create_intent(
            request.user,
            kind,
            {'request_id': str(item.id), 'policy_version': data.get('policy_version', 'DWMT-P1')},
        )
        return redirect('payments:intent_review', intent_id=intent.id)
    else:
        raise DomainError('Choose a supported instruction type.')
    if not form.is_valid():
        return entry_error_response(request, form, kind, 422)
    instruction = dict(form.cleaned_data)
    instruction.pop('local_time', None)
    try:
        intent = workflows.create_intent(request.user, kind, instruction)
    except DomainError as exc:
        if exc.code not in {'INVALID', 'LIMIT', 'CAPACITY', 'INSUFFICIENT_FUNDS', 'STALE'}:
            raise
        form.add_error(None, str(exc))
        return entry_error_response(request, form, kind, exc.status)
    return redirect('payments:intent_review', intent_id=intent.id)


def entry_error_response(request, form, kind, status):
    template = (
        'payments/schedules.html'
        if kind == 'SCHEDULE'
        else 'payments/destinations.html'
        if kind == 'DESTINATION'
        else 'payments/entry.html'
    )
    context = {'form': form, 'kind': kind, 'wallet': wallet_for(request.user), 'page': 'money'}
    if kind == 'SCHEDULE':
        context['schedules'] = (
            Schedule.objects.filter(owner=request.user)
            .select_related('recipient__owner', 'operation')
            .order_by('-created_at')[:50]
        )
    if kind == 'DESTINATION':
        context['destinations'] = Destination.objects.filter(owner=request.user)
    elif kind in {'FUNDING', 'WITHDRAWAL'}:
        context['destinations'] = Destination.objects.filter(owner=request.user, status='ACTIVE')
    return render(request, template, context, status=status)


def prepare_with_kind(request, kind):
    data = request.POST.copy()
    if data.get('kind') and data['kind'] != kind:
        raise DomainError('The route and instruction type do not match.')
    data['kind'] = kind
    request.POST = data
    return prepare_intent(request)


def intent_summary(intent):
    instruction = intent.instruction
    staff_role = {
        'ROLE_CHANGE': 'ACCESS_ADMIN',
        'ROLE_INVITE': 'ACCESS_ADMIN',
        'CASE_DECISION': 'REVIEWER',
        'RESTRICTION': 'REVIEWER',
        'RECOVERY_APPROVAL': 'REVIEWER',
        'GATE_REOPEN': 'REVIEWER',
    }.get(intent.kind)
    if staff_role:
        try:
            assert_eligible(intent.owner, staff_role=staff_role)
        except SecurityError as exc:
            raise DomainError(
                'Your current staff authority does not permit this confirmation.', 'DENIED', 403
            ) from exc
    from ledger.models import Wallet

    summary = {
        'kind': intent.kind,
        'amount_cents': instruction.get('amount_cents'),
        'note': instruction.get('note', ''),
    }
    if summary['amount_cents'] is None and instruction.get('amount'):
        text = str(instruction['amount'])
        whole, _, fraction = text.partition('.')
        summary['amount_cents'] = int(whole) * 100 + int((fraction + '00')[:2])
    if instruction.get('recipient_id'):
        wallet = Wallet.objects.select_related('owner').filter(id=instruction['recipient_id']).first()
        summary['recipient'] = '@' + wallet.owner.handle if wallet else 'Recipient unavailable'
    elif instruction.get('recipient_handle'):
        summary['recipient'] = '@' + instruction['recipient_handle']
    if instruction.get('destination_id'):
        destination = Destination.objects.filter(id=instruction['destination_id'], owner=intent.owner).first()
        summary['recipient'] = destination.label if destination else 'Destination unavailable'
    if instruction.get('request_id'):
        item = (
            MoneyRequest.objects.select_related('requester')
            .filter(id=instruction['request_id'], payer=intent.owner)
            .first()
        )
        if item:
            summary.update({'recipient': '@' + item.requester.handle, 'amount_cents': item.amount_cents})
    if intent.kind == 'DESTINATION':
        summary['recipient'] = instruction.get('label', 'Synthetic destination')
    summary['due_at'] = instruction.get('due_at')
    summary['timezone'] = instruction.get('timezone')
    summary['utc_offset'] = instruction.get('utc_offset')
    summary['decision'] = instruction.get('decision')
    summary['reason'] = instruction.get('reason')
    summary['restriction'] = instruction.get('state') if intent.kind == 'RESTRICTION' else None
    if intent.kind in ('ROLE_CHANGE', 'ROLE_INVITE'):
        from accounts.models import User

        target = (
            User.objects.filter(id=instruction.get('user_id')).first() if instruction.get('user_id') else None
        )
        summary['recipient'] = '@' + target.handle if target else instruction.get('email', 'Staff invitation')
        summary['role'] = instruction.get('role')
        summary['role_action'] = 'Grant' if instruction.get('enabled') else 'Revoke'
        if intent.kind == 'ROLE_INVITE':
            summary['role_action'] = 'Invite with role'
        summary['target_reference'] = instruction.get('user_id')
    if intent.kind == 'CASE_DECISION':
        from audit.services import record
        from oversight.models import Case

        summary['target_reference'] = instruction.get('case_id')
        case = (
            Case.objects.select_related(
                'operation__sender__owner', 'operation__recipient__owner', 'operation__destination'
            )
            .filter(id=instruction.get('case_id'))
            .first()
        )
        if case:
            operation = case.operation
            summary['amount_cents'] = operation.amount_cents
            sender = '@' + operation.sender.owner.handle if operation.sender_id else 'Synthetic USD funding'
            recipient = (
                '@' + operation.recipient.owner.handle
                if operation.recipient_id
                else operation.destination.label
            )
            summary['recipient'] = sender + ' → ' + recipient
            record(
                intent.owner,
                'staff.case.confirmation.read',
                str(case.id),
                reason='Review the exact bound financial decision',
            )
    if intent.kind == 'RESTRICTION':
        target = Wallet.objects.select_related('owner').filter(id=instruction.get('wallet_id')).first()
        summary['recipient'] = '@' + target.owner.handle if target else 'Wallet unavailable'
        summary['target_reference'] = instruction.get('wallet_id')
    if intent.kind == 'RECOVERY_APPROVAL':
        summary['target_reference'] = instruction.get('proposal_id')
    summary['version'] = instruction.get('version')
    return summary


def proof_form(intent, data=None, initial=None):
    initial = {'intent_id': intent.id, 'grant_id': intent.grant_id, 'schema_version': 1, **(initial or {})}
    form = forms.ProofForm(data, initial=initial)
    if intent.consumed_at or intent.verified_at:
        from django import forms as django_forms

        form.fields['otp'].required = False
        form.fields['otp'].widget = django_forms.HiddenInput()
    return form


@login_required
@require_GET
@domain_boundary
@transaction.atomic
def intent_review(request, intent_id):
    from accounts.models import User

    current = User.objects.select_for_update().get(pk=request.user.id)
    workflows._current_actor(request.user, current)
    intent = Intent.objects.select_related('owner').filter(id=intent_id, owner=request.user).first()
    if not intent:
        accepted = ReplayKey.objects.filter(owner=request.user, intent_id=intent_id).first()
        retained = LegacyIntentReference.objects.filter(owner=current, pk=intent_id).exists()
        return render(
            request,
            'payments/error.html',
            {
                'error': 'This review proof is no longer available. Check the retained result or Activity before starting another instruction.',
                'code': 'REVIEW_UNAVAILABLE',
                'lookup_url': f'/payments/actions/{accepted.family}/{accepted.action_key}/'
                if accepted
                else None,
            },
            status=410 if accepted or retained else 404,
        )
    key_name = 'action:' + str(intent.id)
    accepted = (
        ReplayKey.objects.filter(owner=request.user, intent_id=intent.id, family=intent.kind).first()
        if intent.consumed_at
        else None
    )
    action_key = str(accepted.action_key) if accepted else request.session.get(key_name)
    if not action_key:
        if intent.consumed_at:
            return render(
                request,
                'payments/error.html',
                {
                    'error': 'This review was consumed. Check Activity or use the original accepted action reference before starting another instruction.',
                    'code': 'REVIEW_CONSUMED',
                },
                status=410,
            )
        action_key = str(uuid.uuid4())
        request.session[key_name] = action_key
    return render(
        request,
        'payments/review.html',
        {
            'intent': intent,
            'summary': intent_summary(intent),
            'form': proof_form(intent, initial={'action_key': action_key}),
            'wallet': wallet_for(request.user),
            'expired': timezone.now() >= intent.expires_at,
            'page': 'money',
        },
    )


@login_required
@require_POST
@domain_boundary
def verify_and_confirm(request, intent_id):
    intent = Intent.objects.select_related('owner').filter(id=intent_id, owner=request.user).first()
    if intent is None:
        from accounts.services import rate_limit

        allowed = {
            'csrfmiddlewaretoken',
            'intent_id',
            'grant_id',
            'schema_version',
            'policy_version',
            'action_key',
            'otp',
        }
        if set(request.POST) - allowed or any(
            len(request.POST.getlist(key)) != 1 for key in request.POST if key != 'csrfmiddlewaretoken'
        ):
            raise DomainError('Unsupported or repeated confirmation fields.')
        try:
            key = uuid.UUID(request.POST.get('action_key', ''))
            submitted_id = uuid.UUID(request.POST.get('intent_id', ''))
        except (ValueError, TypeError, AttributeError):
            raise DomainError(
                'This review is unavailable. Retrieve the accepted result from Activity.', 'DENIED', 404
            )
        if submitted_id != intent_id:
            raise DomainError('This confirmation names a different instruction.', 'CONFLICT', 409)
        if (
            request.POST.get('policy_version', 'DWMT-P1') != 'DWMT-P1'
            or request.POST.get('schema_version', '1') != '1'
        ):
            raise DomainError('This confirmation schema is unsupported.', 'STALE', 409)
        accepted = match_accepted_reference(request.user, key, intent_id=intent_id)
        rate_limit('status', str(request.user.id), 60, 60)
        result = workflows.resolve_accepted_action(
            request.user, key, intent_id=intent_id, family=accepted.family
        )
        return result_redirect(request, accepted.family, result)
    if 'action_key' not in request.POST:
        allowed = {'csrfmiddlewaretoken', 'otp', 'policy_version', 'schema_version'}
        if set(request.POST) - allowed or any(
            len(request.POST.getlist(key)) != 1 for key in request.POST if key != 'csrfmiddlewaretoken'
        ):
            raise DomainError('Unsupported or repeated proof fields.')
        if (
            request.POST.get('policy_version', 'DWMT-P1') != 'DWMT-P1'
            or request.POST.get('schema_version', '1') != '1'
        ):
            raise DomainError('This proof form is stale.', 'STALE', 409)
        proof = workflows.verify_intent(request.user, intent.id, request.POST.get('otp', ''))
        if 'application/json' in request.headers.get('Accept', ''):
            return JsonResponse(
                {
                    'version': 1,
                    'intent_id': str(proof.id),
                    'grant_id': str(proof.grant_id),
                    'expires_at': proof.expires_at.isoformat().replace('+00:00', 'Z'),
                }
            )
        return redirect('payments:intent_review', intent_id=proof.id)
    form = proof_form(intent, request.POST)
    if form.is_valid():
        try:
            if form.cleaned_data['intent_id'] != intent.id:
                raise DomainError('This proof belongs to a different instruction.', 'DENIED', 403)
            if form.cleaned_data['schema_version'] != 1:
                raise DomainError('This proof schema is unsupported.', 'STALE', 409)
            if form.cleaned_data['grant_id'] and str(form.cleaned_data['grant_id']) != str(intent.grant_id):
                raise DomainError('This grant does not belong to the instruction.', 'DENIED', 403)
            if form.cleaned_data['policy_version'] != 'DWMT-P1':
                raise DomainError(
                    'The monetary policy changed. Start a fresh review.', code='STALE_POLICY', status=409
                )
            # Accepted replay is resolved by the coordinator before requiring new proof.
            if not intent.consumed_at and not intent.verified_at:
                workflows.verify_intent(request.user, intent.id, form.cleaned_data['otp'])
            result = workflows.confirm_intent(request.user, intent.id, form.cleaned_data['action_key'])
            return result_redirect(request, intent, result)
        except DomainError as exc:
            form.add_error(None, str(exc))
            status = getattr(exc, 'status', 422)
    else:
        status = 422
    # OTPs are deliberately never echoed back into the page.
    form.data = form.data.copy()
    form.data['otp'] = ''
    return render(
        request,
        'payments/review.html',
        {
            'intent': intent,
            'summary': intent_summary(intent),
            'form': form,
            'wallet': wallet_for(request.user),
            'expired': timezone.now() >= intent.expires_at,
            'page': 'money',
        },
        status=status,
    )


def result_redirect(request, intent, result):
    kind = intent.kind if isinstance(intent, Intent) else intent
    announce_new = isinstance(intent, Intent) and intent.consumed_at is None
    if isinstance(result, Operation):
        return redirect('payments:detail', operation_id=result.id)
    if isinstance(result, Schedule):
        if announce_new and result.state == 'SCHEDULED':
            messages.success(
                request, 'Your one-time transfer is scheduled. Funds will be checked when it is due.'
            )
        return redirect('payments:schedules')
    if isinstance(result, Destination):
        if announce_new and result.status == 'ACTIVE':
            messages.success(request, 'Synthetic destination linked. It becomes available after ten minutes.')
        return redirect('payments:destinations')
    from oversight.models import Case
    from reports.models import Export

    if isinstance(result, Case):
        return redirect('/staff/cases/' + str(result.id) + '/')
    if isinstance(result, Export):
        return redirect('/account/exports/' + str(result.id) + '/')
    if kind == 'CLOSE':
        from django.contrib.auth import logout

        logout(request)
        messages.success(
            request,
            'Your synthetic wallet is closed. Retained financial history keeps its pseudonymous attribution.',
        )
        return redirect('/')
    if kind in ('ROLE_CHANGE', 'ROLE_INVITE'):
        messages.success(request, 'The staff access instruction was recorded.')
        return redirect('/staff/access/')
    if kind == 'GATE_REOPEN':
        messages.success(request, 'Recovery acknowledgement was recorded after a clean recheck.')
        return redirect('/staff/reconciliation/')
    if kind == 'RECOVERY_APPROVAL':
        messages.success(
            request,
            'The reviewed recovery result was recorded. Run reconciliation before reopening admission.',
        )
        return redirect('/staff/reconciliation/')
    messages.success(request, 'Your instruction was recorded.')
    return redirect('/staff/' if kind in ('CASE_DECISION', 'RESTRICTION') else '/wallet/')


@login_required
@require_POST
@domain_boundary
def confirm_existing(request, kind):
    allowed = {
        'csrfmiddlewaretoken',
        'intent_id',
        'grant_id',
        'action_key',
        'policy_version',
        'schema_version',
    }
    if set(request.POST) - allowed or any(
        len(request.POST.getlist(key)) != 1 for key in request.POST if key != 'csrfmiddlewaretoken'
    ):
        raise DomainError('Unsupported or repeated confirmation fields.')
    try:
        intent_id = uuid.UUID(request.POST.get('intent_id', ''))
        key = uuid.UUID(request.POST.get('action_key', ''))
    except (ValueError, TypeError, AttributeError):
        raise DomainError('Use the original intent reference and action key.')
    intent = Intent.objects.filter(id=intent_id, owner=request.user).first()
    if (
        request.POST.get('policy_version', 'DWMT-P1') != 'DWMT-P1'
        or request.POST.get('schema_version', '1') != '1'
    ):
        raise DomainError('This confirmation is stale. Start a fresh review.', 'STALE', 409)
    if (
        intent is not None
        and request.POST.get('grant_id')
        and str(intent.grant_id) != request.POST['grant_id']
    ):
        raise DomainError('This proof does not belong to the exact instruction.', 'DENIED', 403)
    result = workflows.confirm_intent(request.user, intent_id, key, expected_kind=kind)
    return result_redirect(request, intent or kind, result)


@login_required
@require_GET
@domain_boundary
def detail(request, operation_id):
    operation = get_object_or_404(owned_operations(request.user), id=operation_id)
    decorate_operations([operation], request.user)
    from ledger.models import Journal

    journals = Journal.objects.filter(operation=operation).order_by('committed_at', 'id')
    from oversight.models import Case

    cases = Case.objects.filter(operation=operation, type='DISPUTE', creator=request.user).order_by(
        '-created_at'
    )
    if 'application/json' in request.headers.get('Accept', ''):

        def instant(value):
            return value.isoformat().replace('+00:00', 'Z') if value else None

        actions = ['refresh']
        if operation.actor_id == request.user.id and operation.state in ('READY', 'REVIEW_PENDING'):
            actions.append('cancel')
        return JsonResponse(
            {
                'version': 1,
                'reference': str(operation.id),
                'kind': operation.kind,
                'state': operation.state,
                'amount_cents': operation.amount_cents,
                'currency': operation.currency,
                'parties': {'counterparty': operation.ui_counterparty},
                'timestamps': {
                    'created_at': instant(operation.created_at),
                    'completed_at': instant(operation.completed_at),
                },
                'allowed_actions': actions,
                'uncertainty_reason': 'Independent provider outcome must be investigated under this same reference.'
                if operation.state == 'UNKNOWN'
                else '',
                'timeline': [
                    {'effect': j.effect_kind, 'timestamp': instant(j.committed_at), 'reference': str(j.id)}
                    for j in journals
                ],
            }
        )
    return render(
        request,
        'payments/detail.html',
        {
            'operation': operation,
            'journals': journals,
            'cases': cases,
            'dispute_form': forms.DisputeForm(),
            'can_dispute': operation.kind == 'TRANSFER'
            and operation.state == 'COMPLETED'
            and operation.sender_id
            and operation.sender.owner_id == request.user.id
            and operation.completed_at is not None
            and timezone.now() < operation.completed_at + timedelta(days=30),
            'can_cancel': operation.actor_id == request.user.id
            and operation.state in ('READY', 'REVIEW_PENDING'),
            'page': 'activity',
        },
    )


@login_required
@require_GET
@domain_boundary
def status(request, operation_id):
    operation = get_object_or_404(owned_operations(request.user), id=operation_id)
    from accounts.services import rate_limit

    rate_limit('status', str(request.user.id), 60, 60)
    return JsonResponse(
        {
            'version': 1,
            'reference': str(operation.id),
            'id': str(operation.id),
            'state': operation.state,
            'object_version': operation.version,
            'terminal': operation.state in ('COMPLETED', 'FAILED', 'CANCELLED'),
        }
    )


@login_required
@require_POST
@domain_boundary
def cancel(request, operation_id):
    get_object_or_404(owned_operations(request.user), id=operation_id, actor=request.user)
    form = forms.VersionForm(request.POST)
    if not form.is_valid():
        raise DomainError('Refresh the payment detail and try again.')
    workflows.cancel_operation(request.user, operation_id, form.cleaned_data['version'])
    messages.success(request, 'The instruction was cancelled and its reservations were released.')
    return redirect('payments:detail', operation_id=operation_id)


@login_required
@domain_boundary
def recipients(request):
    if request.method not in ('GET', 'POST'):
        from django.http import HttpResponseNotAllowed

        return HttpResponseNotAllowed(['GET', 'POST'])
    form = forms.RecipientForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        from accounts.models import User
        from accounts.services import rate_limit
        from audit.services import record

        with transaction.atomic():
            assert_eligible(request.user)
            rate_limit('recipient_lookup', str(request.user.id), 10, 60)
            current = User.objects.select_for_update().get(id=request.user.id)
            workflows._current_actor(request.user, current)
            assert_eligible(current)
            target = User.objects.filter(
                handle=form.cleaned_data['handle'],
                status='ACTIVE',
                verified_at__isnull=False,
                wallet__state__in=['OPEN', 'OUTGOING_RESTRICTED'],
            ).first()
            if not target or target.id == request.user.id:
                form.add_error('handle', 'That exact recipient is unavailable.')
            elif Recipient.objects.filter(owner=request.user).count() >= 100:
                form.add_error(None, 'You can save up to 100 recipients. Remove one before adding another.')
            else:
                Recipient.objects.update_or_create(
                    owner=request.user, target=target, defaults={'label': form.cleaned_data['label']}
                )
                record(request.user, 'recipient.saved', str(target.id))
                messages.success(request, 'Recipient saved. The registered handle fixes the destination.')
                return redirect('payments:recipients')
    rows = Recipient.objects.filter(owner=request.user).select_related('target').order_by('label', 'id')
    return render(
        request,
        'payments/recipients.html',
        {'form': form, 'recipients': rows, 'page': 'recipients'},
        status=422 if form.errors else 200,
    )


@login_required
@require_POST
@domain_boundary
def remove_recipient(request, recipient_id):
    item = get_object_or_404(Recipient, id=recipient_id, owner=request.user)
    item.delete()
    messages.success(request, 'Saved recipient removed.')
    return redirect('payments:recipients')


@login_required
@domain_boundary
def requests_page(request):
    if request.method not in ('GET', 'POST'):
        from django.http import HttpResponseNotAllowed

        return HttpResponseNotAllowed(['GET', 'POST'])
    form = forms.RequestForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        item = workflows.create_request(
            request.user,
            form.cleaned_data['payer_handle'],
            form.cleaned_data['amount'],
            form.cleaned_data['note'],
        )
        return redirect('payments:request_detail', request_id=item.id)
    items = (
        MoneyRequest.objects.filter(Q(requester=request.user) | Q(payer=request.user))
        .select_related('requester', 'payer', 'operation')
        .order_by('-created_at')[:50]
    )
    items = list(items)
    now = timezone.now()
    for item in items:
        item.ui_state = 'EXPIRED' if item.state == 'OPEN' and now >= item.expires_at else item.state
    return render(
        request,
        'payments/requests.html',
        {'form': form, 'requests': items, 'page': 'requests'},
        status=422 if form.errors else 200,
    )


@login_required
@require_GET
@domain_boundary
def request_detail(request, request_id):
    item = get_object_or_404(
        MoneyRequest.objects.select_related('requester', 'payer', 'operation').filter(
            Q(requester=request.user) | Q(payer=request.user)
        ),
        id=request_id,
    )
    return render(
        request,
        'payments/request_detail.html',
        {'item': item, 'page': 'requests', 'expired': timezone.now() >= item.expires_at},
    )


@login_required
@require_POST
@domain_boundary
def request_action(request, request_id, action):
    item = get_object_or_404(
        MoneyRequest.objects.filter(Q(requester=request.user) | Q(payer=request.user)), id=request_id
    )
    if action == 'pay':
        if request.POST.get('intent_id'):
            try:
                proof_id = uuid.UUID(request.POST['intent_id'])
            except (ValueError, TypeError, AttributeError):
                raise DomainError('Use a valid instruction reference.')
            proof = Intent.objects.filter(id=proof_id, owner=request.user).first()
            if proof is None:
                try:
                    key = uuid.UUID(request.POST.get('action_key', ''))
                except (ValueError, TypeError, AttributeError):
                    raise DomainError('Use the original request action key.')
                accepted = match_accepted_reference(
                    request.user, key, family='REQUEST_PAY', intent_id=proof_id
                )
                if not accepted.operation_id or accepted.operation_id != item.operation_id:
                    raise DomainError('No accepted payment matches this exact request.', 'CONFLICT', 409)
            elif proof.instruction.get('request_id') != str(item.id):
                raise DomainError('This proof is bound to a different request.', 'DENIED', 403)
            return confirm_existing(request, 'REQUEST_PAY')
        if set(request.POST) - {'csrfmiddlewaretoken', 'policy_version'}:
            raise DomainError('Unsupported request payment fields.')
        if item.payer_id != request.user.id:
            raise Http404
        intent = workflows.create_intent(
            request.user,
            'REQUEST_PAY',
            {'request_id': str(item.id), 'policy_version': request.POST.get('policy_version', 'DWMT-P1')},
        )
        return redirect('payments:intent_review', intent_id=intent.id)
    form = forms.VersionForm(request.POST)
    if not form.is_valid():
        raise DomainError('Refresh the request before deciding it.')
    workflows.decide_request(request.user, item.id, action, form.cleaned_data['version'])
    return redirect('payments:request_detail', request_id=item.id)


@login_required
@domain_boundary
def schedules(request):
    if request.method == 'POST':
        return (
            confirm_existing(request, 'SCHEDULE')
            if request.POST.get('intent_id')
            else prepare_with_kind(request, 'SCHEDULE')
        )
    if request.method != 'GET':
        from django.http import HttpResponseNotAllowed

        return HttpResponseNotAllowed(['GET', 'POST'])
    items = (
        Schedule.objects.filter(owner=request.user)
        .select_related('recipient__owner', 'operation')
        .order_by('-created_at')[:50]
    )
    return render(
        request,
        'payments/schedules.html',
        {
            'schedules': items,
            'form': forms.ScheduleForm(initial={'timezone': request.user.timezone}),
            'page': 'schedules',
        },
    )


@login_required
@require_POST
@domain_boundary
def cancel_schedule(request, schedule_id):
    get_object_or_404(Schedule, id=schedule_id, owner=request.user)
    form = forms.VersionForm(request.POST)
    if not form.is_valid():
        raise DomainError('Refresh the schedules page before cancelling.')
    workflows.cancel_schedule(request.user, schedule_id, form.cleaned_data['version'])
    return redirect('payments:schedules')


@login_required
@domain_boundary
def destinations(request):
    if request.method == 'POST':
        return (
            confirm_existing(request, 'DESTINATION')
            if request.POST.get('intent_id')
            else prepare_with_kind(request, 'DESTINATION')
        )
    if request.method != 'GET':
        from django.http import HttpResponseNotAllowed

        return HttpResponseNotAllowed(['GET', 'POST'])
    return render(
        request,
        'payments/destinations.html',
        {
            'destinations': Destination.objects.filter(owner=request.user).order_by('-created_at'),
            'form': forms.DestinationForm(),
            'page': 'destinations',
        },
    )


@login_required
@require_POST
@domain_boundary
def disable_destination(request, destination_id):
    from accounts.models import User
    from audit.services import record

    with transaction.atomic():
        user = User.objects.select_for_update().get(id=request.user.id)
        workflows._current_actor(request.user, user)
        assert_eligible(user)
        wallet = wallet_for(user)
        if wallet:
            type(wallet).objects.select_for_update().get(id=wallet.id)
        destination = get_object_or_404(
            Destination.objects.select_for_update(), id=destination_id, owner=user
        )
        destination.status = 'DISABLED'
        destination.save(update_fields=['status'])
        record(user, 'destination.disabled', str(destination.id))
    messages.success(
        request,
        'Destination disabled for future admissions. Already submitted payments retain their settlement duty.',
    )
    return redirect('payments:destinations')


@login_required
@require_POST
@domain_boundary
def dispute(request, operation_id):
    get_object_or_404(owned_operations(request.user), id=operation_id)
    form = forms.DisputeForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            'payments/form_page.html',
            {
                'form': form,
                'heading': 'Open a dispute',
                'description': 'Explain what happened. A dispute does not guarantee a refund.',
                'submit_label': 'Open dispute',
                'page': 'activity',
            },
            status=422,
        )
    case = workflows.open_dispute(
        request.user, operation_id, form.cleaned_data['reason'], form.cleaned_data['evidence']
    )
    return redirect('payments:dispute_detail', case_id=case.id)


@login_required
@require_GET
@domain_boundary
def dispute_detail(request, case_id):
    from oversight.models import Case

    case = get_object_or_404(
        Case.objects.select_related('operation'), id=case_id, type='DISPUTE', creator=request.user
    )
    return render(request, 'payments/dispute.html', {'case': case, 'page': 'activity'})


@login_required
@require_GET
@domain_boundary
def accepted_action(request, family, action_key):
    families = {
        'TRANSFER',
        'FUNDING',
        'WITHDRAWAL',
        'REQUEST_PAY',
        'SCHEDULE',
        'DESTINATION',
        'CASE_DECISION',
        'RESTRICTION',
        'CLOSE',
        'EXPORT',
        'ROLE_CHANGE',
        'ROLE_INVITE',
        'GATE_REOPEN',
        'RECOVERY_APPROVAL',
    }
    family = family.upper()
    if family not in families:
        raise Http404
    result = workflows.resolve_accepted_action(request.user, action_key, family=family)
    if 'application/json' not in request.headers.get('Accept', ''):
        return result_redirect(request, family, result)
    state = getattr(result, 'state', getattr(result, 'status', None))
    if hasattr(result, 'is_open'):
        state = 'OPEN' if result.is_open else 'GATED'
    expires = getattr(result, 'expires_at', None)
    if expires and timezone.now() >= expires and result._meta.app_label == 'reports':
        state = 'EXPIRED'
    location = '/wallet/'
    model = result._meta.label_lower
    if isinstance(result, Operation):
        location = f'/payments/{result.id}/'
    elif isinstance(result, Schedule):
        location = '/schedules/'
    elif isinstance(result, Destination):
        location = '/destinations/'
    elif model == 'oversight.case':
        location = f'/staff/cases/{result.id}/'
    elif model == 'oversight.recoveryproposal':
        location = f'/staff/reconciliation/recovery/{result.id}/'
    elif model == 'reports.export':
        location = f'/account/exports/{result.id}/'
    elif family in {'ROLE_CHANGE', 'ROLE_INVITE'}:
        location = '/staff/access/'
    elif family == 'GATE_REOPEN':
        location = '/staff/reconciliation/'
    return JsonResponse(
        {'version': 1, 'reference': str(result.pk), 'family': family, 'state': state, 'detail_url': location}
    )
