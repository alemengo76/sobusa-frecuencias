-- Reversión de la 0007: vuelve a una fila por detección (forma antigua de data_quality_error).
--
-- Cada detección se convierte en una fila con los datos de su evento (tabla_origen, idprogramacion,
-- regla, reglas_version) y los suyos (lote, ruta, causa, valor tal como llegó, fecha):
--   - las detecciones migradas recuperan su id original (id_original) -> las filas de antes de la
--     0007 quedan idénticas en sus 10 columnas;
--   - las detecciones posteriores a la 0007 reciben un id NUEVO de la secuencia de la tabla.
-- Antes de cambiar nada comprueba que el resultado cumpla la restricción antigua
-- UNIQUE (lote, tabla_origen, idprogramacion, regla); si no, se detiene y dice qué casos lo impiden.
-- La secuencia nunca baja: queda en el mayor entre lo ya entregado y el id máximo reconstruido.
SET search_path TO ai_frecuencias;

-- 1. ¿Se puede volver a la restricción antigua? -------------------------------------------------
DO $$
DECLARE
    casos text;
BEGIN
    SELECT string_agg(format('lote %s, viaje %s, regla %s (%s detecciones)', lote, idprogramacion, regla, n),
                      '; ' ORDER BY lote, idprogramacion, regla)
    INTO casos
    FROM (SELECT d.lote, e.tabla_origen, e.idprogramacion, e.regla, count(*) AS n
          FROM data_quality_deteccion d JOIN data_quality_error e ON e.id = d.evento_id
          GROUP BY 1, 2, 3, 4 HAVING count(*) > 1 LIMIT 20) x;
    IF casos IS NOT NULL THEN
        RAISE EXCEPTION 'No se puede revertir la 0007 sin perder datos: la forma antigua admite una sola fila '
                        'por lote, viaje y regla, y hay detecciones de eventos distintos en: %', casos;
    END IF;
END $$;

-- 2. Aviso si las detecciones migradas ya no reproducen la huella guardada en la 0007 -------------
DO $$
DECLARE
    guardada text := substring(obj_description('data_quality_deteccion'::regclass, 'pg_class')
                               FROM 'huella_0007=([0-9a-f]*)');
    actual text;
BEGIN
    SELECT coalesce(md5(string_agg(concat_ws('|',
               d.id_original, quote_nullable(d.lote), quote_nullable(e.tabla_origen),
               quote_nullable(e.idprogramacion), quote_nullable(d.idruta), quote_nullable(e.regla),
               quote_nullable(d.causa), quote_nullable(d.valor_original), quote_nullable(e.reglas_version),
               quote_nullable(d.detectado_en AT TIME ZONE 'UTC')), E'\n' ORDER BY d.id_original)), '')
    INTO actual
    FROM data_quality_deteccion d JOIN data_quality_error e ON e.id = d.evento_id
    WHERE d.id_original IS NOT NULL;
    IF guardada IS NOT NULL AND actual IS DISTINCT FROM guardada THEN
        RAISE WARNING 'Reversión 0007: las detecciones migradas cambiaron después de la migración; '
                      'las filas anteriores a la 0007 no quedarán idénticas a las originales.';
    END IF;
END $$;

-- 3. Filas reconstruidas (tabla de paso) ----------------------------------------------------------
CREATE TABLE data_quality_error_reconstruida AS
SELECT coalesce(r.id_original, nextval(pg_get_serial_sequence('ai_frecuencias.data_quality_error', 'id'))) AS id,
       r.lote, r.tabla_origen, r.idprogramacion, r.idruta, r.regla, r.causa, r.valor, r.reglas_version, r.creado_en
FROM (
    SELECT d.id_original, d.lote, e.tabla_origen, e.idprogramacion, d.idruta, e.regla, d.causa,
           d.valor_original AS valor, e.reglas_version, d.detectado_en AS creado_en
    FROM data_quality_deteccion d JOIN data_quality_error e ON e.id = d.evento_id
    ORDER BY d.id_original NULLS LAST, d.detectado_en, d.id
) r;

-- 4. data_quality_error vuelve a su forma antigua (misma tabla: conserva su secuencia y su llave) --
DROP TABLE data_quality_deteccion;
DROP INDEX IF EXISTS uq_dq_evento;
DROP INDEX IF EXISTS ix_dq_ultimo_lote;
DELETE FROM data_quality_error;
ALTER TABLE data_quality_error
    DROP COLUMN ultimo_lote,
    DROP COLUMN ultima_deteccion,
    ALTER COLUMN idprogramacion DROP NOT NULL;
INSERT INTO data_quality_error (id, lote, tabla_origen, idprogramacion, idruta, regla, causa, valor,
                                reglas_version, creado_en)
SELECT id, lote, tabla_origen, idprogramacion, idruta, regla, causa, valor, reglas_version, creado_en
FROM data_quality_error_reconstruida;
DROP TABLE data_quality_error_reconstruida;
ALTER TABLE data_quality_error
    ADD CONSTRAINT data_quality_error_lote_tabla_origen_idprogramacion_regla_key
    UNIQUE (lote, tabla_origen, idprogramacion, regla);
COMMENT ON TABLE data_quality_error IS NULL;

-- 5. Secuencia: el mayor entre lo ya entregado y el id máximo; nunca se reutiliza un id ------------
DO $$
DECLARE
    sec text := pg_get_serial_sequence('ai_frecuencias.data_quality_error', 'id');
    usado bigint;
    llamado boolean;
    maximo bigint := (SELECT coalesce(max(id), 0) FROM data_quality_error);
BEGIN
    EXECUTE format('SELECT last_value, is_called FROM %s', sec) INTO usado, llamado;
    IF NOT llamado THEN
        usado := usado - 1;
    END IF;
    IF greatest(usado, maximo) >= 1 THEN
        PERFORM setval(sec, greatest(usado, maximo), true);
    END IF;
END $$;
