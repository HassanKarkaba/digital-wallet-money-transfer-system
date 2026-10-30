from datetime import timedelta

import pyotp
from django.contrib import auth, messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from audit.services import record

from . import forms, services
from .models import User, UserSession


def _redirect(target, *args, **kwargs):
    response = redirect(target, *args, **kwargs)
    response.status_code = 303
    return response


def _safe_next(value):
    return (
        value
        if value and value.startswith('/') and not value.startswith('//') and '\\' not in value
        else '/wallet/'
    )


def _form_page(request, title, form, button, *, status=200, text='', extra=None):
    if form.is_bound:
        safe_data = form.data.copy()
        for sensitive in ('otp', 'password', 'recovery_code'):
            if sensitive in safe_data:
                safe_data[sensitive] = ''
        form.data = safe_data
    context = {'title': title, 'form': form, 'button': button, 'intro': text}
    context.update(extra or {})
    return render(request, 'accounts/form.html', context, status=status)


def _error(form, exc):
    form.add_error(None, exc.message)


def _current_active(user, expected):
    if user.status != 'ACTIVE' or not user.is_active or user.authority_version != expected.authority_version:
        raise services.SecurityError('Account authority changed. Sign in again before continuing.', 'STALE')


def _limited_page(request, form, title, button, exc):
    _error(form, exc)
    response = _form_page(request, title, form, button, status=429)
    response['Retry-After'] = str(exc.retry_after)
    return response


@require_http_methods(['GET', 'POST'])
def register(request):
    if request.user.is_authenticated:
        return _redirect('/wallet/')
    form = forms.RegistrationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        values = form.cleaned_data
        try:
            services.recovery_rate(request, values['email'])
            try:
                with transaction.atomic():
                    if (
                        not User.objects.filter(email=values['email']).exists()
                        and not User.objects.filter(handle=values['handle']).exists()
                    ):
                        user = User.objects.create_user(**values)
                        services.queue_verification(user)
                        record(user, 'REGISTERED', user.id)
            except IntegrityError:
                pass
        except services.RateLimited as exc:
            return _limited_page(request, form, 'Create your wallet', 'Create account', exc)
        except services.SecurityError as exc:
            _error(form, exc)
            return _form_page(request, 'Create your wallet', form, 'Create account', status=503)
        messages.success(
            request,
            'If these details can be used, a verification message is waiting in the local mail capture. Sign in or correct your details if needed.',
        )
        return _redirect('accounts:login')
    return _form_page(
        request,
        'Create your wallet',
        form,
        'Create account',
        status=422 if request.method == 'POST' else 200,
        text='A simulated USD wallet for demonstration money with no cash value. Verify your email and enroll an authenticator before moving money.',
        extra={'links': [('Already have an account? Sign in', reverse('accounts:login'))]},
    )


@require_http_methods(['GET', 'POST'])
def verify(request, token):
    if request.method == 'POST':
        try:
            services.verify_email(token)
        except services.SecurityError as exc:
            return render(
                request,
                'accounts/message.html',
                {
                    'title': 'Verification link unavailable',
                    'text': exc.message,
                    'link': reverse('accounts:verification_resend'),
                    'link_label': 'Request a new verification message',
                },
                status=422,
            )
        messages.success(
            request, 'Email verified. Your wallet is open. Sign in and enroll your authenticator to continue.'
        )
        return _redirect('accounts:login')
    return render(
        request,
        'accounts/confirm.html',
        {
            'title': 'Verify your email',
            'text': 'Confirm this link to verify your local demonstration wallet email.',
            'button': 'Verify email',
        },
    )


@require_http_methods(['GET', 'POST'])
def verification_resend(request):
    form = forms.EmailForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.recovery_rate(request, form.cleaned_data['email'])
            with transaction.atomic():
                user = (
                    User.objects.select_for_update()
                    .filter(email=form.cleaned_data['email'], status='ACTIVE', verified_at__isnull=True)
                    .first()
                )
                if user:
                    services.queue_verification(user)
        except services.RateLimited as exc:
            return _limited_page(request, form, 'Send verification message', 'Send message', exc)
        messages.info(
            request,
            'If an eligible account exists, a fresh verification message will appear in local mail capture.',
        )
        return _redirect('accounts:login')
    return _form_page(
        request,
        'Send verification message',
        form,
        'Send message',
        status=422 if request.method == 'POST' else 200,
    )


