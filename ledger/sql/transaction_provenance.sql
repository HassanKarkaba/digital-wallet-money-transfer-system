-- A journal may be created in a subtransaction; record its top-level transaction explicitly.
CREATE OR REPLACE FUNCTION public.dwmt_post_effect(p_operation uuid,p_effect text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE o public.payments_operation; orig public.payments_operation; s public.ledger_wallet; r public.ledger_wallet;
 j uuid; da uuid; ca uuid; stamp timestamptz; seqs jsonb:='{}'::jsonb; existing uuid;
BEGIN
 SELECT * INTO STRICT o FROM public.payments_operation WHERE id=p_operation FOR UPDATE;
 SELECT id INTO existing FROM public.ledger_journal WHERE operation_id=o.id AND effect_kind=p_effect;
 IF existing IS NOT NULL THEN RETURN existing; END IF;
 IF o.currency<>'USD' OR o.amount_cents<1 OR o.amount_cents>250000 OR o.policy_version<>'DWMT-P1' THEN RAISE EXCEPTION 'Unsupported monetary instruction'; END IF;
 PERFORM 1 FROM public.ledger_wallet WHERE id IN(o.sender_id,o.recipient_id) ORDER BY id FOR UPDATE;
 PERFORM 1 FROM public.ledger_ledgeraccount WHERE wallet_id IN(o.sender_id,o.recipient_id) OR (kind='SYSTEM_CASH' AND o.kind IN ('FUNDING','WITHDRAWAL')) ORDER BY id FOR UPDATE;
 SELECT * INTO s FROM public.ledger_wallet WHERE id=o.sender_id;
 SELECT * INTO r FROM public.ledger_wallet WHERE id=o.recipient_id;
 IF p_effect='RESERVE' THEN
  IF o.kind NOT IN ('TRANSFER','WITHDRAWAL') OR o.state NOT IN ('REVIEW_PENDING','READY') OR s.state<>'OPEN' OR s.available_cents<o.amount_cents THEN RAISE EXCEPTION 'Funds unavailable'; END IF;
  SELECT id INTO da FROM public.ledger_ledgeraccount WHERE wallet_id=s.id AND kind='AVAILABLE';
  SELECT id INTO ca FROM public.ledger_ledgeraccount WHERE wallet_id=s.id AND kind='RESERVED';
  INSERT INTO public.ledger_reservation(id,operation_id,wallet_id,amount_cents,kind,state,created_at,release_reason) VALUES(gen_random_uuid(),o.id,s.id,o.amount_cents,'FUNDS','ACTIVE',clock_timestamp(),'');
  PERFORM public.dwmt_day(s.owner_id,o.limit_day,'OUTGOING',o.amount_cents,0);
 ELSIF p_effect='TRANSFER' THEN
  IF o.kind<>'TRANSFER' OR o.state<>'COMPLETED' OR s.state<>'OPEN' OR r.state NOT IN ('OPEN','OUTGOING_RESTRICTED') OR s.id=r.id THEN RAISE EXCEPTION 'Invalid transfer'; END IF;
  SELECT id INTO da FROM public.ledger_ledgeraccount WHERE wallet_id=s.id AND kind='AVAILABLE';
  SELECT id INTO ca FROM public.ledger_ledgeraccount WHERE wallet_id=r.id AND kind='AVAILABLE';
  PERFORM public.dwmt_day(s.owner_id,o.limit_day,'OUTGOING',0,o.amount_cents);
 ELSIF p_effect IN ('HELD_TRANSFER','WITHDRAWAL','RELEASE') THEN
  IF NOT EXISTS(SELECT 1 FROM public.ledger_reservation WHERE operation_id=o.id AND wallet_id=s.id AND kind='FUNDS' AND state='ACTIVE' AND amount_cents=o.amount_cents) THEN RAISE EXCEPTION 'Missing attributable funds'; END IF;
  SELECT id INTO da FROM public.ledger_ledgeraccount WHERE wallet_id=s.id AND kind='RESERVED';
  IF p_effect='RELEASE' THEN
   IF o.state NOT IN ('FAILED','CANCELLED') THEN RAISE EXCEPTION 'Invalid release state'; END IF;
   SELECT id INTO ca FROM public.ledger_ledgeraccount WHERE wallet_id=s.id AND kind='AVAILABLE';
  ELSIF p_effect='HELD_TRANSFER' THEN
   IF o.kind<>'TRANSFER' OR o.state<>'COMPLETED' OR s.state<>'OPEN' OR r.state NOT IN ('OPEN','OUTGOING_RESTRICTED') THEN RAISE EXCEPTION 'Invalid held transfer'; END IF;
   SELECT id INTO ca FROM public.ledger_ledgeraccount WHERE wallet_id=r.id AND kind='AVAILABLE';
   PERFORM public.dwmt_capacity(o.id,'CONSUME');
  ELSE
   IF o.kind<>'WITHDRAWAL' OR o.state<>'COMPLETED' THEN RAISE EXCEPTION 'Invalid withdrawal'; END IF;
   SELECT id INTO ca FROM public.ledger_ledgeraccount WHERE kind='SYSTEM_CASH';
  END IF;
  UPDATE public.ledger_reservation SET state=CASE p_effect WHEN 'RELEASE' THEN 'RELEASED' ELSE 'CONSUMED' END,closed_at=clock_timestamp(),release_reason=CASE p_effect WHEN 'RELEASE' THEN 'Operation terminated' ELSE '' END WHERE operation_id=o.id AND kind='FUNDS' AND state='ACTIVE';
  PERFORM public.dwmt_day(s.owner_id,o.limit_day,'OUTGOING',-o.amount_cents,CASE p_effect WHEN 'RELEASE' THEN 0 ELSE o.amount_cents END);
  IF p_effect='RELEASE' AND o.kind='TRANSFER' THEN PERFORM public.dwmt_capacity(o.id,'RELEASE'); END IF;
 ELSIF p_effect='FUNDING' THEN
  IF o.kind<>'FUNDING' OR o.state<>'COMPLETED' THEN RAISE EXCEPTION 'Invalid funding'; END IF;
  SELECT id INTO da FROM public.ledger_ledgeraccount WHERE kind='SYSTEM_CASH';
  SELECT id INTO ca FROM public.ledger_ledgeraccount WHERE wallet_id=r.id AND kind='AVAILABLE';
  PERFORM public.dwmt_capacity(o.id,'CONSUME');
 ELSIF p_effect='REVERSAL' THEN
  SELECT * INTO STRICT orig FROM public.payments_operation WHERE id=o.original_id;
  IF o.kind<>'REVERSAL' OR o.state<>'COMPLETED' OR orig.kind<>'TRANSFER' OR orig.state<>'COMPLETED' OR orig.amount_cents<>o.amount_cents OR orig.sender_id<>o.recipient_id OR orig.recipient_id<>o.sender_id OR s.state<>'OPEN' OR r.state NOT IN ('OPEN','OUTGOING_RESTRICTED') THEN RAISE EXCEPTION 'Invalid whole reversal'; END IF;
  SELECT id INTO da FROM public.ledger_ledgeraccount WHERE wallet_id=s.id AND kind='AVAILABLE';
  SELECT id INTO ca FROM public.ledger_ledgeraccount WHERE wallet_id=r.id AND kind='AVAILABLE';
 ELSE RAISE EXCEPTION 'Unknown posting effect'; END IF;
 IF da IS NULL OR ca IS NULL OR da=ca THEN RAISE EXCEPTION 'Invalid posting accounts'; END IF;
 -- Credit-normal customer liabilities; debit-normal SYSTEM_CASH.
 UPDATE public.ledger_ledgeraccount SET balance_cents=balance_cents+CASE kind WHEN 'SYSTEM_CASH' THEN o.amount_cents ELSE -o.amount_cents END WHERE id=da;
 UPDATE public.ledger_ledgeraccount SET balance_cents=balance_cents+CASE kind WHEN 'SYSTEM_CASH' THEN -o.amount_cents ELSE o.amount_cents END WHERE id=ca;
 UPDATE public.ledger_wallet w SET
 available_cents=(SELECT balance_cents FROM public.ledger_ledgeraccount WHERE wallet_id=w.id AND kind='AVAILABLE'),
 reserved_cents=(SELECT balance_cents FROM public.ledger_ledgeraccount WHERE wallet_id=w.id AND kind='RESERVED'),sequence=sequence+1,version=version+1
 WHERE w.id IN(o.sender_id,o.recipient_id);
 IF EXISTS(SELECT 1 FROM public.ledger_wallet WHERE id IN(o.sender_id,o.recipient_id) AND available_cents+reserved_cents+incoming_cents>1000000) THEN RAISE EXCEPTION 'Receiving capacity exceeded'; END IF;
 SELECT coalesce(jsonb_object_agg(id::text,sequence),'{}'::jsonb) INTO seqs FROM public.ledger_wallet WHERE id IN(o.sender_id,o.recipient_id);
 stamp:=clock_timestamp(); j:=gen_random_uuid();
 INSERT INTO public.ledger_journal(id,operation_id,effect_kind,committed_at,wallet_sequences,policy_version,sealed,creation_transaction) VALUES(j,o.id,p_effect,stamp,seqs,'DWMT-P1',false,pg_current_xact_id()::text::bigint);
 INSERT INTO public.ledger_entry(id,journal_id,account_id,side,amount_cents) VALUES(gen_random_uuid(),j,da,'DEBIT',o.amount_cents),(gen_random_uuid(),j,ca,'CREDIT',o.amount_cents);
 UPDATE public.ledger_journal SET sealed=true WHERE id=j;
 RETURN j;
END $$;

CREATE OR REPLACE FUNCTION public.dwmt_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_TABLE_NAME='ledger_entry' AND TG_OP='INSERT' THEN
  IF NOT EXISTS(SELECT 1 FROM public.ledger_journal WHERE id=NEW.journal_id AND NOT sealed AND creation_transaction=pg_current_xact_id()::text::bigint) THEN RAISE EXCEPTION 'Journal is sealed'; END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ledger_journal' AND TG_OP='UPDATE' AND NOT OLD.sealed AND NEW.sealed AND EXISTS(SELECT 1 FROM public.ledger_journal WHERE id=OLD.id AND creation_transaction=pg_current_xact_id()::text::bigint) AND (to_jsonb(OLD)-'sealed')=(to_jsonb(NEW)-'sealed') THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'Financial history is immutable';
END $$;


-- Capacity is visible wallet state and advances its optimistic version.
CREATE OR REPLACE FUNCTION public.dwmt_capacity(p_operation uuid,p_action text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE o public.payments_operation; w public.ledger_wallet; r public.ledger_reservation;
BEGIN
 SELECT * INTO STRICT o FROM public.payments_operation WHERE id=p_operation FOR UPDATE;
 IF o.kind NOT IN ('FUNDING','TRANSFER') THEN RAISE EXCEPTION 'Invalid capacity operation'; END IF;
 SELECT * INTO STRICT w FROM public.ledger_wallet WHERE id=o.recipient_id FOR UPDATE;
 SELECT * INTO r FROM public.ledger_reservation WHERE operation_id=o.id AND wallet_id=w.id AND kind='INCOMING' FOR UPDATE;
 IF p_action='RESERVE' THEN
  IF r.id IS NOT NULL THEN
   IF r.state='ACTIVE' THEN RETURN; END IF;
   RAISE EXCEPTION 'Reservation closed';
  END IF;
  IF o.state NOT IN ('REVIEW_PENDING','READY') OR w.state NOT IN ('OPEN','OUTGOING_RESTRICTED') OR w.available_cents+w.reserved_cents+w.incoming_cents+o.amount_cents>1000000 THEN RAISE EXCEPTION 'Capacity unavailable'; END IF;
  IF o.kind='FUNDING' THEN PERFORM public.dwmt_day(w.owner_id,o.limit_day,'INCOMING',o.amount_cents,0); END IF;
  INSERT INTO public.ledger_reservation(id,operation_id,wallet_id,amount_cents,kind,state,created_at,release_reason)
  VALUES(gen_random_uuid(),o.id,w.id,o.amount_cents,'INCOMING','ACTIVE',clock_timestamp(),'');
  UPDATE public.ledger_wallet SET incoming_cents=incoming_cents+o.amount_cents,version=version+1 WHERE id=w.id;
 ELSIF p_action IN ('CONSUME','RELEASE') THEN
  IF r.id IS NULL THEN RAISE EXCEPTION 'Missing incoming reservation'; END IF;
  IF r.state<>'ACTIVE' THEN
   IF (p_action='CONSUME' AND r.state='CONSUMED') OR (p_action='RELEASE' AND r.state='RELEASED') THEN RETURN; END IF;
   RAISE EXCEPTION 'Reservation state conflict';
  END IF;
  IF (p_action='CONSUME' AND o.state<>'COMPLETED') OR (p_action='RELEASE' AND o.state NOT IN ('FAILED','CANCELLED')) THEN RAISE EXCEPTION 'Invalid capacity transition'; END IF;
  UPDATE public.ledger_reservation SET state=CASE p_action WHEN 'CONSUME' THEN 'CONSUMED' ELSE 'RELEASED' END,closed_at=clock_timestamp(),release_reason=CASE p_action WHEN 'RELEASE' THEN 'Operation terminated' ELSE '' END WHERE id=r.id;
  UPDATE public.ledger_wallet SET incoming_cents=incoming_cents-o.amount_cents,version=version+1 WHERE id=w.id;
  IF o.kind='FUNDING' THEN PERFORM public.dwmt_day(w.owner_id,o.limit_day,'INCOMING',-o.amount_cents,CASE p_action WHEN 'CONSUME' THEN o.amount_cents ELSE 0 END); END IF;
 ELSE RAISE EXCEPTION 'Unknown capacity action'; END IF;
END $$;
