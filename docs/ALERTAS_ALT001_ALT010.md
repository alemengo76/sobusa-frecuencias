# Alertas ALT-001 a ALT-010: problema, datos, regla, evidencia y estado real

Base: ZIP `sobusa-frecuencias-limpio` (26-nov-2026) más los cambios locales de esta entrega (v4: entrega operativa). Validado con Python 3.11 y PostgreSQL 16; PostgreSQL 14 queda pendiente. Este documento está escrito para alguien que conoce la operación de Sobusa, pero no el código.

## 1. Cómo leer este documento

**Cuatro estados posibles.** Cada alerta quedó en uno de estos:

| Estado | Qué significa |
| --- | --- |
| **Funciona con los datos disponibles** | Con lo que ya existe en nuestra base y en las tablas de Extreme, una entrada real la calcula, la guarda con historial, evita duplicados y se puede consultar en `GET /api/v1/alerts`. |
| **Conectada, falta una autorización** | El recorrido está conectado y probado de punta a punta, pero en operación responde `no_evaluable` hasta que se apruebe una configuración, una fuente o un cambio de esquema. |
| **Demostrada solo con datos sintéticos** | Funciona en el entorno aislado con datos sintéticos; con datos reales depende de algo que no tenemos. |
| **Pendiente** | Falta una fuente o un criterio para que la regla pueda alertar en operación. |

**Ejecución automática.** El programador (`python -m app.programador`, sección 9) dispara el ciclo diario y el verificador de retraso a horas configuradas. Está implementado y demostrado con un proceso real en un entorno aislado, pero **no está instalado** en ningún entorno de trabajo. Su configuración lista para activar está en `deploy/programador/`, y las horas reales dependen de la política de ALT-009, que todavía no está validada.

**Lo que comparten todas las alertas:**

- **Umbral.** Cada alerta lee su umbral de `ai_frecuencias.umbral`, en la versión vigente, y lo guarda en `umbral_version`.
- **Deduplicación.** Cada alerta tiene una `clave_dedup`. El índice único parcial `uq_alert_dedup_abierta` impide que existan dos alertas abiertas, reconocidas o asignadas con la misma clave. Antes de insertar, además, se descartan las candidatas cuya clave ya está abierta.
- **Historial.** Al crearse, cada alerta recibe su fila `creada` en `alert_history`. Los cambios de estado y los comentarios quedan en la misma bitácora.
- **Resolución.** Se hace con `PATCH /api/v1/alerts/{id}/status` (función `servicios.cambiar_estado_alerta`). Permite reconocer, asignar, comentar, cerrar o descartar, y la respuesta trae el historial.
- **Notificaciones.** Se envían por correo y WhatsApp, de forma simulada, y quedan en `notification_log`. Solo salen para alertas que quedaron guardadas y con severidad `alta`. **ALT-009 nunca notifica**, en ninguna de sus dos condiciones.
- **Severidad.** El esquema solo admite `baja`, `media` y `alta`. La equivalencia propuesta es Crítica = `alta`, pero **está pendiente de aprobación del equipo** (DECISIONES §16.5). No se cambió el catálogo, ni los datos, ni el esquema.

**Los ejemplos** de cada alerta salen de las pruebas citadas. Todos usan datos sintéticos.

---

## 2. ALT-001 a ALT-004: alertas del cálculo de frecuencias

Las reglas de estas cuatro alertas no se tocaron en esta entrega. Lo nuevo es que el programador puede dispararlas cada día con el trabajo opcional `PROGRAMADOR_FRECUENCIAS_HORAS`, que calcula el día siguiente.

**Punto de entrada.** `POST /api/v1/frequency/executions`. En el código, `servicios.ejecutar_frecuencias` llama a `servicios.evaluar_alertas`, que corre en la misma transacción de la predicción y la recomendación.

