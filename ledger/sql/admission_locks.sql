CREATE OR REPLACE FUNCTION public.dwmt_prepare_day(p_owner uuid,p_day date,p_direction text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF p_direction NOT IN ('INCOMING','OUTGOING') THEN RAISE EXCEPTION 'Invalid day direction'; END IF;
 INSERT INTO public.ledger_dailycounter(owner_id,day,direction,reserved_cents,committed_cents)
 VALUES(p_owner,p_day,p_direction,0,0) ON CONFLICT(owner_id,day,direction) DO NOTHING;
 PERFORM 1 FROM public.ledger_dailycounter WHERE owner_id=p_owner AND day=p_day AND direction=p_direction FOR UPDATE;
END $$;
