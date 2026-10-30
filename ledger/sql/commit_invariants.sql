CREATE OR REPLACE FUNCTION public.dwmt_check_cash() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE cash bigint; liabilities bigint;
BEGIN
 SELECT balance_cents INTO STRICT cash FROM public.ledger_ledgeraccount WHERE kind='SYSTEM_CASH';
 SELECT coalesce(sum(available_cents+reserved_cents),0) INTO liabilities FROM public.ledger_wallet;
 IF cash<>liabilities OR cash<0 THEN RAISE EXCEPTION 'Cash and customer liabilities differ'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER cash_consistency AFTER INSERT OR UPDATE ON public.ledger_ledgeraccount DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.kind='SYSTEM_CASH') EXECUTE FUNCTION public.dwmt_check_cash();

CREATE OR REPLACE FUNCTION public.dwmt_check_day() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE r bigint; c bigint; actual_r bigint; actual_c bigint;
BEGIN
 SELECT reserved_cents,committed_cents INTO actual_r,actual_c FROM public.ledger_dailycounter WHERE id=NEW.id;
 IF NEW.direction='OUTGOING' THEN
  SELECT coalesce(sum(amount_cents) FILTER(WHERE state IN ('REVIEW_PENDING','READY','SUBMITTED','UNKNOWN')),0),coalesce(sum(amount_cents) FILTER(WHERE state='COMPLETED'),0) INTO r,c FROM public.payments_operation WHERE actor_id=NEW.owner_id AND limit_day=NEW.day AND kind IN ('TRANSFER','WITHDRAWAL');
 ELSE
  SELECT coalesce(sum(amount_cents) FILTER(WHERE state IN ('REVIEW_PENDING','READY','SUBMITTED','UNKNOWN')),0),coalesce(sum(amount_cents) FILTER(WHERE state='COMPLETED'),0) INTO r,c FROM public.payments_operation WHERE actor_id=NEW.owner_id AND limit_day=NEW.day AND kind='FUNDING';
 END IF;
 IF r<>actual_r OR c<>actual_c OR r+c>500000 THEN RAISE EXCEPTION 'Daily admission projection mismatch'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER day_consistency AFTER INSERT OR UPDATE ON public.ledger_dailycounter DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.dwmt_check_day();
ALTER TABLE public.ledger_dailycounter ADD CONSTRAINT day_direction CHECK(direction IN ('INCOMING','OUTGOING'));
ALTER TABLE public.payments_moneyrequest ADD CONSTRAINT request_legal_amount CHECK(amount_cents BETWEEN 1 AND 250000);
ALTER TABLE public.payments_moneyrequest ADD CONSTRAINT request_legal_state CHECK(state IN ('OPEN','PAYMENT_PENDING','PAID','DECLINED','CANCELLED','EXPIRED','CLOSED_UNPAID'));
ALTER TABLE public.payments_schedule ADD CONSTRAINT schedule_legal_amount CHECK(amount_cents BETWEEN 1 AND 250000);
ALTER TABLE public.payments_schedule ADD CONSTRAINT schedule_legal_state CHECK(state IN ('SCHEDULED','EXECUTED','CANCELLED','FAILED','MISSED'));
CREATE INDEX IF NOT EXISTS operation_day_counter_lookup ON public.payments_operation(actor_id,limit_day,kind,state);