| | ALT-001 Alta demanda prevista | ALT-002 Sobreocupación | ALT-003 Frecuencia insuficiente | ALT-004 Frecuencia excesiva |
| --- | --- | --- | --- | --- |
| Problema operativo | La franja va a pedir mucho más que lo habitual | Con los buses que salen hoy, el bus va lleno | Hacen falta despachos | Los buses van casi vacíos |
| Decisión que apoya | Preparar refuerzo | Subir frecuencia | Programar salidas | Liberar flota |
| Datos | Predicción guardada y referencia día-hora de 8 semanas (`demanda_franja`) | Recomendación a 60 min | Recomendación a 60 min | Recomendación a 60 min |
| Regla y umbral | `pred > ref × 1,3` (v1) | ocupación ≥ 0,90 (v1) | viable y recomendado − actual ≥ 2 buses/h (v2) | ocupación < 0,30 (v1) |
| Por qué esta lógica | Compara con el patrón del mismo día y hora | 90 % es el límite de servicio que fija la especificación | Una diferencia de un solo bus generaba demasiado ruido (DECISIONES §13) | 30 % indica flota ociosa |
| Severidad | media; alta si la puntuación ≥ 1 | alta | media | baja |
| Clave de deduplicación | código + ruta + franja + horizonte | igual | igual | igual |

**Límite conocido de ALT-001.** La referencia solo existe para las franjas que ya tienen fila en `demanda_franja`.

**Respaldo:**

- Reglas: `motor.alt_001` y `motor.alt_002_003_004`.
- Pruebas unitarias en `tests/unit/test_alertas.py`.
- Regresión contra PostgreSQL: `tests/integracion/test_alt009_postgres.py::test_error_sql_revierte_alt009_y_conserva_alt001_a_alt004`, que verifica que las cuatro quedan guardadas con su historial aunque falle ALT-009.

---

## 3. ALT-009, condición «incompletitud»: conectada y verificada

Esta condición ya venía en el ZIP nuevo y no se modificó.

- **Problema operativo.** En un día ya consolidado, algunos viajes elegibles de la ruta no tienen un conteo de pasajeros utilizable.
- **Decisión que apoya.** No usar la demanda de ese día sin revisarla antes.
- **Datos.**
  - Los viajes de `viaje_fuente` de las rutas de la ejecución.
  - Las detecciones de calidad `pasajeros_sin_conteo`. La consulta de evidencia se limita a los viajes evaluados (`idprogramacion = ANY(:ids)`).
- **Punto de entrada.** El mismo de ALT-001 a ALT-004 (`servicios._evaluar_incompletitud`), dentro de un **SAVEPOINT**:
  - Si la evaluación falla, se revierten solo su alerta y su historial; ALT-001 a ALT-004 se conservan.
  - Si lo que falla es abrir o cerrar el savepoint, el error se propaga.
- **Regla.**
  - Se evalúa el último día completo antes del corte (marca de agua − 3 días).
  - `cobertura = 1 − sin conteo / elegibles`.
  - La alerta se genera si `cobertura < cobertura_minima`, que vale 0,90 en la versión 1.
- **Por qué esta lógica.** Mide la calidad de los viajes que sí llegaron. No certifica que haya llegado el universo completo (DECISIONES §16.1).
- **Evidencia.**
  - La cobertura y el umbral.
  - Los ids de los viajes sin conteo, con su lote.
- **Deduplicación.** La clave es `motor.clave_dedup("ALT-009", ruta, día)`, que es distinta de la de retraso. Si se cierra y la condición persiste, **reaparece** como una alerta nueva. Ese es el comportamiento vigente y se conserva.
- **Ejemplos.**
  - Activa: 1 de 2 viajes sin conteo da cobertura 0,5, por debajo de 0,8.
  - No activa: el día está sin consolidar, o no hay marca de agua.
- **Notificaciones.** No notifica.
- **Respaldo:**
  - `tests/integracion/test_alt009_postgres.py`: las 2 pruebas priorizadas.
  - `tests/unit/test_servicio_alt009.py`: 6 pruebas.
  - `tests/integracion/test_alertas_conectadas_postgres.py::test_retraso_e_incompletitud_coexisten_y_no_se_cierran_entre_si`.

---

## 4. ALT-009, condición «retraso de ingesta»: conectada, falta la política horaria

Esta condición quedó corregida y conectada. Solo crea alertas cuando existe una política operativa configurada.

