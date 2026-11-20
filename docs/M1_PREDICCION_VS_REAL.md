# M1: comparación de predicciones guardadas contra la demanda real

**Estado:** implementado en la copia aislada sobre `trazabilidad-predicciones` @ `40aa79b`. Sin commit, sin migración y sin desplegar.

**Reglas:** `m1-1.1` (`app/ml/comparacion_real.py`, constante `VERSION_REGLAS`).

## 1. Qué hace

M1 lee de `ai_frecuencias`, siempre en transacción READ ONLY:
- las predicciones guardadas (`prediction` + `model_registry`);
- la demanda real (`demanda_franja`);
- la marca de agua (`watermark`).

Con eso empareja cada predicción con su real, clasifica la predicción en un estado y calcula métricas **por modelo**.

**No escribe nada.** No lee tablas de Extreme, no entrena ni activa modelos y no cambia ninguna regla de alertas.

## 2. Definiciones

| Concepto | Definición |
|---|---|
| Predicción | Fila de `prediction`: pasajeros de los viajes con **salida programada** en [`franja_inicio`, `franja_inicio` + H), con H = `horizonte_min` ∈ {30, 60, 120}, emitida por `model_id` / `model_version` / `feature_version` con datos hasta `data_cutoff` |
| Objetivo | (`model_id`, `idruta`, `sentido`, `segmento_id`, `franja_inicio`, `horizonte_min`) |
| Real de la ventana | Suma de `demanda_franja.pasajeros` en las H/15 franjas t, t+15, …, t+H−15. Es la misma definición que la etiqueta del FE (comun-1.5) |
| Franja sin fila | No tuvo salidas; aporta 0 |
| Franja con `pasajeros` NULL | Algún viaje elegible no tiene conteo usable o está en cuarentena. **La ventana entera queda sin medición; no se imputa** |
| Marca de agua | `watermark.ultima_actualizacion` de `progvehiculos`: el resultado de todo viaje con `fin_resultado` anterior a ella ya está cargado |
| Corte de consolidación | marca de agua − `DIAS_REVISION` (3 días por defecto). La ingesta relee esos días porque pueden corregirse, así que se tratan como **provisionales** |
| Ingenuo de 7 días | Real de la misma ventana 7 días antes, con las mismas reglas |

## 3. Reglas de emparejamiento y exclusión

Los estados son excluyentes y se asignan **en este orden**. El primer motivo que aplica es el que queda.

| Orden | Estado | Motivo | Condición |
|---|---|---|---|
| 1 | `inconsistente` | `horizonte_invalido` | `horizonte_min` ∉ {30, 60, 120} |
| | | `franja_no_alineada` | `franja_inicio` no es múltiplo de 15 minutos |
| | | `valor_invalido` | `demanda_pred` nula o negativa |
| | | `limites_invalidos` | No se cumple `limite_inferior` ≤ `demanda_pred` ≤ `limite_superior` |
| | | `informacion_futura` | `data_cutoff` > `franja_inicio`: la predicción habría usado datos no disponibles |
| | | `version_distinta_al_registro` | `model_version` o `feature_version` distintas a las de `model_registry` para ese `model_id` |
| 2 | `duplicado` | `pronostico_anterior` | Hay otra predicción del mismo objetivo con `data_cutoff` más reciente. **Se usa la más reciente** |
| | `duplicado` | `repeticion` | Mismo objetivo y mismo `data_cutoff`, con valores idénticos. Se conserva la **primera emitida** (`generated_at` y luego `prediction_id`) |
| | `inconsistente` | `duplicado_con_valores_distintos` | Mismo objetivo y mismo `data_cutoff`, pero con valores distintos. **Se excluyen todas** |
| 3 | `no_disponible` | `sin_marca_agua` | No hay marca de agua |
| | | `sin_consolidar` | El fin de la ventana es posterior al corte de consolidación |
| 4 | `incompleto_cuarentena` | `dia_sin_datos` | Alguna franja de la ventana cae en un día en que la ruta no tiene ninguna fila cargada |
| | | `franja_sin_medicion` | Alguna franja de la ventana tiene `pasajeros` NULL |
| 5 | `valido` | `cero_operativo` | Día cargado, sin franjas NULL, consolidada y **0 salidas**, con el origen en una franja **operativa** del horario. Demanda real = **0**, igual que `etiqueta_operativa` de comun-1.5 |
| | `sin_correspondencia` | `fuera_de_horario` | Lo mismo, pero con el origen fuera del horario de operación. En el FE no existe esa etiqueta |
| | `sin_correspondencia` | `sin_servicio` | Sin salidas y sin horario disponible para ese modelo: no se puede decidir si es 0 |
| 6 | `valido` | `ok` | Ventana medible con salidas. **Solo `ok` y `cero_operativo` entran en las métricas** |

