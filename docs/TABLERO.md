# Tablero SOBUSA · Frecuencias de despacho

Tablero Streamlit que **consume la API `/api/v1`** y presenta sus resultados con la identidad visual
de la maqueta v2 (`maqueta_sobusa_tablero_v2.html`).

```
Usuario → Streamlit (dashboard/) → API FastAPI → servicios y repositorios → PostgreSQL
```

El tablero no se conecta a PostgreSQL, no ejecuta SQL y no recalcula predicciones,
recomendaciones, métricas ni SHAP.

## Ejecución local

```bash
pip install -e ".[tablero]"                       # streamlit>=1.30 y requests (pyproject.toml)
export API_URL=http://localhost:8000/api/v1       # opcional; este es el valor por defecto
streamlit run dashboard/tablero.py
```

Requiere la API en marcha. Funciona con Streamlit 1.30 o superior (usa `st.container(border=True)`,
`st.date_input(value=None)` y `st.rerun`).

## Módulos

| Archivo | Qué hace | Usa Streamlit |
|---|---|---|
| `dashboard/tablero.py` | Navegación, secciones, formularios y estado de la sesión | Sí |
| `dashboard/cliente.py` | Solicitudes HTTP: tiempos de espera, errores uniformes, paginación e identificadores P1 | No |
| `dashboard/exportacion.py` | Lectura completa paginada y verificada, y archivos CSV/XLSX (RF-017) | No |
| `dashboard/estilo.py` | Paleta, CSS y fragmentos HTML de la maqueta; escapa todo texto de la API | No |
| `dashboard/graficos.py` | Gráficas Vega-Lite (`st.vega_lite_chart`, incluido en Streamlit) | No |
| `dashboard/explicacion.py` | RF-015: valida la explicación TreeSHAP y separa las dos importancias globales | No |

Los módulos sin Streamlit se prueban sin interfaz. `tablero.py` se prueba ejecutando el guion
completo con un Streamlit falso estricto y solicitudes HTTP simuladas.

## Secciones y endpoints

| Sección | Endpoint | Uso |
|---|---|---|
| Todas (cabecera) | `GET /models` | Estado de la API y modelo activo |
| Demanda y frecuencias | `POST /frequency/executions` | Crear una ejecución (con confirmación) |
| | `POST /recommendations/calculate` | Recalcular sobre una ejecución de predicción identificada |
| | `GET /frequency/executions/{id}` | Trazabilidad; resolver un id escrito a mano |
| | `GET /predictions` | Gráfica del día (lectura completa) y tabla paginada |
| | `GET /recommendations` | Gráfica, decisión operativa y tabla paginada (filtro «solo no viables») |
| Alertas | `GET /alerts` | Conteos por estado (`paginacion.total`), bandeja paginada y exportación |
| | `PATCH /alerts/{id}/status` | Gestión con validación y confirmación |
| Calidad de datos | `GET /quality/errors` | Errores G3 por lote, paginados, con exportación |
| Evaluación (M1) | `GET /predictions/accuracy` | Métricas por modelo y grupo, mapa ruta × horizonte |
| | `GET /predictions/accuracy/pairs` | Pares con estado y motivo, paginados, con exportación |
| Modelos y explicaciones | `GET /models` | Registro y ficha técnica (sin `artefacto_ruta`) |
| | `POST /models/{id}/activate` | Activación con confirmación; la reversión es activar de nuevo el anterior |
| | `GET /predictions/{id}/explanation` | Explicación TreeSHAP verificada (RF-015) y exportación de aportes |
| Resumen | `GET /alerts`, `GET /models` | Alertas por estado y por ruta; modelo activo |

## Reglas de funcionamiento

### Ejecuciones (P1, DECISIONES.md §15)

- Las predicciones se consultan con `execution_id_predicciones`, que identifica la ejecución dueña
  de las filas, y no con el id de la solicitud.
- Las recomendaciones se consultan con el id de la ejecución de frecuencias o de recomendación.
- Si en un día hay varias ejecuciones, se muestra una sola a la vez, para no mezclar modelos ni
  cortes de datos. Para las ejecuciones encontradas en los datos, la dueña de las predicciones se
  deduce por `prediction_id`.

### Paginación

