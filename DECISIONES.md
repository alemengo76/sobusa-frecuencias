# Decisiones técnicas y supuestos (para E02 y E12)

Fecha: 2 de noviembre de 2026. Cada decisión tiene su porqué y lo que la cambiaría.

## 1. Contrato de disponibilidad D+1

Al predecir en el instante t solo se usan resultados de viajes con salida programada antes de
las 00:00 del día de t. La especificación pide actualización incremental diaria; esa carga
nocturna es el contrato.

- Rezagos autorizados: mismo horario de ayer y de hace 7 días; promedios día-hora de 4 y 8
  semanas; nivel de 28 días cerrando ayer.
- Rezagos de 15 y 60 min: eliminados (en un viaje de ~4 h ese dato no existe al predecir).
- Prueba: `tests/unit/test_ml.py::test_features_sin_fuga_mismo_dia` pone valores absurdos en
  todo el día de t y comprueba que nada cambia.

## 2. Definición de demanda

- Etiqueta: subidas acumuladas de las programaciones con **salida programada** en [t, t+H),
  H = 30, 60 o 120 min, malla de 15 min. Población: finalizado S y cancelada N.
- Extreme agrupa sus tablas (`timbradas_anio`) por **fecha fin** (`fecha_fin_liquidacion` o
  `fechafin`). No se adopta: para despachar importa cuándo sale el bus. Se reportan métricas
  con ambas definiciones en E05.

## 3. Factor de carga (corrección de escala)

`numeropasajeros` es el total de subidas de un viaje de ida y vuelta de ~4 h. La capacidad
limita la gente a bordo al mismo tiempo. Sin corregir, la fórmula da 2 a 3 veces más buses
(ruta 16 en pico: ~49 buses/h frente a 15-30 reales, 196 vehículos frente a 84).

`buses/hora = ceil(subidas_hora × factor_carga_max / (capacidad × ocupación))`

- **Actualizado el 6 de noviembre (param_v2, migración 0003):** el 0,40 PROVISIONAL se reemplazó
  por el factor medido con el sensor (`factor_carga.csv`, reglas sensor-1.1), por ruta y tipo de
  día. `todos` = mediana exacta de la ruta (0,400 / 0,435 / 0,379); laboral, sábado y
  domingo-festivo = mediana ponderada por viajes de las medianas por hora (aproximación; la exacta
  requiere los datos del sensor). Cada franja usa el de su tipo de día (calendario del FE) y, si
  falta, el de `todos`. Lo que se usó queda en `recommendation.parametros_version`
  (p. ej. `param_v2:laboral`). El factor por hora (0,32-0,54) no se implementó: no autorizado.
- param_v1 se conserva (vigente = false) para trazabilidad y reversión (`alembic downgrade 0002`).
- Prueba: `tests/unit/test_frecuencias.py::test_sin_factor_de_carga_la_recomendacion_es_absurda`.

## 4. Capacidad

`vehiculos.capacidad = 0` en toda la flota. Prioridad (`app/calidad/capacidad.py`):
catálogo si > 0 → plazas del reporte de Extreme `rptVehiculos338.xls` → `n_sentados + n_depie`
→ `sin_dato` (no se imputa). El original no se modifica.

- Semilla por ruta (mediana de plazas del reporte): ruta 2 = 52, ruta 16 = 55, ruta 21 = 50.
- Ojo: flota bimodal (buses de ~50 y de ~72 plazas). `scripts/capacidad_flota.py` calcula la
  capacidad ponderada por viajes del segundo semestre de 2025; ese valor reemplaza la semilla.
- El reporte trae datos personales (cédulas, teléfonos): nunca se sube al repositorio.

## 5. Tope de flota

`rutas.flotadisp_prog = 62` (ruta 16) no coincide con lo observado (84 buses en ruta a la vez,
p95 laboral). Tope = p95 de buses activos por día y tipo de día (consulta 7 y
`scripts/capacidad_flota.py`). Semilla: ruta 16 = 99; rutas 2 y 21 sin tope hasta medir.

## 6. Ciclo

Ruta 16: mediana laboral 238,8 min (medida). Rutas 2 y 21: PROVISIONAL 239 hasta correr la
consulta 8. `rutas.tiempo_global = 0`, no sirve.

