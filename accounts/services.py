"""Account authority, proof and local delivery boundaries.

Callers use the database clock after acquiring the decisive rows. Secret
material is encrypted before persistence and never enters audit metadata.
"""

import hashlib
import hmac
import math
import re
import secrets
from datetime import timedelta

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib import auth
from django.contrib.auth.hashers import check_password
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.urls import reverse

from .models import AccountToken, RateBucket, Role, SecurityMail, User, UserSession


class SecurityError(ValueError):
    def __init__(self, message, code='DENIED'):
        self.message = message
        self.code = code
        self.status = {
            'DENIED': 403,
            'INVALID': 422,
            'STALE': 409,
            'EXPIRED': 409,
            'CONFLICT': 409,
            'UNAVAILABLE': 503,
            'RATE_LIMITED': 429,
        }.get(code, 422)
        super().__init__(message)


class RateLimited(SecurityError):
    def __init__(self, retry_after):
        self.retry_after = max(1, retry_after)
        super().__init__('Too many attempts. Try again after the indicated wait.', 'RATE_LIMITED')


def db_now():
    with connection.cursor() as cursor:
        cursor.execute('SELECT clock_timestamp()')
        return cursor.fetchone()[0]


def lock_users(ids):
    return list(User.objects.select_for_update().filter(id__in=set(ids)).order_by('id'))


def assert_eligible(user, outgoing=False, staff_role=None):
    if not user.is_active or user.status != 'ACTIVE':
        raise SecurityError('This account is closed or inactive.')
    if not user.verified_at:
        raise SecurityError('Verify your email before continuing.')
    if not user.mfa_confirmed:
        raise SecurityError('Set up two step verification before continuing.')
    if outgoing and user.outgoing_cooldown_until and db_now() < user.outgoing_cooldown_until:
        raise SecurityError('Outgoing payments are paused for 24 hours after security recovery.')
    if staff_role and not Role.objects.filter(user=user, role=staff_role).exists():
        raise SecurityError('Your current role does not permit this action.')
    return user


def _keyring():
    keys = getattr(settings, 'MFA_ENCRYPTION_KEYS', None)
    if not keys:
        raw = getattr(settings, 'MFA_ENCRYPTION_KEY', None)
        keys = {'v1': raw} if raw else {}
    if isinstance(keys, str):
        keys = {'v1': keys}
    return keys


def encrypt_secret(value):
    version = getattr(settings, 'MFA_KEY_VERSION', 'v1')
    key = _keyring().get(version)
    if not key:
        raise SecurityError('Secret storage is unavailable.', 'UNAVAILABLE')
    try:
        return version + ':' + Fernet(key).encrypt(value.encode()).decode()
    except (TypeError, ValueError) as exc:
        raise SecurityError('Secret storage is unavailable.', 'UNAVAILABLE') from exc


def decrypt_secret(value):
    try:
        version, payload = value.split(':', 1)
        key = _keyring()[version]
        return Fernet(key).decrypt(payload.encode()).decode()
    except (KeyError, ValueError, InvalidToken, TypeError) as exc:
        raise SecurityError(
            'The security key is unavailable. Contact the local operator.', 'UNAVAILABLE'
        ) from exc


@transaction.atomic
def verify_totp(user, code):
    locked = User.objects.select_for_update().get(pk=user.pk)
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
        raise SecurityError('Enter a valid six digit verification code.', 'INVALID')
    if not locked.mfa_secret:
        raise SecurityError('Set up two step verification first.', 'INVALID')
    now = db_now()
    counter = int(now.timestamp()) // 30
    totp = pyotp.TOTP(decrypt_secret(locked.mfa_secret))
    matches = [
        step
        for step in (counter - 1, counter, counter + 1)
        if step > locked.mfa_last_counter and hmac.compare_digest(totp.generate_otp(step), code)
    ]
    if not matches:
        raise SecurityError('The code is invalid or was already used. Wait for a new code.', 'INVALID')
    accepted = max(matches)
    locked.mfa_last_counter = accepted
    locked.save(update_fields=['mfa_last_counter'])
    user.mfa_last_counter = accepted
    return accepted


