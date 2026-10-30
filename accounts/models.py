import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        user = self.model(email=email.strip().lower(), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('handle', 'local_operator')
        extra_fields.setdefault('display_name', 'Local operator')
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    first_name = None
    last_name = None
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=254, unique=True)
    handle = models.CharField(max_length=30, unique=True)
    display_name = models.CharField(max_length=100)
    status = models.CharField(
        max_length=12, default='ACTIVE', choices=[('ACTIVE', 'Active'), ('CLOSED', 'Closed')]
    )
    authority_version = models.PositiveIntegerField(default=1)
    verified_at = models.DateTimeField(null=True, blank=True)
    mfa_confirmed = models.BooleanField(default=False)
    mfa_secret = models.TextField(blank=True)
    mfa_last_counter = models.BigIntegerField(default=-1)
    recovery_hashes = models.JSONField(default=list)
    outgoing_cooldown_until = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default='UTC')
    email_receipts = models.BooleanField(default=True)
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['handle', 'display_name']
    objects = UserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower('email'), name='accounts_email_normalized_unique'),
            models.UniqueConstraint(Lower('handle'), name='accounts_handle_normalized_unique'),
            models.CheckConstraint(
                condition=models.Q(status__in=['ACTIVE', 'CLOSED']), name='accounts_valid_status'
            ),
        ]

    def get_full_name(self):
        return self.display_name

    def get_short_name(self):
        return self.display_name

    def __str__(self):
        return self.handle


class Role(models.Model):
    ROLES = [
        ('SUPPORT', 'Support'),
        ('REVIEWER', 'Reviewer'),
        ('FINANCE', 'Finance'),
        ('ACCESS_ADMIN', 'Access administrator'),
    ]
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='staff_roles')
    role = models.CharField(max_length=20, choices=ROLES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'role'], name='accounts_role_unique')]


class AccountToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    digest = models.CharField(max_length=64)
    authority_version = models.PositiveIntegerField()
    password_fingerprint = models.CharField(max_length=64)
    target_email = models.EmailField(blank=True)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)


class SecurityMail(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    target = models.EmailField()
    subject = models.CharField(max_length=180)
    encrypted_body = models.TextField()
    purpose = models.CharField(max_length=30)
    status = models.CharField(max_length=12, default='PENDING')
    created_at = models.DateTimeField()
    delivered_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=30, blank=True)


class UserSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='active_sessions')
    session_key = models.CharField(max_length=40, unique=True)
    authority_version = models.PositiveIntegerField()
    started_at = models.DateTimeField()
    last_seen = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    recovery_only = models.BooleanField(default=False)


class RateBucket(models.Model):
    key = models.CharField(primary_key=True, max_length=64)
    started_at = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)


class StaffInvitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=254)
    role = models.CharField(max_length=20, choices=Role.ROLES)
    creator = models.ForeignKey(User, on_delete=models.PROTECT, related_name='staff_invitations')
    registered_user = models.ForeignKey(
        User, null=True, on_delete=models.PROTECT, related_name='received_staff_invitations'
    )
    state = models.CharField(max_length=16, default='PENDING')
    created_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['email', 'role'],
                condition=models.Q(state='PENDING'),
                name='one_pending_staff_invitation',
            )
        ]