## 7. Sentido y segmento

`sentido = 'CICLO'` y `segmento_id = 'TOTAL'` (con CHECK en la base). Evidencia:
- `puntoini = puntofin`: cada viaje es un circuito completo.
- El detalle por segmento solo cubre enero-febrero de 2023 y tiene 32% de valores negativos.
- Los scripts de Extreme solo cargan segmentos y estacionalidades para `cootransoriente`.
- La configuración de segmentos depende de si el `idruta` es par o impar (script 99): son de
  prueba, no tramos operativos.
- El trigger asigna cada evento al segmento más cercano: no distingue ida de vuelta.

**Evolución (E12):** segmentar por las "bases" de control de Sobusa y ubicar cada evento de
torniquete avanzando a lo largo de la ruta (`ST_LineLocatePoint` con orden temporal).

## 8. Calidad y cuarentena

**Actualizado el 6 de noviembre (E02):** la ingesta ya no tiene reglas propias (`calidad_v1`,
CAL-001..004). Usa `reglas_comunes.aplicar_cuarentena` de comun-1.5, la misma función con la que
se construye el conjunto de entrenamiento (copia exacta en `app/fe31`, verificada por hash). Nada
se imputa. Cada viaje marcado va a `ai_frecuencias.data_quality_error` con lote, regla, causa y
valor, y `reglas_version = 'comun-1.5'`:

| Regla (comun-1.5) | Causa | Afecta |
|---|---|---|
| pasajeros_extremo | más de 500 pasajeros en un viaje | etiqueta |
| duracion_negativa | fin anterior al inicio | etiqueta |
| inicio_incoherente | inicio fuera de [salida − 1 día, salida + 2 días] | etiqueta |
| duracion_mayor_24h | duración mayor a 24 h (cierre tardío) | etiqueta |
| fin_tardio | fin más de 2 días después de la salida | etiqueta |
| bajadas_extremo | más de 500 bajadas | solo bajadas |
| pasajeros_sin_conteo | elegible sin conteo usable (nulo, negativo) — marca `pax_bloquea` | etiqueta |

Una franja con algún viaje elegible sin conteo usable queda sin medición (NULL), igual que la
ventana de la etiqueta del FE. Se eliminó CAL-003 (3 × plazas): comun-1.5 usa el tope fijo de 500.
Los registros de `calidad_v1` de cargas anteriores se conservan (su `reglas_version` lo dice).

### Eventos y detecciones (migración 0007, 8 de noviembre)

La ingesta trabaja por ventanas (un lote por ventana) y relee los días que Extreme modifica, así
que el mismo error quedaba registrado en varios lotes: 17.418 filas, de las que 2.342 repetían un
error ya registrado (2.324 grupos). Además, los faltantes se guardaban como el texto `'NaN'`
(pandas 3 convertía el `None` en NaN), no como NULL.

- **Evento** (`data_quality_error`): uno por `tabla_origen + idprogramacion + regla +
  reglas_version + valor`. `lote` y `creado_en` = primera detección; `ultimo_lote` y
  `ultima_deteccion` = la más reciente. Si el valor cambia y sigue incumpliendo, o cambia la
  versión de las reglas, es otro evento. Si deja de incumplir, no se registra nada nuevo.
  Si el valor vuelve a uno anterior (A → B → A), se reactiva el evento A.
- **Detección** (`data_quality_deteccion`): una por evento y lote, con la ruta, la causa y el
  valor tal como llegaron (`valor_original`). Un reintento del mismo lote no la repite. Consultar
  un lote = buscar sus detecciones.
- El valor faltante es NULL. PostgreSQL 14 no tiene `NULLS NOT DISTINCT`: el índice usa
  `(valor IS NULL), coalesce(valor, '')`, y el `ON CONFLICT` de la ingesta repite esas expresiones.
- Se retiró `UNIQUE (lote, tabla_origen, idprogramacion, regla)`: no incluye versión ni valor y
  contradecía la identidad del evento. `idprogramacion` pasó a NOT NULL (la única ruta de
  escritura ya lo garantizaba; un viaje sin identificador hace fallar la ventana, como antes).
- Nada se borró: cada fila anterior es una detección con su `id_original`. La migración comprueba
  con una huella md5 de las 10 columnas (NULL, `'NaN'` y `'nan'` distintos) que evento + detección
  reconstruyen exactamente la tabla anterior; la huella queda en el comentario de la tabla.