@require_http_methods(['GET', 'POST'])
def login(request):
    if request.user.is_authenticated:
        return _redirect('/wallet/')
    form = forms.LoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        email = form.cleaned_data['email']
        ip = request.META.get('REMOTE_ADDR', '')
        try:
            services.rate_limit('login-account', email, 5, 900, consume=False)
            services.rate_limit('login-ip', ip, 50, 900, consume=False)
            user = auth.authenticate(request, email=email, password=form.cleaned_data['password'])
            if not user or user.status != 'ACTIVE':
                services.rate_limit('login-account', email, 5, 900)
                services.rate_limit('login-ip', ip, 50, 900)
                form.add_error(None, 'The sign in details are invalid.')
            else:
                destination = _safe_next(request.POST.get('next') or request.GET.get('next'))
                if user.mfa_confirmed:
                    request.session.cycle_key()
                    request.session['login_challenge'] = {
                        'user_id': str(user.pk),
                        'authority_version': user.authority_version,
                        'expires_at': (services.db_now() + timedelta(seconds=120)).isoformat(),
                        'next': destination,
                    }
                    return _redirect('accounts:login_mfa')
                services.begin_session(request, user)
                return _redirect(destination)
        except services.RateLimited as exc:
            return _limited_page(request, form, 'Welcome back', 'Sign in', exc)
    return _form_page(
        request,
        'Welcome back',
        form,
        'Sign in',
        status=422 if request.method == 'POST' else 200,
        extra={
            'next': _safe_next(request.GET.get('next')),
            'links': [
                ('Create an account', reverse('accounts:register')),
                ('Reset your password', reverse('accounts:password_reset')),
                ('Resend verification', reverse('accounts:verification_resend')),
            ],
        },
    )


@require_http_methods(['GET', 'POST'])
def login_mfa(request):
    from django.utils.dateparse import parse_datetime

    challenge = request.session.get('login_challenge')
    if not challenge:
        messages.info(request, 'Sign in again to start a fresh verification challenge.')
        return _redirect('accounts:login')
    user = User.objects.filter(pk=challenge.get('user_id'), status='ACTIVE').first()
    expiry = parse_datetime(challenge.get('expires_at', ''))
    if (
        not user
        or not expiry
        or services.db_now() >= expiry
        or user.authority_version != challenge.get('authority_version')
    ):
        request.session.pop('login_challenge', None)
        messages.info(request, 'Your sign in challenge expired. Sign in again.')
        return _redirect('accounts:login')
    form = forms.OtpForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.rate_limit('login-mfa', user.pk, 5, 900)
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=user.pk)
                if (
                    services.db_now() >= expiry
                    or user.authority_version != challenge['authority_version']
                    or not user.mfa_confirmed
                ):
                    raise services.SecurityError('Your sign in challenge expired. Sign in again.', 'EXPIRED')
                services.verify_totp(user, form.cleaned_data['otp'])
                record(user, 'LOGIN_MFA', user.id)
            request.session.pop('login_challenge', None)
            services.begin_session(request, user)
            return _redirect(_safe_next(challenge.get('next')))
        except services.RateLimited as exc:
            return _limited_page(request, form, 'Verify your sign in', 'Verify and sign in', exc)
        except services.SecurityError as exc:
            _error(form, exc)
    return _form_page(
        request,
        'Verify your sign in',
        form,
        'Verify and sign in',
        status=422 if request.method == 'POST' else 200,
        text='Use a new code from your authenticator. Each code can be used once.',
        extra={'links': [('Use a recovery code', reverse('accounts:mfa_recover'))]},
    )


@login_required
@require_POST
def logout(request):
    with transaction.atomic():
        # Serialize this revocation with financial authority checks on the same
        # security row; a waiting admission then sees the revoked session.
        User.objects.select_for_update().get(pk=request.user.pk)
        UserSession.objects.filter(
            user=request.user, session_key=request.session.session_key, revoked_at__isnull=True
        ).update(revoked_at=services.db_now())
    auth.logout(request)
    return _redirect('accounts:login')


