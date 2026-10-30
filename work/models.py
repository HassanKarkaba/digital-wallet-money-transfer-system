import uuid

from django.db import models
from django.utils import timezone


class Job(models.Model):
    class State(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RUNNING = 'RUNNING', 'Running'
        RETRY = 'RETRY', 'Retry scheduled'
        DONE = 'DONE', 'Done'
        NEEDS_ATTENTION = 'NEEDS_ATTENTION', 'Needs investigation'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=24)
    object_id = models.CharField(max_length=200)
    business_key = models.CharField(max_length=240, unique=True)
    schema_version = models.PositiveSmallIntegerField(default=1)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    due_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    attempt = models.PositiveSmallIntegerField(default=0)
    generation = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=120, blank=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=['state', 'due_at', 'created_at']),
            models.Index(fields=['kind', 'state', 'due_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(state__in=['PENDING', 'RUNNING', 'RETRY', 'DONE', 'NEEDS_ATTENTION']),
                name='job_legal_state',
            )
        ]
        ordering = ['due_at', 'created_at', 'id']


class JobAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name='attempts')
    generation = models.PositiveIntegerField()
    outcome = models.CharField(max_length=24)
    detail = models.CharField(max_length=120, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)


class Heartbeat(models.Model):
    identity = models.CharField(primary_key=True, max_length=100)
    kind = models.CharField(max_length=20, default='worker')
    seen_at = models.DateTimeField(default=timezone.now)
    state = models.CharField(max_length=20, default='RUNNING')
