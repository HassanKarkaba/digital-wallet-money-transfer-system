import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Recipient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='saved_recipients'
    )
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='recipient_of'
    )
    label = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'target'], name='recipient_unique')]


class Operation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='operations')
    sender = models.ForeignKey('ledger.Wallet', null=True, on_delete=models.PROTECT, related_name='outgoing')
    recipient = models.ForeignKey(
        'ledger.Wallet', null=True, on_delete=models.PROTECT, related_name='incoming'
    )
    destination = models.ForeignKey('rails.Destination', null=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    amount_cents = models.BigIntegerField()
    currency = models.CharField(max_length=3, default='USD')
    note = models.CharField(max_length=280, blank=True)
    state = models.CharField(max_length=24)
    policy_version = models.CharField(max_length=16, default='DWMT-P1')
    schema_version = models.PositiveIntegerField(default=1)
    limit_day = models.DateField()
    provider_key = models.CharField(max_length=200, unique=True, null=True)
    sender_label = models.CharField(max_length=100, blank=True)
    recipient_label = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True)
    updated_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)
    failure_reason = models.CharField(max_length=1000, blank=True)
    original = models.OneToOneField('self', null=True, on_delete=models.PROTECT, related_name='reversal')

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_cents__gte=1, amount_cents__lte=250000), name='operation_amount'
            ),
            models.CheckConstraint(condition=Q(currency='USD'), name='operation_usd'),
            models.CheckConstraint(
                condition=Q(kind__in=['TRANSFER', 'FUNDING', 'WITHDRAWAL', 'REVERSAL']), name='operation_kind'
            ),
            models.CheckConstraint(
                condition=Q(
                    state__in=[
                        'REVIEW_PENDING',
                        'READY',
                        'SUBMITTED',
                        'UNKNOWN',
                        'COMPLETED',
                        'FAILED',
                        'CANCELLED',
                    ]
                ),
                name='operation_state',
            ),
        ]
        indexes = [
            models.Index(fields=['sender', '-created_at', '-id']),
            models.Index(fields=['recipient', '-created_at', '-id']),
            models.Index(fields=['state', 'created_at']),
        ]


class Intent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    instruction = models.JSONField()
    digest = models.CharField(max_length=64)
    policy_version = models.CharField(max_length=16, default='DWMT-P1')
    authority_version = models.PositiveIntegerField()
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True)
    consumed_at = models.DateTimeField(null=True)
    grant_id = models.UUIDField(null=True)


class ReplayKey(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    family = models.CharField(max_length=24)
    intent_id = models.UUIDField(null=True)
    action_key = models.UUIDField()
    digest = models.CharField(max_length=64)
    operation = models.ForeignKey(Operation, null=True, on_delete=models.PROTECT)
    target_id = models.UUIDField(null=True)
    target_kind = models.CharField(max_length=24, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'family', 'action_key'], name='replay_scope')]


class MoneyRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='money_requested'
    )
    payer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='money_requests'
    )
    amount_cents = models.BigIntegerField()
    note = models.CharField(max_length=280, blank=True)
    state = models.CharField(max_length=24, default='OPEN')
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    operation = models.OneToOneField(
        Operation, null=True, on_delete=models.PROTECT, related_name='money_request'
    )
    version = models.PositiveIntegerField(default=1)


class Schedule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='schedules')
    recipient = models.ForeignKey('ledger.Wallet', on_delete=models.PROTECT)
    amount_cents = models.BigIntegerField()
    note = models.CharField(max_length=280, blank=True)
    due_at = models.DateTimeField()
    timezone = models.CharField(max_length=64, default='UTC')
    utc_offset = models.CharField(max_length=6, default='+00:00')
    authority_version = models.PositiveIntegerField()
    policy_version = models.CharField(max_length=16, default='DWMT-P1')
    state = models.CharField(max_length=24, default='SCHEDULED')
    operation = models.OneToOneField(Operation, null=True, on_delete=models.PROTECT, related_name='schedule')
    created_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)
    failure_reason = models.CharField(max_length=1000, blank=True)
