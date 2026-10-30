CREATE TRIGGER immutable_replay BEFORE UPDATE OR DELETE ON public.payments_replaykey FOR EACH ROW EXECUTE FUNCTION public.dwmt_immutable();
CREATE OR REPLACE FUNCTION public.dwmt_fixed_delegation() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_TABLE_NAME='payments_schedule' THEN
  IF (to_jsonb(OLD)-ARRAY['state','operation_id','version','failure_reason'])<>(to_jsonb(NEW)-ARRAY['state','operation_id','version','failure_reason']) THEN RAISE EXCEPTION 'Schedule instruction is immutable'; END IF;
 ELSE
  IF (to_jsonb(OLD)-ARRAY['state','operation_id','version'])<>(to_jsonb(NEW)-ARRAY['state','operation_id','version']) THEN RAISE EXCEPTION 'Request instruction is immutable'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER fixed_schedule BEFORE UPDATE ON public.payments_schedule FOR EACH ROW EXECUTE FUNCTION public.dwmt_fixed_delegation();
CREATE TRIGGER fixed_request BEFORE UPDATE ON public.payments_moneyrequest FOR EACH ROW EXECUTE FUNCTION public.dwmt_fixed_delegation();