@login_required
@require_http_methods(['GET', 'POST'])
def enroll(request):
    if request.user.mfa_confirmed:
        messages.info(
            request, 'Two step verification is already enrolled. Use recovery if you need to replace it.'
        )
        return _redirect('accounts:profile')
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if (
            user.status != 'ACTIVE'
            or not user.is_active
            or user.authority_version != request.user.authority_version
        ):
            return _redirect('accounts:login')
        if not user.mfa_secret:
            user.mfa_secret = services.encrypt_secret(pyotp.random_base32())
            user.save(update_fields=['mfa_secret'])
    form = forms.OtpForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.rate_limit('mfa-enroll', user.pk, 5, 900)
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=user.pk)
                _current_active(user, request.user)
                if user.mfa_confirmed:
                    return _redirect('accounts:profile')
                services.verify_totp(user, form.cleaned_data['otp'])
                import secrets

                codes = [secrets.token_hex(10) for _ in range(10)]
                user.recovery_hashes = [make_password(code) for code in codes]
                user.mfa_confirmed = True
                user.save(update_fields=['recovery_hashes', 'mfa_confirmed'])
                record(user, 'MFA_ENROLLED', user.id)
            request.session['recovery_only'] = False
            UserSession.objects.filter(user=user, session_key=request.session.session_key).update(
                recovery_only=False
            )
            return render(
                request, 'accounts/recovery_codes.html', {'codes': codes, 'title': 'Save your recovery codes'}
            )
        except services.RateLimited as exc:
            return _limited_page(request, form, 'Set up two step verification', 'Confirm authenticator', exc)
        except services.SecurityError as exc:
            _error(form, exc)
    secret = services.decrypt_secret(user.mfa_secret)
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name='DWMT Demonstration')
    return _form_page(
        request,
        'Set up two step verification',
        form,
        'Confirm authenticator',
        status=422 if request.method == 'POST' else 200,
        text='Add the secret below to an authenticator app, then enter its six digit code. The secret is shown only during enrollment.',
        extra={'secret': secret, 'provisioning_uri': uri},
    )


@require_http_methods(['GET', 'POST'])
def mfa_recover(request):
    form = forms.RecoveryForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.recovery_rate(request, form.cleaned_data['email'])
            user = User.objects.filter(email=form.cleaned_data['email'], status='ACTIVE').first()
            if not user:
                raise services.SecurityError('The recovery details are invalid.', 'INVALID')
            user = services.recover_mfa(
                user, form.cleaned_data['password'], form.cleaned_data['recovery_code']
            )
            services.begin_session(request, user, recovery_only=True)
            messages.info(
                request, 'Enroll a replacement authenticator. Outgoing payments remain paused for 24 hours.'
            )
            return _redirect('accounts:mfa_enroll')
        except services.RateLimited as exc:
            return _limited_page(request, form, 'Recover authenticator access', 'Start recovery', exc)
        except services.SecurityError as exc:
            _error(form, exc)
    return _form_page(
        request,
        'Recover authenticator access',
        form,
        'Start recovery',
        status=422 if request.method == 'POST' else 200,
        text='Use your password and one unused recovery code. Existing sessions are revoked. You must enroll a new authenticator, and outgoing payments pause for 24 hours.',
    )


@require_http_methods(['GET', 'POST'])
def password_reset(request):
    form = forms.EmailForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.recovery_rate(request, form.cleaned_data['email'])
            with transaction.atomic():
                user = (
                    User.objects.select_for_update()
                    .filter(email=form.cleaned_data['email'], status='ACTIVE')
                    .first()
                )
                if user:
                    token = services.create_token(user, 'RESET')
                    path = reverse('accounts:password_reset_confirm', kwargs={'token': token})
                    services.queue_security_mail(
                        user,
                        user.email,
                        'Reset your demonstration wallet password',
                        'Reset your password at:\n'
                        + path
                        + '\nThis single use link expires in 30 minutes. MFA remains required.',
                        'RESET',
                    )
        except services.RateLimited as exc:
            return _limited_page(request, form, 'Reset your password', 'Send reset message', exc)
        messages.info(
            request, 'If an eligible account exists, a reset message will appear in local mail capture.'
        )
        return _redirect('accounts:login')
    return _form_page(
        request,
        'Reset your password',
        form,
        'Send reset message',
        status=422 if request.method == 'POST' else 200,
    )


