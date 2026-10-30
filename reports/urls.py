from django.urls import path

from . import views

app_name = 'reports'
urlpatterns = [
    path('statements/', views.statements, name='statements'),
    path('statements/generate/', views.generate, name='generate'),
    path('statements/<uuid:statement_id>/', views.statement, name='statement'),
    path('statements/<uuid:statement_id>/download/', views.download, name='download'),
    path('account/data/', views.account_data, name='account_data'),
    path('account/export/', views.prepare_export, name='account_export'),
    path('account/close/', views.prepare_close, name='account_close'),
    path('account/exports/<uuid:export_id>/', views.export_detail, name='export_detail'),
]
