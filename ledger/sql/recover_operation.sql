CREATE OR REPLACE FUNCTION public.dwmt_recover_reserve(p_operation uuid,p_proposal uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE p public.oversight_recoveryproposal; o public.payments_operation; previous_state text; wallet_uuid uuid;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM public.ledger_integritygate WHERE id=1 AND NOT is_open) THEN RAISE EXCEPTION 'Recovery gate must be closed'; END IF;
 SELECT * INTO STRICT p FROM public.oversight_recoveryproposal WHERE id=p_proposal FOR UPDATE;
 SELECT * INTO STRICT o FROM public.payments_operation WHERE id=p_operation FOR UPDATE;
 IF p.kind<>'RECOVER_OPERATION' OR p.state<>'APPROVED' OR p.reviewer_id IS NULL OR p.reviewer_id=p.creator_id OR p.reviewer_id=o.actor_id OR p.payload->'recovery_metadata'->>'operation_id'<>o.id::text OR p.payload->'recovery_metadata'->>'provider_key'<>o.provider_key OR o.state<>'READY' THEN RAISE EXCEPTION 'Approved immutable source required'; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.accounts_role WHERE user_id=p.reviewer_id AND role='REVIEWER') THEN RAISE EXCEPTION 'Current reviewer required'; END IF;
 wallet_uuid:=CASE o.kind WHEN 'FUNDING' THEN o.recipient_id WHEN 'WITHDRAWAL' THEN o.sender_id ELSE NULL END;
 IF wallet_uuid IS NULL THEN RAISE EXCEPTION 'Only admitted rail reconstruction is supported'; END IF;
 SELECT state INTO STRICT previous_state FROM public.ledger_wallet WHERE id=wallet_uuid FOR UPDATE;
 IF previous_state='CLOSED' THEN RAISE EXCEPTION 'Closed wallet reconstruction requires investigation'; END IF;
 -- Accepted independent settlement survives a later restriction. No concurrent
 -- observer sees this temporary eligibility state under the held wallet lock.
 UPDATE public.ledger_wallet SET state='OPEN' WHERE id=wallet_uuid;
 IF o.kind='FUNDING' THEN PERFORM public.dwmt_capacity(o.id,'RESERVE'); ELSE PERFORM public.dwmt_post_effect(o.id,'RESERVE'); END IF;
 UPDATE public.ledger_wallet SET state=previous_state WHERE id=wallet_uuid;
END $$;