- El número de página es un widget cuya clave incluye una firma de los filtros: si los filtros
  cambian, se vuelve a la página 1.
- Se informan el total, el rango de filas y el número de páginas.
- Una página que no existe se avisa; no se muestra como una tabla vacía.

### Caché

- Los resultados se guardan en la sesión mientras no cambie una «generación» de datos. Los errores
  no se guardan.
- El botón «Actualizar datos» y cualquier operación que escribe aumentan la generación, así que nada
  de lo anterior se reutiliza.

### Operaciones que escriben

- Siempre piden una casilla de confirmación y validan los campos obligatorios antes de enviar.
- **Tres resultados posibles:**
  - **Confirmada:** respuesta 2xx con los campos que prueban el resultado.
    - Frecuencias: ids de la ejecución, de la solicitud y de la dueña de las predicciones, y los
      conteos. Una ejecución `fallida` también es un resultado confirmado.
    - Recomendaciones: `execution_id` y los conteos.
    - Activación: el mismo `model_id` pedido y `modelo_anterior`.
    - Alerta: la misma alerta con el estado pedido.
  - **Fallida:** la API respondió con su error uniforme (con `correlation_id`) y revirtió la
    transacción. Se muestra «La operación falló» y se puede reintentar.
  - **No confirmada:** timeout, conexión perdida, 2xx ilegible o sin esos campos, 502/503/504, o un
    error sin el formato de la API (viene de un intermediario). Se muestra «Resultado no confirmado».
    La operación queda bloqueada hasta que el usuario indique que revisó el estado, porque pudo
    haberse aplicado.
- **Bloqueos:** son por operación (frecuencias, recomendaciones, activación) y, en las alertas, por
  alerta.
- **Tras una operación confirmada** de frecuencias, recomendaciones o activación, el resultado se
  guarda y el guion se vuelve a ejecutar (`st.rerun`), para que los listados se recarguen. El PATCH
  de alertas muestra su resultado en el momento; la bandeja se vuelve a consultar en la siguiente
  interacción.

### Modelos activables

- Se ofrecen los modelos `candidato` y `retirado`. No se ofrecen el `activo` ni los `rechazado`.
- Reactivar un `retirado` es una reversión: exige una segunda confirmación.
- El backend (`servicios.verificar_activable`) solo comprueba el artefacto de un GBM y aceptaría
  activar un modelo rechazado. La restricción la pone el tablero.

### Errores

- Se distinguen conexión, timeout, 404, 409, 422, 500, JSON inválido y respuesta sin los campos
  esperados.
- Se muestran el mensaje y el `correlation_id` del formato uniforme, sin la URL ni trazas.
- Un error nunca se convierte en una tabla vacía. Una página distinta de la pedida también es un
  error.

### Seguridad

- **Texto de la API:**
  - en fragmentos HTML se escapa con `html.escape`;
  - en mensajes (`st.error`, `st.warning`, `st.caption`, que interpretan Markdown) se neutraliza con
    `estilo.md`: no puede convertirse en enlace, imagen, énfasis ni fórmula;
  - la explicación y la recomendación de una alerta se muestran con `st.text`.
- **Identificadores escritos por el usuario:** los ids de ejecución deben ser UUID antes de usarse
  en una URL o en un filtro.
- **Nombres de archivo:** se limpian; solo quedan letras, dígitos, `_`, `.` y `-`.
- **Exportaciones:** neutralizan las fórmulas (ver abajo).

### Faltantes

`None` y NaN se muestran como «Sin dato» o como celda vacía, nunca como 0.

## Exportaciones (RF-017)

| Conjunto | Endpoint | Identificador | Notas |
|---|---|---|---|
| `predicciones` | `GET /predictions` | `prediction_id` | Todas las columnas de `prediction` |
| `recomendaciones` | `GET /recommendations` | `recommendation_id` | `es_viable` viaja como `true`/`false` |
| `alertas` | `GET /alerts` | `alert_id` | Sin `usuario_asignado` ni `clave_dedup` |
| `errores_calidad` | `GET /quality/errors` | `deteccion_id` | `lote` obligatorio; `alcance` validado |
| `pares_m1` | `GET /predictions/accuracy/pairs` | `prediction_id` | Con `estado`, `motivo` y `detalle_motivo` |
| Explicación (RF-015) | `GET /predictions/{id}/explanation` | variable | Una fila por variable; la predicción y el modelo van en los metadatos |