**Cambio en m1-1.1 (19-nov):** la ventana operativa sin salidas ya no se excluye: vale 0.

- **Falta de dato:** un día sin cargar, una franja NULL (sin conteo usable o en cuarentena) o una ventana sin consolidar **nunca** se convierten en 0.
- **Horario:** es el `horario` guardado en el artefacto GBM del propio modelo (hash verificado, solo lectura). Para un modelo sin artefacto, como la línea base, se usa el del GBM activo, porque el horario es propiedad de la ruta y no del modelo. La respuesta dice cuál se usó en `parametros.horario_operacion`.
- **Tipo de día:** sale de `rc.calendario`, el mismo calendario del FE, con los festivos.
- **Ingenuo de 7 días:** aplica la misma regla.
- **Comprobado con datos reales** de abril-junio de 2025, en una copia desechable y en solo lectura:
  - 46.874 ventanas operativas válidas en el FE dan el **mismo valor** en M1, con una diferencia máxima de 0,0. Entre ellas están las 256 ventanas sin salidas, que antes quedaban excluidas.
  - Las 5.563 ventanas bloqueadas son `franja_sin_medicion` en M1.
  - No hay ninguna discrepancia de estado.

**Garantías:**
- **No se mezclan horizontes:** el objetivo incluye `horizonte_min`.
- **No se mezclan modelos ni versiones:** toda agregación lleva `model_id` + `model_version`, y una versión que no coincide con el registro se excluye.
- **No se usa información futura:**
  - del lado de la predicción, `data_cutoff` ≤ `franja_inicio`;
  - del lado del real, solo entra lo consolidado.
- **No hay duplicados:** queda una sola predicción por objetivo.
- **Reutilización de ejecuciones:** una ejecución que reutilizó predicciones (`resumen.reutiliza`) se resuelve a la ejecución dueña de las filas, igual que en P1. El campo `execution_id_datos` dice cuál se leyó.
- **Respaldo de GBM a línea base:** cuando una ruta cayó al baseline, sus filas llevan el `model_id` del baseline y se evalúan como baseline.

## 4. Métricas

Se calculan por `model_id`, `model_version` y la agrupación pedida, **solo con filas `valido`**:

| Campo | Definición | Caso borde |
|---|---|---|
| `mae` | media de \|y − ŷ\| | `null` si `n_validas` = 0 |
| `rmse` | raíz de la media de (y − ŷ)² | `null` si `n_validas` = 0 |
| `wape` | Σ\|y − ŷ\| / Σ\|y\| | `null` si Σy = 0 (se añade una advertencia) |
| `smape` | media de 2\|y − ŷ\| / (\|y\| + \|ŷ\|) | Si y = ŷ = 0, ese término vale 0 |
| `sesgo` | media de ŷ − y. Negativo indica que el modelo subestima | `null` si `n_validas` = 0 |
| `cobertura_intervalo` | fracción de filas con límite inferior ≤ y ≤ límite superior. **Es lo que pide A6.** Se compara con `nivel_intervalo_nominal` | `null` si `n_validas` = 0 |
| `mae_relativo_ingenuo_7d` | MAE del modelo / MAE del ingenuo de 7 días, en las **mismas** filas (`n_ingenuo_7d`) | `null` si no hay filas con ingenuo o si el MAE del ingenuo es 0 |
| `conteo_estados` | número de predicciones por estado en el grupo | — |

Reutiliza `app.ml.evaluacion.metricas`, que también es la base de las métricas del modelo.

**Sin observaciones válidas:** todas las métricas son `null`, el campo `advertencia` dice «sin observaciones válidas: no se calculan métricas» y los conteos explican por qué.

**MASE: no se calcula.** Su escala es el MAE del ingenuo en el **entrenamiento** de cada modelo, y `model_registry` no la guarda. Calcularla con datos de monitoreo cambiaría su definición y no sería comparable con el MASE del benchmark (`simetrica.escala_mase`). En su lugar se informa `mae_relativo_ingenuo_7d`, el MAE relativo de Hyndman y Koehler (2006), con escala del mismo periodo. Para tener MASE habría que guardar la escala de entrenamiento por ruta y horizonte en `model_registry.metricas`; esto queda pendiente.

## 5. Reproducibilidad

