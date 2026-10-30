import uuid

from django.conf import settings
from django.db import models


class RecoveryProposal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    finding = models.ForeignKey(
        'oversight.Finding', on_delete=models.PROTECT, related_name='recovery_proposals'
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='proposed_recoveries'
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='approved_recoveries'
    )
    kind = models.CharField(
        max_length=24,
        choices=[
            ('REBUILD_PROJECTIONS', 'Rebuild projections'),
            ('RECOVER_OPERATION', 'Recover missing operation'),
        ],
    )
    state = models.CharField(max_length=16, default='OPEN')
    reason = models.CharField(max_length=1000)
    decision_reason = models.CharField(max_length=1000, blank=True)
    version = models.PositiveIntegerField(default=1)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True)
    applied_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(state__in=['OPEN', 'APPROVED', 'APPLIED', 'BLOCKED']),
                name='recovery_proposal_state',
            )
        ]
