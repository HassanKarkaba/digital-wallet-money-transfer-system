import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Destination(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        DISABLED = 'DISABLED', 'Disabled'

    class Scenario(models.TextChoices):
        SUCCESS = 'SUCCESS', 'Success'
        DECLINE = 'DECLINE', 'Final decline'
        DELAY = 'DELAY', 'Delayed success'
        TIMEOUT_AFTER_COMMIT = 'TIMEOUT_AFTER_COMMIT', 'Timeout after commitment'
        MALFORMED_AFTER_COMMIT = 'MALFORMED_AFTER_COMMIT', 'Malformed response after commitment'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='destinations')
    token_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    label = models.CharField(max_length=80)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    activation_at = models.DateTimeField()
    scenario = models.CharField(max_length=32, choices=Scenario.choices, default=Scenario.SUCCESS)
    created_at = models.DateTimeField(default=timezone.now)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=['ACTIVE', 'DISABLED']), name='destination_legal_status'
            )
        ]

    @property
    def masked_label(self):
        return f'Synthetic •••• {str(self.token_id)[-4:]} · {self.label}'

    def __str__(self):
        return self.masked_label


class CallbackInbox(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, default='simulator')
    event_id = models.UUIDField()
    provider_key = models.CharField(max_length=200)
    payload_digest = models.CharField(max_length=64)
    schema_version = models.PositiveSmallIntegerField(default=1)
    key_id = models.CharField(max_length=32)
    received_at = models.DateTimeField(default=timezone.now)
    state = models.CharField(max_length=20, default='ACCEPTED')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['provider', 'event_id'], name='callback_provider_event_unique')
        ]
        indexes = [models.Index(fields=['provider_key'])]


class CallbackIncident(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_id = models.UUIDField(null=True, blank=True)
    provider_key = models.CharField(max_length=200, blank=True)
    classification = models.CharField(max_length=64)
    digest = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
