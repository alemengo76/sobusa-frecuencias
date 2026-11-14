-- Reversión de la 0008: vuelve a la clave única antigua de prediction.
--
-- Se niega (sin cambiar nada) si la clave antigua no puede representar los datos: después de la 0008,
-- predecir otra vez un mismo día con el mismo modelo y un histórico distinto guarda filas nuevas con la
-- misma (ruta, sentido, segmento, franja, horizonte, modelo, corte); la forma antigua solo admite una.
-- No borra predicciones ni recomendaciones. Se pierde prediction_set (qué ejecución es dueña de cada
-- contenido): las ejecuciones que reutilizaron predicciones lo siguen diciendo en execution_log.resumen.
SET search_path TO ai_frecuencias;

DO $$
DECLARE
    n bigint;
BEGIN
    SELECT count(*) INTO n FROM (
        SELECT 1 FROM prediction
        GROUP BY idruta, sentido, segmento_id, franja_inicio, horizonte_min, model_id, data_cutoff
        HAVING count(*) > 1) x;
    IF n > 0 THEN
        RAISE EXCEPTION 'No se puede revertir la 0008: % combinaciones (ruta, sentido, segmento, franja, '
                        'horizonte, modelo, corte) tienen predicciones de más de una ejecución; la clave '
                        'antigua solo admite una', n;
    END IF;
END $$;

DROP INDEX ix_recommendation_ejecucion;
ALTER TABLE recommendation DROP CONSTRAINT ck_recommendation_con_prediccion;
DROP TABLE prediction_set;
ALTER TABLE prediction
    DROP CONSTRAINT ck_prediction_franja_15,
    DROP CONSTRAINT uq_prediction_ejecucion;
ALTER TABLE prediction
    ADD CONSTRAINT prediction_idruta_sentido_segmento_id_franja_inicio_horizon_key
        UNIQUE (idruta, sentido, segmento_id, franja_inicio, horizonte_min, model_id, data_cutoff);
