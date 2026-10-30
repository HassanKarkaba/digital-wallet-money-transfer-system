import uuid
from datetime import timedelta
from functools import wraps

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from accounts.models import Role, User
from accounts.services import assert_eligible, db_now
from audit.services import record
from ledger.models import IntegrityGate, Wallet
from payments import workflows
from payments.errors import DomainError
from payments.forms import StrictForm
from payments.models import Operation
from payments.views import domain_boundary, redirect

from .models import Case, Finding, ReconciliationRun


def staff_required(roles=None):
    allowed = set(roles or ('SUPPORT', 'REVIEWER', 'FINANCE', 'ACCESS_ADMIN'))

    def decorate(view):
        @login_required
        @domain_boundary
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            def invoke(current):
                assert_eligible(current)
                actual = set(Role.objects.filter(user=current).values_list('role', flat=True))
                if not actual.intersection(allowed):
                    raise DomainError(
                        'Your current staff role does not permit this workspace.', 'DENIED', 403
                    )
                request.staff_roles = actual
                return view(request, *args, **kwargs)

            if request.method == 'GET':
                with transaction.atomic():
                    current = User.objects.select_for_update().get(pk=request.user.id)
                    workflows._current_actor(request.user, current)
                    return invoke(current)
            # Monetary and queued POST coordinators own their complete sorted lock order.
            return invoke(request.user)

        return wrapped

    return decorate


class DecisionForm(StrictForm):
    decision = forms.ChoiceField(
        choices=[('APPROVE', 'Approve'), ('REJECT', 'Reject'), ('CLOSE_NO_REFUND', 'Close without refund')]
    )
    reason = forms.CharField(
        min_length=10,
        max_length=1000,
        widget=forms.Textarea(attrs={'rows': 4}),
        help_text='Customer-visible decision explanation. Do not include private staff notes or credentials.',
    )
    version = forms.IntegerField(widget=forms.HiddenInput)


class ReasonForm(StrictForm):
    reason = forms.CharField(min_length=10, max_length=1000, widget=forms.Textarea(attrs={'rows': 3}))
    version = forms.IntegerField(required=False, widget=forms.HiddenInput)


class RestrictionForm(ReasonForm):
    state = forms.ChoiceField(
        choices=[('OPEN', 'Open'), ('OUTGOING_RESTRICTED', 'Outgoing restricted'), ('FROZEN', 'Frozen')],
        label='Wallet state',
    )


@staff_required()
@require_GET
def workspace(request):
    if not request.staff_roles.intersection({'SUPPORT', 'REVIEWER', 'FINANCE'}):
        return redirect('/staff/access/')
    query = Case.objects.select_related('operation__sender__owner', 'operation__recipient__owner', 'creator')
    state = request.GET.get('state', 'OPEN')
    case_type = request.GET.get('type', '')
    age = request.GET.get('age', '')
    if state:
        query = query.filter(state=state)
    if case_type in ('RISK', 'DISPUTE', 'REVERSAL', 'SUPPORT'):
        query = query.filter(type=case_type)
    if age:
        try:
            hours = int(age)
            if hours not in (1, 6, 24):
                raise ValueError
        except ValueError:
            raise DomainError('Choose a supported queue age.')
        query = query.filter(created_at__lte=db_now() - timedelta(hours=hours))
    page_obj = Paginator(query.order_by('created_at', 'id'), 30).get_page(request.GET.get('page', 1))
    exact = request.GET.get('q', '').strip()
    customer = None
    operation = None
    if exact:
        if len(exact) > 100:
            raise DomainError('Use an exact handle or UUID reference.')
        if not request.staff_roles.intersection({'SUPPORT', 'REVIEWER', 'FINANCE'}):
            raise DomainError(
                'An operational support, review, or finance role is required for an exact investigation.',
                'DENIED',
                403,
            )
        try:
            reference = uuid.UUID(exact)
        except ValueError:
            reference = None
        if reference:
            operation = (
                Operation.objects.select_related('sender__owner', 'recipient__owner')
                .filter(pk=reference)
                .first()
            )
            customer = User.objects.filter(pk=reference).first()
        else:
            customer = User.objects.filter(handle=exact.removeprefix('@')).first()
        record(
            request.user,
            'staff.exact_lookup',
            exact,
            reason='Exact customer reference for support or wallet restriction investigation',
            metadata={'found': bool(customer or operation)},
        )
    record(request.user, 'staff.queue.read', 'case_queue', reason='Operational risk and dispute review')
    return render(
        request,
        'oversight/workspace.html',
        {
            'cases': page_obj.object_list,
            'page_obj': page_obj,
            'state': state,
            'case_type': case_type,
            'age': age,
            'query': exact,
            'customer': customer,
            'found_operation': operation,
            'roles': request.staff_roles,
            'gate': IntegrityGate.objects.filter(pk=1).first(),
            'page': 'staff',
        },
    )


