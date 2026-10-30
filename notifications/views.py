from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_POST

from accounts.services import db_now
from payments.views import redirect

from .models import Notification


@login_required
def inbox(request):
    page = Paginator(Notification.objects.filter(recipient=request.user), 30).get_page(
        request.GET.get('page', 1)
    )
    return render(request, 'notifications/inbox.html', {'page_obj': page, 'notifications': page.object_list})


@login_required
@require_POST
def mark_read(request, notification_id):
    count = Notification.objects.filter(pk=notification_id, recipient=request.user).update(read_at=db_now())
    if not count:
        raise Http404
    return redirect('notifications:inbox')


@login_required
@require_POST
def preferences(request):
    unknown = set(request.POST) - {'csrfmiddlewaretoken', 'email_receipts'}
    if unknown or request.POST.get('email_receipts', '') not in ['', 'on', 'true', 'false']:
        messages.error(request, 'Invalid receipt preference.')
    else:
        request.user.email_receipts = request.POST.get('email_receipts') in ['on', 'true']
        request.user.save(update_fields=['email_receipts'])
        messages.success(
            request, 'Receipt preference saved. Security and in-app money updates remain available.'
        )
    return redirect('notifications:inbox')
