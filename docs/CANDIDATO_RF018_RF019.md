# Candidato reproducible y comparación con el activo (RF-018 / RF-019)

**Estado (19-nov-2026):** implementado en la rama aislada `m1-prediccion-vs-real`, sin commit.

- Nada se registra en `model_registry` ni se activa.
- El artefacto activo (`gbm_fe31_20261107T065452`, SHA-256 `37b5a28a…`) no se toca.

## 1. Contrato (documentado, sin agregar requisitos)

| Fuente | Qué exige |
|---|---|
| Especificación v1.5, RF-018 | Incorporar datos nuevos validados al histórico; evaluar el modelo activo; reentrenar de forma controlada; **registrar datos usados, experimentos, métricas, artefactos, versiones y aprobación** |
| Especificación v1.5, RF-019 | **Comparar cada modelo reentrenado contra el activo con un conjunto de validación temporal.** Activar solo si cumple los **umbrales aprobados**, con trazabilidad y reversión |
| Especificación v1.5, §9.5 | Reentrenamiento controlado (no se reemplaza el activo con cada registro). Si el candidato no mejora, se conserva el vigente y se registra la decisión |
| Especificación v1.5, §14.2 | La demo incluye un ciclo completo: datos nuevos → incremental → error → candidato → comparación → activar o rechazar |
| `contexto_portable_Alejandra.md` (A4/T7) | El candidato **reusa** `scripts/entrenar_gbm_fe31.py`; la comparación es temporal contra el activo; los umbrales de activación necesitan aprobación |

**Sobre el nombre «P1».** En los documentos del proyecto, P1 es la **trazabilidad de predicciones**: commit `40aa79b`, migración 0008, criterios A1/A2. Lo que se implementa aquí corresponde a **RF-018 y RF-019 (A4/T7, base de C2)**.

## 2. Qué ya existía

| Pieza | Dónde | Lo que hace |
|---|---|---|
| Entrenamiento manual | `scripts/entrenar_gbm_fe31.py` | Particiones E (< 2025-01), A (ene-mar 2025) y V (abr-jun 2025); modelo final con todo lo anterior a 2025-07 |
| Registro | `--registrar` en el mismo script | Escribe en `model_registry` con hash, semilla y versión de datos |
| Activación | `POST /models/{id}/activate`, `verificar_activable`, `model_history` | Activación manual con bitácora y reversión |

**Lo que faltaba:**
- un corte duro de datos: el activo calculó el horario y los días anómalos con datos hasta 2025-12 (revisión de A6);
- reproducibilidad por construcción: la versión salía de la hora;
- un manifiesto de experimento;
- separación física del activo;
- una comparación candidato/activo sobre las mismas observaciones.

## 3. Qué se implementó

| Archivo | Qué hace |
|---|---|
| `app/ml/candidato.py` | `ConfigCandidato` (particiones, semilla y la receta fija del GBM FE 3.1). Recorta los datos antes de todo y calcula calendario, cuarentena, días y **horario solo con viajes < `datos_hasta`**. La versión sale de la huella de la configuración y los datos. Escribe el manifiesto. `guardar` va a `modelos/candidatos/` y no reemplaza un protegido ni un candidato distinto |
| `app/ml/comparacion_modelos.py` | `comprobar_periodo`: el periodo debe ser posterior al entrenamiento de **todos** los modelos y estar en `PERIODOS_APROBADOS`, que hoy está vacío. `comparar`: cada modelo predice cada día con `modelo_gbm.predecir_dia` y solo viajes anteriores a ese día; se usan solo las ventanas que **todos** predijeron y con etiqueta completa (comun-1.5, faltantes nunca en 0); métricas por ruta × horizonte; diferencia diaria de MAE con IC 95 % (bootstrap de bloques de 7 días); huella. **Sin veredicto**: faltan los umbrales de RF-019 |
| `scripts/entrenar_candidato.py` | Línea de comandos. Lectura READ ONLY de `viaje_fuente` con el corte **en la consulta** (`fechasalida < datos_hasta`). Comprueba que los artefactos de `modelos/` no cambiaron |
| `scripts/comparar_candidato.py` | Línea de comandos. Valida el periodo **antes** de leer un solo viaje |

