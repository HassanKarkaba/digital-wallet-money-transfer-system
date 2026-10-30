from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = 'Apply runtime grants with migration credentials; no credentials are printed.'

    def handle(self, *args, **options):
        with connections['default'].cursor() as c:
            c.execute('GRANT USAGE ON SCHEMA public TO wallet_runtime, ledger_owner')
            c.execute('GRANT CREATE ON SCHEMA public TO ledger_owner')
            c.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO wallet_runtime')
            c.execute('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO wallet_runtime, ledger_owner')
            c.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ledger_owner')
            for table in [
                'ledger_wallet',
                'ledger_ledgeraccount',
                'ledger_journal',
                'ledger_entry',
                'ledger_reservation',
                'ledger_dailycounter',
            ]:
                c.execute(f'REVOKE INSERT,UPDATE,DELETE ON public.{table} FROM wallet_runtime')
                c.execute(f'GRANT SELECT,UPDATE ON public.{table} TO wallet_runtime') if False else None
            # PostgreSQL row locks require a table-level UPDATE privilege or at least one column UPDATE privilege.
            c.execute('GRANT UPDATE(state,version) ON public.ledger_wallet TO wallet_runtime')
            c.execute('GRANT UPDATE(currency) ON public.ledger_ledgeraccount TO wallet_runtime')
            c.execute('REVOKE UPDATE,DELETE ON public.audit_auditevent FROM wallet_runtime')
            c.execute(
                'REVOKE DELETE ON public.payments_operation,public.payments_replaykey FROM wallet_runtime'
            )
            c.execute('REVOKE UPDATE ON public.payments_replaykey FROM wallet_runtime')
            c.execute(
                "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname LIKE 'dwmt_%'"
            )
            for (fn,) in c.fetchall():
                c.execute(f'ALTER FUNCTION {fn} OWNER TO ledger_owner')
                c.execute(f'REVOKE ALL ON FUNCTION {fn} FROM PUBLIC')
            for signature in [
                'dwmt_open_wallet(uuid)',
                'dwmt_post_effect(uuid,text)',
                'dwmt_capacity(uuid,text)',
                'dwmt_prepare_day(uuid,date,text)',
                'dwmt_rebuild_projection(uuid)',
                'dwmt_recover_reserve(uuid,uuid)',
            ]:
                c.execute(f'GRANT EXECUTE ON FUNCTION public.{signature} TO wallet_runtime')
            c.execute(
                "INSERT INTO ledger_ledgeraccount(id,wallet_id,kind,currency,balance_cents) SELECT gen_random_uuid(),NULL,'SYSTEM_CASH','USD',0 WHERE NOT EXISTS(SELECT 1 FROM ledger_ledgeraccount WHERE kind='SYSTEM_CASH')"
            )
        with connections['simulator'].cursor() as c:
            c.execute('GRANT USAGE ON SCHEMA public TO simulator_runtime')
            c.execute('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA public TO simulator_runtime')
            c.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO simulator_runtime')
            c.execute('REVOKE DELETE ON public.simulator_providereffect FROM simulator_runtime')
        self.stdout.write('Runtime database permissions installed.')
