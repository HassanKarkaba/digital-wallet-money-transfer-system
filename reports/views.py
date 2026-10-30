from datetime import timedelta

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from accounts.services import db_now
from payments import workflows
from payments.errors import DomainError
from payments.forms import StrictForm
from payments.views import domain_boundary, redirect

from .forms import StatementForm
from .models import Export, Statement
from .services import queue_statement, resolve_private


class AccountActionForm(StrictForm):
    extra_allowed = StrictForm.extra_allowed | {'action'}
    password = forms.CharField(label='Current password', widget=forms.PasswordInput, max_length=128)
    acknowledgement = forms.CharField(
        required=False,
        max_length=100,
        label='Closure acknowledgement',
        help_text='For closure only: enter CLOSE MY WALLET.',
    )


def _owner_file(request, model, file_id):
    item = get_object_or_404(model, id=file_id, owner=request.user)
    if db_now() >= item.expires_at or item.state == 'EXPIRED':
        raise DomainError('This private file expired. Generate a fresh export.', 'FILE_EXPIRED', 410)
    if item.state != 'READY':
        raise DomainError('This file is not ready. Return to its status page.', 'FILE_PENDING', 409)
    return item, resolve_private(item.file_name)


@login_required
@require_GET
@domain_boundary
def statements(request):
    now = db_now()
    form = StatementForm(
        initial={
            'start': now.date().replace(day=1),
            'end': (now + timedelta(days=1)).date(),
            'timezone': request.user.timezone,
        }
    )
    return render(
        request,
        'reports/statements.html',
        {'form': form, 'statements': Statement.objects.filter(owner=request.user)[:50], 'page': 'statements'},
    )


@login_required
@require_POST
@domain_boundary
def generate(request):
    form = StatementForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            'reports/statements.html',
            {
                'form': form,
                'statements': Statement.objects.filter(owner=request.user)[:50],
                'page': 'statements',
            },
            status=422,
        )
    item = queue_statement(
        request.user,
        form.cleaned_data['start_at'],
        form.cleaned_data['end_at'],
        form.cleaned_data['timezone'],
    )
    if 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse(
            {
                'version': 1,
                'reference': str(item.id),
                'state': item.state,
                'detail_url': f'/statements/{item.id}/',
            },
            status=202,
        )
    return redirect('reports:statement', statement_id=item.id)


@login_required
@require_GET
@domain_boundary
def statement(request, statement_id):
    item = get_object_or_404(Statement, id=statement_id, owner=request.user)
    if item.state != 'READY':
        if db_now() >= item.expires_at:
            item.state = 'EXPIRED'
        return render(request, 'reports/statement_pending.html', {'statement': item, 'page': 'statements'})
    item, _ = _owner_file(request, Statement, statement_id)
    try:
        page = max(1, min(int(request.GET.get('page', 1)), 2001))
    except ValueError:
        raise DomainError('Enter a valid statement page.')
    offset = (page - 1) * 50
    return render(
        request,
        'reports/statement.html',
        {
            'statement': item,
            'rows': item.rows[offset : offset + 50],
            'page_number': page,
            'has_next': offset + 50 < item.row_count,
            'has_previous': page > 1,
            'page': 'statements',
        },
    )


@login_required
@require_GET
@domain_boundary
def download(request, statement_id):
    item, path = _owner_file(request, Statement, statement_id)
    response = FileResponse(
        path.open('rb'),
        as_attachment=True,
        filename=f'demo-wallet-statement-{item.start_at.date()}-{item.id}.csv',
        content_type='text/csv; charset=utf-8',
    )
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@domain_boundary
def account_data(request):
    if request.method not in ('GET', 'POST'):
        from django.http import HttpResponseNotAllowed

        return HttpResponseNotAllowed(['GET', 'POST'])
    from accounts.services import closure_blockers

    form = AccountActionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        if not request.user.check_password(form.cleaned_data['password']):
            form.add_error('password', 'The current password is incorrect.')
        else:
            action = request.POST.get('action', 'EXPORT')
            if action not in ('EXPORT', 'CLOSE'):
                raise DomainError('Choose export or closure.')
            instruction = {}
            if action == 'CLOSE':
                instruction = {
                    'acknowledgement': form.cleaned_data['acknowledgement'],
                    'version': request.user.authority_version,
                }
            intent = workflows.create_intent(request.user, action, instruction)
            return redirect('payments:intent_review', intent_id=intent.id)
    if form.is_bound:
        form.data = form.data.copy()
        form.data['password'] = ''
    blockers = closure_blockers(request.user)
    return render(
        request,
        'reports/account_data.html',
        {
            'form': form,
            'exports': Export.objects.filter(owner=request.user)[:20],
            'blockers': blockers,
            'page': 'settings',
        },
        status=422 if form.errors else 200,
    )


@login_required
@require_POST
@domain_boundary
def prepare_export(request):
    return _prepare_account_action(request, 'EXPORT')


@login_required
@require_POST
@domain_boundary
def prepare_close(request):
    return _prepare_account_action(request, 'CLOSE')


def _prepare_account_action(request, action):
    allowed = {'csrfmiddlewaretoken', 'password', 'acknowledgement', 'version'}
    if set(request.POST) - allowed:
        raise DomainError('Unsupported account action fields.')
    if not request.user.check_password(request.POST.get('password', '')):
        raise DomainError('Confirm your current password before continuing.')
    instruction = (
        {}
        if action == 'EXPORT'
        else {
            'acknowledgement': request.POST.get('acknowledgement'),
            'version': request.POST.get('version', request.user.authority_version),
        }
    )
    intent = workflows.create_intent(request.user, action, instruction)
    return redirect('payments:intent_review', intent_id=intent.id)


@login_required
@require_GET
@domain_boundary
def export_detail(request, export_id):
    item = get_object_or_404(Export, id=export_id, owner=request.user)
    if request.GET.get('download') == '1':
        item, path = _owner_file(request, Export, export_id)
        response = FileResponse(
            path.open('rb'),
            as_attachment=True,
            filename=f'demo-wallet-data-{item.id}.json',
            content_type='application/json',
        )
        response['Cache-Control'] = 'no-store'
        return response
    return render(
        request,
        'reports/export.html',
        {'export': item, 'expired': db_now() >= item.expires_at, 'page': 'settings'},
    )
