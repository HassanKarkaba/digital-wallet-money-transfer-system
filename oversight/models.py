import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Case(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey('payments.Operation', on_delete=models.PROTECT, related_name='cases')
    type = models.CharField(max_length=16)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_cases'
    )
    assigned_finance = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='assigned_finance_cases'
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='reviewed_cases'
    )
    reason = models.CharField(max_length=1000)
    evidence = models.TextField(blank=True)
    state = models.CharField(max_length=24, default='OPEN')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True)
    decided_at = models.DateTimeField(null=True)
    decision_reason = models.CharField(max_length=1000, blank=True)
    parent = models.ForeignKey('self', null=True, on_delete=models.PROTECT, related_name='proposals')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['operation'], condition=Q(type='DISPUTE', state='OPEN'), name='one_open_dispute'
            )
        ]


class ReconciliationRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True)
    checked = models.PositiveIntegerField(default=0)
    clean = models.BooleanField(default=False)
    summary = models.JSONField(default=dict)
    state = models.CharField(
        max_length=16,
        default='PENDING',
        choices=[
            ('PENDING', 'Pending'),
            ('RUNNING', 'Running'),
            ('COMPLETED', 'Completed'),
            ('FAILED', 'Failed'),
        ],
    )
    phase = models.CharField(max_length=16, default='INIT')
    cursor = models.CharField(max_length=200, blank=True)
    provider_high_water = models.CharField(max_length=200, blank=True)
    local_high_water = models.CharField(max_length=200, blank=True)
    cutoff_at = models.DateTimeField(null=True)
    step = models.PositiveIntegerField(default=0)
    actor_authority_version = models.PositiveIntegerField(null=True)
    apply_observations = models.BooleanField(default=True)
    max_records = models.PositiveIntegerField(default=1000000)
    daily_key = models.CharField(max_length=80, unique=True, null=True)
    failure_code = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(state__in=['PENDING', 'RUNNING', 'COMPLETED', 'FAILED']),
                name='reconciliation_legal_state',
            ),
            models.CheckConstraint(
                condition=Q(phase__in=['INIT', 'PROVIDER', 'LOCAL', 'LEDGER', 'DONE']),
                name='reconciliation_legal_phase',
            ),
        ]
        indexes = [models.Index(fields=['state', 'created_at'])]


class Finding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(ReconciliationRun, null=True, on_delete=models.PROTECT, related_name='findings')
    classification = models.CharField(max_length=40)
    operation = models.ForeignKey('payments.Operation', null=True, on_delete=models.PROTECT)
    provider_key = models.CharField(max_length=200, blank=True)
    details = models.JSONField(default=dict)
    state = models.CharField(max_length=24, default='OPEN')
    created_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    reason = models.CharField(max_length=1000, blank=True)


# Keep Django model discovery explicit for the separate recovery responsibility.
from .recovery_models import RecoveryProposal  # noqa: E402,F401


class CaseStatement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name='statements')
    statement = models.OneToOneField(
        'reports.Statement', on_delete=models.PROTECT, related_name='case_access'
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='requested_case_statements'
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='inspected_case_statements'
    )
    created_at = models.DateTimeField()
    purpose = models.CharField(max_length=1000)