def _bucket_key(scope, identity):
    return hmac.new(
        settings.SECRET_KEY.encode(), (scope + '\0' + str(identity)).encode(), hashlib.sha256
    ).hexdigest()


@transaction.atomic
def rate_limit(scope, identity, limit, seconds, consume=True):
    """Atomic fixed window beginning at the first event, shared by all processes."""
    key = _bucket_key(scope, identity)
    try:
        with transaction.atomic():
            RateBucket.objects.get_or_create(key=key, defaults={'started_at': db_now(), 'count': 0})
    except IntegrityError:
        pass
    bucket = RateBucket.objects.select_for_update().get(pk=key)
    now = db_now()
    ends = bucket.started_at + timedelta(seconds=seconds)
    if now >= ends:
        bucket.started_at = now
        bucket.count = 0
        ends = now + timedelta(seconds=seconds)
    if bucket.count >= limit:
        raise RateLimited(math.ceil((ends - now).total_seconds()))
    if consume:
        bucket.count += 1
        bucket.save(update_fields=['started_at', 'count'])
    return bucket.count


def recovery_rate(request, email):
    rate_limit('recovery-address', email, 3, 3600)
    rate_limit('recovery-ip', request.META.get('REMOTE_ADDR', ''), 20, 3600)


def _fingerprint(user):
    return hashlib.sha256(user.password.encode()).hexdigest()


def _token_text(token, secret):
    return str(token.id) + '.' + secret


def create_token(user, kind, target_email=''):
    now = db_now()
    secret = secrets.token_urlsafe(32)
    token = AccountToken.objects.create(
        user=user,
        kind=kind,
        digest=hashlib.sha256(secret.encode()).hexdigest(),
        authority_version=user.authority_version,
        password_fingerprint=_fingerprint(user),
        target_email=target_email,
        created_at=now,
        expires_at=now + (timedelta(minutes=30) if kind == 'RESET' else timedelta(hours=24)),
    )
    return _token_text(token, secret)


def token_user_id(value, kind):
    try:
        token_id, _ = value.split('.', 1)
        return AccountToken.objects.only('user_id').get(pk=token_id, kind=kind).user_id
    except (ValueError, ValidationError, AccountToken.DoesNotExist):
        raise SecurityError('This link is invalid or expired. Request a new link.', 'INVALID') from None


def checked_token(value, kind, locked_user):
    try:
        token_id, secret = value.split('.', 1)
        token = AccountToken.objects.select_for_update().get(pk=token_id, kind=kind, user=locked_user)
    except (ValueError, ValidationError, AccountToken.DoesNotExist):
        raise SecurityError('This link is invalid or expired. Request a new link.', 'INVALID') from None
    now = db_now()
    if (
        token.used_at
        or now >= token.expires_at
        or token.authority_version != locked_user.authority_version
        or not hmac.compare_digest(token.password_fingerprint, _fingerprint(locked_user))
        or not hmac.compare_digest(token.digest, hashlib.sha256(secret.encode()).hexdigest())
    ):
        raise SecurityError('This link is invalid or expired. Request a new link.', 'INVALID')
    return token, now


@transaction.atomic
def queue_security_mail(user, target, subject, body, purpose):
    mail = SecurityMail.objects.create(
        user=user,
        target=target,
        subject=subject,
        encrypted_body=encrypt_secret(body),
        purpose=purpose,
        created_at=db_now(),
    )
    if purpose != 'STAFF_INVITATION':
        from notifications.services import notify_in_app

        event_key = (
            f'security:email-change:{user.pk}:{user.authority_version}'
            if purpose == 'EMAIL_CHANGE_NOTICE'
            else f'security-mail:{mail.pk}'
        )
        notify_in_app(
            user,
            event_key,
            subject,
            'A security message was queued in private local mail capture. Review your account at /accounts/profile/. '
            'If you did not initiate this action, contact the local operator.',
            security=True,
        )
    return mail


