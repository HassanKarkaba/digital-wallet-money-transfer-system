import uuid

from django.conf import settings
from django.db import models


class Statement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='statements')
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    timezone = models.CharField(max_length=64, default='UTC')
    wallet_sequence = models.BigIntegerField(default=0)
    opening_available_cents = models.BigIntegerField(default=0)
    opening_reserved_cents = models.BigIntegerField(default=0)
    closing_available_cents = models.BigIntegerField(default=0)
    closing_reserved_cents = models.BigIntegerField(default=0)
    rows = models.JSONField(default=list)
    row_count = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=16, default='PENDING')
    file_name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    failure_reason = models.CharField(max_length=1000, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['owner', '-created_at'])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_at__gt=models.F('start_at')), name='statement_positive_range'
            )
        ]


class Export(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='private_exports'
    )
    file_name = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=16, default='PENDING')
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    failure_reason = models.CharField(max_length=1000, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