- **Problema operativo.** La carga diaria no llegó completa y a tiempo durante dos ciclos seguidos. Con eso, las predicciones se calcularon con datos atrasados.
- **Decisión que apoya.** Escalar con el responsable de la carga. Un reintento manual queda registrado, pero hoy no cuenta (ver la regla 1).
- **Datos.** Solo los intentos de ingesta registrados en `execution_log` (tipo `ingesta`) con su fuente y su alcance. Las filas antiguas que no traen `fuente` no se usan.

### Puntos de entrada

| Comando | Qué hace |
| --- | --- |
| `python -m app.ciclo_alertas` | `ciclo_alertas.correr()`: registra el intento de ingesta antes de empezar, ingiere, cierra el intento con su evidencia y luego verifica el retraso. |
| `python -m app.ciclo_alertas --reintento-de AAAA-MM-DD --autorizado-por "nombre"` | Reintento manual asociado a su ciclo original. El nombre se guarda como `autorizacion_declarada_por`: es la **declaración** del operador, no una autorización verificada. |
| `python -m app.ciclo_alertas --solo-verificar-retraso` | `ciclo_alertas.verificar_retraso()`: verifica sin ingerir, así que corre aunque la ingesta nunca haya arrancado. |

### Identidad de un ciclo

Un ciclo se identifica como `<fuente>|ruta:<idruta>|<fecha operativa>`. El alcance es la ruta, de modo que una ingesta de las rutas 2, 16 y 21 cuenta como evidencia para los tres alcances.

**Política.** Se configura en hora de Bogotá con cinco variables de entorno, ninguna con valor por defecto:

- `ALT009_RETRASO_APERTURA`
- `ALT009_RETRASO_VENCIMIENTO`
- `ALT009_RETRASO_TOLERANCIA_MIN`
- `ALT009_RETRASO_POLITICA_VERSION`
- `ALT009_RETRASO_INICIO_MONITOREO`

Si falta cualquiera, el resultado es `no_evaluable / politica_operativa_no_configurada`. El ciclo del día d abre en `d + apertura` y vence en `d + vencimiento + tolerancia`. Las pruebas usan 00:00 y 05:00, que son **solo valores de prueba**.

### Regla `alt009-retraso-v1`

1. **Cada intento se vincula con un solo ciclo.**
   - Por su hora de **inicio**: pertenece al ciclo d si empezó entre la apertura de d y la apertura de d+1.
   - Un reintento manual con `ciclo_objetivo` va a ese ciclo **solo si un mecanismo aprobado verifica su autorización** (`verificar_autorizacion`). Ese mecanismo no existe todavía: quién autoriza y cómo se verifica queda pendiente. Por eso, en operación, ningún reintento manual cuenta. El intento y la declaración se conservan para auditoría en `intentos_no_usados`, con el motivo `autorizacion_declarada_no_verificada` o `reintento_sin_autorizacion`, y no satisfacen ni recuperan ningún ciclo.
   - Las pruebas demuestran, con un verificador **sintético**, cómo se comportaría un reintento verificado: cuenta para su ciclo original y recupera el episodio.
2. **Cuándo se cumple un ciclo.** Un ciclo está *cumplido* si un intento suyo terminó `exitosa`, con `completa` y `consulta_valida`, a más tardar en el vencimiento.
   - Una ingesta sin viajes nuevos cuenta si acreditó una consulta válida: recorrió al menos una ventana hasta el máximo de la fuente.
   - Si la fuente no devolvió filas, no se acredita.
3. **Ciclos que no se cumplen.** Un ciclo es *pendiente* si todavía no vence. Si ya venció sin cumplirse es *vencido*, con una de estas causas: `no_iniciada`, `fallida`, `parcial`, `sin_consulta_valida`, `en_curso` o `tardia`.
4. **Cuándo hay alerta.** Hay incumplimiento con **2 ciclos vencidos consecutivos** (DECISIONES §16.2). Se crea **una alerta por episodio** y por ruta.

**Por qué esta lógica.** Vincular cada intento por su inicio evita el error de la versión anterior. Ahí, una ingesta que terminaba tarde satisfacía también el ciclo siguiente.

### Identidad del episodio y duplicados

