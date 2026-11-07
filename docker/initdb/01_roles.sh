#!/bin/bash
# Simula en local los permisos que exige la especificación:
#   - sobusa_lector: solo SELECT sobre las tablas fuente, transacciones de solo lectura.
#   - sobusa_escritor: dueño del esquema ai_frecuencias; no puede escribir en las tablas fuente.
# Cuando Extreme entregue sus usuarios, solo cambia el .env.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS postgis;

    CREATE ROLE sobusa_lector LOGIN PASSWORD '${LECTOR_PASSWORD}';
    ALTER ROLE sobusa_lector SET default_transaction_read_only = on;
    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO sobusa_lector;
    GRANT USAGE ON SCHEMA public TO sobusa_lector;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO sobusa_lector;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sobusa_lector;

    CREATE ROLE sobusa_escritor LOGIN PASSWORD '${ESCRITOR_PASSWORD}';
    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO sobusa_escritor;
    CREATE SCHEMA IF NOT EXISTS ai_frecuencias AUTHORIZATION sobusa_escritor;
    GRANT USAGE ON SCHEMA public TO sobusa_escritor;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO sobusa_escritor;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sobusa_escritor;
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;

    -- El lector también puede consultar ai_frecuencias (tablero y API de consulta)
    GRANT USAGE ON SCHEMA ai_frecuencias TO sobusa_lector;
    ALTER DEFAULT PRIVILEGES FOR ROLE sobusa_escritor IN SCHEMA ai_frecuencias
        GRANT SELECT ON TABLES TO sobusa_lector;
EOSQL
