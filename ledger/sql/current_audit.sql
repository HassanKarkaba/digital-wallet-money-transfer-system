-- Every protected operation version requires its own matching mandatory audit.
CREATE OR REPLACE FUNCTION public.dwmt_check_operation() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE o public.payments_operation;
BEGIN
 SELECT * INTO o FROM public.payments_operation WHERE id=NEW.id;
 IF NOT EXISTS(SELECT 1 FROM public.audit_auditevent WHERE resource=o.id::text AND metadata->>'state'=o.state AND metadata->>'version'=o.version::text) THEN RAISE EXCEPTION 'Operation lacks required audit'; END IF;
 IF o.state='COMPLETED' AND NOT EXISTS(SELECT 1 FROM public.ledger_journal WHERE operation_id=o.id AND effect_kind IN('TRANSFER','HELD_TRANSFER','FUNDING','WITHDRAWAL','REVERSAL')) THEN RAISE EXCEPTION 'Completed operation lacks settlement'; END IF;
 IF o.state IN ('COMPLETED','CANCELLED','FAILED') AND EXISTS(SELECT 1 FROM public.ledger_reservation WHERE operation_id=o.id AND state='ACTIVE') THEN RAISE EXCEPTION 'Terminal operation retains reservation'; END IF;
 IF o.state IN ('REVIEW_PENDING','READY','SUBMITTED','UNKNOWN') AND o.kind IN ('WITHDRAWAL','TRANSFER') AND NOT EXISTS(SELECT 1 FROM public.ledger_reservation WHERE operation_id=o.id AND kind='FUNDS' AND state='ACTIVE' AND amount_cents=o.amount_cents) THEN RAISE EXCEPTION 'Pending operation lacks funds'; END IF;
 IF o.state IN ('REVIEW_PENDING','READY','SUBMITTED','UNKNOWN') AND o.kind IN ('FUNDING','TRANSFER') AND NOT EXISTS(SELECT 1 FROM public.ledger_reservation WHERE operation_id=o.id AND kind='INCOMING' AND state='ACTIVE' AND amount_cents=o.amount_cents) THEN RAISE EXCEPTION 'Pending operation lacks capacity'; END IF;
 RETURN NULL;
END $$;