- **Clave.** Es `ALT-009|retraso|<fuente>|ruta:<r>|<fecha del primer ciclo vencido>`.
- **Si el episodio crece.** La clave no cambia. El crecimiento se anota en el historial (`[alt009-retraso] ciclos_vencidos=N`) sin modificar la alerta.
- **Si cambia el rango que consulta el verificador.** Cuando la racha empieza en el primer ciclo observado, se reutiliza la clave de un episodio previo de la misma ruta que todavía no se haya recuperado.
- **Episodio cerrado o descartado.** No se vuelve a crear al observar de nuevo la misma racha.
- **Racha nueva.** Una recuperación termina el episodio, aunque no haya un ciclo cumplido después. El primer vencimiento posterior, por sí solo, no abre nada; si vence otro ciclo consecutivo, es un episodio nuevo con su propia alerta. La alerta anterior no se recrea.
- **Concurrencia.** El verificador toma un bloqueo consultivo por fuente (`pg_advisory_xact_lock`). Por eso dos verificaciones simultáneas no duplican alertas ni notas.

### Recuperación y evidencia

- **Recuperación.** La primera ingesta satisfactoria y utilizable de la misma fuente y alcance que termina después del último vencimiento de la racha recupera el episodio. Puede ser una ingesta tardía del último ciclo vencido; los reintentos sin autorización verificada no recuperan.
  - Se anota en el historial: `[alt009-retraso] recuperado en <momento> por la ingesta <execution_id>`.
  - El estado de la alerta no cambia: **una alerta cerrada no se reabre**.
  - Se conservan los vencimientos originales.
  - Nunca cierra ni descarta la alerta de incompletitud.
- **Evidencia en la alerta.**
  - `franja_inicio`: el vencimiento del primer ciclo.
  - `valor_observado`: los ciclos vencidos al crearse.
  - `umbral`: 2.
  - Texto con cada ciclo (id, vencimiento, causa e intentos), la versión de la política y la aclaración de que no atribuye la causa a la fuente ni certifica la completitud.
- **Evidencia en la ejecución `alertas` (campo `resumen`).**
  - Cada ciclo con su apertura, vencimiento, límite y estado.
  - Los intentos de cada ciclo y su resultado.
  - La recuperación y los intentos no usados.
- **Severidad.**
  - Funcional: Crítica.
  - Persistida: `alta`, con la equivalencia pendiente de aprobación.
  - **Sin notificaciones.**

### Ejemplos

**Activa la alerta:**

1. Día 1 cumplido; día 2 con ingesta fallida; día 3 sin ninguna ingesta. Resultado: una alerta y, cuando el día 4 se cumple, una nota de recuperación.
2. De punta a punta (`test_recuperacion_tardia_y_episodio_nuevo_de_punta_a_punta`): vencen 1 y 2 y se abre la alerta A. Una ingesta tardía (2-nov 10:00) la recupera y deja la nota. El 3 vence solo y no abre nada. El 4 vence y se abre la alerta B por el episodio nuevo. A no se recrea, la alerta de incompletitud no cambia, y repetir la verificación no duplica alertas ni notas.
3. Ingesta que empieza a las 04:50 del día 2 y termina a las 05:30 (`test_ingesta_tardia_no_satisface_el_ciclo_siguiente`). El día 2 queda `tardia` y recuperado; el día 3 sigue vencido (`no_iniciada`), porque la hora de fin de esa ingesta no lo satisface. El 3, solo, no abre alerta; cuando también vence el 4, sí.

**No activa la alerta:** un ciclo parcial y otro sin consulta válida, separados por ciclos cumplidos; el primer vencimiento después de una recuperación; un reintento con la autorización solo declarada. Un día sin viajes, pero con consulta válida, queda cumplido.

### Respaldo

- Reglas: `app/alertas/retraso_ingesta.py`.
- Persistencia: `app/alertas/flujo.py::verificar_retraso`.
- Entradas: `app/ciclo_alertas.py` (el ciclo G4 de `app/ciclo.py` se conserva aparte, para rangos manuales).
- Pruebas unitarias:
  - `tests/unit/test_retraso_ingesta.py`: 26 pruebas.
  - `tests/unit/test_ciclo_alertas.py`.