@staff_required(('SUPPORT', 'REVIEWER', 'FINANCE'))
@require_GET
def case_detail(request, case_id):
    case = get_object_or_404(
        Case.objects.select_related(
            'operation__sender__owner',
            'operation__recipient__owner',
            'operation__destination',
            'creator',
            'reviewer',
            'parent',
        ),
        id=case_id,
    )
    participants = {case.operation.actor_id}
    if case.operation.sender_id:
        participants.add(case.operation.sender.owner_id)
    if case.operation.recipient_id:
        participants.add(case.operation.recipient.owner_id)
    can_decide = (
        'REVIEWER' in request.staff_roles
        and request.user.id not in participants
        and request.user.id != case.creator_id
        and case.state == 'OPEN'
    )
    can_propose = (
        'FINANCE' in request.staff_roles
        and case.type == 'DISPUTE'
        and case.state in ('OPEN', 'BLOCKED_FUNDS', 'BLOCKED_CAPACITY')
    )
    decision_form = DecisionForm(initial={'version': case.version}, auto_id='decision_%s')
    if case.type in ('DISPUTE', 'SUPPORT'):
        decision_form.fields['decision'].choices = [('CLOSE_NO_REFUND', 'Close without refund')]
    else:
        decision_form.fields['decision'].choices = [('APPROVE', 'Approve'), ('REJECT', 'Reject')]
    record(request.user, 'staff.case.read', str(case.id), reason='Assigned operational case investigation')
    return render(
        request,
        'oversight/case.html',
        {
            'case': case,
            'operation': case.operation,
            'can_decide': can_decide,
            'can_propose': can_propose,
            'decision_form': decision_form,
            'reason_form': ReasonForm(auto_id='proposal_%s'),
            'assignment_form': CaseAssignmentForm(initial={'version': case.version}, auto_id='assignment_%s'),
            'can_assign_finance': bool(request.staff_roles.intersection({'SUPPORT', 'REVIEWER'})),
            'can_inspect_statement': 'FINANCE' in request.staff_roles
            and case.assigned_finance_id == request.user.id,
            'proposals': case.proposals.select_related('reviewer', 'creator').order_by('-created_at'),
            'page': 'staff',
        },
    )


@staff_required(('REVIEWER',))
@require_POST
def prepare_decision(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    form = DecisionForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            'payments/form_page.html',
            {
                'form': form,
                'heading': 'Review case decision',
                'description': 'A reason and current version are required.',
                'submit_label': 'Review decision',
                'page': 'staff',
            },
            status=422,
        )
    intent = workflows.create_intent(
        request.user, 'CASE_DECISION', {'case_id': str(case.id), **form.cleaned_data}
    )
    return redirect('payments:intent_review', intent_id=intent.id)


@staff_required(('REVIEWER',))
@require_POST
def prepare_restriction(request, wallet_id):
    wallet = get_object_or_404(Wallet, id=wallet_id)
    form = RestrictionForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            'payments/form_page.html',
            {
                'form': form,
                'heading': 'Review wallet restriction',
                'description': 'Give the exact target and an accountable reason.',
                'submit_label': 'Review restriction',
                'page': 'staff',
            },
            status=422,
        )
    instruction = {'wallet_id': str(wallet.id), **form.cleaned_data}
    instruction['version'] = form.cleaned_data.get('version') or wallet.version
    intent = workflows.create_intent(request.user, 'RESTRICTION', instruction)
    return redirect('payments:intent_review', intent_id=intent.id)


