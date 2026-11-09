# Etapa vacía con nombre: en modo "wheelhouse", docker compose la reemplaza por la carpeta
# WHEELHOUSE_DIR (additional_contexts). En modo "internet" no se usa. Ver docs/WHEELHOUSE.md.
# (Sin línea "# syntax=": obligaría a descargar el frontend de BuildKit, justo lo que se evita.)
FROM scratch AS wheelhouse

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Usuario sin privilegios (SonarQube marca contenedores que corren como root)
RUN useradd --create-home --uid 10001 sobusa

COPY pyproject.toml ./
COPY app ./app
# Dos formas de instalar las dependencias (docs/WHEELHOUSE.md):
#   PIP_MODO=internet   (predeterminado) pip descarga de PyPI, como siempre.
#   PIP_MODO=wheelhouse sin red: solo desde la carpeta preparada y verificada (wheelhouse/).
ARG PIP_MODO=internet
RUN case "$PIP_MODO" in internet|wheelhouse) ;; \
      *) echo "PIP_MODO debe ser 'internet' o 'wheelhouse' (recibido: '$PIP_MODO')" >&2; exit 1 ;; esac

# Modo internet. Sin "pip install --upgrade pip": el pip de la imagen base basta y así cada
# construcción usa la misma versión. El caché de pip (BuildKit) queda fuera de la imagen.
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$PIP_MODO" = "internet" ]; then \
      PIP_DEFAULT_TIMEOUT=600 PIP_RETRIES=10 python -m pip install ".[dev]"; \
    fi

# Modo wheelhouse: --network=none garantiza que nada se descarga. Primero las herramientas de
# construcción fijadas, se comprueba que quedaron instaladas, y luego el proyecto sin aislamiento.
COPY docker/requisitos-congelados.txt docker/requisitos-construccion.txt docker/instalar_wheelhouse.sh \
     scripts/wheelhouse_comun.py scripts/verificar_wheelhouse.py /opt/wheelhouse-listas/
RUN --network=none --mount=type=bind,from=wheelhouse,target=/wheelhouse \
    if [ "$PIP_MODO" = "wheelhouse" ]; then \
      sh /opt/wheelhouse-listas/instalar_wheelhouse.sh /wheelhouse /opt/wheelhouse-listas; \
    fi

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