- Pruebas contra PostgreSQL con rollback (`test_alertas_conectadas_postgres.py`): `test_retraso_*`, `test_ingesta_tardia_no_satisface_el_ciclo_siguiente`, `test_reintento_declarado_*`, `test_episodio_cerrado_*`, `test_la_identidad_*`, `test_sin_politica_*`, la de coexistencia y `test_recuperacion_tardia_y_episodio_nuevo_de_punta_a_punta`.
- Pruebas contra la instancia exclusiva (`test_alertas_ciclo_postgres.py`): ciclo con fallo, ciclo ejecutado dos veces, `GET /alerts` y `PATCH` por HTTP, dos verificadores concurrentes, verificador durante una ingesta en curso, y dos pruebas de la limpieza (una ejecución ajena de la misma ventana sobrevive; la limpieza corre aunque falle una aserción).

---

## 5. ALT-005, ALT-007 y ALT-010: conectadas, falta aprobar la tabla de completitud

**Punto de entrada.** El ciclo diario (`ciclo_alertas.correr`, que el programador dispara) llama a `flujo.alertas_tras_ingesta`. Cada evaluación corre en su propio SAVEPOINT, en este orden:

1. completitud (`flujo.evaluar_completitud`);
2. `flujo.evaluar_alt_005_007`, que usa `desde_m1.evaluar_tras_ingesta` (solo lectura);
3. `flujo.evaluar_alt_010`, que usa `alt010_desde_m1.simular` (solo lectura).

### Evidencia de completitud: no existía; ahora hay mecanismo y propuesta de tabla

Busqué un procedimiento del equipo para obtener y cargar esta evidencia en el repositorio, en DECISIONES, en los documentos del proyecto y en la copia de GitHub de tu equipo. **No existe.** DECISIONES solo menciona un «procedimiento privado» sin describirlo, y ninguna migración crea la tabla. Por eso entrego tres piezas:

- **El mecanismo:** `app/calidad/completitud.py` (regla `completitud-v2`). Calcula el estado de cada ruta y día de los últimos 35 días consolidados, que cubren la ventana de ALT-010. Para marcar un día **completo** exige cuatro condiciones:
  1. el día está consolidado;
  2. la última ingesta registrada terminó exitosa, completa y con consulta válida;
  3. **la referencia externa:** los identificadores de programación de esa ruta y día (`progvehiculos`, consultado en solo lectura mediante `fuentes_extreme.ProgramacionesPorDia`) son únicos y coinciden con los de `viaje_fuente`;
  4. la cobertura de conteos de los viajes elegibles es mayor o igual al umbral vigente de ALT-009 (0,90).

  Con 1 a 3 pero sin 4, o con copia incompleta, el día queda `parcial`. Sin consolidar, sin una ingesta válida o sin identificadores de referencia válidos, queda `desconocido`. La evidencia guarda la referencia, el total de identificadores faltantes y sobrantes, la cobertura, la marca, el corte, la ingesta y los motivos.

  **Límite:** «completo» significa que nuestra copia coincide con lo que Extreme tiene programado, no que Extreme haya registrado todo lo que ocurrió (DECISIONES §16.6).
- **La propuesta de tabla:** `propuestas/migraciones/0010_completitud_ruta_dia.*`, **sin aplicar**. Crea dos tablas: `completitud_ruta_dia`, con el estado vigente, y `completitud_ruta_dia_historial`, donde cada cambio guarda antes la versión anterior.
- **Comportamiento sin la tabla:** el mecanismo calcula los estados, **no escribe nada** y responde `no_evaluable / tabla_pendiente_de_aprobacion` con el conteo de lo que escribiría. ALT-005, ALT-007 y ALT-010 responden `no_evaluable` con los días sin evidencia.

