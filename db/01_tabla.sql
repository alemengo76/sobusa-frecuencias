\set ON_ERROR_STOP on
DO $$
BEGIN
    IF current_database() <> 'sobusa_alertas_render' THEN
        RAISE EXCEPTION 'Use únicamente la base separada sobusa_alertas_render';
    END IF;
END
$$;
CREATE TABLE public.alertas_publicas (
    alert_id bigint PRIMARY KEY,
    codigo text NOT NULL CHECK (codigo ~ '^ALT-[0-9]{3}$'),
    severidad text NOT NULL CHECK (severidad IN ('baja','media','alta')),
    estado text NOT NULL CHECK (estado IN ('abierta','reconocida','asignada','cerrada','descartada')),
    idruta integer NOT NULL CHECK (idruta > 0),
    franja_inicio timestamptz,
    horizonte_min integer,
    valor_observado numeric,
    valor_esperado numeric,
    umbral numeric,
    puntuacion_anomalia numeric,
    explicacion text,
    recomendacion text
);
CREATE INDEX ON public.alertas_publicas (idruta, estado, alert_id DESC);
REVOKE ALL ON public.alertas_publicas FROM PUBLIC;
