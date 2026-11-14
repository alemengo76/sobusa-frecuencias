# Integración del gradient boosting, calidad comun-1.5 y factor de carga medido

Versión aislada para revisión, del 6 de noviembre de 2026. Carpeta:
`Data Challenge\sobusa-prototipo-integracion`. El prototipo original (`sobusa-prototipo`) no se tocó.
Respaldo previo: `Data Challenge\respaldo_20261106_antes_integracion_gbm\`.

## 1. Qué quedó listo

**Actualizado el 7 de noviembre:** la sección 7 se corrió completa en otro computador (MacBook de
Mariangel, Docker Desktop) y los seis criterios de aceptación quedaron comprobados.

| Criterio de aceptación | Estado | Evidencia (7 nov) |
|---|---|---|
| 1. Docker levanta | ✅ | `docker compose up` aplica 0003 y 0004; API en http://localhost:8000/docs |
| 2. Lee la base local | ✅ | `verificar_integracion --comparar`: datos previos idénticos; reingesta comun-1.5: 564.837 viajes en `viaje_fuente`, 198.997 franjas |
| 3. Predicciones del GBM desde la API | ✅ | `POST /frequency/executions` (2026-01-01 y 2025-12-10): `respaldo = false`, `filas_por_modelo = {"2": ...}` |
| 4. Líneas base como respaldo | ✅ | Pruebas unitarias y de API; se vio en vivo (ver "Corrección del 7 de noviembre") |
| 5. Una sola referencia de calidad | ✅ | 9.034 registros con `reglas_version = comun-1.5` |
| 6. Recomendaciones con factores medidos | ✅ | `param_v2:laboral` (0,419 / 0,459 / 0,387) y `param_v2:domingo_festivo` (0,365 / 0,418 / 0,372) |

Pruebas dentro del contenedor: **107 pasan, 2 se saltan** (las que comparan con la carpeta
`fe_v4_comun15`, que no está dentro de la imagen), 0 fallan.

**Modelo oficial:** `modelos/gbm_fe31_20261107T065452` (model_id 2, activo), entrenado dentro de Docker
con los viajes de la base local. Sha256 `37b5a28a1c807e08…`, scikit-learn 1.9.1, numpy 2.4.6,
pandas 3.1.1. Sus métricas son **idénticas** a las preliminares (WAPE 0,1385): el entrenamiento es
reproducible en otro computador. `pyproject.toml` fija ahora scikit-learn 1.9.1 para que el artefacto
cargue en cualquier contenedor nuevo. El respaldo de esa base está en
`Data Challenge\respaldo_20261107_mac_mariangel\sobusa_amiga.backup` (verificado: versión 0004, modelo 2
activo, 564.837 viajes, 12 filas param_v2 vigentes).

**Corrección del 7 de noviembre.** La prueba de integración pidió el 31 de diciembre y el GBM pasó al
respaldo en las rutas 2 y 16: hace 7 días era Nochebuena, que quedó de baja calidad, y la regla exigía
el rezago de 7 días. El modelo se entrenó con esos huecos, así que ahora solo se usa el respaldo si
faltan **todos** los rezagos semanales o el nivel de 28 días. Prueba nueva:
`test_predice_aunque_falte_el_rezago_de_7_dias`.

**Hallazgos de la prueba real (pendientes, ver sección 8):** 293 alertas en un día laboral (se evalúan
en los 3 horizontes y ALT-003 salta con 1 bus/hora de déficit); la cuarentena repite viajes releídos en
varias ventanas (9.034 filas, 7.731 únicas); `finalizada_en` queda igual a `iniciada_en`.
En la ruta 16, 11 franjas pico son "no viables": necesitan 100 a 116 buses y el tope es 99.

## 2. Cómo se integra el gradient boosting en la API

```
Entrenamiento (fuera de la API, una vez o cuando se reentrene)
  scripts/entrenar_gbm_fe31.py
    viajes (viaje_fuente o CSV) -> app/fe31 (copia EXACTA de reglas_comunes + conjunto_entrenamiento)
    -> 29 variables FE 3.1, objetivo y_pasajeros -> HistGradientBoosting (Poisson)
    -> modelos/gbm_fe31_<fecha>.pkl + .json (hash sha256, versiones, métricas)
    -> model_registry (estado 'candidato', hiperparametros.metodo = 'gbm_fe31')