@require_http_methods(['GET', 'POST'])
def password_reset_confirm(request, token):
    form = forms.PasswordResetForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.reset_password(token, form.cleaned_data['password'])
        except services.SecurityError as exc:
            _error(form, exc)
        else:
            auth.logout(request)
            messages.success(
                request, 'Password changed. Sign in again. Your authenticator is still required.'
            )
            return _redirect('accounts:login')
    return _form_page(
        request,
        'Choose a new password',
        form,
        'Change password',
        status=422 if request.method == 'POST' else 200,
        extra={'links': [('Request a fresh reset link', reverse('accounts:password_reset'))]},
    )


@login_required
@require_http_methods(['GET', 'POST'])
def profile(request):
    form = forms.ProfileForm(
        request.POST or None,
        initial={'display_name': request.user.display_name, 'timezone': request.user.timezone},
    )
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=request.user.pk)
                _current_active(user, request.user)
                user.display_name = form.cleaned_data['display_name']
                user.timezone = form.cleaned_data['timezone']
                user.save(update_fields=['display_name', 'timezone'])
                record(user, 'PROFILE_UPDATED', user.pk)
        except services.SecurityError as exc:
            _error(form, exc)
        else:
            messages.success(request, 'Profile updated.')
            return _redirect('accounts:profile')
    return render(
        request,
        'accounts/profile.html',
        {'title': 'Account settings', 'form': form},
        status=422 if request.method == 'POST' else 200,
    )


@login_required
@require_http_methods(['GET', 'POST'])
def email_change(request):
    form = forms.EmailChangeForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.recovery_rate(request, form.cleaned_data['email'])
            services.rate_limit('email-change-proof', request.user.pk, 5, 900)
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=request.user.pk)
                _current_active(user, request.user)
                services.assert_eligible(user)
                if not user.check_password(form.cleaned_data['password']):
                    raise services.SecurityError('The confirmation details are invalid.', 'INVALID')
                services.verify_totp(user, form.cleaned_data['otp'])
                value = services.create_token(user, 'EMAIL_CHANGE', target_email=form.cleaned_data['email'])
                path = reverse('accounts:email_change_confirm', kwargs={'token': value})
                services.queue_security_mail(
                    user,
                    form.cleaned_data['email'],
                    'Verify your new demonstration wallet email',
                    'Activate your new email at:\n' + path + '\nThis link expires in 24 hours.',
                    'EMAIL_CHANGE_VERIFY',
                )
                services.queue_security_mail(
                    user,
                    user.email,
                    'Demonstration wallet email change requested',
                    'A new email address was requested. It will replace this address only after verification.',
                    'SECURITY',
                )
                record(user, 'EMAIL_CHANGE_REQUESTED', user.pk)
            messages.info(
                request,
                'A verification message is queued in local mail capture. Your current address stays active until verification.',
            )
            return _redirect('accounts:profile')
        except services.RateLimited as exc:
            return _limited_page(request, form, 'Change your email', 'Send verification', exc)
        except services.SecurityError as exc:
            _error(form, exc)
    return _form_page(
        request,
        'Change your email',
        form,
        'Send verification',
        status=422 if request.method == 'POST' else 200,
        text='Confirm your password and a new authenticator code. Activating the verified address will revoke sessions and pending authorizations.',
    )


@require_http_methods(['GET', 'POST'])
def email_change_confirm(request, token):
    if request.method == 'POST':
        try:
            services.complete_email_change(token)
        except services.SecurityError as exc:
            return render(
                request,
                'accounts/message.html',
                {
                    'title': 'Email change unavailable',
                    'text': exc.message,
                    'link': reverse('accounts:login'),
                    'link_label': 'Sign in',
                },
                status=422,
            )
        auth.logout(request)
        messages.success(request, 'Your verified email is active. Sign in again with the new address.')
        return _redirect('accounts:login')
    return render(
        request,
        'accounts/confirm.html',
        {
            'title': 'Activate your new email',
            'text': 'This replaces your previous address, revokes sessions, and invalidates outstanding payment authorizations.',
            'button': 'Activate verified email',
        },
    )


@login_required
@require_http_methods(['GET'])
def sessions(request):
    rows = UserSession.objects.filter(user=request.user, revoked_at__isnull=True).order_by('-started_at')
    return render(
        request,
        'accounts/sessions.html',
        {
            'title': 'Signed in sessions',
            'sessions': rows,
            'current_key': request.session.session_key,
            'form': forms.SessionRevokeForm(),
        },
    )


