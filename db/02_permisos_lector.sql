\set ON_ERROR_STOP on
DO $$
BEGIN
    IF current_database() <> 'sobusa_alertas_render' THEN
        RAISE EXCEPTION 'Use únicamente la base separada sobusa_alertas_render';
    END IF;
END
$$;
GRANT CONNECT ON DATABASE sobusa_alertas_render TO sobusa_alertas_lector;
GRANT USAGE ON SCHEMA public TO sobusa_alertas_lector;
REVOKE ALL ON public.alertas_publicas FROM sobusa_alertas_lector;
GRANT SELECT ON public.alertas_publicas TO sobusa_alertas_lector;
ALTER ROLE sobusa_alertas_lector SET default_transaction_read_only = on;