Activación (igual que antes, con bitácora y reversión)
  POST /api/v1/models/{id}/activate
    -> NUEVO: antes de activar, abre el artefacto; si no carga, responde 409 con el motivo

Predicción (cada solicitud; NO entrena)
  POST /api/v1/predictions/run  ->  servicios.predecir()
    si el activo es gbm_fe31:
      1. carga el artefacto UNA vez (caché) y verifica su hash ANTES de abrirlo
      2. lee de viaje_fuente los viajes de [D-35 días, D)   (contrato D+1)
      3. reglas comun-1.5 + horario de operación guardado en el artefacto
      4. construir_conjunto() del FE -> filas del día D -> predicción + intervalo 80 %
      5. si algo falla en una ruta: esa ruta se predice con la línea base (METODO_RESPALDO)
    si el activo es una línea base: igual que antes (dia_hora_4sem / dia_hora_8sem / ultima_semana)
  -> cada fila de prediction guarda el model_id que la produjo; execution_log guarda
     modelo_activo, respaldo (sí/no), motivo y filas por modelo
```

Puntos importantes:

- **Mismas variables al entrenar y al predecir.** No se reescribió el FE: `app/fe31` contiene los dos
  archivos de `fe_v4_comun15` sin cambios (una prueba compara su sha256). Comprobé con los 564.837 viajes
  reales, en 28 días elegidos al azar (incluidos festivos y fin de año), que las 29 variables que calcula
  la API para el día D son **idénticas** a las del conjunto de entrenamiento, y que la predicción también.
  Tarda unos 0,4 s por día para las tres rutas.
- **Sin fuga.** Para el día D solo entran viajes con salida anterior a las 00:00 de D. Una prueba cambia
  los pasajeros del día D en adelante y verifica que la predicción no cambia.
- **El modelo nunca se entrena en una solicitud.** Una prueba hace fallar `fit()` durante la predicción.
- **Artefacto seguro y reproducible.** Junto al `.pkl` queda un `.json` con hash, semilla,
  hiperparámetros, versión de datos, `feature_version` (sobusa-fe-3.1), `reglas_version` (comun-1.5),
  versiones de scikit-learn, numpy y pandas, particiones y métricas. Si la versión de scikit-learn
  del contenedor no es la del entrenamiento, el artefacto no se abre (un pickle de otra versión
  puede dar resultados distintos sin avisar). Por eso el modelo oficial se entrena **dentro** de Docker.
- **Por qué `viaje_fuente` (migración 0004).** El GBM necesita los viajes, no solo `demanda_franja`.
  Leerlos de Extreme en cada solicitud haría que la API dependa de su servidor (que ya falló antes).
  La ingesta, que ya relee cada día completo, ahora también copia sus viajes (9 columnas, sin
  conductor ni placa). Además, así se puede reentrenar desde la base local sin el CSV de 123 MB.

## 3. Comparación del modelo frente a las líneas base

**Preliminar.** Entrené con el CSV de la v3 que tengo (564.837 viajes, mismo número que el oficial, sha
`2472c2b6…`); el oficial (`2319dfc2…`) no está en su computador. Hay que repetirlo con sus datos
(sección 7). Evaluación: abril a junio de 2025, filas habilitadas del FE 3.1, **las mismas filas para
todos los métodos** (42.474). La prueba (julio a diciembre de 2025) no se tocó.

WAPE (menor es mejor):

| Ruta | H | GBM FE 3.1 | día-hora 4 sem | día-hora 8 sem | última semana | método Extreme |
|---|---|---|---|---|---|---|
| 2 | 30 | **0,222** | 0,289 | 0,276 | 0,365 | 0,277 |
| 2 | 60 | **0,161** | 0,225 | 0,216 | 0,280 | 0,234 |
| 2 | 120 | **0,125** | 0,186 | 0,177 | 0,225 | 0,208 |
| 16 | 30 | **0,155** | 0,223 | 0,212 | 0,279 | 0,187 |
| 16 | 60 | **0,117** | 0,190 | 0,180 | 0,236 | 0,160 |
| 16 | 120 | **0,092** | 0,171 | 0,162 | 0,213 | 0,143 |
| 21 | 30 | **0,275** | 0,337 | 0,321 | 0,425 | 0,303 |
| 21 | 60 | **0,196** | 0,252 | 0,241 | 0,319 | 0,231 |
| 21 | 120 | **0,145** | 0,199 | 0,190 | 0,253 | 0,195 |
| **Total** | | **0,139** | 0,206 | 0,196 | 0,257 | 0,191 |
| Hora pico | | **0,128** | 0,189 | 0,172 | 0,231 | 0,177 |
| Hora valle | | **0,144** | 0,215 | 0,209 | 0,271 | 0,199 |

MAE total: GBM 97 pasajeros por ventana frente a 135 del mejor método de referencia (Extreme).
El GBM gana en las 9 combinaciones de ruta y horizonte y en pico y valle: reduce el WAPE un 28 %
frente al mejor de referencia. El intervalo del 80 % cubre entre el 78 % y el 82 % de los casos reales
según ruta y horizonte. La variable más importante es `y_media_4_semanas`; le siguen el horizonte,
la marca de calendario y el tipo de día. Detalle en `docs/resultados_gbm_preliminar/`
(`metricas.csv` con MAE, RMSE, WAPE y sMAPE por ruta, horizonte y pico/valle; `importancia_global.csv`;
`explicacion_local.csv`; `cobertura_intervalos.csv`).

Ojo al leerlo: la ruta 21 en 30 minutos sigue con un error de 27,5 %. Con pocos buses por ventana el
conteo salta mucho; para frecuencias conviene mirar 60 minutos.

## 4. E02 · Calidad comun-1.5 en la ingesta

- `app/calidad/cuarentena.py` ya no define reglas: llama a `aplicar_cuarentena` de comun-1.5 y traduce
  el resultado a `data_quality_error` (lote, idprogramacion, idruta, regla, causa, valor,
  `reglas_version = comun-1.5`). Las reglas y su causa están en `DECISIONES.md`, sección 8.
- Los nulos y negativos se registran como `pasajeros_sin_conteo` (es la marca `pax_bloquea` del FE):
  siguen siendo consultables por lote, con causa y fila.
- `demanda_franja` usa la misma definición que la etiqueta del FE: una franja queda sin medición si
  algún viaje elegible no tiene conteo usable.
- Diferencias con `calidad_v1`: desaparece CAL-003 (3 × plazas; comun-1.5 usa 500 fijos); se agregan
  inicio incoherente, fin tardío, duración negativa y bajadas extremas; los viajes no finalizados ya no
  vuelven incompleta su franja (no son elegibles, igual que en el FE).
- Pruebas: los viajes válidos de la ingesta son exactamente los `pax_valido` del FE con 2.000 viajes
  aleatorios con errores; ningún código `CAL-` queda en el módulo.
- **Hay que volver a correr la ingesta** con `--reiniciar` (sección 7): recalcula `demanda_franja` con
  las reglas nuevas y llena `viaje_fuente`. Las filas antiguas de `calidad_v1` en
  `data_quality_error` se conservan (se distinguen por `reglas_version`).

## 5. E03 · Factor de carga param_v2

Migración `0003` (solo datos). `param_v1` queda guardado como no vigente.

| Ruta | todos | laboral | sábado | domingo-festivo |
|---|---|---|---|---|
| 2 | 0,400 | 0,419 | 0,378 | 0,365 |
| 16 | 0,435 | 0,459 | 0,429 | 0,418 |
| 21 | 0,379 | 0,387 | 0,381 | 0,372 |

- `todos` es la mediana exacta de la ruta en `factor_carga.csv`. Por tipo de día, el archivo solo trae
  medianas por hora: uso la mediana ponderada por número de viajes. Aplicada a la ruta completa da
  0,398 / 0,436 / 0,387 frente a las exactas 0,400 / 0,435 / 0,379, así que el error es de hasta 0,008
  (en la ruta 21). Si quieren la exacta, hay que sacarla de los datos del sensor.
- El tipo de día sale del calendario del FE (festivos con Ley Emiliani), el mismo del modelo.
- Cada recomendación guarda qué fila usó: `parametros_version = 'param_v2:laboral'`.
- Capacidad, ocupación, ciclo y flota tope se copiaron de la fila vigente: param_v2 solo cambia el factor.
- No hay cifras en el código: los valores están en la tabla. Una prueba verifica que la migración
  coincide con lo que sale del archivo (`datos/referencia/factor_carga_sensor11.csv`).

## 6. Pruebas

Aquí: **103 pasan, 3 se saltan** (las que necesitan FastAPI o PostgreSQL con SQLAlchemy). Antes de los
cambios eran 54 y 1 saltada. Se cambió una prueba existente (`test_calidad.py`): verificaba los
códigos CAL de las reglas que se reemplazaron.

| Prueba obligatoria | Dónde |
|---|---|
| 1. Todas las existentes | `pytest` (las 54 anteriores siguen pasando, salvo la de códigos CAL, que se reescribió) |
| 2. Integración del GBM | `tests/unit/test_modelo_gbm.py` (12), `tests/unit/test_servicio_prediccion.py` (11) |
| 3. La API predice con el modelo entrenado | `tests/api/test_api_gbm.py` (**córranla en Windows**) y `test_servicio_prediccion.py::test_gbm_activo_predice_con_el_modelo_entrenado` |
| 4. Líneas base como respaldo | `test_artefacto_alterado_usa_la_linea_base`, `test_respaldo_por_ruta_cuando_una_no_tiene_viajes`, `test_lineas_base_siguen_funcionando` (3 métodos) |
| 5. Sin valores inválidos | `test_predicciones_validas`, más `contrato.validar_predicciones` en cada ejecución |
| 6. Factor por ruta y tipo de día | `tests/unit/test_parametros.py` (12 combinaciones, respaldo `todos`, ruta sin parámetros = error) |
| 7. Docker y PostgreSQL | `tests/integracion/test_postgres.py` (**dentro de Docker**) |
| 8. No se perdieron datos | `scripts/verificar_integracion.py --guardar/--comparar`; hecho aquí con `psql` sobre la copia de su base: 9 tablas idénticas después de 0003 + 0004, e idénticas también tras revertir |

## 7. Cómo correrlo desde cero (Windows, PowerShell, en `sobusa-prototipo-integracion`)

Esta carpeta es otro proyecto de Docker Compose: tiene **su propia base**, vacía al empezar, y no
toca la del prototipo original. Como las dos usan los puertos 5432 y 8000, primero se apaga la
original. Los nombres `postgres` y `sobusa` son los de `POSTGRES_USER` y `POSTGRES_DB` de su `.env`.

```powershell
# 0. Apagar el prototipo original SIN borrar sus datos (sin -v)
cd "..\sobusa-prototipo";  docker compose down;  cd "..\sobusa-prototipo-integracion"
#    Agreguen al .env de esta carpeta (está en .env.example): MODELOS_DIR=modelos y METODO_RESPALDO=dia_hora_4sem