@staff_required(('FINANCE',))
@require_POST
def propose_reversal(request, case_id):
    case = get_object_or_404(Case, id=case_id, type='DISPUTE')
    form = ReasonForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            'payments/form_page.html',
            {
                'form': form,
                'heading': 'Propose whole reversal',
                'description': 'Only the whole original amount can be proposed.',
                'submit_label': 'Create proposal',
                'page': 'staff',
            },
            status=422,
        )
    proposal = workflows.propose_reversal(request.user, case.id, form.cleaned_data['reason'])
    messages.success(request, 'Whole reversal proposed. An eligible different reviewer must decide it.')
    return redirect('oversight:case_detail', case_id=proposal.id)


@staff_required(('SUPPORT', 'FINANCE'))
@require_POST
def open_support_case(request, operation_id):
    operation = get_object_or_404(Operation, id=operation_id)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        raise DomainError('Give a reason of 10 to 1,000 characters.')
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        workflows._current_actor(request.user, user)
        assert_eligible(user)
        if not Role.objects.filter(user=user, role__in=['SUPPORT', 'FINANCE']).exists():
            raise DomainError('Your staff authority changed.', 'DENIED', 403)
        case = Case.objects.create(
            operation=operation,
            type='SUPPORT',
            creator=user,
            reason=form.cleaned_data['reason'],
            state='OPEN',
            created_at=db_now(),
        )
        record(user, 'support.case.opened', str(case.id), reason=form.cleaned_data['reason'])
    return redirect('oversight:case_detail', case_id=case.id)


@staff_required(('FINANCE', 'REVIEWER'))
@require_GET
def reconciliation(request):
    record(
        request.user,
        'staff.reconciliation.read',
        'reconciliation',
        reason='Operational integrity investigation',
    )
    from .models import RecoveryProposal

    proposals = RecoveryProposal.objects.select_related('creator', 'reviewer', 'finding').order_by(
        '-created_at'
    )[:30]
    return render(
        request,
        'oversight/reconciliation.html',
        {
            'proposals': proposals,
            'runs': ReconciliationRun.objects.order_by('-created_at')[:20],
            'findings': Finding.objects.select_related('operation', 'resolved_by').order_by('-created_at')[
                :50
            ],
            'gate': IntegrityGate.objects.filter(pk=1).first(),
            'can_run': 'FINANCE' in request.staff_roles,
            'can_reopen': 'REVIEWER' in request.staff_roles,
            'reason_form': ReasonForm(),
            'page': 'staff',
        },
    )


@staff_required(('FINANCE',))
@require_POST
def run_reconciliation(request):
    from payments.reconciliation import queue_reconciliation

    run = queue_reconciliation(request.user)
    messages.success(
        request,
        f'Reconciliation queued under reference {run.id}. Progress and findings remain available on this page.',
    )
    return redirect('oversight:reconciliation')


@staff_required(('FINANCE',))
@require_POST
def resolve_finding(request, finding_id):
    form = ReasonForm(request.POST)
    if not form.is_valid():
        raise DomainError('Provide an investigation reason of 10 to 1,000 characters.')
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=request.user.id)
        workflows._current_actor(request.user, current)
        assert_eligible(current, staff_role='FINANCE')
        finding = get_object_or_404(Finding.objects.select_for_update(), id=finding_id)
        if finding.state != 'OPEN':
            raise DomainError('This finding was already resolved.', 'STALE', 409)
        # A note never starts provider work or manufactures payment certainty.
        run = (
            ReconciliationRun.objects.filter(completed_at__isnull=False)
            .order_by('-completed_at', '-id')
            .first()
        )
        now = db_now()
        baseline = (getattr(run, 'cutoff_at', None) or run.created_at) if run else None
        if (
            not run
            or not run.clean
            or now - run.completed_at > timedelta(minutes=5)
            or baseline < finding.created_at
        ):
            raise DomainError(
                'Queue a fresh reconciliation and wait for a completed clean recheck after this finding before recording its resolution.',
                'UNRESOLVED',
                409,
            )
        finding.state = 'RESOLVED'
        finding.resolved_at = now
        finding.resolved_by = current
        finding.reason = form.cleaned_data['reason']
        finding.save(update_fields=['state', 'resolved_at', 'resolved_by', 'reason'])
        record(
            current,
            'reconciliation.finding.resolved',
            str(finding.id),
            reason=finding.reason,
            metadata={'clean_run': str(run.id)},
        )
    return redirect('oversight:reconciliation')


