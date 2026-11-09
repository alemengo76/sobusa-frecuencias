-- 0007 · Errores de calidad como EVENTOS + DETECCIONES (DECISIONES.md §8).
--
-- Antes: una fila por lote, viaje y regla. Como la ingesta trabaja por ventanas (un lote por
-- ventana) y relee los días que Extreme modifica, el mismo error quedaba repetido en varios lotes.
--
-- Ahora:
--   data_quality_error     = EVENTOS: uno por tabla_origen + idprogramacion + regla + reglas_version
--                            + valor. lote / creado_en = primera detección; ultimo_lote /
--                            ultima_deteccion = la más reciente.
--   data_quality_deteccion = DETECCIONES: una por evento y lote en que se vio, con la ruta, la causa
--                            y el valor tal como llegaron. id propio; id_original = id de la fila
--                            antigua (solo las migradas). Con esto el downgrade reconstruye la tabla.
--
-- Nada se pierde: cada una de las filas actuales queda como una detección con su id_original, y la
-- migración comprueba (huella md5 de las 10 columnas, distinguiendo NULL, 'NaN' y 'nan') que con
-- evento + detección se reconstruye exactamente la tabla original. Si no, falla y no cambia nada.
--
-- Normalización: los textos 'NaN' / 'nan' del valor (un faltante mal guardado) pasan a NULL en el
-- EVENTO. El texto exacto queda en data_quality_deteccion.valor_original.
SET search_path TO ai_frecuencias;

-- 0. Huella y tamaño de la tabla original (variables de esta transacción) -----------------------
SELECT set_config('sobusa.dq0007_filas', (SELECT count(*)::text FROM data_quality_error), true),
       set_config('sobusa.dq0007_max_id', (SELECT coalesce(max(id), 0)::text FROM data_quality_error), true),
       set_config('sobusa.dq0007_huella', (
           SELECT coalesce(md5(string_agg(concat_ws('|',
                      t.id, quote_nullable(t.lote), quote_nullable(t.tabla_origen),
                      quote_nullable(t.idprogramacion), quote_nullable(t.idruta), quote_nullable(t.regla),
                      quote_nullable(t.causa), quote_nullable(t.valor), quote_nullable(t.reglas_version),
                      quote_nullable(t.creado_en AT TIME ZONE 'UTC')), E'\n' ORDER BY t.id)), '')
           FROM data_quality_error t), true);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM data_quality_error WHERE idprogramacion IS NULL) THEN
        RAISE EXCEPTION '0007: hay errores de calidad sin idprogramacion; no se pueden identificar como eventos';
    END IF;
END $$;

-- 1. Tabla de detecciones ------------------------------------------------------------------------
CREATE TABLE data_quality_deteccion (
    id              bigserial PRIMARY KEY,
    id_original     bigint UNIQUE,
    evento_id       bigint NOT NULL REFERENCES data_quality_error (id),
    lote            text NOT NULL,
    detectado_en    timestamptz NOT NULL,
    idruta          integer,
    causa           text NOT NULL,
    valor_original  text,
    UNIQUE (evento_id, lote)
);
COMMENT ON TABLE data_quality_deteccion IS
    'Cada vez que un evento de calidad se detectó en un lote (con la ruta, causa y valor de ese lote). '
    'id_original = id de la fila en data_quality_error antes de la migración 0007.';
COMMENT ON COLUMN data_quality_deteccion.valor_original IS 'Texto exacto recibido (puede ser NULL, ''NaN'' o ''nan'')';

-- 2. Cada fila actual -> una detección del evento que queda (primera detección del grupo) ---------
INSERT INTO data_quality_deteccion (id_original, evento_id, lote, detectado_en, idruta, causa, valor_original)
SELECT g.id, g.evento_id, g.lote, g.creado_en, g.idruta, g.causa, g.valor
FROM (
    SELECT t.*,
           first_value(t.id) OVER (
               PARTITION BY t.tabla_origen, t.idprogramacion, t.regla, t.reglas_version,
                            (n.valor IS NULL), coalesce(n.valor, '')
               ORDER BY t.creado_en, t.id) AS evento_id
    FROM data_quality_error t
    CROSS JOIN LATERAL (SELECT CASE WHEN t.valor IN ('NaN', 'nan') THEN NULL ELSE t.valor END AS valor) n
) g
ORDER BY g.creado_en, g.id;

-- 3. Columnas de última detección; valor normalizado en el evento --------------------------------
ALTER TABLE data_quality_error
    ADD COLUMN ultimo_lote      text,
    ADD COLUMN ultima_deteccion timestamptz;

UPDATE data_quality_error e
SET ultimo_lote = u.lote,
    ultima_deteccion = u.detectado_en,
    valor = CASE WHEN e.valor IN ('NaN', 'nan') THEN NULL ELSE e.valor END
FROM (
    SELECT DISTINCT ON (evento_id) evento_id, lote, detectado_en
    FROM data_quality_deteccion
    ORDER BY evento_id, detectado_en DESC, id_original DESC
) u
WHERE e.id = u.evento_id;

-- 4. Las filas repetidas salen de la tabla de eventos (siguen completas como detecciones) ---------
DELETE FROM data_quality_error e
WHERE NOT EXISTS (SELECT 1 FROM data_quality_deteccion d WHERE d.evento_id = e.id);

-- 5. Restricciones e índices de la identidad nueva ----------------------------------------------
ALTER TABLE data_quality_error
    ALTER COLUMN ultimo_lote SET NOT NULL,
    ALTER COLUMN ultima_deteccion SET NOT NULL,
    ALTER COLUMN idprogramacion SET NOT NULL;

