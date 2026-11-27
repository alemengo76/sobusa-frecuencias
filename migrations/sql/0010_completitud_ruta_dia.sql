-- 0010 · Evidencia de completitud por ruta y día.
-- La lee app/db/lectura_real.completitud_por_dia (M1, ALT-005, ALT-007, ALT-010) y la escribe
-- app/calidad/completitud.registrar desde el ciclo diario (regla completitud-v2).
-- Numeración: la 0009 está reservada para la migración de sentido IDA/VUELTA (DECISIONES §14).
-- Solo crea objetos nuevos en ai_frecuencias. No toca tablas existentes ni de Extreme.
SET search_path TO ai_frecuencias;

CREATE TABLE completitud_ruta_dia (
    idruta          integer     NOT NULL CHECK (idruta IN (2, 16, 21)),
    fecha           date        NOT NULL,
    estado          text        NOT NULL CHECK (estado IN ('completo', 'parcial', 'desconocido')),
    regla_version   text        NOT NULL,
    evidencia       jsonb       NOT NULL,
    execution_id    uuid        NOT NULL REFERENCES execution_log (execution_id),
    calculado_en    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (idruta, fecha)
);
COMMENT ON TABLE completitud_ruta_dia IS
  'Estado vigente de completitud por ruta y día (regla completitud-v2). Ver app/calidad/completitud.py.';

-- Cada recálculo que cambia el estado o la evidencia copia antes la fila vigente aquí.
CREATE TABLE completitud_ruta_dia_historial (LIKE completitud_ruta_dia);
ALTER TABLE completitud_ruta_dia_historial ADD COLUMN archivado_en timestamptz NOT NULL DEFAULT now();
CREATE INDEX ix_completitud_hist ON completitud_ruta_dia_historial (idruta, fecha, archivado_en);