# 1. Pruebas sin base (aquí sí corren las de la API)
pip install -e ".[dev]"
pytest -q

# 2. Base nueva de esta carpeta + copia de sus datos de hoy (respaldo del 6 de noviembre)
docker compose up -d db
docker compose cp "..\respaldo_20261106_antes_integracion_gbm\sobusa.backup" db:/tmp/sobusa.backup
docker compose exec db pg_restore -U postgres -d sobusa -n ai_frecuencias --clean --if-exists /tmp/sobusa.backup

# 3. Huella de los datos ANTES de migrar (la base está en la versión 0002)
docker compose build app
docker compose run --rm app python -m scripts.verificar_integracion --guardar salidas_modelo_fe31/huella_antes.json

# 4. Levantar la API (aplica 0003 y 0004 al arrancar) y comparar
docker compose up -d app
docker compose logs app            # debe decir "Running upgrade 0002 -> 0003" y "0003 -> 0004"
docker compose exec app python -m scripts.verificar_integracion --comparar salidas_modelo_fe31/huella_antes.json

# 5. Volver a ingerir con comun-1.5 (lee Extreme; llena viaje_fuente y rehace demanda_franja)
docker compose exec app python -m app.ingesta --reiniciar

# 6. Entrenar el GBM DENTRO del contenedor y registrarlo (unos 2 minutos, 1,6 GB de memoria)
docker compose exec app python -m scripts.entrenar_gbm_fe31 --fuente local --registrar
#    -> imprime la tabla de WAPE y "Registrado model_id=N como candidato"

