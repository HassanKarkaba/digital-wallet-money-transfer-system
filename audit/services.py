from .models import AuditEvent


def record(
    actor,
    action,
    resource,
    outcome='SUCCESS',
    reason='',
    metadata=None,
    purpose='',
    correlation=None,
    worker='',
):
    from accounts.services import db_now

    values = {
        'actor': actor if getattr(actor, 'is_authenticated', False) else None,
        'worker': worker or ('' if actor else 'local-worker'),
        'authority_version': getattr(actor, 'authority_version', None),
        'action': action,
        'resource': str(resource),
        'outcome': outcome,
        'reason': reason,
        'purpose': purpose,
        'metadata': metadata or {},
        'created_at': db_now(),
    }
    if correlation:
        values['correlation'] = correlation
    return AuditEvent.objects.create(**values)
