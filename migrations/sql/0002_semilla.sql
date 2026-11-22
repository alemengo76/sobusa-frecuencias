-- E04: datos semilla. Valores marcados PROVISIONAL deben reemplazarse con una nueva versión.
SET search_path TO ai_frecuencias;

INSERT INTO alert_catalog (codigo, nombre, severidad_base, recomendacion, evaluable, nota) VALUES
    ('ALT-001', 'Alta demanda prevista', 'media', 'Preparar buses de refuerzo para la franja indicada.', true, NULL),
    ('ALT-002', 'Sobreocupación prevista (>= 90%)', 'alta', 'Aumentar la frecuencia a la recomendada o despachar refuerzo.', true, NULL),
    ('ALT-003', 'Frecuencia insuficiente', 'media', 'Ajustar la programación a los buses/hora recomendados.', true, NULL),
    ('ALT-004', 'Frecuencia excesiva (< 30%)', 'baja', 'Evaluar reducir salidas en la franja para ahorrar flota.', true, NULL),
    ('ALT-005', 'Caída anormal de demanda', 'media', 'Verificar novedades viales o fallas de conteo en la ruta.', true, NULL),
    ('ALT-006', 'Despacho ausente o tardío', 'alta', 'Contactar al despachador y reprogramar la salida.', true, 'Requiere fechainicio real de progvehiculos'),
    ('ALT-007', 'Desviación del pronóstico', 'media', 'Revisar la franja; si se repite, evaluar reentrenamiento.', true, NULL),
    ('ALT-008', 'Consolidado anómalo', 'media', 'Comparar el consolidado con los viajes y reportar a Extreme.', true, 'Compara timbradas_consolidado contra los viajes; ver hallazgo del paso 10 de fn_insert_timbradas_anio'),
    ('ALT-009', 'Datos incompletos', 'media', 'Revisar la carga del día y la cuarentena antes de usar los datos.', true, NULL),
    ('ALT-010', 'Modelo degradado', 'alta', 'Entrenar un candidato y compararlo contra el modelo activo.', true, NULL);

INSERT INTO umbral (codigo, parametro, valor, version, creado_por) VALUES
    ('ALT-001', 'factor_sobre_referencia', 1.3, 1, 'semilla'),
    ('ALT-002', 'ocupacion_maxima', 0.9, 1, 'semilla'),
    ('ALT-003', 'deficit_buses_hora', 1.0, 1, 'semilla'),
    ('ALT-004', 'ocupacion_minima', 0.3, 1, 'semilla'),
    ('ALT-005', 'caida_relativa', 0.4, 1, 'semilla'),
    ('ALT-006', 'retraso_maximo_min', 10.0, 1, 'semilla'),
    ('ALT-007', 'error_relativo', 0.35, 1, 'semilla'),
    ('ALT-008', 'diferencia_relativa', 0.2, 1, 'semilla'),
    ('ALT-009', 'cobertura_minima', 0.9, 1, 'semilla'),
    ('ALT-010', 'degradacion_wape', 0.2, 1, 'semilla');

-- Semilla SINTÉTICA de demostración. Valores no operativos.
INSERT INTO parametro_operativo (idruta, tipo_dia, capacidad_nominal, capacidad_fuente, ocupacion_objetivo,
                                 ciclo_min, factor_carga_max, factor_carga_fuente, flota_tope, version) VALUES
    (2, 'todos', 40, 'fixture_sintetico', 0.70, 120, 0.30, 'fixture_sintetico', NULL, 'param_v1'),
    (16, 'todos', 42, 'fixture_sintetico', 0.70, 130, 0.30, 'fixture_sintetico', NULL, 'param_v1'),
    (21, 'todos', 44, 'fixture_sintetico', 0.70, 140, 0.30, 'fixture_sintetico', NULL, 'param_v1');

-- Modelo base registrado para que existan predicciones desde el primer día
INSERT INTO model_registry (nombre, version, tipo, estado, semilla, hiperparametros, version_datos,
                            feature_version, activado_en, activado_por) VALUES
    ('linea_base_dia_hora', 'baseline_dh_v1', 'baseline', 'activo', 0, '{"semanas": 4}',
     'fixture_sintetico', 'sobusa-fe-2.1-d1', now(), 'semilla');