| | ALT-005 Caída anormal | ALT-007 Desviación del pronóstico | ALT-010 Modelo degradado |
| --- | --- | --- | --- |
| Problema | La demanda real quedó muy por debajo de la prevista | El pronóstico se equivocó mucho en cualquier dirección | El error reciente del modelo activo empeoró |
| Decisión | Investigar un evento o una falla de conteo antes de recortar flota | Desconfiar del modelo en esa franja | Entrenar y comparar un candidato |
| Datos | M1: predicciones del modelo activo a 60 min frente a `demanda_franja` consolidada, solo en días `completo` | Igual | M1 a 60 min: 7 días recientes frente a los 28 anteriores |
| Regla y umbral | `(real − pred) / max(pred, 1) ≤ −0,40` (v1) | `|rel| > 0,35` (v1), si no es ALT-005 | `WAPE_reciente > WAPE_ref × 1,2` (v1) |
| Severidad / notifica | media / no | media o alta / solo si es alta | alta / sí (simulado) |
| Clave de deduplicación | código + ruta + franja + horizonte | igual | código + ruta + horizonte |

**Pruebas:**

- **Recorrido completo con datos sintéticos, en una transacción que se revierte** (`test_completitud_llena_la_tabla_con_evidencia_y_desbloquea_005_007`):
  - el mecanismo llena la tabla (aplicada solo dentro de esa transacción) con los estados completo, parcial y desconocido y su evidencia;
  - repetir no reescribe nada; un cambio deja la versión anterior en el historial;
  - con esa evidencia, ALT-005 y ALT-007 alertan.
- **Recorrido automático en el entorno aislado** (sección 9): el día D queda `completo` y produce 3 alertas ALT-005 (8, 26 y 44 subidas frente a 80 previstas). El día D−3 queda `parcial` por cobertura del 75 %.
- **Casos negativos:** `test_completitud_sin_tabla_calcula_pero_no_escribe`, `test_005_007_sin_evidencia_*`, `test_005_007_sin_tabla_*` y `test_010_sin_degradacion_no_alerta`.

---

## 6. ALT-006: salidas tardías conectadas; ausencias pendientes de evidencia de cobertura

- **Problema y decisión.** Salidas que arrancaron más de 10 minutos después de lo programado (`retraso_maximo_min` = 10, v1). La decisión que apoya es corregir el despacho y revisar con el despachador.
- **Fuente**, comprobada en el diccionario de datos:
  - `progvehiculos.fechasalida`: la salida **programada**;
  - `progvehiculos.fechainicio`: el **inicio efectivo**, es decir, la hora real observada;
  - `cancelada`.

  El ciclo las copia en `ai_frecuencias.viaje_fuente`, y la alerta se calcula sobre esa copia.
- **Punto de entrada.** El ciclo diario, en `flujo.evaluar_alt_006`. Evalúa las salidas que esta ingesta volvió consolidadas; la primera vez, solo el último día consolidado.
- **Regla `alt006-v2`.** Hay alerta si `inicio real − salida programada > 10 min`. Una desviación de más de 120 minutos se trata como dato extremo y solo va al diagnóstico. La severidad vigente es `alta` y notifica (simulado).
- **Salida sin inicio registrado.** Queda `indeterminada` y **no alerta**: un `fechainicio` nulo solo indica que falta la evidencia de salida.
  - Para declararla «ausente» falta evidencia de que la fuente cubre ese alcance.
  - Lo que hay en el diccionario (`excedeplazosalida`, `activa`, `gtransid`, `conteo_pasajeros_destino`) no tiene una semántica confirmada para eso.
  - El parámetro `evidencia_cobertura` ya está en la regla, esperando una fuente aprobada.
- **Evidencia guardada.** Cada programación tardía con su desfase (por ejemplo, «16… +25 min»), las evaluadas, las canceladas y la marca de agua. El resumen de la ejecución cuenta cada clase, incluida `indeterminada`.
- **Deduplicación.** La clave es código + ruta + franja de 15 minutos.
- **Pruebas:**
  - `test_006_salida_tardia_con_hora_real_alerta_y_sin_inicio_queda_indeterminada`: una salida tardía de +25 min alerta; una de +7 min no; una sin inicio queda indeterminada; una cancelada se excluye; no se duplica; se consulta.
  - `test_006_salida_a_tiempo_no_alerta`: con +10 min exactos no alerta.
  - En el recorrido automático (sección 9), la salida tardía del día D−3 genera la alerta. La consulté y la reconocí por HTTP.

---

## 7. ALT-008: conectada a `timbradas_anio`, falta confirmar su significado con Extreme

