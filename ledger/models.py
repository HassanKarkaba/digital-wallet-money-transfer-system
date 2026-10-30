import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Wallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='wallet')
    currency = models.CharField(max_length=3, default='USD')
    state = models.CharField(max_length=24, default='OPEN')
    available_cents = models.BigIntegerField(default=0)
    reserved_cents = models.BigIntegerField(default=0)
    incoming_cents = models.BigIntegerField(default=0)
    sequence = models.BigIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(currency='USD'), name='wallet_usd'),
            models.CheckConstraint(
                condition=Q(available_cents__gte=0, reserved_cents__gte=0, incoming_cents__gte=0),
                name='wallet_nonnegative',
            ),
            models.CheckConstraint(
                condition=Q(state__in=['OPEN', 'OUTGOING_RESTRICTED', 'FROZEN', 'CLOSED']),
                name='wallet_legal_state',
            ),
        ]


class LedgerAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, null=True, on_delete=models.PROTECT, related_name='accounts')
    kind = models.CharField(max_length=16)
    currency = models.CharField(max_length=3, default='USD')
    balance_cents = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['wallet', 'kind'], name='account_wallet_kind'),
            models.UniqueConstraint(fields=['kind'], condition=Q(kind='SYSTEM_CASH'), name='one_system_cash'),
            models.CheckConstraint(condition=Q(balance_cents__gte=0), name='account_nonnegative'),
            models.CheckConstraint(condition=Q(currency='USD'), name='account_usd'),
            models.CheckConstraint(
                condition=(
                    Q(wallet__isnull=True, kind='SYSTEM_CASH')
                    | Q(wallet__isnull=False, kind__in=['AVAILABLE', 'RESERVED'])
                ),
                name='account_legal_kind',
            ),
        ]


class Journal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey('payments.Operation', on_delete=models.PROTECT, related_name='journals')
    effect_kind = models.CharField(max_length=24)
    committed_at = models.DateTimeField()
    wallet_sequences = models.JSONField(default=dict)
    policy_version = models.CharField(max_length=16, default='DWMT-P1')
    sealed = models.BooleanField(default=False)
    creation_transaction = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['operation', 'effect_kind'], name='operation_effect_unique')
        ]
        indexes = [models.Index(fields=['committed_at', 'id'])]


class Entry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name='entries')
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name='entries')
    side = models.CharField(max_length=6)
    amount_cents = models.BigIntegerField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount_cents__gt=0), name='entry_positive'),
            models.CheckConstraint(condition=Q(side__in=['DEBIT', 'CREDIT']), name='entry_side'),
        ]


class Reservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey('payments.Operation', on_delete=models.PROTECT, related_name='reservations')
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='reservations')
    amount_cents = models.BigIntegerField()
    kind = models.CharField(max_length=10)
    state = models.CharField(max_length=10, default='ACTIVE')
    created_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True)
    release_reason = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['operation', 'wallet', 'kind'], name='reservation_identity'),
            models.CheckConstraint(condition=Q(amount_cents__gt=0), name='reservation_positive'),
            models.CheckConstraint(
                condition=Q(state__in=['ACTIVE', 'CONSUMED', 'RELEASED']), name='reservation_state'
            ),
            models.CheckConstraint(condition=Q(kind__in=['FUNDS', 'INCOMING']), name='reservation_kind'),
        ]


class DailyCounter(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    day = models.DateField()
    direction = models.CharField(max_length=8)
    reserved_cents = models.BigIntegerField(default=0)
    committed_cents = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['owner', 'day', 'direction'], name='day_counter_identity'),
            models.CheckConstraint(
                condition=Q(reserved_cents__gte=0, committed_cents__gte=0), name='counter_nonnegative'
            ),
        ]


class IntegrityGate(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    is_open = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    reason = models.CharField(max_length=1000, default='Environment initialization required.')
    environment_id = models.UUIDField()
    recovery_generation = models.PositiveIntegerField(default=1)
    last_seen_at = models.DateTimeField(null=True)
    changed_at = models.DateTimeField(null=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    policy_version = models.CharField(max_length=16, default='DWMT-P1')
