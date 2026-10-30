from django.urls import path

from . import views

app_name = 'payments'
urlpatterns = [
    path('payments/actions/<str:family>/<uuid:action_key>/', views.accepted_action, name='accepted_action'),
    path('', views.landing, name='landing'),
    path('wallet/', views.dashboard, name='dashboard'),
    path('activity/', views.activity, name='activity'),
    path('send/', views.payment_entry, {'kind': 'TRANSFER'}, name='send'),
    path('funding/', views.payment_entry, {'kind': 'FUNDING'}, name='funding'),
    path('withdrawals/', views.payment_entry, {'kind': 'WITHDRAWAL'}, name='withdraw'),
    path('payments/intents/', views.prepare_intent, name='prepare'),
    path('payments/intents/<uuid:intent_id>/', views.intent_review, name='intent_review'),
    path('payments/intents/<uuid:intent_id>/verify/', views.verify_and_confirm, name='intent_confirm'),
    path('transfers/confirm/', views.confirm_existing, {'kind': 'TRANSFER'}, name='transfer_confirm'),
    path('funding/confirm/', views.confirm_existing, {'kind': 'FUNDING'}, name='funding_confirm'),
    path('withdrawals/confirm/', views.confirm_existing, {'kind': 'WITHDRAWAL'}, name='withdrawal_confirm'),
    path('payments/<uuid:operation_id>/', views.detail, name='detail'),
    path('payments/<uuid:operation_id>/status/', views.status, name='status'),
    path('payments/<uuid:operation_id>/cancel/', views.cancel, name='cancel'),
    path('payments/<uuid:operation_id>/disputes/', views.dispute, name='dispute'),
    path('disputes/<uuid:case_id>/', views.dispute_detail, name='dispute_detail'),
    path('recipients/', views.recipients, name='recipients'),
    path('recipients/<uuid:recipient_id>/remove/', views.remove_recipient, name='recipient_remove'),
    path('requests/', views.requests_page, name='requests'),
    path('requests/<uuid:request_id>/', views.request_detail, name='request_detail'),
    path('requests/<uuid:request_id>/<str:action>/', views.request_action, name='request_action'),
    path('schedules/', views.schedules, name='schedules'),
    path('schedules/<uuid:schedule_id>/cancel/', views.cancel_schedule, name='schedule_cancel'),
    path('destinations/', views.destinations, name='destinations'),
    path(
        'destinations/<uuid:destination_id>/disable/', views.disable_destination, name='destination_disable'
    ),
]
