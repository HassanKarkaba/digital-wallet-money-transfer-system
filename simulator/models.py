import uuid

from django.db import models
from django.utils import timezone


class SimulatorEnvironment(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    environment_id = models.UUIDField()
    recovery_generation = models.PositiveIntegerField(default=1)
    last_seen = models.DateTimeField(default=timezone.now)


class ProviderEffect(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recovery_metadata = models.JSONField(default=dict)
    provider_key = models.CharField(max_length=200, unique=True)
    operation_id = models.UUIDField()
    environment_id = models.UUIDField()
    direction = models.CharField(max_length=12)
    token_id = models.UUIDField()
    owner_id = models.UUIDField()
    amount_cents = models.BigIntegerField()
    currency = models.CharField(max_length=3, default='USD')
    state = models.CharField(max_length=12)
    scenario = models.CharField(max_length=32)
    provider_sequence = models.PositiveIntegerField(default=1)
    schema_version = models.PositiveSmallIntegerField(default=1)
    occurred_at = models.DateTimeField(default=timezone.now)
    finalize_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__gte=1, amount_cents__lte=250000),
                name='provider_amount_policy',
            ),
            models.CheckConstraint(condition=models.Q(currency='USD'), name='provider_usd'),
            models.CheckConstraint(
                condition=models.Q(direction__in=['FUNDING', 'WITHDRAWAL']), name='provider_direction_legal'
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=['PENDING', 'SUCCESS', 'DECLINED']), name='provider_state_legal'
            ),
        ]
        indexes = [models.Index(fields=['state', 'finalize_at'])]


class ProviderEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    effect = models.ForeignKey(ProviderEffect, on_delete=models.PROTECT, related_name='events')
    provider_sequence = models.PositiveIntegerField()
    body = models.TextField()
    state = models.CharField(max_length=16, default='PENDING')
    attempts = models.PositiveIntegerField(default=0)
    due_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['effect', 'provider_sequence'], name='provider_effect_event_sequence_unique'
            )
        ]
        indexes = [models.Index(fields=['state', 'due_at'])]