- Cada respuesta trae una `huella`: SHA-256 de las reglas, los parámetros (marca de agua, margen, corte) y cada par (`prediction_id`, estado, motivo, predicción, límites, real, ingenuo). **Misma huella significa mismo resultado.**
- La huella **cambia si cambia el real**, por ejemplo cuando una relectura corrige un día o una franja sale de cuarentena. Por eso se informa la marca de agua y el lote.
- Se calcula bajo demanda; no hay tabla nueva. Si más adelante se quiere fijar una evaluación (por ejemplo, para la decisión de activar un modelo en C2), haría falta una migración 0009 con una tabla `prediction_evaluation`, que necesita aprobación.

## 6. Contrato de salida

### `GET /api/v1/predictions/accuracy`

**Filtros:**
- `model_id`, `execution_id` (UUID);
- `idruta` ∈ {2, 16, 21};
- `horizonte_min` ∈ {30, 60, 120};
- `desde`, `hasta` sobre `franja_inicio`, en el intervalo [desde, hasta);
- `agrupar_por`, repetible, con valores en {`idruta`, `horizonte_min`, `execution_id`, `dia`, `hora`, `tipo_dia`}. Por defecto `idruta` + `horizonte_min`.

```json
{
 "parametros": {"reglas": "m1-1.1", "filtros": {...}, "marca_agua": "...", "lote_marca_agua": "...",
                "margen_consolidacion_dias": 3, "corte_consolidacion": "...", "fuente_real": "...",
                "agrupar_por": ["idruta", "horizonte_min"]},
 "huella": "<sha256>",
 "modelos": {"7": {"nombre": "...", "version_registro": "...", "estado": "activo"}},
 "conteo_estados": {"valido": 0, "no_disponible": 0, "incompleto_cuarentena": 0,
                    "sin_correspondencia": 0, "duplicado": 0, "inconsistente": 0},
 "conteo_motivos": {"ok": 0, "...": 0},
 "total": [ {"model_id": 7, "model_version": "...", <métricas>} ],
 "datos": [ {"model_id": 7, "model_version": "...", "idruta": 16, "horizonte_min": 60,
             "n_predicciones": 0, "n_validas": 0, "conteo_estados": {...}, "mae": 0, "rmse": 0,
             "wape": 0, "smape": 0, "sesgo": 0, "suma_real": 0, "suma_pred": 0,
             "cobertura_intervalo": 0, "nivel_intervalo_nominal": 0.8, "n_ingenuo_7d": 0,
             "mae_mismas_filas_ingenuo": 0, "mae_ingenuo_7d": 0, "mae_relativo_ingenuo_7d": 0,
             "advertencia": null} ]
}
```

### `GET /api/v1/predictions/accuracy/pairs`

**Filtros:** los mismos, más `estado`, `pagina` y `tamano` (máximo 500). Hay paginación estándar.

**Campos de cada fila:**
- `prediction_id`, `execution_id`, `model_id`, `model_version`, `feature_version`;
- `idruta`, `franja_inicio`, `fin_ventana`, `horizonte_min`, `data_cutoff`, `generated_at`;
- `demanda_pred`, `limite_inferior`, `limite_superior`;
- `demanda_real`, `error` (ŷ − y), `dentro_intervalo`, `demanda_ingenuo_7d`;
- `programaciones_ventana`, `estado`, `motivo`, `detalle_motivo`.

**Errores** con el formato uniforme:
- **422** por filtros inválidos o por más de 300.000 predicciones (`LIMITE_PREDICCIONES`);
- **404** si la ejecución no existe.

Los 10 endpoints existentes no cambian.

### Para alertas y tablero (sin conectarlas todavía)

| Consumidor | Cómo se alimenta |
|---|---|
| ALT-005 / ALT-007 | `comparacion_real.para_alt_005_007(pares)` devuelve las columnas que espera `motor.alt_005_007`, más los límites y `prediction_id`. Exige un solo `model_id`, el que se monitorea |
| ALT-010 | `comparacion_real.para_alt_010(reciente, referencia)` recibe dos salidas de `agregar(..., ('idruta','horizonte_min'))` del mismo modelo, un periodo reciente y uno de referencia, **ambos predicción-real**. Devuelve `idruta, horizonte_min, wape_reciente, wape_referencia` |
| A6 | `cobertura_intervalo` frente a `nivel_intervalo_nominal`, por ruta, horizonte u hora |
| Tablero | `accuracy` para las tarjetas y tablas; `accuracy/pairs` para la serie predicho contra real y la lista de exclusiones |

## 7. Pendientes

- Conectar ALT-005, ALT-007 y ALT-010 en `servicios.evaluar_alertas` (C1). Hay que decidir:
  - qué modelo se monitorea (el activo);
  - la ventana reciente y la de referencia de ALT-010;
  - si ALT-007 pasa a «real fuera del intervalo», como dice la especificación (§5.2).