- Downgrade: una fila por detección. Las anteriores a la 0007 recuperan su `id`; las posteriores
  reciben ids nuevos. Se niega si la forma antigua no puede representar los datos (dos eventos con
  el mismo lote, viaje y regla). Las secuencias nunca bajan: se usa el mayor entre lo ya entregado
  y el `id` máximo, en el upgrade y en el downgrade.
- Pendiente (E12): marcar como "resuelto" un evento cuando el viaje releído deja de incumplir.

### Consulta de errores de calidad por lote (G3)

- Se conserva el endpoint `GET /api/v1/quality/errors`, bajo la ruta
  `/api/v1/quality`, sin crear una ruta alternativa `/api/v1/data-quality/errors`.
- La consulta se realiza sobre las detecciones (`data_quality_deteccion`),
  relacionadas con su evento (`data_quality_error`). Un lote se consulta a
  partir de sus detecciones.
- Se admiten los filtros `idruta`, `regla`, `tabla_origen`,
  `reglas_version` y `causa`. Los filtros son independientes y pueden
  combinarse; cuando se usan varios, todos deben cumplirse.
- **Decisión aprobada por Alejandra:** incorporar `causa` como filtro
  opcional e independiente para permitir consultar las detecciones por su causa específica.
  Se utiliza `d.causa`, correspondiente a la causa de cada detección,
  y no `e.causa`, que conserva la causa de la primera detección del evento.
- La comparación de `causa` es exacta, no parcial. El texto depende de la
  versión de las reglas y de la descripción asociada a cada regla; por tanto,
  causas de distintas versiones pueden tener textos diferentes.
- `alcance=ventana` consulta el lote exacto. `alcance=ejecucion` consulta
  solo sus ventanas: el lote seguido de `_` y al menos tres dígitos, sin
  incluir el lote exacto. `alcance=auto` combina ambos criterios. Los filtros adicionales
  se aplican al resultado de ese alcance, no forman parte de su condición OR.
- No se requiere una migración para incorporar el filtro `causa`: la columna
  ya existe en `data_quality_deteccion`.

## 9. Hallazgos en los scripts de Extreme (para comunicarles)

1. `fn_insert_timbradas_anio`, paso 10: al restar la contribución anterior no filtra por
   `idprogramacion`; cada franja de `timbradas_anio` terminaría con el último viaje procesado.
   Verificar con las consultas 1 a 3. Si se confirma, `timbradas_anio` y
   `timbradas_consolidado` no se usan como referencia.
2. Los campos `acumulada_*` parecen contadores de por vida del torniquete (consulta 5).
3. `1. tablas.sql` no es el DDL real (error de sintaxis y `UNIQUE` distinto al `ON CONFLICT`).
4. El cargue histórico de segmentos usa fecha fin; el trigger, la hora real del evento.
5. Los errores de segmentación se silencian (`EXCEPTION WHEN OTHERS`).

## 10. Línea base adicional: "método Extreme"

Promedio por tipo de día y franja calculado solo con entrenamiento (reproduce la lógica de
`timbradas_consolidado` sin su fuga ni su error). Sirve para decir cuánto mejora el modelo
frente a cómo se calcula hoy.

## 11. Marca de agua de la lectura incremental

`progvehiculos` no tiene columna de última actualización (la `ultima_actualizacion` de la
especificación existe en las tablas `timbradas_*`). La marca de agua es
`COALESCE(fecha_fin_liquidacion, fechafin)`: el momento en que el resultado del viaje quedó
disponible, la misma regla que usa `fn_insert_timbradas_anio`. En cada carga se releen además
los últimos 3 días (`DIAS_REVISION`) para capturar liquidaciones o correcciones tardías; como
cada día se recalcula completo, releer no duplica. Limitación: una corrección hecha más de 3 días
después no se detecta; pedir a Extreme si existe una columna de modificación.

## 12. Modelo de producción: gradient boosting FE 3.1 (6 de noviembre)

- Variables: las 29 de FE 3.1 (`conjunto_entrenamiento.VARIABLES`), calculadas por el mismo
  código al entrenar y al predecir (`app/fe31`). Objetivo: `y_pasajeros`. `y_abordajes` no se usa.