def queue_verification(user):
    value = create_token(user, 'VERIFY')
    path = reverse('accounts:verify', kwargs={'token': value})
    queue_security_mail(
        user,
        user.email,
        'Verify your demonstration wallet email',
        'This is a local demonstration wallet.\nVerify your email at:\n'
        + path
        + '\nThis link expires in 24 hours.',
        'VERIFY',
    )


@transaction.atomic
def verify_email(value):
    user_id = token_user_id(value, 'VERIFY')
    user = User.objects.select_for_update().get(pk=user_id)
    token, now = checked_token(value, 'VERIFY', user)
    if user.status != 'ACTIVE':
        raise SecurityError('This link is invalid or expired.', 'INVALID')
    if not user.verified_at:
        user.verified_at = now
        user.save(update_fields=['verified_at'])
        from ledger.posting import open_wallet

        open_wallet(user)
    token.used_at = now
    token.save(update_fields=['used_at'])
    from .models import StaffInvitation

    StaffInvitation.objects.filter(email=user.email, state='PENDING').update(
        state='REGISTERED', registered_user=user
    )
    from audit.services import record

    record(user, 'EMAIL_VERIFIED', user.id)
    return user


def revoke_sessions(user, keep_key=None):
    now = db_now()
    sessions = UserSession.objects.filter(user=user, revoked_at__isnull=True)
    if keep_key:
        sessions = sessions.exclude(session_key=keep_key)
    keys = list(sessions.values_list('session_key', flat=True))
    sessions.update(revoked_at=now)
    Session.objects.filter(session_key__in=keys).delete()


def begin_session(request, user, recovery_only=False):
    auth.login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    request.session.cycle_key()
    request.session.save()
    now = db_now()
    UserSession.objects.create(
        user=user,
        session_key=request.session.session_key,
        authority_version=user.authority_version,
        started_at=now,
        last_seen=now,
        recovery_only=recovery_only,
    )
    request.session['authority_version'] = user.authority_version
    request.session['recovery_only'] = recovery_only
    request.session.set_expiry(12 * 3600)


@transaction.atomic
def reset_password(value, password):
    user_id = token_user_id(value, 'RESET')
    user = User.objects.select_for_update().get(pk=user_id)
    token, now = checked_token(value, 'RESET', user)
    if user.status != 'ACTIVE':
        raise SecurityError('This link is invalid or expired.', 'INVALID')
    user.set_password(password)
    user.authority_version += 1
    user.save(update_fields=['password', 'authority_version'])
    token.used_at = now
    token.save(update_fields=['used_at'])
    revoke_sessions(user)
    from audit.services import record

    record(user, 'PASSWORD_RESET', user.id)
    queue_security_mail(
        user,
        user.email,
        'Your demonstration wallet password changed',
        'Your password changed. Existing sessions and payment authorizations were revoked. MFA remains required.',
        'SECURITY',
    )
    return user


@transaction.atomic
def recover_mfa(user, password, recovery_code):
    user = User.objects.select_for_update().get(pk=user.pk)
    if not user.check_password(password) or user.status != 'ACTIVE':
        raise SecurityError('The recovery details are invalid.', 'INVALID')
    index = next(
        (i for i, hashed in enumerate(user.recovery_hashes) if check_password(recovery_code.strip(), hashed)),
        None,
    )
    if index is None:
        raise SecurityError('The recovery details are invalid.', 'INVALID')
    hashes = list(user.recovery_hashes)
    hashes.pop(index)
    now = db_now()
    user.recovery_hashes = hashes
    user.mfa_confirmed = False
    user.mfa_secret = ''
    user.mfa_last_counter = -1
    user.authority_version += 1
    user.outgoing_cooldown_until = now + timedelta(hours=24)
    user.save(
        update_fields=[
            'recovery_hashes',
            'mfa_confirmed',
            'mfa_secret',
            'mfa_last_counter',
            'authority_version',
            'outgoing_cooldown_until',
        ]
    )
    revoke_sessions(user)
    from audit.services import record

    record(user, 'MFA_RECOVERY', user.id)
    queue_security_mail(
        user,
        user.email,
        'Demonstration wallet security recovery started',
        'Security recovery revoked your sessions. Enroll a new authenticator. Outgoing payments are paused for 24 hours.',
        'RECOVERY',
    )
    return user