- Correr M1 sobre la base de la demo. El periodo debe respetar B1 (monitoreo julio-septiembre de 2025). Octubre-diciembre es la prueba interna y no debe tocarse.
- MASE con escala de entrenamiento guardada en `model_registry`.
- Pico y valle: hoy se puede agrupar por `hora`. La marca pico/valle del equipo (`evaluacion.marcar_pico`) necesita los datos de entrenamiento del modelo.
- Tabla persistente de evaluaciones (0009), solo si C2 la necesita.
- `EXPLAIN` de las consultas y medición del p95 (R1–R3). Filtrar solo por `model_id` recorre toda `prediction`, porque no hay índice por `model_id`. Si el p95 lo exige, un índice `(model_id, franja_inicio)` sería una migración no destructiva (0009), que necesita aprobación.

## 8. ALT-005 y ALT-007 desde M1 (simulación, aprobado el 19-nov)

**Módulo:** `app/alertas/desde_m1.py`. **Estado:** solo simulación. No inserta alertas ni notificaciones, no está enganchado a `servicios.evaluar_alertas` y `modo="real"` responde 409.

| Decisión aprobada | Cómo se implementa |
|---|---|
| Solo el modelo activo, respetando el `model_id` y el respaldo | `lectura_real.ejecuciones_prediccion_de_modelo`: ejecuciones `prediccion` exitosas con `resumen.modelo_activo` (o `model_id`, en las anteriores al GBM) igual al activo. Las filas de respaldo conservan el `model_id` de la línea base, se evalúan aparte y salen con `origen_modelo = "respaldo"` |
| Horizonte de 60 minutos | `HORIZONTES_MONITOREO = (60,)` |
| Solo real consolidado, con margen de 3 días | El corte de M1 (marca − `DIAS_REVISION`) |
| Después de la ingesta | `evaluar_tras_ingesta` (contrato en el §9) |
| Trazabilidad sin migración | En `explicacion` se añade «[Trazabilidad: modelo, versión, origen, predicción, ejecución, umbral vN, M1 m1-1.1 huella]». Lo mismo va estructurado en `trazabilidad`. Con `alert.umbral_version` = versión vigente. `alert.execution_id` queda para la ejecución `alertas` del modo real |
| Regla y umbrales de ALT-005 sin cambios | `motor.alt_005_007` se usa tal cual, con `umbrales_vigentes`. Una prueba compara alerta por alerta contra la regla aplicada directamente |
| ALT-007 por intervalo, solo como propuesta | `propuesta_alt_007_intervalo`: se informa en `propuesta_alt_007` y **nunca** genera alertas (§10) |

**Deduplicación:** se usa `motor.deduplicar` contra `claves_alertas_abiertas`, con la misma clave que hoy (código, ruta, franja, horizonte).

**Idempotencia:** con las mismas marcas y los mismos datos, la salida es idéntica, incluida la huella de M1. Una ventana se evalúa en una sola ingesta: la primera en que queda consolidada.

## 9. Contrato con G4 (ejecución programada)

```python
from app.db import lectura_real as lr
from app.db.conexion import consultar_resultados
from app.alertas import desde_m1
from app import ingesta

with consultar_resultados() as c:            # 1) marca ANTES de la ingesta
    previa = lr.marca_agua(c)
marca_anterior = None if previa is None else previa["ultima_actualizacion"]
res = ingesta.correr()                       # 2) ingesta incremental (no cambia)
with consultar_resultados() as c:            # 3) alertas sobre lo que quedó consolidado, solo lectura
    sim = desde_m1.evaluar_tras_ingesta(c, marca_anterior, res["marca_agua"], modo="simulacion")
```

- **Entrada:** la marca de agua anterior y la nueva. La nueva debe ser la vigente en `watermark`; si no, la función responde 409 y no evalúa nada.
- **Ventana evaluada:** las ventanas con `fin_ventana` en (marca_anterior − 3 d, marca_nueva − 3 d]. Sin marca anterior, se toma todo lo consolidado. Si la marca no avanzó, el resultado es vacío.
- **Salida:** un diccionario con:
  - `simulacion`, `escrito`, `notificado`;
  - las marcas y los cortes;
  - el número de ejecuciones monitoreadas y cuántas cayeron al respaldo;
  - `conteo_estados_m1`, `candidatas`, `por_codigo`, `por_origen` y `nuevas_tras_deduplicar`;
  - `alertas` (con `trazabilidad`) y `propuesta_alt_007`.
