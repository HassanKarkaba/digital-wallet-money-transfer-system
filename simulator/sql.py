# Installed by the simulator protection migration after the model tables exist.
PROTECTION_SQL = r"""
CREATE OR REPLACE FUNCTION public.dwmt_provider_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Provider effects are retained'; END IF;
  IF ROW(NEW.provider_key, NEW.operation_id, NEW.environment_id, NEW.direction, NEW.token_id,
         NEW.owner_id, NEW.amount_cents, NEW.currency, NEW.scenario, NEW.schema_version, NEW.created_at)
     IS DISTINCT FROM ROW(OLD.provider_key, OLD.operation_id, OLD.environment_id, OLD.direction,
         OLD.token_id, OLD.owner_id, OLD.amount_cents, OLD.currency, OLD.scenario, OLD.schema_version, OLD.created_at)
  THEN RAISE EXCEPTION 'Provider instruction is immutable'; END IF;
  IF OLD.state IN ('SUCCESS', 'DECLINED') AND NEW IS DISTINCT FROM OLD
  THEN RAISE EXCEPTION 'Provider terminal result is immutable'; END IF;
  IF OLD.state = 'PENDING' AND (NEW.state NOT IN ('PENDING', 'SUCCESS', 'DECLINED') OR NEW.provider_sequence < OLD.provider_sequence)
  THEN RAISE EXCEPTION 'Invalid provider transition'; END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS dwmt_provider_immutable ON public.simulator_providereffect;
CREATE TRIGGER dwmt_provider_immutable BEFORE UPDATE OR DELETE ON public.simulator_providereffect
  FOR EACH ROW EXECUTE FUNCTION public.dwmt_provider_immutable();
"""