- `HistGradientBoostingRegressor`, pérdida Poisson, un solo modelo con el horizonte como variable,
  categóricas con códigos fijos. Número de árboles elegido con la partición de ajuste
  (ene-mar 2025), no con una muestra aleatoria.
- La API no entrena: carga el artefacto registrado (hash verificado antes de abrirlo, misma
  versión de scikit-learn) y, si no puede, predice con la línea base registrada y lo deja escrito
  en la ejecución. Ver `docs/INTEGRACION_GBM.md`.
- Los viajes se copian en `ai_frecuencias.viaje_fuente` durante la ingesta (migración 0004), para
  que el modelo calcule sus variables desde la base local y no dependa de Extreme en cada solicitud.

## 13. Alertas de frecuencia solo a 60 minutos y ALT-003 desde 2 buses/hora (7 de noviembre)

- Prueba real del 10 de diciembre de 2025: 293 alertas en un día. Causas: ALT-002, 003 y 004 se
  evaluaban en los tres horizontes (la misma situación salía tres veces) y ALT-003 saltaba con
  1 bus/hora de déficit.
- ALT-002, ALT-003 y ALT-004 se evalúan solo con las recomendaciones de 60 min
  (`HORIZONTE_ALERTAS_FRECUENCIA` en `app/alertas/catalogo.py`). Las predicciones y
  recomendaciones de 30 y 120 min se siguen calculando y guardando; ALT-001 y las demás no cambian.
- ALT-003: versión 2 del umbral con 2,0 buses/hora (migración 0005, solo datos). La versión 1
  (1,0) queda guardada con vigente = false; cada alerta guarda `umbral_version`.
- Efecto medido sobre ese día: de 293 a unas 59 alertas. Pendiente: unir franjas seguidas en
  episodios (bajaría a unas 15-25).
- La capacidad (52 / 55 / 50) ya incluye pasajeros de pie (`datos/vehiculo_capacidad.csv`:
  plazas = sentados + pie), así que las alertas de sobreocupación en pico son probablemente reales.

## 14. Reconciliación de alertas abiertas al cambiar una regla (8 de noviembre)

**Regla: todo cambio de umbral o de alcance de una alerta va acompañado de su reconciliación.**
La deduplicación solo permite una alerta abierta por situación (código, ruta, franja y horizonte),
así que una alerta vieja que la regla nueva ya no generaría quedaría abierta indefinidamente y
taparía las detecciones nuevas.

Migración 0006 (solo datos), aplicada después de la 0005:

| Alertas abiertas (base del 7 nov) | Cantidad | Qué se hizo |
|---|---|---|
| ALT-002/003/004 con horizonte 30 o 120 min | 422 | `descartada`, motivo: solo se evalúan a 60 min |
| ALT-003 a 60 min con déficit menor al umbral vigente (1 < 2) | 61 | `descartada`, motivo con el déficit y las dos versiones |
| Siguen cumpliendo la regla vigente | 137 | Sin cambios en su fila (estado, usuario, `umbral_version` = 1); solo un comentario de revalidación en el historial |

- Se usa `descartada` (no `cerrada`): `cerrada` queda para cuando alguien resolvió la situación.
- El umbral de ALT-003 se lee de la tabla `umbral` vigente; no está escrito en la migración.
- Todo queda en `alert_history` con usuario `migracion_0006` y el motivo de cada grupo.
- Reversión (`alembic downgrade 0005`): solo reabre una alerta si sigue descartada, si la fila de la
  0006 es la última de su historial (nadie la tocó después) y si su situación no tiene ya otra
  alerta abierta. Vuelve a su estado anterior (por ejemplo `reconocida`, con su usuario). Las demás
  se dejan y se informan. La reversión se registra como historial nuevo; no se borra nada.
- La migración de sentido IDA/VUELTA, si se autoriza, pasa a ser la 0009 (la 0007 es la de eventos de calidad, §8; la 0008, la de trazabilidad de predicciones, §15).

## 15. Trazabilidad entre predicciones y recomendaciones (P1, migración 0008, 12 de noviembre)