- **Problema.** El consolidado de Extreme y la suma de los viajes difieren en más del 20 % (`diferencia_relativa` = 0,2, v1). La decisión que apoya es no usar ese consolidado y reportarlo a Extreme.
- **Fuentes**, identificadas en el diccionario, en DECISIONES §2 y §9 y en `docs/consultas_verificacion.sql` (consulta 2):

  | | Referencia | Lo evaluado |
  | --- | --- | --- |
  | Tabla de Extreme | `timbradas_anio` (`ano`, `id_ruta`, `subidas`, `nro_viajes`) | `progvehiculos` |
  | Qué se toma | Subidas por ruta y año | `SUM(numeropasajeros)` por ruta y año |
  | Quién lo produce | Extreme, con `fn_insert_timbradas_anio`, agrupado por fecha fin | Viajes con `apto_estadisticas_historicas` y el mismo criterio de fecha: `COALESCE(fecha_fin_liquidacion, fechafin)` |
  | Adaptador | `fuentes_extreme.ConsolidadoTimbradasAnio` | `fuentes_extreme.SubidasViajesAnio` |

  Las dos se leen con el usuario de solo lectura.
- **Independencia comprobada en el código.** `flujo._independientes` rechaza la comparación en estos casos:
  - una de las dos fuentes está en `ai_frecuencias`;
  - las dos son la misma tabla;
  - alguna no es de solo lectura o la referencia no está habilitada;
  - las granularidades difieren.

  La referencia es el agregado propio de Extreme. Lo evaluado son los viajes, no nuestro `demanda_franja`.
- **Períodos.** Solo se comparan años cerrados y consolidados: el 1 de enero siguiente debe ser anterior a marca − 3 días. La alerta queda identificada por ruta y año.
- **Evidencia guardada.** Las dos sumas, la diferencia, las dos tablas de origen y el diagnóstico del hallazgo §9.1: la proporción de franjas con `nro_viajes = 1`. Si es del 90 % o más, se anota como posible falla de acumulación de Extreme.
- **Habilitación.** `ALT008_REFERENCIA=timbradas_anio`. Por defecto está apagada y responde `no_evaluable / fuente_no_autorizada_o_no_de_solo_lectura`.
- **Pruebas:**
  - `test_008_compara_consolidado_independiente_y_alerta_por_periodo`, con adaptadores sintéticos: 2023 con +5 % no alerta; 2024 con −88 % alerta; no se duplica.
  - Pruebas unitarias de independencia (7 casos rechazados) y de años cerrados.
  - En el recorrido automático (sección 9), con tablas `public.timbradas_anio` y `public.progvehiculos` **sintéticas** leídas con el usuario de solo lectura: 2024 da 120 frente a 1.000 (−88 %), con alerta.
- **Qué falta para usarla con datos reales:**
  - Que Extreme confirme qué representa `timbradas_anio.subidas` y si su período es el año de fecha fin.
  - Que confirme si el hallazgo §9.1 (paso 10 de `fn_insert_timbradas_anio`) es real. Si lo es, ALT-008 alertará todos los años, y ese es justamente el hallazgo que hay que reportar.
- **Si se prefiere otro consolidado, hay que pedir a Extreme** una vista de solo lectura con los campos `idruta`, `periodo_inicio`, `periodo_fin`, `granularidad` (día o mes), `subidas`, `viajes`, `fuente_calculo` y `actualizado_en`, calculada de forma independiente de `progvehiculos.numeropasajeros`. Por ejemplo, desde `conteo_pasajeros_destino`.

---

## 8. Tabla final por alerta

