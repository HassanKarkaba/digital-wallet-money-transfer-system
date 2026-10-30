from django.urls import path

from . import views

app_name = 'accounts'
urlpatterns = [
    path('register/', views.register, name='register'),
    path('verify/<str:token>/', views.verify, name='verify'),
    path('verification/resend/', views.verification_resend, name='verification_resend'),
    path('login/', views.login, name='login'),
    path('login/mfa/', views.login_mfa, name='login_mfa'),
    path('logout/', views.logout, name='logout'),
    path('mfa/enroll/', views.enroll, name='mfa_enroll'),
    path('mfa/recover/', views.mfa_recover, name='mfa_recover'),
    path('password/reset/', views.password_reset, name='password_reset'),
    path('password/reset/<str:token>/', views.password_reset_confirm, name='password_reset_confirm'),
    path('profile/', views.profile, name='profile'),
    path('email/change/', views.email_change, name='email_change'),
    path('email/change/<str:token>/', views.email_change_confirm, name='email_change_confirm'),
    path('sessions/', views.sessions, name='sessions'),
    path('sessions/<uuid:session_id>/revoke/', views.session_revoke, name='session_revoke'),
]