**Se conservan todas las garantías de la exportación de alertas:**

- lectura de todas las páginas (tamaño 500);
- límite de 10.000 filas;
- validación de filtros;
- verificación del total, del tamaño de cada página, de los identificadores duplicados y de la
  primera página al cerrar;
- ningún archivo parcial ante un error;
- neutralización de fórmulas;
- faltantes como celda vacía y rechazo de valores no finitos;
- metadatos en el XLSX y aviso de que la exportación no es una instantánea transaccional;
- invalidación del archivo preparado si cambian los filtros o los datos.

Para M1 se añade una comprobación más: la `huella` de la evaluación debe ser la misma en todas las
páginas.

## Adaptación de la maqueta v2

| Elemento de la maqueta | En Streamlit | Motivo |
|---|---|---|
| Paleta, tipografía, barra lateral verde, logo | CSS inyectado (`estilo.CSS`) y radio de navegación estilizado | Los selectores internos de Streamlit (`data-testid`) pueden cambiar entre versiones: el estilo es de mejor esfuerzo |
| Cabecera con píldoras (API, modelo activo) | Fragmento HTML con datos de `GET /models` | La píldora «Datos hasta» se muestra en el contexto de Demanda, desde el `data_cutoff` de las predicciones elegidas |
| Tarjetas con punto, título, subtítulo y fuente | `st.container(border=True)` y encabezado HTML | Los widgets no pueden ir dentro de un `<div>` propio |
| Indicadores, listas, píldoras, chips con forma | Fragmentos HTML (`estilo.kpis`, `lista`, `leyenda`, `chip`) | Igual que la maqueta; el estado se indica con texto y forma, no solo con color |
| Gráficas de demanda, frecuencia, anillos, mapa de calor, barras | Vega-Lite con los colores de la maqueta | El tooltip y la guía vertical de la maqueta se sustituyen por el tooltip de Vega-Lite |
| Tablas con selección de fila al hacer clic | `st.dataframe` y un selector de fila | La selección por clic exige Streamlit 1.35; el requisito es 1.30 |
| Pestañas 01/02 de Demanda | Radio horizontal | Así solo se dibuja la vista elegida, sin repetir consultas |
| Formulario de alerta con «Asignar a» condicional | Campo siempre visible, con ayuda | Dentro de `st.form` los campos no reaccionan antes de enviar |
| «Exportar recomendaciones/predicciones · pendiente P1» | Exportaciones activas | P1 ya está implementado (DECISIONES.md §15) |

**Resumen.** En la maqueta, el desempeño y la comparación con referencias salen de archivos CSV
(`comparacion.csv`, `metricas.csv`). Se reemplazan por la ficha del modelo activo y un acceso a
«Evaluación (M1)», por dos razones:

- el tablero solo consume la API;
- la cobertura registrada (0,802) proviene de un modelo de evaluación distinto del artefacto activo.

Los conteos de alertas sí salen de la API.

**Modelo.** El mapa de calor, la cobertura y la comparación de la maqueta salían de CSV.

- El mapa ruta × horizonte se dibuja con `GET /predictions/accuracy`, en «Evaluación (M1)».
- La importancia global se muestra la del artefacto, que llega con cada explicación.
- La comparación con líneas base no tiene un endpoint: se omite.

**Catálogo de alertas.** No hay endpoint para él, y copiarlo en el tablero lo dejaría desactualizado
(por ejemplo, el estado «conectada / sin conectar»). Se omite.

**Datos ficticios de la maqueta.** No se usan: todo valor sale de la API o se muestra «Sin dato».

## Explicación del modelo (RF-015)

Integrada de forma selectiva desde el commit `646ce5b` (rama `feature/rf015-tablero-explicacion`).
Solo se trajo la explicación del modelo: ni RF-006 ni la lectura paginada propia de esa rama.
Consume el backend que ya está en `main`.

Tres explicaciones, que el tablero muestra por separado:

