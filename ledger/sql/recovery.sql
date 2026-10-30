-- Reviewed projection reconstruction only. This function never creates,
-- updates or deletes a journal, entry, reservation or payment instruction.
CREATE OR REPLACE FUNCTION public.dwmt_rebuild_projection(p_proposal uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE p public.oversight_recoveryproposal; w public.ledger_wallet;
 owner_uuid uuid; reviewer_uuid uuid; creator_uuid uuid; wallet_uuid uuid;
 gate_open boolean; available_total bigint; reserved_total bigint;
 funds_total bigint; incoming_total bigint; before_accounts jsonb; after_accounts jsonb;
 before_state jsonb; after_state jsonb; account_count integer;
BEGIN
 -- Discover references, then lock in the same gate/authority/business/wallet/
 -- account order as the surrounding coordinator. Reread decisive data later.
 SELECT * INTO STRICT p FROM public.oversight_recoveryproposal WHERE id=p_proposal;
 wallet_uuid := (p.payload->>'wallet_id')::uuid;
 reviewer_uuid := p.reviewer_id; creator_uuid := p.creator_id;
 SELECT owner_id INTO STRICT owner_uuid FROM public.ledger_wallet WHERE id=wallet_uuid;
 SELECT is_open INTO STRICT gate_open FROM public.ledger_integritygate WHERE id=1 FOR UPDATE;
 IF gate_open THEN RAISE EXCEPTION 'Projection recovery requires closed integrity gate'; END IF;
 PERFORM 1 FROM public.accounts_user WHERE id IN(owner_uuid,reviewer_uuid,creator_uuid) ORDER BY id FOR UPDATE;
 SELECT * INTO STRICT p FROM public.oversight_recoveryproposal WHERE id=p_proposal FOR UPDATE;
 IF p.kind<>'REBUILD_PROJECTIONS' OR p.state<>'APPROVED' OR p.reviewer_id IS NULL OR p.reviewer_id=p.creator_id OR p.reviewer_id=owner_uuid THEN
  RAISE EXCEPTION 'Distinct approved projection reconstruction required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM public.accounts_user u JOIN public.accounts_role r ON r.user_id=u.id
               WHERE u.id=p.reviewer_id AND u.status='ACTIVE' AND u.is_active AND u.verified_at IS NOT NULL
                 AND u.mfa_confirmed AND r.role='REVIEWER') THEN
  RAISE EXCEPTION 'Current reviewer authority required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM public.audit_auditevent a WHERE a.resource=p.id::text AND a.actor_id=p.reviewer_id
               AND a.action='RECOVERY_APPROVED' AND a.created_at>=p.created_at) THEN
  RAISE EXCEPTION 'Mandatory reconstruction approval audit required';
 END IF;
 IF (p.payload->>'wallet_id')::uuid<>wallet_uuid OR (p.payload->>'schema_version')::integer<>1 THEN
  RAISE EXCEPTION 'Recovery representation changed';
 END IF;
 SELECT * INTO STRICT w FROM public.ledger_wallet WHERE id=wallet_uuid FOR UPDATE;
 PERFORM 1 FROM public.ledger_ledgeraccount WHERE wallet_id=w.id ORDER BY id FOR UPDATE;
 SELECT count(*),jsonb_object_agg(id::text,balance_cents) INTO account_count,before_accounts
 FROM public.ledger_ledgeraccount WHERE wallet_id=w.id AND kind IN('AVAILABLE','RESERVED') AND currency='USD';
 IF account_count<>2 OR w.currency<>'USD' THEN RAISE EXCEPTION 'Existing USD account identities required'; END IF;
 SELECT coalesce(sum(CASE e.side WHEN 'CREDIT' THEN e.amount_cents ELSE -e.amount_cents END),0)
 INTO available_total FROM public.ledger_ledgeraccount a LEFT JOIN public.ledger_entry e ON e.account_id=a.id
 WHERE a.wallet_id=w.id AND a.kind='AVAILABLE';
 SELECT coalesce(sum(CASE e.side WHEN 'CREDIT' THEN e.amount_cents ELSE -e.amount_cents END),0)
 INTO reserved_total FROM public.ledger_ledgeraccount a LEFT JOIN public.ledger_entry e ON e.account_id=a.id
 WHERE a.wallet_id=w.id AND a.kind='RESERVED';
 SELECT coalesce(sum(amount_cents),0) INTO funds_total FROM public.ledger_reservation
 WHERE wallet_id=w.id AND kind='FUNDS' AND state='ACTIVE';
 SELECT coalesce(sum(amount_cents),0) INTO incoming_total FROM public.ledger_reservation
 WHERE wallet_id=w.id AND kind='INCOMING' AND state='ACTIVE';
 IF available_total<0 OR reserved_total<0 OR reserved_total<>funds_total OR available_total+reserved_total+incoming_total>1000000 THEN
  RAISE EXCEPTION 'Immutable entries and reservations do not prove a valid projection';
 END IF;
 SELECT jsonb_object_agg(id::text,CASE kind WHEN 'AVAILABLE' THEN available_total ELSE reserved_total END)
 INTO after_accounts FROM public.ledger_ledgeraccount WHERE wallet_id=w.id;
 before_state:=jsonb_build_object('available_cents',w.available_cents,'reserved_cents',w.reserved_cents,
                 'incoming_cents',w.incoming_cents,'sequence',w.sequence,'version',w.version,'accounts',before_accounts);
 after_state:=jsonb_build_object('available_cents',available_total,'reserved_cents',reserved_total,
                 'incoming_cents',incoming_total,'accounts',after_accounts);
 IF before_state<>(p.payload->'before') OR after_state<>(p.payload->'expected') THEN
  RAISE EXCEPTION 'Exact reviewed reconstruction diff is stale or invalid';
 END IF;
 UPDATE public.ledger_ledgeraccount SET balance_cents=CASE kind WHEN 'AVAILABLE' THEN available_total ELSE reserved_total END
 WHERE wallet_id=w.id AND kind IN('AVAILABLE','RESERVED');
 UPDATE public.ledger_wallet SET available_cents=available_total,reserved_cents=reserved_total,
       incoming_cents=incoming_total,version=version+1 WHERE id=w.id;
 RETURN jsonb_build_object('wallet_id',w.id,'before',before_state,'after',after_state,'applied_at',clock_timestamp());
END $$;
