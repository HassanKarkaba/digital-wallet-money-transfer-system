import json
import os
from datetime import timedelta

from django.conf import settings
from django.db import connection


def readiness():
    state = {
        'primary': False,
        'integrity': False,
        'recovery': False,
        'worker': False,
        'simulator': False,
        'mail': False,
        'private_storage': False,
    }
    try:
        from accounts.services import db_now
        from ledger.models import IntegrityGate
        from work.models import Heartbeat

        now = db_now()
        with connection.cursor() as c:
            c.execute("SELECT to_regprocedure('public.dwmt_post_effect(uuid,text)') IS NOT NULL")
            state['primary'] = c.fetchone()[0]
        state['primary'] = state['primary'] and enforcement_ready()
        gate = IntegrityGate.objects.get(pk=1)
        state['integrity'] = gate.is_open and gate.policy_version == 'DWMT-P1'
        if gate.last_seen_at and now < gate.last_seen_at - timedelta(seconds=60):
            state['integrity'] = False
        marker = json.loads(settings.RECOVERY_MARKER_PATH.read_text())
        state['recovery'] = (
            str(marker['environment_id']) == str(gate.environment_id) == str(settings.ENVIRONMENT_ID)
            and marker['generation'] == gate.recovery_generation == settings.RECOVERY_GENERATION
        )
        state['worker'] = Heartbeat.objects.filter(
            kind='worker', state='RUNNING', seen_at__gt=now - timedelta(seconds=30)
        ).exists()
        from rails.adapter import check_identity

        check_identity()
        state['simulator'] = Heartbeat.objects.filter(
            kind='simulator', state='RUNNING', seen_at__gt=now - timedelta(seconds=30)
        ).exists()
    except Exception:
        pass
    from pathlib import Path

    for name, folder, cap in [
        ('mail', settings.EMAIL_FILE_PATH, 100 * 1024 * 1024),
        ('private_storage', settings.PRIVATE_ROOT, 1024 * 1024 * 1024),
    ]:
        try:
            p = Path(folder)
            state[name] = (
                p.is_dir()
                and os.access(p, os.W_OK)
                and sum(f.stat().st_size for f in p.rglob('*') if f.is_file()) < cap
            )
        except OSError:
            pass
    return state


def enforcement_ready(check_runtime_role=True):
    """Inspect the capabilities relied on by admission, without writing data."""
    functions = [
        'public.dwmt_open_wallet(uuid)',
        'public.dwmt_post_effect(uuid,text)',
        'public.dwmt_capacity(uuid,text)',
        'public.dwmt_prepare_day(uuid,date,text)',
        'public.dwmt_rebuild_projection(uuid)',
        'public.dwmt_recover_reserve(uuid,uuid)',
    ]
    triggers = [
        ('ledger_entry', 'immutable_entry'),
        ('ledger_journal', 'immutable_journal'),
        ('ledger_journal', 'balanced_journal'),
        ('ledger_entry', 'balanced_entries'),
        ('ledger_wallet', 'wallet_consistency'),
        ('ledger_ledgeraccount', 'cash_consistency'),
        ('ledger_dailycounter', 'day_consistency'),
        ('payments_operation', 'operation_consistency'),
        ('payments_operation', 'operation_instruction'),
        ('payments_replaykey', 'immutable_replay'),
        ('payments_schedule', 'fixed_schedule'),
        ('payments_moneyrequest', 'fixed_request'),
        ('audit_auditevent', 'immutable_audit'),
    ]
    from django.apps import apps

    tables = [
        'public.' + model._meta.db_table
        for model in apps.get_models(include_auto_created=True)
        if model._meta.app_label != 'simulator' and not model._meta.proxy
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT bool_and(to_regprocedure(signature) IS NOT NULL) FROM unnest(%s::text[]) AS signature',
            [functions],
        )
        if not cursor.fetchone()[0]:
            return False
        cursor.execute(
            'SELECT bool_and(to_regclass(table_name) IS NOT NULL) FROM unnest(%s::text[]) AS table_name',
            [tables],
        )
        if not cursor.fetchone()[0]:
            return False
        cursor.execute(
            """SELECT count(*) FROM unnest(%s::text[],%s::text[]) AS wanted(table_name,trigger_name)
          JOIN pg_class c ON c.relname=wanted.table_name JOIN pg_namespace n ON n.oid=c.relnamespace AND n.nspname='public'
          JOIN pg_trigger t ON t.tgrelid=c.oid AND t.tgname=wanted.trigger_name AND NOT t.tgisinternal AND t.tgenabled IN ('O','A')""",
            [[x[0] for x in triggers], [x[1] for x in triggers]],
        )
        if cursor.fetchone()[0] != len(triggers):
            return False
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM django_migrations WHERE app='ledger' AND name='0010_current_operation_audit')"
        )
        if not cursor.fetchone()[0]:
            return False
        if check_runtime_role:
            cursor.execute("""SELECT current_user='wallet_runtime'
              AND NOT has_column_privilege('ledger_wallet','available_cents','UPDATE')
              AND NOT has_column_privilege('ledger_wallet','reserved_cents','UPDATE')
              AND NOT has_column_privilege('ledger_wallet','incoming_cents','UPDATE')
              AND NOT has_column_privilege('ledger_ledgeraccount','balance_cents','UPDATE')
              AND NOT has_table_privilege('ledger_journal','INSERT,UPDATE,DELETE')
              AND NOT has_table_privilege('ledger_entry','INSERT,UPDATE,DELETE')
              AND NOT has_table_privilege('ledger_reservation','INSERT,UPDATE,DELETE')
              AND NOT has_table_privilege('audit_auditevent','UPDATE,DELETE')""")
            if not cursor.fetchone()[0]:
                return False
    return True