@transaction.atomic
def complete_email_change(value):
    user_id = token_user_id(value, 'EMAIL_CHANGE')
    user = User.objects.select_for_update().get(pk=user_id)
    token, now = checked_token(value, 'EMAIL_CHANGE', user)
    old_email = user.email
    if User.objects.exclude(pk=user.pk).filter(email=token.target_email).exists():
        raise SecurityError('This email cannot be activated. Start again from settings.', 'INVALID')
    user.email = token.target_email
    user.verified_at = now
    user.authority_version += 1
    user.save(update_fields=['email', 'verified_at', 'authority_version'])
    token.used_at = now
    token.save(update_fields=['used_at'])
    revoke_sessions(user)
    from audit.services import record

    record(user, 'EMAIL_CHANGED', user.id)
    for target in (old_email, user.email):
        queue_security_mail(
            user,
            target,
            'Your demonstration wallet email changed',
            'A newly verified email address was activated. Previous sessions and payment authorizations were revoked.',
            'EMAIL_CHANGE_NOTICE',
        )
    return user


def process_security_mail(limit=20):
    """Bounded durable local capture. Loss of capture acknowledgement can duplicate a file."""
    delivered = 0
    from django.db.models import Q

    now = db_now()
    ids = list(
        SecurityMail.objects.filter(status__in=['PENDING', 'RETRY'])
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by('created_at', 'id')
        .values_list('id', flat=True)[:limit]
    )
    for mail_id in ids:
        preliminary = SecurityMail.objects.filter(pk=mail_id).values('user_id').first()
        if not preliminary:
            continue
        with transaction.atomic():
            users = lock_users([preliminary['user_id']])
            if not users:
                continue
            user = users[0]
            mail = (
                SecurityMail.objects.select_for_update(skip_locked=True)
                .filter(pk=mail_id, user=user, status__in=['PENDING', 'RETRY'])
                .first()
            )
            if not mail:
                continue
            if user.status != 'ACTIVE' or not user.is_active:
                mail.status = 'CANCELLED'
                mail.save(update_fields=['status'])
                continue
            allowed_target = mail.target == user.email or mail.purpose in [
                'EMAIL_CHANGE_VERIFY',
                'EMAIL_CHANGE_NOTICE',
                'STAFF_INVITATION',
            ]
            if (
                mail.purpose == 'STAFF_INVITATION'
                and not Role.objects.filter(user=user, role='ACCESS_ADMIN').exists()
            ):
                allowed_target = False
            if not allowed_target:
                mail.status = 'CANCELLED'
                mail.save(update_fields=['status'])
                continue
            mail.attempts += 1
            try:
                from notifications.services import capture_mail

                capture_mail(mail.target, mail.subject, decrypt_secret(mail.encrypted_body))
            except (OSError, SecurityError):
                mail.status = 'RETRY' if mail.attempts < 6 else 'ATTENTION'
                mail.error_code = 'LOCAL_CAPTURE_FAILED'
                mail.next_attempt_at = db_now() + timedelta(
                    seconds=(1, 5, 15, 60, 300)[min(mail.attempts - 1, 4)]
                )
                mail.save(update_fields=['status', 'attempts', 'error_code', 'next_attempt_at'])
            else:
                mail.status = 'DELIVERED'
                mail.delivered_at = db_now()
                mail.error_code = ''
                mail.save(update_fields=['status', 'delivered_at', 'attempts', 'error_code'])
                delivered += 1
    return delivered