@login_required
@require_POST
def session_revoke(request, session_id):
    session = get_object_or_404(UserSession, pk=session_id, user=request.user)
    form = forms.SessionRevokeForm(request.POST)
    if not form.is_valid():
        return _form_page(request, 'Revoke session', form, 'Revoke session', status=422)
    try:
        # This bucket commits before proof validation, so a rejected proof's
        # transaction rollback cannot refund its brute-force budget.
        services.rate_limit('session-revoke-proof', request.user.pk, 5, 900)
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            _current_active(user, request.user)
            services.assert_eligible(user)
            if not user.check_password(form.cleaned_data['password']):
                raise services.SecurityError('The confirmation details are invalid.', 'INVALID')
            services.verify_totp(user, form.cleaned_data['otp'])
            tracked = UserSession.objects.select_for_update().get(pk=session.pk, user=user)
            from django.contrib.sessions.models import Session

            Session.objects.filter(session_key=tracked.session_key).delete()
            tracked.revoked_at = services.db_now()
            tracked.save(update_fields=['revoked_at'])
            record(user, 'SESSION_REVOKED', tracked.pk)
    except services.RateLimited as exc:
        return _limited_page(request, form, 'Revoke session', 'Revoke session', exc)
    except services.SecurityError as exc:
        _error(form, exc)
        return _form_page(request, 'Revoke session', form, 'Revoke session', status=exc.status)
    if session.session_key == request.session.session_key:
        auth.logout(request)
        return _redirect('accounts:login')
    messages.success(request, 'Session revoked.')
    return _redirect('accounts:sessions')


@login_required
@require_http_methods(['GET', 'POST'])
def staff_access(request):
    form = forms.StaffAccessForm(request.POST or None)
    try:
        services.assert_eligible(request.user, staff_role='ACCESS_ADMIN')
    except services.SecurityError as exc:
        return render(
            request,
            'accounts/message.html',
            {
                'title': 'Staff access unavailable',
                'text': exc.message,
                'link': reverse('accounts:profile'),
                'link_label': 'Account security',
            },
            status=403,
        )
    if request.method == 'POST' and form.is_valid():
        if form.cleaned_data['action'] == 'INVITE':
            from payments.errors import DomainError
            from payments.workflows import create_intent

            try:
                intent = create_intent(
                    request.user,
                    'ROLE_INVITE',
                    {'email': form.cleaned_data['email'], 'role': form.cleaned_data['role']},
                )
                return _redirect('/payments/intents/' + str(intent.pk) + '/')
            except DomainError as exc:
                form.add_error(None, str(exc))
            return _form_page(request, 'Manage staff access', form, 'Review invitation', status=422)
        target = User.objects.filter(
            email=form.cleaned_data['email'], status='ACTIVE', verified_at__isnull=False, mfa_confirmed=True
        ).first()
        if not target:
            form.add_error(
                'email',
                'Invite this person to register, verify the exact synthetic email, and enroll an authenticator first.',
            )
        else:
            from payments.errors import DomainError
            from payments.workflows import create_intent

            try:
                with transaction.atomic():
                    record(
                        request.user,
                        'STAFF_ACCOUNT_LOOKUP',
                        target.pk,
                        purpose='Exact reference for role administration',
                    )
                    intent = create_intent(
                        request.user,
                        'ROLE_CHANGE',
                        {
                            'user_id': str(target.pk),
                            'role': form.cleaned_data['role'],
                            'enabled': form.cleaned_data['action'] == 'GRANT',
                            'version': target.authority_version,
                        },
                    )
                return _redirect('/payments/intents/' + str(intent.pk) + '/')
            except DomainError as exc:
                form.add_error(None, str(exc))
    from .models import StaffInvitation

    record(
        request.user,
        'STAFF_ACCESS_VIEW',
        request.user.pk,
        purpose='Review staff invitations and access administration',
    )
    invitations = StaffInvitation.objects.order_by('-created_at', '-id')[:50]
    return render(
        request,
        'accounts/staff_access.html',
        {'title': 'Manage staff access', 'form': form, 'invitations': invitations},
        status=422 if request.method == 'POST' else 200,
    )