Estado (14 de noviembre): migración 0008, huella, contrato, repositorio y servicios implementados. La integración P1 superó las pruebas unitarias, de API y de integración en una base PostgreSQL desechable. Se verificaron la trazabilidad de predicciones y recomendaciones, la reutilización por huella, la atomicidad y el rechazo seguro del downgrade cuando existen colisiones con la clave antigua.

**La migración 0008 todavía NO está aplicada a la base local**, que permanece en `0007`. No iniciar la aplicación ni ejecutar migraciones sobre esa base hasta contar con autorización explícita. Tampoco realizar commit ni push sin autorización.

Problema (auditoría del 10 de noviembre, confirmado en la base): 3.729 de 4.236 recomendaciones sin
`prediction_id` y dos ejecuciones de predicción "exitosas" que declaraban 507 y 585 filas sin tener
ninguna. Causas: (A1) la clave única de `prediction` no incluía la ejecución y `data_cutoff` es siempre
el día a las 00:00, así que repetir el mismo día con el mismo modelo chocaba; `ON CONFLICT DO NOTHING`
descartaba las filas y la función devolvía las filas *enviadas*. Además, si el histórico se había
corregido, las predicciones nuevas no se podían guardar. (A2) `ejecutar_frecuencias` recomendaba sobre
las predicciones en memoria, que no tienen `prediction_id`. La corrección del 3 de noviembre solo
cubría `/recommendations/calculate`.

- **Cada ejecución guarda sus predicciones**: clave única `(execution_id, idruta, sentido,
  segmento_id, franja_inicio, horizonte_min)`. `franja_minutos` no entra en la clave: es siempre 15
  (regla `CHECK (franja_minutos = 15)` en la base y en el contrato).
- **Sin duplicar lo idéntico**: la huella `huella-pred/1` (SHA-256, `app/ml/contrato.py`) resume el
  contenido que se guarda (ruta, sentido, segmento, franja, duración, horizonte, demanda, límites,
  nivel, definición de tiempo, modelo, versión de modelo y de variables, corte de datos) más la
  solicitud (día, rutas, horizontes). Se calcula sobre los mismos valores que se insertan: decimales
  redondeados a 2 (ROUND_HALF_UP, como `numeric(10,2)`), fechas en hora local de Bogotá sin zona;
  nulos e infinitos son error. Si una ejecución obtiene una huella ya guardada, reutiliza las filas
  de la dueña (`execution_log.resumen.reutiliza`). Si el histórico cambió y eso cambia algún valor,
  la huella cambia y se guardan filas nuevas: nunca se reutiliza una predicción obsoleta.
- **Un solo dueño aunque lleguen dos solicitudes a la vez**: `prediction_set (huella PK,
  execution_id UNIQUE)`. Reserva, predicciones y cierre van en la MISMA transacción; si algo falla,
  el rollback quita también la reserva y la falla se registra en una transacción aparte. Con
  `READ COMMITTED` (el predeterminado de PostgreSQL), la segunda solicitud espera a la primera: si la
  primera confirma, reutiliza; si se revierte, pasa a ser la dueña. `lock_timeout` de 30 s.
- **Recomendaciones nuevas siempre con predicción**: `CHECK (prediction_id IS NOT NULL) NOT VALID`.
  Las 3.729 anteriores se conservan sin cambios (no se cruzan por ruta y franja: sería inventar el
  vínculo). Consecuencia: un `UPDATE` sobre una de ellas falla si no se le asigna `prediction_id`;
  hoy el código no actualiza recomendaciones. No ejecutar `VALIDATE CONSTRAINT`.
- **Dos identificadores en las respuestas** (`POST /predictions/run`, y el `resumen` de
  `GET /frequency/executions/{id}`): `execution_id` identifica la SOLICITUD (cada llamada crea la
  suya, con su estado, parámetros y correlation-id); `execution_id_predicciones` identifica la
  ejecución DUEÑA de las filas de `prediction`. Son iguales cuando la solicitud guardó predicciones
  nuevas; distintos cuando reutilizó (entonces `resumen.reutiliza` también lo dice y
  `predicciones_guardadas` es 0). `GET /predictions?execution_id=` filtra literalmente por la columna
  `prediction.execution_id`: con el id de una solicitud que reutilizó devuelve una página vacía
  (`total` 0, código 200), no un error. Para ver sus predicciones: leer
  `execution_id_predicciones` y consultar `GET /predictions?execution_id=<ese id>`.
  `POST /recommendations/calculate` sí acepta cualquiera de los dos (resuelve la dueña).
  Las recomendaciones pertenecen a la ejecución que las calculó (`GET /recommendations?execution_id=`
  de la ejecución de frecuencias o de recomendación) y cada una apunta por `prediction_id` a una fila
  de la dueña; antes de guardarlas se comprueba que hay exactamente una por predicción y que ruta,
  sentido, segmento, franja y horizonte coinciden con los de su predicción.
