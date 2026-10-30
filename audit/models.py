import uuid

from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    worker = models.CharField(max_length=60, blank=True)
    authority_version = models.PositiveIntegerField(null=True)
    action = models.CharField(max_length=80)
    resource = models.CharField(max_length=100)
    outcome = models.CharField(max_length=30)
    reason = models.TextField(blank=True)
    purpose = models.CharField(max_length=160, blank=True)
    correlation = models.UUIDField(default=uuid.uuid4)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('Audit records are append only')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('Audit records are append only')
