-- M1: disponibilidad de datos en la base de la demo. ESTRICTAMENTE DE SOLO LECTURA.
--
-- No lee demanda real de octubre-diciembre de 2025 (B1 sin confirmar): toda consulta sobre
-- demanda_franja está acotada a franja_inicio < 2025-10-01 o >= 2026-01-01.
-- Las fechas de prediction sí se listan completas: son salidas nuestras, no datos reales.
--
-- Uso (PowerShell, en la carpeta del repositorio, con el contenedor db encendido):
--   Get-Content scripts\m1_disponibilidad_readonly.sql | docker compose exec -T db psql -U sobusa_lector -d sobusa -v ON_ERROR_STOP=1 > m1_disponibilidad_salida.txt

BEGIN TRANSACTION READ ONLY;
SET LOCAL timezone = 'America/Bogota';
SHOW transaction_read_only;

\echo == 1. Modelo activo ==
SELECT model_id, nombre, version, feature_version, estado, activado_en,
       hiperparametros->>'entrenado_hasta' AS entrenado_hasta_hiper,
       metricas->>'entrenado_hasta' AS entrenado_hasta_metricas
FROM ai_frecuencias.model_registry ORDER BY model_id;

\echo == 2. Predicciones guardadas por modelo y dia objetivo (hora de Bogota) ==
SELECT p.model_id, p.model_version, p.franja_inicio::date AS dia_objetivo,
       count(*) AS predicciones, count(DISTINCT p.execution_id) AS ejecuciones,
       min(p.data_cutoff) AS data_cutoff_min, max(p.data_cutoff) AS data_cutoff_max,
       min(p.generated_at) AS emitidas_desde, max(p.generated_at) AS emitidas_hasta
FROM ai_frecuencias.prediction p
GROUP BY 1, 2, 3 ORDER BY 3, 1;

\echo == 3. demanda_franja: cobertura FUERA de oct-dic 2025 ==
SELECT idruta,
       min(franja_inicio) FILTER (WHERE franja_inicio < '2025-10-01') AS primera,
       max(franja_inicio) FILTER (WHERE franja_inicio < '2025-10-01') AS ultima_antes_oct2025,
       count(*) FILTER (WHERE franja_inicio >= '2025-07-01' AND franja_inicio < '2025-10-01') AS franjas_jul_sep_2025,
       count(*) FILTER (WHERE franja_inicio >= '2025-07-01' AND franja_inicio < '2025-10-01'
                        AND pasajeros IS NULL) AS sin_medicion_jul_sep_2025,
       count(*) FILTER (WHERE franja_inicio >= '2026-01-01') AS franjas_desde_2026
FROM ai_frecuencias.demanda_franja
WHERE franja_inicio < '2025-10-01' OR franja_inicio >= '2026-01-01'
GROUP BY idruta ORDER BY idruta;

\echo == 4. Marca de agua y corte de consolidacion (margen 3 dias) ==
SELECT tabla_origen, ultima_actualizacion, ultima_actualizacion - interval '3 days' AS corte_consolidacion,
       lote, actualizada_en
FROM ai_frecuencias.watermark;

\echo == 5. Predicciones emparejables en el periodo de monitoreo autorizado (jul-sep 2025) ==
SELECT p.model_id, p.horizonte_min, count(*) AS predicciones,
       count(*) FILTER (WHERE p.franja_inicio + make_interval(mins => p.horizonte_min::int)
                        <= (SELECT ultima_actualizacion - interval '3 days' FROM ai_frecuencias.watermark
                            WHERE tabla_origen = 'progvehiculos')) AS consolidadas
FROM ai_frecuencias.prediction p
WHERE p.franja_inicio >= '2025-07-01' AND p.franja_inicio < '2025-09-30 22:00'
GROUP BY 1, 2 ORDER BY 1, 2;

ROLLBACK;
