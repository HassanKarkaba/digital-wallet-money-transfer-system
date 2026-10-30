from django.http import JsonResponse
from django.urls import include, path

from accounts import views as account_views

from .health import readiness


def live(request):
    return JsonResponse({'status': 'live'})


def ready(request):
    state = readiness()
    return JsonResponse(
        {'status': 'ready' if all(state.values()) else 'degraded'}, status=200 if all(state.values()) else 503
    )


urlpatterns = [
    path('accounts/', include('accounts.urls')),
    path('', include('payments.urls')),
    path('', include('rails.urls')),
    path('', include('reports.urls')),
    path('staff/access/', account_views.staff_access, name='staff_access'),
    path('', include('oversight.urls')),
    path('', include('notifications.urls')),
    path('health/live/', live),
    path('health/ready/', ready),
]