def change_role(actor, target_id, role, enabled, version):
    """Called only after the coordinator binds proof and locks both users in UUID order."""
    if not connection.in_atomic_block:
        raise RuntimeError('Role changes require a protected primary transaction.')
    locked = {row.pk: row for row in lock_users([actor.pk, target_id])}
    current_actor = locked.get(actor.pk)
    target = next((row for key, row in locked.items() if str(key) == str(target_id)), None)
    if not current_actor or not target:
        raise SecurityError('Account not found.', 'INVALID')
    assert_eligible(current_actor, staff_role='ACCESS_ADMIN')
    allowed = {item[0] for item in Role.ROLES}
    if role not in allowed or type(enabled) is not bool or type(version) is not int:
        raise SecurityError('Choose a current supported role and action.', 'INVALID')
    if target.status != 'ACTIVE' or target.authority_version != version:
        raise SecurityError('The account changed. Review the current authority and try again.', 'STALE')
    if enabled:
        assert_eligible(target)
    old = Role.objects.filter(user=target, role=role).exists()
    if enabled:
        Role.objects.get_or_create(user=target, role=role)
    else:
        Role.objects.filter(user=target, role=role).delete()
    if old != enabled:
        target.authority_version += 1
        target.save(update_fields=['authority_version'])
        revoke_sessions(target)
    from audit.services import record

    record(
        current_actor,
        'STAFF_ROLE_CHANGED',
        target.pk,
        purpose='Explicit staff access administration',
        metadata={
            'role': role,
            'enabled': enabled,
            'previous': old,
            'target_authority_version': target.authority_version,
        },
    )
    queue_security_mail(
        target,
        target.email,
        'Demonstration wallet staff access updated',
        'Your staff permissions changed. Sign in again. Verified email and an enrolled authenticator are required for staff access.',
        'SECURITY',
    )
    return target


def closure_blockers(user):
    from django.db.models import Q

    from ledger.models import Wallet
    from oversight.models import Case
    from payments.models import MoneyRequest, Operation, Schedule

    wallet = Wallet.objects.filter(owner=user).first()
    if not wallet:
        return ['Verify your email to open a wallet before using wallet closure.']
    blockers = []
    if wallet.available_cents or wallet.reserved_cents:
        blockers.append('Withdraw or resolve all available and reserved demonstration funds first.')
    if wallet.incoming_cents:
        blockers.append('Resolve all pending incoming capacity reservations first.')
    parties = Q(sender=wallet) | Q(recipient=wallet) | Q(actor=user)
    if Operation.objects.filter(
        parties, state__in=['REVIEW_PENDING', 'READY', 'SUBMITTED', 'UNKNOWN']
    ).exists():
        blockers.append(
            'Resolve queued, reviewed or uncertain payment obligations first. Submitted rails require investigation, not cancellation.'
        )
    if MoneyRequest.objects.filter(
        Q(requester=user) | Q(payer=user), state__in=['OPEN', 'PAYMENT_PENDING']
    ).exists():
        blockers.append('Cancel or resolve active money requests involving your account.')
    if Schedule.objects.filter(owner=user, state='SCHEDULED').exists():
        blockers.append('Cancel pending scheduled transfers first.')
    if Schedule.objects.filter(recipient=wallet, state='SCHEDULED').exists():
        blockers.append(
            'A pending scheduled transfer is addressed to this wallet. Ask the sender to cancel it.'
        )
    relevant_ops = Operation.objects.filter(parties).values('id')
    unresolved_cases = Case.objects.filter(
        operation_id__in=relevant_ops, state__in=['OPEN', 'BLOCKED_FUNDS', 'BLOCKED_CAPACITY']
    ).exclude(
        state__in=['BLOCKED_FUNDS', 'BLOCKED_CAPACITY'],
        parent__state__in=['REVERSED', 'CLOSED_NO_REFUND', 'CANCELLED', 'REJECTED', 'EXPIRED'],
    )
    if unresolved_cases.exists():
        blockers.append('An unresolved review, dispute or reversal case must be closed first.')
    now = db_now()
    if Operation.objects.filter(
        Q(sender=wallet) | Q(recipient=wallet),
        kind='TRANSFER',
        state='COMPLETED',
        completed_at__gt=now - timedelta(days=30),
        reversal__isnull=True,
    ).exists():
        blockers.append(
            'The 30 day dispute window has not elapsed for every unreversed completed internal transfer.'
        )
    return blockers