- **Si `res["pendiente"]` es verdadero** (quedan ventanas de ingesta): se evalúa igual con la marca alcanzada. La siguiente ingesta sigue desde ahí sin repetir.
- **Para pasar al modo real hace falta aprobación.** Implicaría:
  - crear un `execution_log` con tipo `alertas` (el tipo ya existe en 0001);
  - `repo.insertar_alertas(eid, nuevas, versiones)`, que ya usa `ON CONFLICT` sobre la clave abierta;
  - `servicios.notificar`.

  Ninguno de estos pasos necesita una migración.
- **Dependencia de G4:** este bloque supone que G4 ejecuta `ingesta.correr()` (Mariangel, `app/ciclo.py`). G4 no existe todavía en este repositorio. Mientras no exista, la función se puede llamar a mano.

## 10. Propuesta para la definición definitiva de ALT-007

**Lo que dice la especificación v1.5:**
- **§5.2:** ALT-007 «Desviación del pronóstico», preventiva. Condición: «La demanda real supera el intervalo estimado por el modelo». Acción: «Revisar frecuencia y recalibrar escenario».
- **§5.1:** el parámetro «Error de pronóstico» es la «desviación entre demanda pronosticada y consolidada», con valor inicial «≥ 15 % o valor aprobado».

**Lo que hace la regla actual** (`motor.alt_005_007`, umbral `error_relativo = 0,35`): \|real − pred\| / max(pred, 1) > 35 % en cualquier dirección, excluyendo las caídas que ya son ALT-005. **No usa el intervalo.**

**Variantes, calculadas en `propuesta_alt_007` sin generar alertas:**

| Variante | Condición | Tasa esperada si el intervalo está calibrado | Comentario |
|---|---|---|---|
| A (literal) | real > `limite_superior` | ~10 % de las ventanas | Lo que dice §5.2. Las caídas quedan en ALT-005 |
| B | real fuera de [inferior, superior] | ~20 % | Por debajo se solapa con ALT-005 |
| A ∧ 35 % | real > superior **y** error relativo > `error_relativo` vigente | menor que A | Conserva el umbral versionado actual y filtra las ventanas de bajo volumen |

**Puntuación de anomalía propuesta:** (real − superior) / (superior − pred). Vale 0 en el borde y 1 cuando el exceso iguala el semiancho del intervalo.

**Recomendación:** variante **A ∧ umbral vigente**, activándola **después de A6**.
- Con un intervalo del 80 %, la variante A sola dispararía en torno al 10 % de las ventanas: unas 6 alertas diarias por ruta a 60 minutos. Eso es ruido operativo.
- La cobertura real del intervalo del modelo activo **no está medida**: 0,802 es de otro modelo (ver la revisión de A6).
- **No requiere migración:** usa el umbral `error_relativo` que ya existe y los límites de cada predicción.

**Decisión del equipo (19-nov-2026):**
- La variante futura de ALT-007 será **A ∧ umbral**: real > `limite_superior` **y** error relativo > umbral vigente.
- El umbral **se queda en 35 %**.
- **La regla activa no cambia todavía.**
- La activación queda pendiente de A6, G4 y una aprobación posterior.

## 11. Pruebas de integración que fallan sin datos restaurados

Son **12 fallas, que ya existían en `40aa79b` sin ningún cambio**. Se comprobó corriendo la misma suite en la base desechable con y sin los parches: son las mismas. Todas necesitan una base con datos restaurados (DECISIONES §15), no una vacía creada con migraciones.

| Prueba | Por qué falla en una base vacía |
|---|---|
| `test_dq_eventos::test_secuencia_no_baja_en_el_downgrade` | La secuencia de `data_quality_error` no tiene valor (`None`) sin filas previas |
| `test_dq_eventos::test_secuencia_atrasada_se_corrige_en_el_upgrade` | Lo mismo |
| `test_postgres::test_lee_la_base_local` | Exige `demanda_franja` con filas |
| `test_postgres::test_modelo_activo_predice` | Sin historia en `demanda_franja`, la línea base recibe `None` (`servicios.py:49`) |
| `test_trazabilidad::test_reproduce_a1_…` y otras 7 de `test_trazabilidad` | Predicen el 2025-12-03 con datos reales. Sin datos, la ejecución falla o no guarda filas, y las pruebas de error no se disparan. **Ojo:** ese día cae en octubre-diciembre (B1) |

**Las pruebas nuevas no dependen de datos restaurados:** `test_m1_postgres` y `test_alertas_desde_m1_postgres` siembran sus propios datos dentro de una transacción que se revierte.
