#!/bin/bash
# Prepara la base de la suite de alertas que CONFIRMA datos (tests/integracion/test_alertas_ciclo_postgres.py)
# en una instancia PostgreSQL EXCLUSIVA para esa suite: local por socket Unix, sin otras bases de usuario.
# Datos 100 % sintéticos. No arranca la aplicación ni Compose; aplica las migraciones con Alembic.
#
# Uso: preparar_bd_suite_alertas.sh <repo> <directorio_del_socket> <puerto> <bd> <venv>
#
# Aborta, ANTES de crear roles, permisos, esquema o datos, si:
#   - el nombre no es sobusa_desechable_suite_* (y nunca acepta sobusa_desechable_db);
#   - la conexión no es por socket local (el segundo argumento debe ser una ruta absoluta);
#   - la base destino ya existe (no se borra ni se reutiliza: elija otro nombre o una instancia nueva);
#   - la instancia tiene cualquier otra base de usuario (no es exclusiva).
# No contiene DROP: nunca elimina una base, un rol ni un objeto existente.
set -euo pipefail
if [ "$#" -ne 5 ]; then
  echo "uso: $0 <repo> <directorio_del_socket> <puerto> <bd> <venv>" >&2; exit 2
fi
REPO=$1; SOCK=$2; PORT=$3; BD=$4; VENV=$5
case "$BD" in
  sobusa_desechable_db) echo "ABORTA: '$BD' es la base desechable del equipo; esta suite exige una instancia propia" >&2; exit 1;;
  sobusa_desechable_suite_*) ;;
  *) echo "ABORTA: nombre de base no permitido '$BD' (se exige sobusa_desechable_suite_*)" >&2; exit 1;;
esac
case "$SOCK" in
  /*) ;;
  *) echo "ABORTA: '$SOCK' no es un directorio de socket local (ruta absoluta)" >&2; exit 1;;
esac
P="psql -h $SOCK -p $PORT -U postgres -v ON_ERROR_STOP=1 -X -q -At"

# ---- comprobaciones de solo lectura: nada cambia si alguna falla
existe=$($P -d postgres -c "SELECT count(*) FROM pg_database WHERE datname = '$BD'")
if [ "$existe" != "0" ]; then
  echo "ABORTA: la base '$BD' ya existe; no se modifica ni se borra" >&2; exit 1
fi
otras=$($P -d postgres -c "SELECT string_agg(datname, ', ') FROM pg_database
                           WHERE datname NOT IN ('postgres', 'template0', 'template1')")
if [ -n "$otras" ]; then
  echo "ABORTA: la instancia no es exclusiva de esta suite (tiene: $otras)" >&2; exit 1
fi

# ---- a partir de aquí la instancia está vacía y la base no existe
$P -d postgres -c "CREATE DATABASE $BD"
$P -d postgres -c "COMMENT ON DATABASE $BD IS 'sobusa_suite_alertas: instancia dedicada, datos sintéticos'"
$P -d "$BD" <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sobusa_lector') THEN CREATE ROLE sobusa_lector LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sobusa_escritor') THEN CREATE ROLE sobusa_escritor LOGIN; END IF;
END \$\$;
ALTER ROLE sobusa_lector SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE $BD TO sobusa_lector, sobusa_escritor;
GRANT USAGE ON SCHEMA public TO sobusa_lector, sobusa_escritor;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sobusa_lector, sobusa_escritor;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA ai_frecuencias AUTHORIZATION sobusa_escritor;
GRANT USAGE ON SCHEMA ai_frecuencias TO sobusa_lector;
ALTER DEFAULT PRIVILEGES FOR ROLE sobusa_escritor IN SCHEMA ai_frecuencias GRANT SELECT ON TABLES TO sobusa_lector;
SQL
cd "$REPO"
DATABASE_URL_WRITE="postgresql+psycopg://sobusa_escritor@/$BD?host=$SOCK&port=$PORT" "$VENV/bin/alembic" upgrade head
$P -d "$BD" -c "SELECT version_num FROM ai_frecuencias.alembic_version"
