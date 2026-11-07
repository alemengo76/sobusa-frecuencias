-- E03: param_v2. Reemplaza el factor de carga PROVISIONAL 0,40 por el medido con el sensor
-- (fe_v4_comun15/salidas_feature_engineering_v4/factor_carga.csv, reglas sensor-1.1; copia en
-- datos/referencia/factor_carga_sensor11.csv). Solo datos: no cambia ninguna tabla.
--   todos            = mediana por viaje de la ruta (fila 'todos' del archivo, exacta)
--   laboral/sabado/  = mediana ponderada por viajes de las medianas por hora de ese tipo de día
--   domingo_festivo    (app/optimizacion/parametros.derivar_factor_carga; una prueba verifica que
--                       estos números salen del archivo)
-- Capacidad, ocupación objetivo, ciclo, flota tope y headway mínimo se copian de la fila vigente
-- de cada ruta: param_v2 solo cambia el factor de carga y lo abre por tipo de día.
-- No incluye factores por hora (requieren otra tabla; no autorizado todavía).
SET search_path TO ai_frecuencias;

-- 1. Nuevas filas (todavía no vigentes) a partir de la fila 'todos' vigente de cada ruta
INSERT INTO parametro_operativo (idruta, tipo_dia, capacidad_nominal, capacidad_fuente, ocupacion_objetivo,
                                 ciclo_min, factor_carga_max, factor_carga_fuente, flota_tope,
                                 headway_minimo_min, version, vigente)
SELECT b.idruta, f.tipo_dia, b.capacidad_nominal, b.capacidad_fuente, b.ocupacion_objetivo, b.ciclo_min,
       f.factor, f.fuente, b.flota_tope, b.headway_minimo_min, 'param_v2', false
FROM parametro_operativo b
JOIN (VALUES
    (2,  'todos',           0.400, 'sensor-1.1 factor_carga.csv: mediana de la ruta'),
    (2,  'laboral',         0.419, 'sensor-1.1 factor_carga.csv: mediana ponderada por hora'),
    (2,  'sabado',          0.378, 'sensor-1.1 factor_carga.csv: mediana ponderada por hora'),
    (2,  'domingo_festivo', 0.365, 'sensor-1.1 factor_carga.csv: mediana ponderada por hora'),
    (16, 'todos',           0.435, 'sensor-1.1 factor_carga.csv: mediana de la ruta'),
    (16, 'laboral',         0.459, 'sensor-1.1 factor_carga.csv: mediana ponderada por hora'),
    (16, 'sabado',          0.429, 'sensor-1.1 factor_carga.csv: mediana ponderada por hora'),
    (16, 'domingo_festivo', 0.418, 'sensor-1.1 factor_carga.csv: mediana ponderada por hora'),
    (21, 'todos',           0.379, 'sensor-1.1 factor_carga.csv: mediana de la ruta'),
    (21, 'laboral',         0.387, 'sensor-1.1 factor_carga.csv: mediana ponderada por hora'),
    (21, 'sabado',          0.381, 'sensor-1.1 factor_carga.csv: mediana ponderada por hora'),
    (21, 'domingo_festivo', 0.372, 'sensor-1.1 factor_carga.csv: mediana ponderada por hora')
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
