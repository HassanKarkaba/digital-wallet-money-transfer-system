from django.urls import path

from . import views

app_name = 'oversight'
urlpatterns = [
    path('staff/cases/<uuid:case_id>/assign-finance/', views.assign_finance, name='assign_finance'),
    path('staff/cases/<uuid:case_id>/statements/', views.case_statements, name='case_statements'),
    path(
        'staff/cases/<uuid:case_id>/statements/<uuid:link_id>/',
        views.case_statement_detail,
        name='case_statement_detail',
    ),
    path(
        'staff/cases/<uuid:case_id>/statements/<uuid:link_id>/download/',
        views.case_statement_download,
        name='case_statement_download',
    ),
    path('staff/reconciliation/recovery/propose/', views.propose_recovery, name='recovery_propose'),
    path('staff/reconciliation/recovery/<uuid:proposal_id>/', views.recovery_detail, name='recovery_detail'),
    path(
        'staff/reconciliation/recovery/<uuid:proposal_id>/approve/',
        views.prepare_recovery_approval,
        name='recovery_approve',
    ),
    path('staff/', views.workspace, name='workspace'),
    path('staff/cases/<uuid:case_id>/', views.case_detail, name='case_detail'),
    path('staff/cases/<uuid:case_id>/decide/', views.prepare_decision, name='decide'),
    path('staff/wallets/<uuid:wallet_id>/restriction/', views.prepare_restriction, name='restriction'),
    path('staff/disputes/<uuid:case_id>/propose-reversal/', views.propose_reversal, name='propose_reversal'),
    path('staff/reconciliation/', views.reconciliation, name='reconciliation'),
    path('staff/reconciliation/run/', views.run_reconciliation, name='run_reconciliation'),
    path('staff/reconciliation/<uuid:finding_id>/resolve/', views.resolve_finding, name='resolve_finding'),
    path('staff/access/', views.access, name='access'),
    path('staff/reconciliation/reopen/', views.prepare_gate_reopen, name='gate_reopen'),
    path('staff/operations/<uuid:operation_id>/support/', views.open_support_case, name='support_case'),
]