-- La restricción antigua UNIQUE (lote, tabla_origen, idprogramacion, regla) contradice la identidad
-- del evento (no incluye versión ni valor): se retira. Se busca por sus columnas, no por su nombre.
DO $$
DECLARE
    nombre text;
BEGIN
    SELECT c.conname INTO nombre
    FROM pg_constraint c
    WHERE c.conrelid = 'data_quality_error'::regclass AND c.contype = 'u'
      AND pg_get_constraintdef(c.oid) = 'UNIQUE (lote, tabla_origen, idprogramacion, regla)';
    IF nombre IS NOT NULL THEN
        EXECUTE format('ALTER TABLE data_quality_error DROP CONSTRAINT %I', nombre);
    END IF;
END $$;

-- PostgreSQL 14 no tiene NULLS NOT DISTINCT: (valor IS NULL) + coalesce(valor, '') hace que dos NULL
-- choquen y que NULL y '' no se confundan. La ingesta usa EXACTAMENTE estas expresiones en ON CONFLICT.
CREATE UNIQUE INDEX uq_dq_evento ON data_quality_error
    (tabla_origen, idprogramacion, regla, reglas_version, (valor IS NULL), (coalesce(valor, '')));
CREATE INDEX ix_dq_ultimo_lote ON data_quality_error (ultimo_lote);
CREATE INDEX ix_dq_deteccion_lote ON data_quality_deteccion (lote);

-- 6. Secuencia de eventos: nunca por debajo de un id ya usado ------------------------------------
-- No se asume nada: se compara el último valor entregado con el mayor id original (incluidas las
-- filas que ahora solo existen como detecciones) y se deja el mayor.
DO $$
DECLARE
    sec text := pg_get_serial_sequence('ai_frecuencias.data_quality_error', 'id');
    usado bigint;
    llamado boolean;
    maximo bigint := current_setting('sobusa.dq0007_max_id')::bigint;
BEGIN
    EXECUTE format('SELECT last_value, is_called FROM %s', sec) INTO usado, llamado;
    IF NOT llamado THEN
        usado := usado - 1;
    END IF;
    IF greatest(usado, maximo) >= 1 THEN
        PERFORM setval(sec, greatest(usado, maximo), true);
    END IF;
END $$;

-- [verificacion] ---------------------------------------------------------------------------------
-- Si algo no cuadra, la migración falla y no queda ningún cambio.
DO $$
DECLARE
    filas bigint := current_setting('sobusa.dq0007_filas')::bigint;
    huella text := current_setting('sobusa.dq0007_huella');
    reconstruida text;
    n bigint;
    sec text := pg_get_serial_sequence('ai_frecuencias.data_quality_error', 'id');
    usado bigint;
BEGIN
    SELECT count(*) INTO n FROM data_quality_deteccion WHERE id_original IS NOT NULL;
    IF n <> filas THEN
        RAISE EXCEPTION '0007: % detecciones migradas para % filas originales', n, filas;
    END IF;

    SELECT coalesce(md5(string_agg(concat_ws('|',
               d.id_original, quote_nullable(d.lote), quote_nullable(e.tabla_origen),
               quote_nullable(e.idprogramacion), quote_nullable(d.idruta), quote_nullable(e.regla),
               quote_nullable(d.causa), quote_nullable(d.valor_original), quote_nullable(e.reglas_version),
               quote_nullable(d.detectado_en AT TIME ZONE 'UTC')), E'\n' ORDER BY d.id_original)), '')
    INTO reconstruida
    FROM data_quality_deteccion d JOIN data_quality_error e ON e.id = d.evento_id
    WHERE d.id_original IS NOT NULL;
    IF reconstruida IS DISTINCT FROM huella THEN
        RAISE EXCEPTION '0007: evento + detección no reconstruyen la tabla original (huella distinta)';
    END IF;

    IF EXISTS (SELECT 1 FROM data_quality_error e
               WHERE e.ultima_deteccion <> (SELECT max(d.detectado_en) FROM data_quality_deteccion d
                                            WHERE d.evento_id = e.id)) THEN
        RAISE EXCEPTION '0007: ultima_deteccion no coincide con la detección más reciente';
    END IF;

    EXECUTE format('SELECT last_value FROM %s', sec) INTO usado;
    IF usado < (SELECT coalesce(max(id_original), 0) FROM data_quality_deteccion) THEN
        RAISE EXCEPTION '0007: la secuencia de eventos quedó por debajo de un id ya usado';
    END IF;
END $$;
-- [/verificacion] --------------------------------------------------------------------------------

-- La huella queda guardada en el comentario de la tabla de detecciones: el downgrade la usa para
-- avisar si alguien cambió a mano las detecciones migradas.
DO $$
BEGIN
    EXECUTE format('COMMENT ON TABLE data_quality_deteccion IS %L',
        'Cada vez que un evento de calidad se detectó en un lote (ruta, causa y valor de ese lote). '
        || 'id_original = id de la fila en data_quality_error antes de la migración 0007. '
        || 'huella_0007=' || current_setting('sobusa.dq0007_huella')
        || ';filas_0007=' || current_setting('sobusa.dq0007_filas'));
END $$;

COMMENT ON TABLE data_quality_error IS
    'Eventos de calidad: uno por tabla_origen + idprogramacion + regla + reglas_version + valor. '
    'lote/creado_en = primera detección; ultimo_lote/ultima_deteccion = la más reciente. '
    'Cada detección por lote está en data_quality_deteccion.';