@staff_required(('REVIEWER',))
@require_POST
def prepare_gate_reopen(request):
    form = ReasonForm(request.POST)
    if not form.is_valid():
        raise DomainError('Give an accountable reason of 10 to 1,000 characters.')
    gate = IntegrityGate.objects.get(pk=1)
    intent = workflows.create_intent(
        request.user,
        'GATE_REOPEN',
        {
            'reason': form.cleaned_data['reason'],
            'version': form.cleaned_data.get('version') or gate.version,
        },
    )
    return redirect('payments:intent_review', intent_id=intent.id)


def access(request):
    from accounts.views import staff_access

    return staff_access(request)


class RecoveryProposalForm(StrictForm):
    finding_id = forms.UUIDField(label='Exact finding reference')
    kind = forms.ChoiceField(
        choices=[
            ('REBUILD_PROJECTIONS', 'Rebuild projections from immutable entries'),
            ('RECOVER_OPERATION', 'Recover retained independent provider operation'),
        ]
    )
    reason = forms.CharField(min_length=10, max_length=1000, widget=forms.Textarea(attrs={'rows': 3}))


@staff_required(('FINANCE',))
@require_POST
def propose_recovery(request):
    from .recovery import propose_recovery as propose

    form = RecoveryProposalForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            'payments/form_page.html',
            {
                'form': form,
                'heading': 'Propose reviewed recovery',
                'description': 'Select an exact finding. The server derives the authoritative recovery plan.',
                'submit_label': 'Create recovery proposal',
                'page': 'staff',
            },
            status=422,
        )
    proposal = propose(
        request.user, form.cleaned_data['finding_id'], form.cleaned_data['reason'], form.cleaned_data['kind']
    )
    return redirect('oversight:recovery_detail', proposal_id=proposal.id)


@staff_required(('FINANCE', 'REVIEWER'))
@require_GET
def recovery_detail(request, proposal_id):
    from .models import RecoveryProposal

    proposal = get_object_or_404(
        RecoveryProposal.objects.select_related(
            'creator', 'reviewer', 'finding__operation__sender', 'finding__operation__recipient'
        ),
        id=proposal_id,
    )
    participants = {proposal.creator_id}
    operation = proposal.finding.operation
    if operation:
        participants.add(operation.actor_id)
        if operation.sender_id:
            participants.add(operation.sender.owner_id)
        if operation.recipient_id:
            participants.add(operation.recipient.owner_id)
    if proposal.payload.get('wallet_id'):
        owner_id = (
            Wallet.objects.filter(id=proposal.payload['wallet_id']).values_list('owner_id', flat=True).first()
        )
        if owner_id:
            participants.add(owner_id)
    record(
        request.user,
        'staff.recovery_proposal.read',
        str(proposal.id),
        reason='Reviewed recovery investigation',
    )
    return render(
        request,
        'oversight/recovery.html',
        {
            'proposal': proposal,
            'can_approve': 'REVIEWER' in request.staff_roles
            and request.user.id not in participants
            and proposal.state == 'OPEN',
            'reason_form': ReasonForm(initial={'version': proposal.version}),
            'page': 'staff',
        },
    )


@staff_required(('REVIEWER',))
@require_POST
def prepare_recovery_approval(request, proposal_id):
    from .models import RecoveryProposal

    proposal = get_object_or_404(RecoveryProposal, id=proposal_id)
    form = ReasonForm(request.POST)
    if not form.is_valid() or form.cleaned_data.get('version') is None:
        raise DomainError('Give a reason and current recovery proposal version.')
    intent = workflows.create_intent(
        request.user,
        'RECOVERY_APPROVAL',
        {
            'proposal_id': str(proposal.id),
            'reason': form.cleaned_data['reason'],
            'version': form.cleaned_data['version'],
        },
    )
    return redirect('payments:intent_review', intent_id=intent.id)