- Downgrade: vuelve a la clave antigua y se niega si dos ejecuciones guardaron predicciones con la
  misma (ruta, sentido, segmento, franja, horizonte, modelo, corte). Se pierde `prediction_set`.
- Las pruebas que confirman transacciones (`tests/integracion/test_trazabilidad.py`) solo corren en
  una base desechable (`SOBUSA_BD_DESECHABLE=1` y nombre `sobusa_desechable*`).

### Verificaciones posteriores (14 de noviembre)

- **Registro seguro de fallos:** `ejecutar_frecuencias` utiliza `_registrar_fallo` para registrar errores en una transacción independiente. Si el registro del fallo también falla, no debe ocultar la excepción original.
- **Lectura completa de predicciones:** `predicciones_de_ejecucion` compara la cantidad de filas recuperadas con el total informado por el repositorio. Si no coinciden, lanza `RuntimeError` y no continúa con una lectura incompleta. Prueba específica: `test_predicciones_de_ejecucion_rechaza_lectura_incompleta`.
- **Pruebas unitarias y API:** 202 pruebas unitarias/API aprobadas, 0 fallidas y 0 omitidas, ejecutadas el 14 de noviembre de 2026 en el entorno del proyecto. Se generaron 1.238 advertencias, principalmente de compatibilidad con Pandas 4. Permanecen advertencias de compatibilidad futura de Pandas y Starlette.
- **Pruebas de integración PostgreSQL:** 39 aprobadas en base desechable. E7 verificó 260 recomendaciones nuevas, ninguna sin `prediction_id` y ninguna con enlaces inconsistentes. E8 verificó que el downgrade se rechaza cuando la clave antigua produciría colisiones, conservando la base en `0008`.
- **Precaución para pruebas de integración:** test_reproduce_a1 requiere una base de datos desechable recién restaurada. No debe repetirse sobre la misma base sin restaurarla previamente, porque los datos residuales pueden alterar el resultado.

## 16. Alcance funcional de ALT-009: datos incompletos y retrasos de ingesta

**Estado: decisión aprobada por el equipo el 22 de noviembre de 2026.**

### Decisión

ALT-009 cubrirá dos condiciones de incumplimiento relacionadas, pero diferentes:

1. **Incompletitud de los datos:** existen viajes elegibles observados cuya información obligatoria de pasajeros no tiene un conteo utilizable.
2. **Retraso de ingesta:** la información correspondiente a un ciclo de actualización esperado no se recibe correctamente dentro del plazo establecido.

Las dos condiciones forman parte de ALT-009 porque la especificación técnica v1.5, §5.2, agrupa la falta de información obligatoria y su retraso bajo la misma alerta.

No se eliminará la lógica existente de detección de conteos incompletos para implementar la detección de retrasos. Ambas condiciones deberán coexistir y conservar su significado propio.

### 1. Condición de incompletitud

Se conserva la lógica existente de cobertura de conteos de pasajeros sobre los viajes elegibles observados.

La cobertura se calcula como:

`1 - (viajes elegibles sin conteo usable / viajes elegibles observados)`

Se considera que un conteo no es utilizable cuando es nulo o negativo, de acuerdo con las reglas de calidad vigentes.

Esta condición permite detectar problemas en los datos de viajes que ya fueron recibidos. No demuestra que se haya recibido el universo completo de información esperado.

Por tanto, la cobertura de los viajes observados no se utilizará como evidencia de que la fuente entregó todos los registros que debía entregar.

### 2. Condición de retraso de ingesta

Se incorporará una detección independiente de retrasos en la actualización de datos.

El ciclo esperado es diario y se evalúa por fuente y alcance. Se genera un incumplimiento de retraso tras dos ciclos diarios consecutivos vencidos sin una ingesta completa y exitosa dentro del plazo de cada ciclo.

