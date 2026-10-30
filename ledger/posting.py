from django.db import connection

from .models import Wallet


def _call(name, *args):
    if not connection.in_atomic_block:
        raise RuntimeError('Ledger changes require an enclosing primary transaction.')
    allowed = {'dwmt_post_effect', 'dwmt_capacity', 'dwmt_open_wallet'}
    if name not in allowed:
        raise ValueError('Unknown ledger boundary')
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT public.{name}(' + ','.join(['%s'] * len(args)) + ')', args)
        return cursor.fetchone()[0]


def open_wallet(user):
    from django.db import transaction

    with transaction.atomic():
        return Wallet.objects.get(pk=_call('dwmt_open_wallet', user.pk))


def post_effect(operation, effect_kind):
    return _call('dwmt_post_effect', operation.pk, effect_kind)


def reserve(operation):
    if operation.kind == 'FUNDING':
        return capacity(operation, 'RESERVE')
    result = post_effect(operation, 'RESERVE')
    if operation.kind == 'TRANSFER':
        capacity(operation, 'RESERVE')
    return result


def capacity(operation, action):
    return _call('dwmt_capacity', operation.pk, action)


def release(operation):
    if operation.kind == 'FUNDING':
        return capacity(operation, 'RELEASE')
    return post_effect(operation, 'RELEASE')


def lock_resources(wallet_ids, kind, owner_id=None, day=None):
    """Acquire every lower rank before the coordinator's final proof/time check."""
    from django.db.models import Q

    from accounts.services import db_now

    from .models import LedgerAccount

    query = Q(wallet_id__in=[w for w in wallet_ids if w])
    if kind in {'FUNDING', 'WITHDRAWAL'}:
        query |= Q(kind='SYSTEM_CASH')
    list(LedgerAccount.objects.select_for_update().filter(query).order_by('id'))
    stamp = db_now()
    if owner_id:
        original = day
        while True:
            selected = original or stamp.date()
            with connection.cursor() as c:
                c.execute(
                    'SELECT public.dwmt_prepare_day(%s,%s,%s)',
                    [owner_id, selected, 'INCOMING' if kind == 'FUNDING' else 'OUTGOING'],
                )
            stamp = db_now()
            if original or stamp.date() == selected:
                break
    return stamp
