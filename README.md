# SOBUSA · API de frecuencias para el Data Challenge

La edición de demostración contiene la API FastAPI, las migraciones, la lógica de calidad, optimización, alertas y el dashboard. Su perfil de instalación nueva utiliza una semilla **sintética de demostración**, con capacidades y factores ficticios. Los resultados obtenidos con esa semilla sirven para verificar funcionalidad y contratos. Las evaluaciones históricas de operación se conservan aparte.

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

Para ensayar con una base desechable utilice la plantilla `.env.example` y configure localmente secretos únicos y DSN de esa base. Si se usa Docker Compose, el backend dentro del contenedor debe recibir el DSN de escritura de su base desechable mediante `DATABASE_URL_WRITE_DOCKER`. Las migraciones de una instalación nueva cargan parámetros sintéticos identificados por `capacidad_fuente` y `factor_carga_fuente`. Revise la configuración por defecto de publicación de puertos de Compose antes de ejecutarlo. Los parámetros de una base empresarial existente requieren su procedimiento de despliegue autorizado.

## Insumos empresariales autorizados

- El registro conserva el modelo activo y la línea base. Para usar un `gbm_fe31` específico, obtenga el par JSON/pickle de un canal privado autorizado, verifique su procedencia y SHA-256 y configure `MODELOS_DIR`. Aplique las operaciones de registro y activación solo en la base autorizada. Los artefactos entrenados se suministran fuera de Git.
- Para derivar factores de carga con insumos autorizados, `derivar_factor_carga` acepta un archivo explícito. El reporte por vehículo y las extracciones se suministran de forma local a sus scripts. Las pruebas traen un CSV sintético con su propia semilla.
- Los datos, el histórico y los pesos del ensamble RRAP v4 + LightGBM L3 permanecen separados. Esta copia revisa el saneamiento del backend empresarial. El candidato se tratará en un cambio independiente una vez acreditados los permisos.
