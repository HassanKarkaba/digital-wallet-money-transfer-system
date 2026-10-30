from django.urls import path

from . import views

app_name = 'notifications'
urlpatterns = [
    path('notifications/', views.inbox, name='inbox'),
    path('notifications/<uuid:notification_id>/read/', views.mark_read, name='read'),
    path('notifications/preferences/', views.preferences, name='preferences'),
]