def close_account_locked(user, final_guard=None):
    """Bound-proof coordinator must hold user, obligations and wallet locks before calling."""
    if not connection.in_atomic_block:
        raise RuntimeError('Account closure requires a protected primary transaction.')
    assert_eligible(user)
    blockers = closure_blockers(user)
    if blockers:
        raise SecurityError(' '.join(blockers), 'CONFLICT')
    from django.db.models import F

    from ledger.models import Wallet
    from rails.models import Destination

    wallet = Wallet.objects.select_for_update().get(owner=user)
    if wallet.available_cents or wallet.reserved_cents or wallet.incoming_cents:
        raise SecurityError('The wallet changed. Review closure eligibility again.', 'STALE')
    if final_guard:
        final_guard()
    Destination.objects.filter(owner=user, status='ACTIVE').update(
        status='DISABLED', version=F('version') + 1
    )
    wallet.state = 'CLOSED'
    wallet.version += 1
    wallet.save(update_fields=['state', 'version'])
    revoke_sessions(user)
    Role.objects.filter(user=user).delete()
    user.status = 'CLOSED'
    user.is_active = False
    user.authority_version += 1
    user.email = 'closed-' + str(user.pk) + '@dwmt.test'
    user.handle = 'closed_' + user.pk.hex[:23]
    user.display_name = 'Closed account'
    user.verified_at = None
    user.mfa_confirmed = False
    user.mfa_secret = ''
    user.recovery_hashes = []
    user.timezone = 'UTC'
    user.email_receipts = False
    user.set_unusable_password()
    user.save(
        update_fields=[
            'status',
            'is_active',
            'authority_version',
            'email',
            'handle',
            'display_name',
            'verified_at',
            'mfa_confirmed',
            'mfa_secret',
            'recovery_hashes',
            'timezone',
            'email_receipts',
            'password',
        ]
    )
    from audit.services import record

    record(
        user,
        'ACCOUNT_CLOSED',
        user.pk,
        reason='Customer confirmed closure after final export warning.',
        metadata={
            'wallet_reference': str(wallet.pk),
            'financial_attribution': 'Retained pseudonymous identity',
        },
    )
    return wallet


def invite_staff_locked(actor, email, role):
    """Create a local invitation only; a separate reviewed grant confers authority."""
    if not connection.in_atomic_block:
        raise RuntimeError('Staff invitations require a bound primary transaction.')
    assert_eligible(actor, staff_role='ACCESS_ADMIN')
    email = str(email).strip().lower()
    from django.core.validators import validate_email

    try:
        validate_email(email)
    except ValidationError:
        raise SecurityError('Enter an exact synthetic .test email address.', 'INVALID') from None
    if (
        not email.isascii()
        or len(email) > 254
        or not email.endswith('.test')
        or role not in {choice[0] for choice in Role.ROLES}
    ):
        raise SecurityError('Choose a supported role and exact synthetic .test email address.', 'INVALID')
    from .models import StaffInvitation

    invitation, created = StaffInvitation.objects.get_or_create(
        email=email, role=role, state='PENDING', defaults={'creator': actor, 'created_at': db_now()}
    )
    if created:
        queue_security_mail(
            actor,
            email,
            'Invitation to the local demonstration wallet staff workspace',
            'You were invited to prepare a local demonstration account for the ' + role + ' role.\n'
            'Register at /accounts/register/ using this exact synthetic email, verify the locally captured message, '
            'and enroll an authenticator.\n'
            'This invitation grants no permissions. An access administrator must separately review and confirm a role grant.\n'
            'Invitation reference: ' + str(invitation.pk),
            'STAFF_INVITATION',
        )
    from audit.services import record

    record(
        actor,
        'STAFF_INVITATION_CREATED',
        invitation.pk,
        purpose='Explicit staff invitation',
        metadata={'role': role, 'created': created},
    )
    return invitation
