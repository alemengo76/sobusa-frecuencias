-- Semilla SINTÉTICA de demostración. Mantiene el esquema y la versión lógica.
SET search_path TO ai_frecuencias;

-- 1. Nuevas filas (todavía no vigentes) a partir de la fila 'todos' vigente de cada ruta
INSERT INTO parametro_operativo (idruta, tipo_dia, capacidad_nominal, capacidad_fuente, ocupacion_objetivo,
                                 ciclo_min, factor_carga_max, factor_carga_fuente, flota_tope,
                                 headway_minimo_min, version, vigente)
SELECT b.idruta, f.tipo_dia, b.capacidad_nominal, b.capacidad_fuente, b.ocupacion_objetivo, b.ciclo_min,
       f.factor, f.fuente, b.flota_tope, b.headway_minimo_min, 'param_v2', false
FROM parametro_operativo b
JOIN (VALUES
    (2, 'todos', 0.310, 'fixture_sintetico'),
    (2, 'laboral', 0.320, 'fixture_sintetico'),
    (2, 'sabado', 0.330, 'fixture_sintetico'),
    (2, 'domingo_festivo', 0.340, 'fixture_sintetico'),
    (16, 'todos', 0.340, 'fixture_sintetico'),
    (16, 'laboral', 0.350, 'fixture_sintetico'),
    (16, 'sabado', 0.360, 'fixture_sintetico'),
    (16, 'domingo_festivo', 0.370, 'fixture_sintetico'),
    (21, 'todos', 0.370, 'fixture_sintetico'),
    (21, 'laboral', 0.380, 'fixture_sintetico'),
    (21, 'sabado', 0.390, 'fixture_sintetico'),
    (21, 'domingo_festivo', 0.400, 'fixture_sintetico')
) AS f (idruta, tipo_dia, factor, fuente) ON f.idruta = b.idruta
WHERE b.vigente AND b.tipo_dia = 'todos' AND b.version <> 'param_v2';

DO $$
BEGIN
    IF (SELECT count(*) FROM parametro_operativo WHERE version = 'param_v2') <> 12 THEN
        RAISE EXCEPTION 'param_v2: se esperaban 12 filas (3 rutas x 4 tipos de día); falta la fila vigente de alguna ruta';
    END IF;
END $$;

-- 2. La versión anterior deja de estar vigente (no se borra: queda para trazabilidad y reversión)
UPDATE parametro_operativo SET vigente = false WHERE vigente AND version <> 'param_v2';
-- 3. param_v2 entra en vigencia
UPDATE parametro_operativo SET vigente = true WHERE version = 'param_v2';
