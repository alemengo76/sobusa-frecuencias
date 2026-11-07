-- Consultas de solo lectura para verificar hallazgos en los scripts de Extreme (E07).
-- Ejecutar con el usuario de lectura. Ninguna modifica datos.

-- 1. ¿timbradas_anio acumula viajes o guarda solo el último? (paso 10 de fn_insert_timbradas_anio)
--    Si casi todo sale con nro_viajes = 1, la acumulación no funciona.
SELECT nro_viajes, COUNT(*) AS franjas
FROM timbradas_anio WHERE id_ruta IN (2, 16, 21)
GROUP BY nro_viajes ORDER BY nro_viajes;

-- 2. Totales anuales de timbradas_anio frente a los viajes (deberían ser parecidos)
SELECT t.id_ruta, t.ano, t.subidas_anio, v.subidas_viajes,
       ROUND(100.0 * (t.subidas_anio - v.subidas_viajes) / NULLIF(v.subidas_viajes, 0), 1) AS dif_pct
FROM (SELECT id_ruta, ano, SUM(subidas) AS subidas_anio FROM timbradas_anio
      WHERE id_ruta IN (2, 16, 21) GROUP BY 1, 2) t
JOIN (SELECT idruta AS id_ruta, EXTRACT(YEAR FROM COALESCE(fecha_fin_liquidacion, fechafin))::int AS ano,
             SUM(numeropasajeros) AS subidas_viajes
      FROM progvehiculos WHERE idruta IN (2, 16, 21) AND apto_estadisticas_historicas
      GROUP BY 1, 2) v USING (id_ruta, ano)
ORDER BY 1, 2;

-- 3. Segunda prueba con el consolidado: si acumulara bien, nro_viajes sería de cientos
SELECT id_ruta, MAX(nro_viajes) AS max_viajes, ROUND(AVG(nro_viajes), 1) AS prom_viajes
FROM timbradas_consolidado WHERE id_ruta IN (2, 16, 21) GROUP BY id_ruta;

-- 4. Configuración de segmentos de nuestras rutas
SELECT * FROM rutas_segmentos_config WHERE idruta IN (2, 16, 21);
SELECT idruta, COUNT(*) AS segmentos, ROUND(SUM(longitud_m)) AS metros
FROM rutas_segmentos WHERE activo GROUP BY idruta;

-- 5. ¿Los campos acumulada_* son contadores de por vida del torniquete?
--    Si acum_al_inicio es enorme frente a subidas_del_viaje, no sirven como carga a bordo.
EXPLAIN SELECT idprogramacion FROM conteo_pasajeros_destino WHERE idprogramacion = 1;  -- revisar índice primero
SELECT idprogramacion, MIN(acumulada_subida) AS acum_al_inicio, SUM(cantidad_subida) AS subidas_del_viaje
FROM conteo_pasajeros_destino
WHERE idprogramacion IN (SELECT idprogramacion FROM progvehiculos
                         WHERE idruta = 16 AND fechasalida::date = '2025-03-04' LIMIT 20)
GROUP BY idprogramacion;

-- 6. Factor de carga (carga máxima a bordo / subidas del viaje), un mes de la ruta 16.
--    Ejecutar SOLO si el EXPLAIN anterior muestra índice por idprogramacion.
WITH v AS (
  SELECT idprogramacion FROM progvehiculos
  WHERE idruta = 16 AND finalizado = 'S' AND cancelada = 'N'
    AND fechasalida >= '2025-03-01' AND fechasalida < '2025-04-01'
),
e AS (
  SELECT c.idprogramacion, c.fecha, c.id,
         COALESCE(c.cantidad_subida, 0) AS s, COALESCE(c.cantidad_bajada, 0) AS b
  FROM conteo_pasajeros_destino c JOIN v USING (idprogramacion)
),
carga AS (
  SELECT idprogramacion, s, b,
         SUM(s - b) OVER (PARTITION BY idprogramacion ORDER BY fecha, id) AS a_bordo
  FROM e
),
viaje AS (
  SELECT idprogramacion, MAX(a_bordo) AS carga_max, SUM(s) AS subidas, SUM(b) AS bajadas
  FROM carga GROUP BY idprogramacion
)
SELECT COUNT(*) AS viajes,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY carga_max::numeric / subidas) AS factor_mediano
FROM viaje
WHERE subidas > 0 AND ABS(subidas - bajadas)::numeric / subidas <= 0.2;

-- 7. Tope de flota observado: p95 de buses distintos por día laborable, 2025
SELECT idruta, percentile_cont(0.95) WITHIN GROUP (ORDER BY buses) AS flota_p95
FROM (SELECT idruta, fechasalida::date AS dia, COUNT(DISTINCT idvehiculo) AS buses
      FROM progvehiculos
      WHERE idruta IN (2, 16, 21) AND cancelada = 'N' AND fechasalida >= '2025-01-01'
        AND EXTRACT(ISODOW FROM fechasalida) <= 5
      GROUP BY 1, 2) d
GROUP BY idruta;

-- 8. Ciclo (duración mediana del viaje completo) por ruta, día laborable, 2025
SELECT idruta, percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM fechafin - fechainicio) / 60) AS ciclo_min
FROM progvehiculos
WHERE idruta IN (2, 16, 21) AND finalizado = 'S' AND cancelada = 'N' AND fechasalida >= '2025-01-01'
  AND fechafin > fechainicio AND fechafin - fechainicio < interval '8 hours'
  AND EXTRACT(ISODOW FROM fechasalida) <= 5
GROUP BY idruta;
