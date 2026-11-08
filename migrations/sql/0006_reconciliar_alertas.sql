-- Reconciliación de alertas abiertas tras dos cambios de regla (solo datos):
--   a) ALT-002, ALT-003 y ALT-004 se evalúan solo con el horizonte de 60 min
--      (app/alertas/catalogo.py: HORIZONTE_ALERTAS_FRECUENCIA);
--   b) ALT-003 pasó a la versión vigente del umbral (2,0 buses/h, migración 0005).
-- Regla: todo cambio de umbral o de alcance de una alerta va con su reconciliación (DECISIONES.md §14).
--
-- Qué hace:
--   1. Pasa a 'descartada' las alertas abiertas (abierta, reconocida o asignada) que la regla vigente
--      ya no generaría, con una fila en alert_history (cambio_estado, motivo específico del grupo).
--   2. A las que sí la cumplen solo les agrega un comentario en alert_history. No se modifica su fila:
--      conservan estado, usuario_asignado, umbral, umbral_version y actualizada_en.
-- El umbral de ALT-003 se lee de la tabla umbral (vigente), no se escribe aquí.
-- Todas las filas de historial de esta migración llevan usuario = 'migracion_0006' (así se revierte).
SET search_path TO ai_frecuencias;

-- 1. Descartar lo que ya no aplica ---------------------------------------------------------------
WITH vigente AS (
    SELECT valor, version FROM umbral WHERE codigo = 'ALT-003' AND vigente
),
objetivo AS (
    SELECT a.alert_id, a.estado,
           CASE WHEN a.horizonte_min <> 60 THEN
                    'Reconciliación 0006: las alertas de frecuencia (ALT-002, ALT-003, ALT-004) solo se '
                    || 'evalúan con el horizonte de 60 min; esta era de ' || a.horizonte_min || ' min.'
                ELSE
                    'Reconciliación 0006: ALT-003 con umbral v' || v.version || ' = ' || v.valor::float
                    || ' buses/h; el déficit de esta alerta es ' || (a.valor_esperado - a.valor_observado)::float
                    || ' (creada con umbral v' || coalesce(a.umbral_version::text, '?') || ' = ' || a.umbral::float || ').'
           END AS motivo
    FROM alert a CROSS JOIN vigente v
    WHERE a.estado IN ('abierta', 'reconocida', 'asignada')
      AND a.codigo IN ('ALT-002', 'ALT-003', 'ALT-004')
      AND (a.horizonte_min <> 60
           OR (a.codigo = 'ALT-003'
               AND coalesce(a.umbral_version, 0) < v.version
               AND a.valor_esperado - a.valor_observado < v.valor))
),
historial AS (
    INSERT INTO alert_history (alert_id, accion, estado_antes, estado_nuevo, comentario, usuario)
    SELECT alert_id, 'cambio_estado', estado, 'descartada', motivo, 'migracion_0006' FROM objetivo
    RETURNING alert_id
)
UPDATE alert a SET estado = 'descartada', actualizada_en = now()
FROM objetivo o WHERE a.alert_id = o.alert_id;

-- 2. Revalidar (solo historial) las que siguen abiertas -----------------------------------------
INSERT INTO alert_history (alert_id, accion, comentario, usuario)
SELECT a.alert_id, 'comentario',
       'Revalidada en la reconciliación 0006: cumple la regla vigente (horizonte de 60 min'
       || CASE WHEN a.codigo = 'ALT-003'
               THEN '; déficit ' || (a.valor_esperado - a.valor_observado)::float || ' >= umbral v'
                    || v.version || ' = ' || v.valor::float || ' buses/h'
               ELSE '' END
       || '). Conserva su umbral_version original.',
       'migracion_0006'
FROM alert a CROSS JOIN (SELECT valor, version FROM umbral WHERE codigo = 'ALT-003' AND vigente) v
WHERE a.estado IN ('abierta', 'reconocida', 'asignada')
  AND a.codigo IN ('ALT-002', 'ALT-003', 'ALT-004');

-- 3. Comprobaciones ------------------------------------------------------------------------------
DO $$
DECLARE
    malas integer;
BEGIN
    SELECT count(*) INTO malas
    FROM alert a, umbral u
    WHERE u.codigo = 'ALT-003' AND u.vigente
      AND a.estado IN ('abierta', 'reconocida', 'asignada')
      AND a.codigo IN ('ALT-002', 'ALT-003', 'ALT-004')
      AND (a.horizonte_min <> 60
           OR (a.codigo = 'ALT-003' AND a.valor_esperado - a.valor_observado < u.valor));
    IF malas > 0 THEN
        RAISE EXCEPTION 'Reconciliación 0006: quedaron % alertas abiertas que la regla vigente no generaría', malas;
    END IF;
    IF EXISTS (SELECT 1 FROM alert a WHERE a.estado = 'descartada'
               AND NOT EXISTS (SELECT 1 FROM alert_history h WHERE h.alert_id = a.alert_id
                               AND h.estado_nuevo = 'descartada')) THEN
        RAISE EXCEPTION 'Reconciliación 0006: hay alertas descartadas sin su fila de historial';
    END IF;
END $$;
