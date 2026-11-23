# SOBUSA · Pronóstico, frecuencias y alertas

La edición de demostración contiene la API FastAPI, las migraciones, la lógica de calidad, optimización, alertas y el dashboard. Su perfil de instalación nueva utiliza una semilla **sintética de demostración**, con capacidades y factores ficticios. Los resultados obtenidos con esa semilla sirven para verificar funcionalidad y contratos. Las evaluaciones históricas de operación se conservan aparte.

| Componente | Dónde empezar |
| --- | --- |
| API, registro de modelos y persistencia | [app/api](app/api), [app/ml](app/ml) y [migraciones](migrations) |
| Dashboard empresarial | [dashboard/tablero.py](dashboard/tablero.py) |
| Recomendaciones y alertas | [optimización](app/optimizacion) y [estado de las alertas](docs/ALERTAS.md) |
| Análisis exploratorio y gráficos | [EDA y visualizaciones sintéticas](eda/README.md) |
| Modelo original SOBUSA v1.0 | [Instalación y ejemplos de Python, FastAPI y Jupyter](README_INFERENCIA.md) |

El modelo original combina la media de cinco semillas RRAP v4 con la media de cinco semillas LightGBM L3, con pesos fijos de 50/50. Cubre las rutas 2, 16 y 21 y ventanas de 30, 60 y 120 minutos. La API empresarial conserva su registro y `gbm_fe31` cuando ese modelo está activo. El candidato queda deshabilitado por defecto y requiere selección explícita. Para ejecutar el candidato con los pesos congelados hay que suministrar el motor y el paquete privado autorizado.

## Instalación y pruebas sin PostgreSQL

Use Python 3.12 x64 en Windows. En PowerShell, desde la raíz:

```powershell
py -3.12 -m venv .venv
& ./.venv/Scripts/python.exe -m pip install --only-binary=:all: --require-hashes -r dependencias/backend-win-amd64-py312.lock
& ./.venv/Scripts/python.exe -m pip check
$env:PYTHON_DOTENV_DISABLED = '1'
& ./.venv/Scripts/python.exe -B -m pytest -q -rs -p no:cacheprovider tests/unit tests/api
```

Ejecute las pruebas en una sesión sin DSN empresariales. Las pruebas de API usan ASGI, dobles de repositorio y viajes sintéticos. Las pruebas de PostgreSQL y la evaluación con archivos del modelo empresarial requieren un entorno separado.

## API local

```powershell
& ./.venv/Scripts/python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Consulte `/salud` y `/docs`. Las operaciones sobre predicciones persistidas, recomendaciones, alertas y registro requieren PostgreSQL. `/salud` verifica el proceso HTTP.

Con la API levantada, ejecute el dashboard existente desde otra consola. El tablero sigue conectado a los endpoints empresariales. La API candidata se usa por separado siguiendo su guía.

```powershell
& ./.venv/Scripts/python.exe -m pip install "streamlit>=1.30"
$env:API_URL = 'http://127.0.0.1:8000'
& ./.venv/Scripts/python.exe -m streamlit run dashboard/tablero.py
```

Para ensayar con una base desechable utilice la plantilla `.env.example` y configure localmente secretos únicos y DSN de esa base. Si se usa Docker Compose, el backend dentro del contenedor debe recibir el DSN de escritura de su base desechable mediante `DATABASE_URL_WRITE_DOCKER`. Las migraciones de una instalación nueva cargan parámetros sintéticos identificados por `capacidad_fuente` y `factor_carga_fuente`. Revise la configuración por defecto de publicación de puertos de Compose antes de ejecutarlo. Los parámetros de una base empresarial existente requieren su procedimiento de despliegue autorizado.

## Insumos empresariales autorizados

- El registro conserva el modelo activo y la línea base. Para usar un `gbm_fe31` específico, obtenga el par JSON/pickle de un canal privado autorizado, verifique su procedencia y SHA-256 y configure `MODELOS_DIR`. Aplique las operaciones de registro y activación solo en la base autorizada. Los artefactos entrenados se suministran fuera de Git.
- Para derivar factores de carga con insumos autorizados, `derivar_factor_carga` acepta un archivo explícito. El reporte por vehículo y las extracciones se suministran de forma local a sus scripts. Las pruebas traen un CSV sintético con su propia semilla.
- Los pesos de `gbm_fe31`, el motor RRAP y el procesamiento privado del candidato, sus diez pesos, el horario, el manifiesto y las extracciones históricas se suministran fuera de Git. El modo sintético del candidato necesita el mismo motor y los mismos modelos congelados. La validación retrospectiva requiere también el histórico y su referencia archivada.
- Las operaciones nuevas con datos reales requieren acreditar disponibilidad al corte. El candidato entrega puntos y simulaciones de frecuencia a 60 minutos. No dispone de intervalos calibrados.