| Alerta | Qué funciona con los datos disponibles | Qué quedó demostrado solo con datos sintéticos | Autorización exacta que falta para la activación real |
| --- | --- | --- | --- |
| ALT-001 | Calcula y guarda en `POST /frequency/executions`; la referencia requiere filas en `demanda_franja` | En el recorrido automático la regla corrió, pero los datos no la activaron | Ninguna nueva. Para que corra cada día: aprobar el despliegue del programador con `PROGRAMADOR_FRECUENCIAS_HORAS` |
| ALT-002 / ALT-003 | Igual | Igual (no se activaron con los datos de la demo) | Igual |
| ALT-004 | Igual | En el recorrido automático: 9 alertas (ocupación del 17 %) | Igual |
| ALT-009 incompletitud | Funciona hoy en el cálculo de frecuencias, sin notificar | En el recorrido automático: 1 alerta (cobertura del 75 %) | Aprobar la equivalencia Crítica = `alta` (DECISIONES §16.5) |
| ALT-009 retraso | Conectada al ciclo y al verificador. Sin política responde `no_evaluable` | En el recorrido automático, sin ninguna ingesta, el verificador abrió 3 alertas (rutas 2, 16 y 21) | Política validada (`ALT009_RETRASO_*`: apertura, vencimiento, tolerancia, versión, inicio), el mecanismo para verificar reintentos (P5), la severidad y el despliegue del programador |
| ALT-005 / ALT-007 | Conectadas. Sin la tabla de completitud responden `no_evaluable` | Con la propuesta 0010 aplicada en el entorno aislado: 3 alertas ALT-005 de punta a punta | Aprobar la migración 0010, el criterio `completitud-v2` y el despliegue del programador |
| ALT-010 | Igual | Con evidencia sintética de completitud (prueba con rollback) | Igual que ALT-005 |
| ALT-006 | Salidas tardías con hora real: conectada al ciclo, lista con los datos que ya trae la ingesta | En el recorrido automático: 1 alerta (+25 min), consultada y reconocida por HTTP | Para las tardías, solo el despliegue del programador. Para las **ausencias**: una fuente aprobada de evidencia de cobertura por ruta y día |
| ALT-008 | Conectada a `timbradas_anio` y `progvehiculos`, de solo lectura. Apagada por defecto | En el recorrido automático, con tablas sintéticas: 1 alerta (−88 %) | Que Extreme confirme el significado y el período de `timbradas_anio` y el hallazgo §9.1; luego habilitar `ALT008_REFERENCIA=timbradas_anio` |

---

## 9. Programador y recorrido automático completo

**Implementación.** `app/programador.py` y `deploy/programador/` (ver su README).

**Pruebas unitarias con reloj simulado** (`tests/unit/test_programador.py`, 8 pruebas):
- el disparo ocurre a su hora y nunca antes;
- el verificador corre mientras el ciclo está colgado;
- un ciclo que falla no detiene el programador;
- sin horario, no programa nada.

**Recorrido automático de punta a punta** (`tests/integracion/test_recorrido_automatico_e2e.py`). Usé una instancia PostgreSQL **recién creada y exclusiva**, preparada con `scripts/preparar_bd_suite_alertas.sh`. Las tablas `public.progvehiculos` y `public.timbradas_anio` son **sintéticas** y simulan a Extreme: las crea el administrador de esa instancia y la aplicación las lee con el usuario de solo lectura. La propuesta 0010 se aplicó **solo en esa base**. El programador corrió como **proceso real**, con horas de prueba a pocos segundos:

1. **Primer disparo.** El programador lanza el ciclo y el verificador, cada uno a su hora.
   - El ciclo hace la ingesta completa, llena la completitud (35 días × 3 rutas) y abre ALT-006 (+25 min) y ALT-008 (−88 %).
   - El verificador no encuentra retraso.
2. **Predicción del día D** por HTTP (`POST /api/v1/frequency/executions`). Se guardan 9 predicciones, 9 recomendaciones, 9 alertas ALT-004 y 1 de incompletitud de ALT-009.
3. **Llegan datos nuevos a la fuente** sintética y el programador vuelve a disparar. D queda `completo` y se abren 3 alertas ALT-005 con su trazabilidad.
4. **La ingesta no arranca:** su hora queda dos horas después, así que solo se dispara el verificador. Abre 3 alertas de retraso con dos ciclos `no_iniciada`.
5. **La fuente es inalcanzable.** La ingesta queda `fallida` y el retraso se verifica igual, sin duplicar.
6. **Se repite todo sin datos nuevos.** El conteo de alertas por código no cambia.
7. **Consulta por HTTP.** `GET /api/v1/alerts` devuelve ALT-005, ALT-006, ALT-008 y ALT-009. `PATCH` reconoce la ALT-006 y su historial queda `creada → cambio_estado`. ALT-009 no generó ninguna notificación.