1. **Explicación individual TreeSHAP**, en «Modelos y explicaciones». Consulta
   `GET /predictions/{id}/explanation` solo al pulsar «Solicitar explicación» o «Explicar esta
   predicción», y muestra lo siguiente:
   - las 29 variables, de mayor a menor aporte, con su signo, su factor multiplicativo y una
     interpretación;
   - la referencia y la predicción reconstruida;
   - las verificaciones de la API;
   - las descargas CSV/XLSX.
2. **Importancia global**, con dos medidas que no se mezclan:
   - por **permutación**: `metricas.importancia_top10` de `GET /models`, calculada con el modelo de
     evaluación. Aparece en la ficha técnica;
   - por **ganancia**: `importancia_global_artefacto` del artefacto de producción. Aparece con la
     explicación individual.
3. **Regla operativa**, en «Demanda y frecuencias › 02 · Decisión operativa». Explica los buses y el
   headway. TreeSHAP explica la demanda, no los buses, y el tablero lo dice en las dos secciones.

`explicacion.validar_explicacion` descarta la respuesta completa, sin mostrar ninguna parte, en
estos casos:

- el estado no es `reconstruida_y_verificada`;
- `prediction_id` no coincide con el pedido;
- `model_id`, `model_version` o `execution_id` no coinciden con los de la predicción seleccionada;
- no hay exactamente 29 aportes con variables únicas y valores finitos;
- falla `referencia + suma de aportes = predicción` en escala log;
- `factor` no es `exp(aporte)` o los valores en pasajeros no son `exp(log)`;
- faltan las verificaciones o la importancia global;
- la identidad TreeSHAP está fuera de su tolerancia.

Más casos:

- Si la predicción es de un modelo de línea base según `GET /models`, no se llama a la API.
- Una respuesta 404, 409, 5xx o un fallo de red se informa sin interrumpir el resto de la sección.
- Si el id se escribe a mano, el tablero no tiene la fila de esa predicción, porque la API no
  consulta predicciones por id. Valida todo lo demás y advierte que no pudo contrastar la ejecución
  ni el modelo. Si la predicción se elige en la tabla o desde la decisión, la comprobación es
  completa.

## Limitaciones que dependen del backend

- `GET /quality/errors` exige el lote y no hay endpoint para listar lotes: se escribe a mano.
- `GET /predictions` no filtra por `model_id` (la API no expone ese filtro). Por eso el tablero
  separa por ejecución.
- `GET /alerts` con `desde`/`hasta` excluye las alertas con `franja_inicio` nulo (lo advierte la
  exportación).
- M1 recalcula la evaluación en cada página de pares. La exportación verifica la `huella`, pero la
  lectura no es transaccional.
- `GET /models` devuelve `artefacto_ruta`. El tablero lo oculta, pero la API lo sigue exponiendo.
- `GET /predictions/accuracy` y `/pairs` no declaran sus respuestas de error en OpenAPI, aunque
  pueden responder 404 o 422.
- No hay un endpoint de historial de una alerta. El historial aparece en la respuesta del PATCH.
- No hay `GET /predictions/{id}`. Una explicación pedida con un id escrito a mano no se puede
  contrastar con la ejecución ni el modelo de su predicción.

## Pruebas

```bash
pytest tests/unit/test_tablero.py tests/unit/test_exportacion.py \
       tests/unit/test_cliente_tablero.py tests/unit/test_tablero_componentes.py \
       tests/unit/test_explicacion_modelo.py
```

No usan red, base de datos, credenciales ni `.env`.

## Aspecto visual

- Tema claro fijo en `.streamlit/config.toml` (ejecutar `streamlit run` desde la raíz del repositorio).
- Tipografía Geist (Google Fonts); si no hay internet, cae a la fuente del sistema sin romper nada.
- La barra lateral tiene iconos por sección, los selectores horizontales se ven como control segmentado y
  los campos, botones, avisos y desplegables siguen la paleta de la maqueta.
- El endpoint de origen de cada tarjeta («GET /alerts», etc.) queda oculto por defecto; se muestra con
  «Mostrar origen técnico» en la barra lateral (útil para la sustentación técnica).
- La cabecera muestra la fecha de hoy y una línea que explica la sección; el pie lateral, la hora de la
  última consulta a la API. Ningún valor de datos es inventado: lo que no llega de la API sigue como «Sin dato».
