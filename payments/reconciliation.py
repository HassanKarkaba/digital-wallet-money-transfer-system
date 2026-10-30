"""Read both independent stores; apply only authoritative existing settlements."""

from datetime import timedelta

from django.db import connection, transaction

from accounts.services import db_now
from audit.services import record
from ledger.models import IntegrityGate
from oversight.models import Finding, ReconciliationRun

from .errors import DomainError


def verify_ledger():
    issues = []
    # Set-based aggregate queries avoid a query per customer on the acceptance dataset.
    with connection.cursor() as c:
        c.execute(
            "SELECT j.id FROM ledger_journal j LEFT JOIN ledger_entry e ON e.journal_id=j.id GROUP BY j.id HAVING count(e.id)<2 OR sum(CASE e.side WHEN 'DEBIT' THEN e.amount_cents ELSE -e.amount_cents END)<>0 OR NOT bool_and(j.sealed)"
        )
        issues += [{'classification': 'UNBALANCED_JOURNAL', 'reference': str(row[0])} for row in c.fetchall()]
        c.execute(
            """SELECT a.id FROM ledger_ledgeraccount a LEFT JOIN ledger_entry e ON e.account_id=a.id GROUP BY a.id HAVING a.balance_cents<>coalesce(sum(CASE WHEN (a.kind='SYSTEM_CASH' AND e.side='DEBIT') OR (a.kind<>'SYSTEM_CASH' AND e.side='CREDIT') THEN e.amount_cents ELSE -e.amount_cents END),0)"""
        )
        issues += [
            {'classification': 'PROJECTION_MISMATCH', 'reference': str(row[0])} for row in c.fetchall()
        ]
        c.execute(
            """SELECT w.id FROM ledger_wallet w WHERE w.available_cents<0 OR w.reserved_cents<0 OR w.incoming_cents<0 OR w.available_cents+w.reserved_cents+w.incoming_cents>1000000 OR w.available_cents<>coalesce((SELECT balance_cents FROM ledger_ledgeraccount a WHERE a.wallet_id=w.id AND kind='AVAILABLE'),0) OR w.reserved_cents<>coalesce((SELECT balance_cents FROM ledger_ledgeraccount a WHERE a.wallet_id=w.id AND kind='RESERVED'),0) OR w.reserved_cents<>coalesce((SELECT sum(amount_cents) FROM ledger_reservation r WHERE r.wallet_id=w.id AND kind='FUNDS' AND state='ACTIVE'),0) OR w.incoming_cents<>coalesce((SELECT sum(amount_cents) FROM ledger_reservation r WHERE r.wallet_id=w.id AND kind='INCOMING' AND state='ACTIVE'),0)"""
        )
        issues += [
            {'classification': 'RESERVATION_MISMATCH', 'reference': str(row[0])} for row in c.fetchall()
        ]
        c.execute(
            "SELECT coalesce(sum(available_cents+reserved_cents),0)=(SELECT balance_cents FROM ledger_ledgeraccount WHERE kind='SYSTEM_CASH') FROM ledger_wallet"
        )
        if c.fetchone()[0] is not True:
            issues.append({'classification': 'CASH_LIABILITY_MISMATCH'})
        c.execute(
            "SELECT id FROM payments_operation o WHERE state='COMPLETED' AND NOT EXISTS(SELECT 1 FROM ledger_journal j WHERE j.operation_id=o.id AND effect_kind IN ('TRANSFER','HELD_TRANSFER','FUNDING','WITHDRAWAL','REVERSAL'))"
        )
        issues += [{'classification': 'MISSING_JOURNAL', 'reference': str(row[0])} for row in c.fetchall()]
    return issues


def close_gate(reason, actor=None):
    with transaction.atomic():
        gate = IntegrityGate.objects.select_for_update().get(pk=1)
        gate.is_open = False
        gate.version += 1
        gate.reason = reason[:1000]
        gate.changed_at = db_now()
        gate.changed_by = actor
        gate.save()
        record(actor, 'integrity.closed', 'environment', reason=reason)


def queue_reconciliation(actor=None, apply=True, limit=1000000, daily_key=None):
    from .reconciliation_progress import queue_reconciliation as queue

    return queue(actor, apply, limit, daily_key)


def advance_reconciliation(run_id, job_id=None, generation=None, batch=200):
    from .reconciliation_progress import advance_reconciliation as advance

    return advance(run_id, job_id, generation, batch)


def reconcile(actor=None, apply=True, limit=1000000):
    """Explicit operator wrapper; HTTP callers queue durable work instead."""
    from .reconciliation_progress import reconcile_inline

    return reconcile_inline(actor, apply, limit)


def reopen_gate(actor, reason, final_guard=None):
    """A second reviewer acknowledges a fresh clean recheck; operator cannot override."""
    from accounts.services import assert_eligible

    assert_eligible(actor, staff_role='REVIEWER')
    if not 10 <= len(reason.strip()) <= 1000:
        raise DomainError('Give an investigation reason of 10 to 1,000 characters.')
    if verify_ledger():
        raise DomainError('Accounting checks are not clean.', 'CONFLICT', 409)
    last = ReconciliationRun.objects.order_by('-created_at').first()
    if not last or not last.clean or db_now() - last.completed_at > timedelta(minutes=5):
        raise DomainError('Run a fresh clean reconciliation first.', 'CONFLICT', 409)
    with transaction.atomic():
        gate = IntegrityGate.objects.select_for_update().get(pk=1)
        import json

        from django.conf import settings

        from rails.adapter import check_identity

        marker = json.loads(settings.RECOVERY_MARKER_PATH.read_text())
        if (
            str(marker.get('environment_id')) != str(gate.environment_id)
            or str(gate.environment_id) != str(settings.ENVIRONMENT_ID)
            or marker.get('generation') != gate.recovery_generation
            or gate.recovery_generation != settings.RECOVERY_GENERATION
        ):
            raise DomainError('Recovery identities must agree before reopening.', 'CONFLICT', 409)
        check_identity()
        from config.health import enforcement_ready

        if not enforcement_ready(check_runtime_role=not getattr(settings, 'VERIFY_SYNCHRONOUS_WORK', False)):
            raise DomainError(
                'Protected posting capabilities must be restored before reopening.', 'UNAVAILABLE', 503
            )
        if Finding.objects.filter(state='OPEN').exclude(classification='STALE_UNKNOWN').exists():
            raise DomainError(
                'Resolve each investigation finding against a clean recheck before reopening.',
                'CONFLICT',
                409,
            )
        if (last.cutoff_at or last.created_at) < gate.changed_at:
            raise DomainError('Run a new clean reconciliation after the latest incident.', 'CONFLICT', 409)
        if gate.changed_by_id == actor.pk or last.actor_id == actor.pk:
            raise DomainError('A different reviewer must acknowledge this recovery.', 'DENIED', 403)
        if final_guard:
            final_guard()
        gate.is_open = True
        gate.version += 1
        gate.reason = reason
        gate.changed_by = actor
        gate.changed_at = db_now()
        gate.save()
        record(actor, 'integrity.reopened', 'environment', reason=reason)
        return gate
