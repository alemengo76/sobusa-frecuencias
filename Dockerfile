FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Usuario sin privilegios (SonarQube marca contenedores que corren como root)
RUN useradd --create-home --uid 10001 sobusa

COPY pyproject.toml ./
COPY app ./app
RUN python -m pip install --upgrade pip
RUN PIP_DEFAULT_TIMEOUT=600 python -m pip install --no-cache-dir ".[dev]"

COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts
COPY tests ./tests
# factor_carga medido (param_v2): lo usan las pruebas que verifican la migración 0003
COPY datos/referencia ./datos/referencia
# Artefactos de modelos: en docker-compose es un volumen (./modelos). El GBM se entrena DENTRO
# del contenedor para que la versión de scikit-learn sea la misma con la que la API lo carga.
RUN mkdir -p /app/modelos /app/salidas_modelo_fe31 && chown sobusa /app/modelos /app/salidas_modelo_fe31
ENV MODELOS_DIR=/app/modelos

USER sobusa
EXPOSE 8000
# Aplica migraciones y arranca la API
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.api.main:app --host 0.0.0.0 --port 8000"]

