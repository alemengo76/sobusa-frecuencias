-- ALT-003 (frecuencia insuficiente): umbral de 1,0 a 2,0 buses/hora de déficit, como VERSIÓN NUEVA.
-- Con 1 bus/hora la alerta saltaba casi en cualquier franja (el 10 de diciembre de 2025, 65 de las
-- franjas de 60 min); con 2 buses/hora quedan 32. La versión anterior se conserva (vigente = false).
-- Solo datos: no cambia ninguna tabla. El catálogo del código (app/alertas/catalogo.py) no se toca:
-- su valor solo es la semilla y el respaldo cuando la base no tiene umbral vigente.
SET search_path TO ai_frecuencias;

INSERT INTO umbral (codigo, parametro, valor, version, vigente, creado_por)
SELECT 'ALT-003', 'deficit_buses_hora', 2.0, max(version) + 1, false, 'migracion_0005'
FROM umbral WHERE codigo = 'ALT-003';

UPDATE umbral SET vigente = false
WHERE codigo = 'ALT-003' AND vigente AND creado_por <> 'migracion_0005';

UPDATE umbral SET vigente = true
WHERE codigo = 'ALT-003' AND creado_por = 'migracion_0005';

DO $$
BEGIN
    IF (SELECT count(*) FROM umbral WHERE codigo = 'ALT-003' AND vigente AND valor = 2.0) <> 1 THEN
        RAISE EXCEPTION 'ALT-003: se esperaba exactamente una versión vigente con valor 2.0';
    END IF;
END $$;
