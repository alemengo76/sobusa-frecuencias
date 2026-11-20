# Sobusa · Frecuencias de despacho (Data Challenge 2026)

Prototipo que lee las tablas de Extreme en modo solo lectura, predice la demanda por ruta y
franja (30, 60 y 120 min), recomienda frecuencias, genera alertas explicables y lo expone en
una API REST (`/api/v1`) y un tablero.

Rutas: 2 (SILENCIO), 16 (CRA 54) y 21 (CALLE 72). Las decisiones y supuestos están en
[`DECISIONES.md`](DECISIONES.md).

## Arranque rápido

```bash
cp .env.example .env              # y cambiar las contraseñas
docker compose up --build         # Postgres 14 + PostGIS y la API, con migraciones
# Documentación OpenAPI: http://localhost:8000/docs
```

Para trabajar con datos reales en local:

```bash
pip install -e ".[dev]"
# 1. Cargar el extracto del equipo en el Postgres local (simula las tablas de Extreme)
python -m scripts.cargar_extracto_local --base <carpeta del proyecto> --extracciones <carpetas raw>
# 2. Lectura incremental (llena ai_frecuencias.demanda_franja y la cuarentena)
DATABASE_URL_READ=... DATABASE_URL_WRITE=... python -m app.ingesta
# 3. Calcular frecuencias de un día
curl -X POST localhost:8000/api/v1/frequency/executions -H "Content-Type: application/json" \
     -d '{"dia": "2025-10-01", "idrutas": [2, 16, 21]}'
# 4. Tablero (consume la API en API_URL; por defecto http://localhost:8000/api/v1)
pip install -e ".[tablero]" && streamlit run dashboard/tablero.py
```

El tablero (secciones, endpoints, exportaciones y adaptación de la maqueta v2) está documentado en
`docs/TABLERO.md`.

Cuando Extreme entregue los usuarios, solo cambian `DATABASE_URL_READ` y `DATABASE_URL_WRITE`.

## Pruebas

```bash
pytest                 # unitarias, de ML (fuga y no regresión), de calidad y de la API (con mocks)
pytest -s tests/unit/test_ingesta.py   # imprime tiempo y memoria de 100.000 registros
```

## Estructura

| Carpeta | Contenido | Entregable |
|---|---|---|
| `app/ml/` | Variables D+1, líneas base, método Extreme, gradient boosting, evaluación, contrato | E05 |
| `app/optimizacion/` | Fórmula de frecuencias con factor de carga y "no viable" | E06 |
| `app/alertas/` | ALT-001 a ALT-010, deduplicación, transiciones de estado | E06 |
| `app/calidad/` | Capacidad completada y reglas de cuarentena | E07 |
| `app/ingesta.py` | Lectura incremental con marca de agua, idempotente | E07 |
| `app/api/` | Los 10 endpoints, errores uniformes, Correlation-Id, logs JSON | E06, E10 |
| `migrations/` | Alembic + DDL de `ai_frecuencias` + datos semilla | E04 |
| `docker/`, `Dockerfile`, `docker-compose.yml` | Contenedores y roles lector/escritor | E09 |
| `scripts/` | Carga del extracto, entrenamiento y evaluación, capacidad y flota | E05, E07 |
| `docs/` | Consultas de verificación (E07) y correcciones al feature engineering | E02, E07 |
| `tests/` | Pruebas | E08 |

## Integración del 6 de noviembre (versión aislada para revisión)

- El modelo de producción es el **gradient boosting FE 3.1 / comun-1.5** (`app/ml/modelo_gbm.py`),
  entrenado con `scripts/entrenar_gbm_fe31.py` y servido por la API; las tres líneas base quedan de
  respaldo automático.
- La ingesta aplica las reglas **comun-1.5** (las del FE) y copia los viajes a `ai_frecuencias.viaje_fuente`.
- Las recomendaciones usan el **factor de carga medido** (`param_v2`, por ruta y tipo de día).

Explicación, comparación con las líneas base, pruebas, pasos desde cero y pendientes:
[`docs/INTEGRACION_GBM.md`](docs/INTEGRACION_GBM.md).

## Estado (2 de noviembre de 2026)

**Probado:**
- 51 pruebas de lógica: frecuencias, alertas, variables sin fuga, líneas base, intervalos,
  métricas, contrato, capacidad, cuarentena, idempotencia de la ingesta y gradient boosting.
- El DDL y la semilla en PostgreSQL 16: roles (el lector no escribe; el escritor no toca las
  tablas de Extreme), deduplicación de alertas, un solo modelo activo, rechazo de capacidad 0,
  de límites invertidos y de "no viable" sin motivo.
- `scripts/entrenar_y_evaluar.py` completo con datos simulados.

**Escrito pero sin ejecutar** (el entorno donde se construyó no tenía acceso a internet para
instalar FastAPI, SQLAlchemy, Alembic ni Docker):
- La API (`app/api`, `app/servicios.py`, `app/db`) y sus pruebas con mocks (`tests/api`).
- `docker compose up`, Alembic, `app/ingesta.py` contra la base, `scripts/cargar_extracto_local.py`
  y el tablero.

Lo primero que hay que hacer es `pip install -e ".[dev]" && pytest` y luego `docker compose up`,
y corregir lo que falle.

**Valores provisionales** (ver `DECISIONES.md`): factor de carga 0,40; ciclo de las rutas 2 y 21;
tope de flota de las rutas 2 y 21; bandas de intervalo por defecto mientras no se carguen las de
validación.

**Pendiente:** reentrenamiento automático con decisión de activar o rechazar (la activación y
reversión ya existen en `POST /models/{id}/activate`), alertas ALT-005 a ALT-010 conectadas a la
ejecución diaria (las reglas ya están y tienen pruebas), pruebas de carga con Locust, contrato
OpenAPI con Schemathesis, SonarQube.