# 7. Activarlo (si el artefacto no carga, responde 409 y dice por qué). Cambien N.
curl.exe -X POST http://localhost:8000/api/v1/models/N/activate -H "Content-Type: application/json" `
  -d '{\"usuario\": \"ale\", \"motivo\": \"GBM FE 3.1 mejora WAPE\"}'

# 8. Predecir y recomendar (el día siguiente al último con datos)
curl.exe -X POST http://localhost:8000/api/v1/frequency/executions -H "Content-Type: application/json" `
  -d '{\"dia\": \"2026-01-24\", \"idrutas\": [2, 16, 21]}'
#    En GET /api/v1/frequency/executions/{id} y en la ejecución de predicción: respaldo = false

# 9. Pruebas contra PostgreSQL dentro de Docker
docker compose exec app pytest -q tests/integracion

# Reversión, si algo sale mal
#   - volver a la línea base: POST /api/v1/models/1/activate
#   - quitar param_v2 y viaje_fuente: docker compose exec app alembic downgrade 0002
#   - volver al original: docker compose down aquí y docker compose up -d en sobusa-prototipo
```

Atajo sin reentrenar: `modelos/` trae el artefacto preliminar
(`gbm_fe31_20261106T105509`). Se registra con
`docker compose exec app python -m scripts.entrenar_gbm_fe31 --registrar-artefacto modelos/gbm_fe31_20261106T105509.json`,
pero **solo carga si el contenedor tiene scikit-learn 1.9.1**. Si no, la activación responde 409 y
hay que entrenar (paso 6). Para la entrega final, usen el modelo del paso 6: está hecho con sus datos.

Si al entrenar Docker dice `Permission denied` en `/app/modelos`, corran el paso 6 con
`docker compose exec -u root app ...`.

## 8. Limitaciones y pendientes

1. ~~Docker, Alembic y la API sin probar~~: comprobados el 7 de noviembre (sección 1).
2. ~~Métricas preliminares~~: repetidas con la base local, idénticas (sección 1). Úsenlas en E05.
2b. **Alertas**: ~~ALT-002/003/004 solo con H = 60 min y ALT-003 a 2 buses/hora~~ (rama
   `alertas-60min`, migraciones 0005 y 0006 —la 0006 reconcilia las alertas abiertas—; ver `DECISIONES.md`
   §13 y §14). Falta unir franjas seguidas en episodios.
2c. ~~**Cuarentena**: no volver a registrar un viaje con la misma regla y versión en otro lote~~:
   eventos + detecciones (migración 0007, `DECISIONES.md` §8).
2d. **execution_log.finalizada_en**: usar `clock_timestamp()` y no `now()` (que es la hora de inicio
   de la transacción).
2e. ~~**Capacidad**: confirmar si 52 / 55 / 50 incluyen pasajeros de pie~~: sí los incluyen
   (`datos/vehiculo_capacidad.csv`, plazas = sentados + pie).
3. **Hay que reingerir con `--reiniciar`** (hecho en el Mac el 7 de noviembre; en otro computador basta restaurar `sobusa_amiga.backup`): la API usa `demanda_franja` para las líneas base y
   `viaje_fuente` para el GBM. Mientras no se reingiera, el GBM no encuentra viajes y la API usa la
   línea base (lo dice en la ejecución).
4. **El factor por tipo de día es una aproximación** (error de hasta 0,008). El factor por hora no se
   implementó (no está autorizado).
5. **Las predicciones del GBM cubren solo el horario de operación** de cada tipo de día (como la
   etiqueta del FE). Las líneas base dan todas las franjas. Fuera del horario no hay recomendación del GBM.
6. **Intervalos**: salen de los errores del período de ajuste (ene-mar 2025) con el modelo entrenado
   hasta 2024. El modelo final se entrena hasta junio de 2025: sus intervalos podrían ser algo anchos.
7. **Ruta 21 a 30 min**: el WAPE sigue alto (27,5 %).
8. **`pickle`**: SonarQube lo marcará como punto a revisar. Se mitiga verificando el hash registrado
   antes de abrir el archivo; documéntenlo en el reporte.
9. **Sin cambios** (como pidieron): `y_abordajes` no entra al modelo; no hay sensores en la ingesta ni
   se usa la tabla de 42 GB; no hay factores por hora; no hay IDA/VUELTA ni B1-B5; FE 3.2 no
   reemplaza a FE 3.1. Las migraciones nuevas se numeran 0003 a 0008 (la 0008 es la de
   trazabilidad de predicciones, `DECISIONES.md` §15); si se autoriza la de sentido y segmentos, va
   después (0009).
10. `app/ml/gbm.py` y `scripts/entrenar_y_evaluar.py` (el candidato anterior) se dejaron con una nota de
    que fueron reemplazados; se pueden borrar cuando revisen esta versión.

## 9. Registro de archivos

Nuevos:

| Archivo | Para qué |
|---|---|
| `app/fe31/__init__.py`, `reglas_comunes.py`, `conjunto_entrenamiento.py` | FE 3.1 / comun-1.5, copia exacta con huellas |
| `app/fe31/viajes.py` | Limpieza del ETL y formato del FE, un solo camino para ingesta, entrenamiento y predicción |
| `app/ml/modelo_gbm.py` | Variables, entrenamiento, artefacto con hash y predicción del GBM |
| `app/optimizacion/parametros.py` | Parámetros por ruta y tipo de día; derivación de param_v2 |
| `migrations/versions/0003_param_v2.py`, `migrations/sql/0003_param_v2.sql` | param_v2 |
| `migrations/versions/0004_viaje_fuente.py`, `migrations/sql/0004_viaje_fuente.sql` | Tabla viaje_fuente |
| `scripts/entrenar_gbm_fe31.py` | Entrenamiento, comparación, artefacto y registro |
| `scripts/verificar_integracion.py` | Huella de datos antes y después; prueba de predicción sin guardar |
| `datos/referencia/factor_carga_sensor11.csv` | Copia de `factor_carga.csv` (sin datos personales) |
| `modelos/gbm_fe31_20261106T105509.pkl/.json` | Artefacto preliminar |
| `docs/INTEGRACION_GBM.md`, `docs/resultados_gbm_preliminar/` | Este documento y las métricas |
| `tests/unit/test_fe31.py`, `test_modelo_gbm.py`, `test_parametros.py`, `test_servicio_prediccion.py`, `sinteticos.py` | Pruebas nuevas |
| `tests/api/test_api_gbm.py`, `tests/integracion/test_postgres.py` | Pruebas de API y de PostgreSQL |

Modificados:

| Archivo | Cambio |
|---|---|
| `app/servicios.py` | `predecir()` con GBM y respaldo por ruta; `predecir_gbm()`; `verificar_activable()`; parámetros por tipo de día |
| `app/api/main.py` | La activación verifica el artefacto del GBM (409 si no carga) |
| `app/db/repositorio.py` | Parámetros vigentes de todos los tipos de día; `viajes_fuente()`; `modelo_respaldo()`; `model_id` por fila |
| `app/calidad/cuarentena.py` | Adaptador de comun-1.5 (reemplaza calidad_v1) |
| `app/ingesta.py` | Lee `numerobajadas`; franjas con la definición del FE; copia viajes a `viaje_fuente` |
| `app/optimizacion/frecuencias.py` | `recomendar()` elige parámetros por ruta y tipo de día |
| `app/core/config.py`, `.env.example` | `MODELOS_DIR`, `METODO_RESPALDO` |
| `Dockerfile`, `docker-compose.yml` | Carpeta y volumen de modelos; datos de referencia |
| `tests/unit/test_calidad.py` | La prueba de cuarentena verifica las reglas comun-1.5 |
| `DECISIONES.md` | Secciones 3, 8 y 12 nuevas |
| `app/ml/gbm.py`, `scripts/entrenar_y_evaluar.py` | Solo una nota de que fueron reemplazados |
| `.gitignore` | Salidas del entrenamiento |