class CaseAssignmentForm(StrictForm):
    finance_email = forms.EmailField(max_length=254, label='Exact Finance account email')
    reason = forms.CharField(
        min_length=10, max_length=1000, widget=forms.Textarea(attrs={'rows': 3}), label='Assignment purpose'
    )
    version = forms.IntegerField(widget=forms.HiddenInput)


class CaseStatementForm(StrictForm):
    owner_id = forms.ChoiceField(label='Case participant')
    start = forms.DateField(label='Start date · UTC', widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='End date · UTC (exclusive)', widget=forms.DateInput(attrs={'type': 'date'}))
    timezone = forms.CharField(max_length=64, initial='UTC', label='Display timezone')
    purpose = forms.CharField(
        min_length=10,
        max_length=1000,
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Staff-only investigation purpose. Excluded from the customer statement file.',
    )
    version = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, participants=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['owner_id'].choices = [(str(user.id), '@' + user.handle) for user in participants]

    def clean(self):
        cleaned = super().clean()
        from reports.forms import StatementForm

        if all(cleaned.get(key) for key in ('start', 'end', 'timezone')):
            period = StatementForm(
                {
                    'start': cleaned['start'].isoformat(),
                    'end': cleaned['end'].isoformat(),
                    'timezone': cleaned['timezone'],
                }
            )
            if not period.is_valid():
                raise forms.ValidationError(
                    'Choose a valid IANA timezone and a period no longer than 366 days.'
                )
            cleaned['start_at'] = period.cleaned_data['start_at']
            cleaned['end_at'] = period.cleaned_data['end_at']
        return cleaned


@staff_required(('SUPPORT', 'REVIEWER'))
@require_POST
def assign_finance(request, case_id):
    form = CaseAssignmentForm(request.POST)
    if not form.is_valid():
        raise DomainError('Provide an exact Finance email, purpose, and current case version.')
    target = (
        Role.objects.select_related('user')
        .filter(role='FINANCE', user__email=form.cleaned_data['finance_email'].strip().lower())
        .first()
    )
    if not target:
        raise DomainError('That exact eligible Finance account is unavailable.')
    with transaction.atomic():
        users = {
            user.id: user
            for user in User.objects.select_for_update()
            .filter(id__in=[request.user.id, target.user_id])
            .order_by('id')
        }
        current = users[request.user.id]
        assignee = users[target.user_id]
        workflows._current_actor(request.user, current)
        assert_eligible(current)
        if not Role.objects.filter(user=current, role__in=['SUPPORT', 'REVIEWER']).exists():
            raise DomainError('Your assignment authority changed.', 'DENIED', 403)
        assert_eligible(assignee, staff_role='FINANCE')
        case = get_object_or_404(Case.objects.select_for_update(), id=case_id)
        if case.version != form.cleaned_data['version']:
            raise DomainError('The case changed. Refresh before assigning an investigator.', 'STALE', 409)
        previous = case.assigned_finance_id
        case.assigned_finance = assignee
        case.version += 1
        case.save(update_fields=['assigned_finance', 'version'])
        record(
            current,
            'staff.case.finance_assigned',
            str(case.id),
            reason=form.cleaned_data['reason'],
            metadata={'previous': str(previous) if previous else None, 'assigned_finance': str(assignee.id)},
        )
    messages.success(request, 'The Finance investigator was assigned to this exact case.')
    return redirect('oversight:case_detail', case_id=case_id)


def _case_participants(case):
    users = []
    if case.operation.sender_id:
        users.append(case.operation.sender.owner)
    if case.operation.recipient_id and case.operation.recipient.owner_id not in {user.id for user in users}:
        users.append(case.operation.recipient.owner)
    return users


def _assigned_case(request, case_id, *, lock=False):
    from django.http import Http404

    current = User.objects.select_for_update().get(pk=request.user.id) if lock else request.user
    if lock:
        workflows._current_actor(request.user, current)
    try:
        assert_eligible(current, staff_role='FINANCE')
    except ValueError:
        raise Http404 from None
    query = Case.objects.select_related('operation__sender__owner', 'operation__recipient__owner')
    if lock:
        query = query.select_for_update(of=('self',))
    return get_object_or_404(query, id=case_id, assigned_finance=current)