Un ciclo se considerará satisfecho únicamente cuando exista evidencia de una ingesta completa y exitosa para el alcance evaluado.

Se distinguirán, como mínimo, los siguientes resultados:

- Ingesta exitosa y completa.
- Ingesta fallida.
- Ingesta parcial.
- Ingesta no iniciada.
- Ingesta exitosa sin novedades, siempre que exista evidencia de consulta válida del alcance.

Las ingestas parciales o fallidas dejan el ciclo sin satisfacer. La ausencia de datos nuevos es inconcluyente por sí sola. Para contar una ingesta sin novedades como exitosa se requiere evidencia de una consulta válida que cubra el alcance del ciclo.

La hora de vencimiento será configurable y queda pendiente de validación con Extreme o con el responsable operativo. La apertura y la tolerancia también quedan pendientes de validación operativa.

### 3. Separación de condiciones y reconciliación

Aunque ambas condiciones pertenezcan a ALT-009, deberán identificarse internamente de forma diferenciada.

La detección, persistencia, consulta y recuperación de una condición no deberá cerrar, descartar ni ocultar automáticamente la otra.

La deduplicación y la identidad de las situaciones deberán permitir que ambas condiciones coexistan cuando corresponda.

Cualquier cambio en el alcance o comportamiento de ALT-009 deberá acompañarse de una estrategia de reconciliación de las alertas abiertas, de acuerdo con la regla establecida en la sección 14 de este documento.

### 4. Persistencia y trazabilidad

La condición de incompletitud deberá conservar la evidencia de los viajes observados y sus conteos no utilizables, conforme al mecanismo de calidad existente.

La condición de retraso deberá contar con trazabilidad de los ciclos esperados y de los intentos de ingesta asociados. Como mínimo, deberá poder determinarse:

- Fuente y alcance evaluados.
- Identificador del ciclo y sus instantes de apertura y vencimiento.
- Política de calendario aplicada.
- Intentos de ingesta y sus resultados.
- Evidencia de finalización completa o parcial.
- Momento de recuperación, si ocurre después del vencimiento.

Una ingesta completa y exitosa posterior para la misma fuente y alcance recupera la alerta de retraso. Se registra el momento de recuperación y se conserva el incumplimiento histórico con el resultado de cada ciclo según su plazo original.

### 5. Severidad

La severidad funcional de ALT-009 es **Crítica**, según la especificación técnica v1.5. La equivalencia técnica propuesta para la persistencia es `alta`, el valor que admite el esquema actual. Su uso queda pendiente de aprobación del equipo.

Una vez aprobada la equivalencia, la implementación deberá alinear el catálogo, la persistencia y las validaciones, y verificar con pruebas la clasificación funcional y el valor persistido.

### 6. Alcance y límites

Esta decisión no certifica que se haya recibido el universo completo de programaciones esperado. La detección de retrasos acredita el cumplimiento o incumplimiento de ciclos de actualización definidos, no la completitud absoluta de la fuente.

El 26 de noviembre de 2026, Alejandra aprobó el ajuste de incompletitud de ALT-009 en `app/servicios.py` y la incorporación de `app/ciclo_alertas.py` para el ciclo automático de alertas. El programador, los horarios, la migración `0010`, las notificaciones y la autorización de reintentos siguen pendientes de activación o aprobación operativa, según corresponda.

### 7. Criterios para considerar ALT-009 implementada

La implementación deberá demostrar, mediante pruebas, al menos:

- Detección de conteos incompletos sin confundirla con retrasos de ingesta.
- Detección de dos ciclos consecutivos vencidos.
- Diferenciación entre ingesta fallida, parcial, no iniciada y exitosa sin novedades.
- Ausencia de alertas falsas cuando no hay viajes nuevos, pero sí una consulta válida.
- Coexistencia de las dos condiciones sin cierres cruzados.
- Deduplicación de episodios y persistencia de su historial.
- Recuperación tardía sin borrar el incumplimiento anterior.
- Comportamiento correcto ante ejecuciones concurrentes del proceso de ingesta y del verificador.

La implementación solo podrá considerarse terminada cuando se verifique el flujo completo de detección, persistencia, consulta, deduplicación y recuperación.
