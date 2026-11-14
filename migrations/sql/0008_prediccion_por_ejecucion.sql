-- 0008 · Trazabilidad entre predicciones y recomendaciones (DECISIONES.md §15).
--
-- Antes: prediction tenía UNIQUE (idruta, sentido, segmento_id, franja_inicio, horizonte_min, model_id,
-- data_cutoff) y la inserción usaba ON CONFLICT DO NOTHING. Repetir el mismo día con el mismo modelo
-- descartaba las filas sin avisar (la ejecución decía N y guardaba 0) y, si el histórico había
-- cambiado, conservaba las predicciones viejas.
--
-- Ahora:
--   - Cada ejecución guarda sus propias predicciones: UNIQUE (execution_id, idruta, sentido,
--     segmento_id, franja_inicio, horizonte_min). Su índice también sirve para leer por ejecución.
--   - prediction_set: una fila por CONTENIDO (huella huella-pred/1, app/ml/contrato.py). La clave
--     primaria garantiza un solo dueño por huella aunque dos solicitudes lleguen a la vez; quien
--     obtiene el mismo contenido reutiliza las filas del dueño en lugar de duplicarlas.
--   - franja_minutos = 15 pasa de invariante del código a regla de la base (validada).
--   - recommendation.prediction_id obligatorio para las filas NUEVAS (CHECK ... NOT VALID: las
--     recomendaciones anteriores sin prediction_id se conservan como están).
--
-- No modifica, borra ni mueve ninguna fila existente.
SET search_path TO ai_frecuencias;

-- 0. Comprobaciones previas: si alguna falla, la migración no cambia nada ----------------------
DO $$
DECLARE
    n bigint;
BEGIN
    SELECT count(*) INTO n FROM prediction WHERE franja_minutos IS DISTINCT FROM 15;
    IF n > 0 THEN
        RAISE EXCEPTION '0008: % predicciones tienen franja_minutos distinto de 15; no se puede exigir 15', n;
    END IF;

    SELECT count(*) INTO n FROM (
        SELECT 1 FROM prediction
        GROUP BY execution_id, idruta, sentido, segmento_id, franja_inicio, horizonte_min
        HAVING count(*) > 1) x;
    IF n > 0 THEN
        RAISE EXCEPTION '0008: % combinaciones (ejecución, ruta, sentido, segmento, franja, horizonte) '
                        'están repetidas; la clave nueva no se puede crear', n;
    END IF;
END $$;

-- 1. Clave única por ejecución (se retira la antigua, buscada por su definición) -----------------
DO $$
DECLARE
    nombre text;
BEGIN
    SELECT c.conname INTO nombre
    FROM pg_constraint c
    WHERE c.conrelid = 'prediction'::regclass AND c.contype = 'u'
      AND pg_get_constraintdef(c.oid) =
          'UNIQUE (idruta, sentido, segmento_id, franja_inicio, horizonte_min, model_id, data_cutoff)';
    IF nombre IS NULL THEN
        RAISE EXCEPTION '0008: no se encontró la clave única antigua de prediction (¿se cambió a mano?)';
    END IF;
    EXECUTE format('ALTER TABLE prediction DROP CONSTRAINT %I', nombre);
END $$;

ALTER TABLE prediction
    ADD CONSTRAINT uq_prediction_ejecucion
        UNIQUE (execution_id, idruta, sentido, segmento_id, franja_inicio, horizonte_min),
    ADD CONSTRAINT ck_prediction_franja_15 CHECK (franja_minutos = 15);

-- 2. Un solo dueño por contenido --------------------------------------------------------------
CREATE TABLE prediction_set (
    huella        text PRIMARY KEY CHECK (huella ~ '^[0-9a-f]{64}$'),
    execution_id  uuid NOT NULL UNIQUE REFERENCES execution_log (execution_id),
    formato       text NOT NULL,
    filas         integer NOT NULL CHECK (filas > 0),
    creado_en     timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE prediction_set IS
    'Un conjunto de predicciones por contenido (huella SHA-256, formato huella-pred/1). execution_id es la '
    'ejecución dueña de las filas en prediction; otra ejecución con la misma huella las reutiliza.';

-- 3. Recomendaciones nuevas siempre enlazadas a su predicción ----------------------------------
-- NOT VALID: no se revisan las filas existentes (las anteriores a la 0008 pueden tener NULL), pero
-- todo INSERT y todo UPDATE posterior debe cumplirla. No ejecutar VALIDATE CONSTRAINT mientras
-- existan esas filas.
ALTER TABLE recommendation
    ADD CONSTRAINT ck_recommendation_con_prediccion CHECK (prediction_id IS NOT NULL) NOT VALID;
CREATE INDEX ix_recommendation_ejecucion ON recommendation (execution_id);

-- [verificacion] ---------------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'prediction'::regclass
                   AND conname = 'ck_prediction_franja_15' AND convalidated) THEN
        RAISE EXCEPTION '0008: ck_prediction_franja_15 no quedó validada';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'recommendation'::regclass
                   AND conname = 'ck_recommendation_con_prediccion' AND NOT convalidated) THEN
        RAISE EXCEPTION '0008: ck_recommendation_con_prediccion debía quedar NOT VALID';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'prediction'::regclass AND contype = 'u'
               AND pg_get_constraintdef(oid) LIKE '%model_id, data_cutoff%') THEN
        RAISE EXCEPTION '0008: la clave única antigua sigue existiendo';
    END IF;
END $$;
-- [/verificacion] --------------------------------------------------------------------------------