@login_required
@domain_boundary
def case_statements(request, case_id):
    from datetime import timedelta

    from reports.case_access import queue_case_statement

    from .models import CaseStatement

    case = _assigned_case(request, case_id)
    participants = _case_participants(case)
    now = db_now()
    form = CaseStatementForm(
        request.POST or None,
        participants=participants,
        initial={
            'version': case.version,
            'start': now.date().replace(day=1),
            'end': (now + timedelta(days=1)).date(),
            'timezone': 'UTC',
        },
    )
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        link = queue_case_statement(
            request.user,
            case.id,
            uuid.UUID(data['owner_id']),
            data['start_at'],
            data['end_at'],
            data['timezone'],
            data['purpose'],
            data['version'],
        )
        return redirect('oversight:case_statement_detail', case_id=case.id, link_id=link.id)
    if request.method not in ('GET', 'POST'):
        from django.http import HttpResponseNotAllowed

        return HttpResponseNotAllowed(['GET', 'POST'])
    with transaction.atomic():
        case = _assigned_case(request, case_id, lock=True)
        record(
            request.user,
            'staff.case.statements.read',
            str(case.id),
            reason='Assigned Finance statement investigation',
        )
        return render(
            request,
            'oversight/case_statements.html',
            {
                'case': case,
                'form': form,
                'case_statements': CaseStatement.objects.filter(case=case)
                .select_related('statement', 'owner')
                .order_by('-created_at')[:30],
                'page': 'staff',
            },
            status=422 if form.errors else 200,
        )


@login_required
@domain_boundary
@require_GET
@transaction.atomic
def case_statement_detail(request, case_id, link_id):
    from .models import CaseStatement

    case = _assigned_case(request, case_id, lock=True)
    link = get_object_or_404(
        CaseStatement.objects.select_related('statement', 'owner'), id=link_id, case=case
    )
    item = link.statement
    record(
        request.user,
        'staff.case.statement.read',
        str(link.id),
        reason=link.purpose,
        metadata={'case': str(case.id), 'owner': str(link.owner_id)},
    )
    if item.state != 'READY':
        if db_now() >= item.expires_at:
            item.state = 'EXPIRED'
        return render(
            request,
            'reports/statement_pending.html',
            {'statement': item, 'statement_detail_url': request.path, 'staff_case': case, 'page': 'staff'},
        )
    if db_now() >= item.expires_at:
        raise DomainError(
            'This case statement expired. Queue a fresh authorized statement.', 'FILE_EXPIRED', 410
        )
    try:
        page_number = max(1, min(int(request.GET.get('page', 1)), 2001))
    except ValueError:
        raise DomainError('Enter a valid statement page.')
    offset = (page_number - 1) * 50
    return render(
        request,
        'reports/statement.html',
        {
            'statement': item,
            'rows': item.rows[offset : offset + 50],
            'page_number': page_number,
            'has_next': offset + 50 < item.row_count,
            'has_previous': page_number > 1,
            'staff_case': case,
            'case_statement': link,
            'staff_download_url': f'/staff/cases/{case.id}/statements/{link.id}/download/',
            'page': 'staff',
        },
    )


@login_required
@domain_boundary
@require_GET
@transaction.atomic
def case_statement_download(request, case_id, link_id):
    from django.http import FileResponse

    from reports.services import resolve_private

    from .models import CaseStatement

    case = _assigned_case(request, case_id, lock=True)
    link = get_object_or_404(CaseStatement.objects.select_related('statement'), id=link_id, case=case)
    item = link.statement
    if db_now() >= item.expires_at:
        raise DomainError('This private case statement expired.', 'FILE_EXPIRED', 410)
    if item.state != 'READY':
        raise DomainError('This case statement is not ready.', 'FILE_PENDING', 409)
    record(
        request.user,
        'staff.case.statement.downloaded',
        str(link.id),
        reason=link.purpose,
        metadata={'case': str(case.id), 'statement': str(item.id)},
    )
    path = resolve_private(item.file_name)
    response = FileResponse(
        path.open('rb'),
        as_attachment=True,
        filename=f'demo-case-statement-{item.id}.csv',
        content_type='text/csv; charset=utf-8',
    )
    response['Cache-Control'] = 'no-store'
    return response
