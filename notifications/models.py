import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='notifications'
    )
    event_key = models.CharField(max_length=240)
    template_version = models.PositiveSmallIntegerField(default=1)
    title = models.CharField(max_length=160)
    body = models.TextField(max_length=2000)
    mandatory = models.BooleanField(default=True)
    security = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True)
    delivery_state = models.CharField(max_length=16, default='PENDING')
    delivered_at = models.DateTimeField(null=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=120, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['recipient', 'event_key', 'template_version'],
                name='notification_event_recipient_template_unique',
            )
        ]
        indexes = [models.Index(fields=['recipient', '-created_at', '-id'])]
        ordering = ['-created_at', '-id']
