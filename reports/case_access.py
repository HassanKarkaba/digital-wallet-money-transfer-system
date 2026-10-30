"""Narrow read authority for a retained statement linked to an assigned case."""

from accounts.services import assert_eligible
from payments.errors import DomainError


def generation_delegation(statement_id, *, lock_case=False):
    from oversight.models import Case, CaseStatement

    link = (
        CaseStatement.objects.select_related(
            'requested_by', 'owner', 'statement', 'case__operation__sender', 'case__operation__recipient'
        )
        .filter(statement_id=statement_id)
        .first()
    )
    if not link:
        return None
    case = Case.objects.select_for_update().get(pk=link.case_id) if lock_case else link.case
    if case.assigned_finance_id != link.requested_by_id:
        raise DomainError('The Finance assignment changed before statement generation.', 'DENIED', 403)
    try:
        assert_eligible(link.requested_by, staff_role='FINANCE')
    except ValueError as exc:
        raise DomainError(
            'The Finance requester no longer has current statement authority.', 'DENIED', 403
        ) from exc
    parties = set()
    operation = link.case.operation
    if operation.sender_id:
        parties.add(operation.sender.owner_id)
    if operation.recipient_id:
        parties.add(operation.recipient.owner_id)
    if link.owner_id != link.statement.owner_id or link.owner_id not in parties:
        raise DomainError('The statement delegation does not match this case participant.', 'CONFLICT', 409)
    return link


def queue_case_statement(actor, case_id, owner_id, start, end, timezone, purpose, version):
    from django.db import transaction

    from accounts.models import User
    from accounts.services import db_now
    from audit.services import record
    from oversight.models import Case, CaseStatement

    from .services import queue_statement

    if not 10 <= len(purpose.strip()) <= 1000:
        raise DomainError('Give an investigation purpose of 10 to 1,000 characters.')
    with transaction.atomic():
        users = {
            u.id: u
            for u in User.objects.select_for_update().filter(id__in=[actor.id, owner_id]).order_by('id')
        }
        current = users.get(actor.id)
        owner = users.get(owner_id)
        if not current or not owner:
            raise DomainError('The case participant is unavailable.', 'DENIED', 404)
        from payments.workflows import _current_actor

        _current_actor(actor, current)
        try:
            assert_eligible(current, staff_role='FINANCE')
        except ValueError as exc:
            raise DomainError('Your Finance authority changed.', 'DENIED', 403) from exc
        case = (
            Case.objects.select_for_update(of=('self',))
            .select_related('operation__sender', 'operation__recipient')
            .get(pk=case_id)
        )
        if case.assigned_finance_id != current.id:
            raise DomainError('This case is not assigned to your current Finance authority.', 'DENIED', 404)
        if case.version != version:
            raise DomainError(
                'The case changed. Review its current assignment before continuing.', 'STALE', 409
            )
        parties = set()
        if case.operation.sender_id:
            parties.add(case.operation.sender.owner_id)
        if case.operation.recipient_id:
            parties.add(case.operation.recipient.owner_id)
        if owner.id not in parties:
            raise DomainError('This customer is not a participant in the assigned case.', 'DENIED', 404)
        statement = queue_statement(
            owner,
            start,
            end,
            timezone,
            allow_inactive_owner=True,
            audit_actor=current,
            purpose=purpose.strip(),
        )
        link = CaseStatement.objects.create(
            case=case,
            statement=statement,
            requested_by=current,
            owner=owner,
            created_at=db_now(),
            purpose=purpose.strip(),
        )
        record(
            current,
            'staff.case.statement.queued',
            str(link.id),
            reason=link.purpose,
            metadata={'case': str(case.id), 'statement': str(statement.id), 'owner': str(owner.id)},
        )
        return link