**Datos autorizados para entrenar:** anteriores al 2025-07-01 (`DATOS_AUTORIZADOS_HASTA`), los mismos del activo. Cualquier corte posterior es rechazado hasta que se confirme B1.

## 4. Corrida real (copia desechable de la base, 19-nov)

**Configuración:** `entrenamiento_hasta 2025-01-01`, `ajuste_hasta 2025-04-01`, `datos_hasta 2025-07-01`. Es la receta del activo, más el corte duro.

| | Candidato `cand_gbm_fe31_r1_26ba0762a4fd` | Activo `gbm_fe31_20261107T065452` |
|---|---|---|
| SHA-256 del artefacto | `358cffd3…5918b`, **idéntico en dos corridas** | `37b5a28a…eadf1` (no modificado) |
| Datos | viajes < 2025-07-01, `datos_sha256 7e176ac4…` | todos los viajes cargados (hasta 2025-12) |
| Horario y días | calculados con viajes < 2025-07-01 | calculados con todo el histórico |
| Filas del modelo final | 466.628 | 466.468 |
| Árboles elegidos en el ajuste | **598 (tope 600)** | 531 |
| Diagnóstico de desarrollo (abr-jun 2025) | WAPE 0,1349 · MAE 100,9 · cobertura 80 % 0,804 | WAPE 0,1385 · cobertura 0,802 |

**Cómo leer la tabla:**
- **No es una comparación.** Ambas cifras son del modelo de ajuste (entrenado hasta diciembre de 2024), no de los artefactos finales.
- Las filas son distintas: el activo usó las filas comunes a todos los métodos, y su selección de días usó datos futuros.
- La comparación justa exige un periodo posterior al 2025-06-30, que no está aprobado.
- **Las 160 filas de diferencia y los 67 árboles más** son el efecto medible de quitar la fuga de selección.
- **598 de 600** indica que la parada temprana quedó en el tope: la curva quizá seguía bajando. Subir el tope es cambiar la receta y necesita aprobación.

## 5. Pruebas

- `tests/unit/test_candidato.py` (16 pruebas):
  - reproducibilidad: mismo SHA aunque cambie el orden de las filas;
  - ningún dato posterior al corte cambia el resultado;
  - particiones sin solape y con purga;
  - receta fija y rechazo de datos más allá de lo autorizado;
  - el activo no se sobrescribe ni se reemplaza un candidato distinto;
  - la API carga el candidato;
  - un periodo no aprobado se detiene sin predecir ni leer;
  - un periodo solapado con el entrenamiento se rechaza;
  - mismas ventanas para los dos modelos, comparación reproducible y sin datos posteriores al periodo;
  - las etiquetas no convierten faltantes en cero;
  - la huella de la receta no depende del fin de línea;
  - los scripts.
- `tests/integracion/test_candidato_postgres.py` (2 pruebas):
  - el corte se aplica en la consulta;
  - la base y la memoria dan el mismo SHA;
  - el script funciona con la fuente local.

  Corren solo en una base desechable y borran su lote.

## 6. Decisiones pendientes (para C2)

1. **Periodo de comparación** (B1). Propuesto: julio-septiembre de 2025, para comparar el activo con candidatos entrenados con datos < 2025-07-01. Para un ciclo de reentrenamiento con datos nuevos (por ejemplo, entrenar hasta agosto y comparar en septiembre) hay que ampliar `DATOS_AUTORIZADOS_HASTA`.
2. **Umbrales de RF-019.** Hay que decidir:
   - la métrica principal;
   - el margen («mejora o mantiene»);
   - si se exige el IC del bootstrap;
   - si se exige no empeorar en ninguna combinación ruta × horizonte.

   Guardarlos en `umbral` es una migración de datos y requiere aprobación.
3. **Registro del candidato en `model_registry`** (escritura, estado `candidato`). La función existe en el script del activo, pero no se ejecutó.
4. **Tope de árboles (600).** Mantenerlo o probar un tope mayor como experimento aparte.
5. **Base de datos (D1).** La corrida usó una copia desechable de un respaldo del 8-nov, con la marca de agua 2026-01-23. La corrida oficial debe hacerse sobre la base de la demo.
6. **Intervalos.** Como en el activo, se calibran con el modelo de ajuste y se aplican al modelo final (hallazgo de A6). No se cambió para mantener la receta; queda para A6 y E12.
