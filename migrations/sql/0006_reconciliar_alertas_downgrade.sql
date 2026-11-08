-- Reversión de la reconciliación 0006. Cuidadosa: solo deshace lo que nadie tocó después.
--
-- Una alerta descartada por la 0006 vuelve a su estado anterior (estado_antes de su fila de historial)
-- SOLO si:
--   - sigue 'descartada';
--   - la fila de la 0006 es la ÚLTIMA de su historial (nadie la comentó, asignó ni cambió después);
--   - su clave_dedup no está ocupada por otra alerta abierta creada después (reabrirla violaría la
--     regla de una sola alerta abierta por situación).
-- Las que no cumplen se dejan como están y se informa cuántas fueron (NOTICE).
-- No se borra historial: la reversión se registra como filas nuevas (usuario 'migracion_0006_reversion').
SET search_path TO ai_frecuencias;


-- 1. Reabrir las descartadas que se pueden revertir sin pisar decisiones posteriores ---------------
WITH descarte AS (
    SELECT h.id, h.alert_id, h.estado_antes
    FROM alert_history h
    WHERE h.usuario = 'migracion_0006' AND h.accion = 'cambio_estado' AND h.estado_nuevo = 'descartada'
),
reversibles AS (
    SELECT d.alert_id, d.estado_antes
    FROM descarte d JOIN alert a ON a.alert_id = d.alert_id
    WHERE a.estado = 'descartada'
      AND NOT EXISTS (SELECT 1 FROM alert_history p WHERE p.alert_id = d.alert_id AND p.id > d.id)
      AND NOT EXISTS (SELECT 1 FROM alert b WHERE b.clave_dedup = a.clave_dedup AND b.alert_id <> a.alert_id
                      AND b.estado IN ('abierta', 'reconocida', 'asignada'))
),
historial AS (
    INSERT INTO alert_history (alert_id, accion, estado_antes, estado_nuevo, comentario, usuario)
    SELECT alert_id, 'cambio_estado', 'descartada', estado_antes,
           'Reversión de la reconciliación 0006 (alembic downgrade).', 'migracion_0006_reversion'
    FROM reversibles
    RETURNING alert_id
)
UPDATE alert a SET estado = r.estado_antes, actualizada_en = now()
FROM reversibles r WHERE a.alert_id = r.alert_id;

-- 2. Anular la revalidación (comentario nuevo; el de la 0006 se conserva como historia) --------------
INSERT INTO alert_history (alert_id, accion, comentario, usuario)
SELECT DISTINCT h.alert_id, 'comentario',
       'Reversión de la reconciliación 0006: queda sin efecto la revalidación.', 'migracion_0006_reversion'
FROM alert_history h
WHERE h.usuario = 'migracion_0006' AND h.accion = 'comentario';

-- 3. Informar lo que no se pudo revertir ---------------------------------------------------------
DO $$
DECLARE
    pendientes integer;
BEGIN
    SELECT count(*) INTO pendientes
    FROM alert a
    JOIN LATERAL (SELECT max(h.id) AS id FROM alert_history h
                  WHERE h.alert_id = a.alert_id AND h.usuario = 'migracion_0006'
                    AND h.accion = 'cambio_estado') d ON d.id IS NOT NULL
    WHERE a.estado = 'descartada'
      AND NOT EXISTS (SELECT 1 FROM alert_history r WHERE r.alert_id = a.alert_id AND r.id > d.id
                      AND r.usuario = 'migracion_0006_reversion' AND r.accion = 'cambio_estado');
    IF pendientes > 0 THEN
        RAISE NOTICE 'Reversión 0006: % alertas siguen descartadas porque tuvieron cambios posteriores '
                     'o su situación ya tiene otra alerta abierta. Revíselas a mano.', pendientes;
    END IF;
END $$;
