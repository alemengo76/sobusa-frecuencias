"""Ejecuta un archivo .sql completo dentro de la transacción de Alembic.

Se usa el cursor del driver (psycopg 3) sin parámetros: así se permiten varias
sentencias y bloques DO $$ ... $$, y los '%' del texto no se toman como marcadores.
"""
from pathlib import Path

from alembic import op

SQL = Path(__file__).resolve().parent / "sql"


def ejecutar_archivo(nombre: str) -> None:
    texto = (SQL / nombre).read_text(encoding="utf-8")
    conexion = op.get_bind().connection.driver_connection
    with conexion.cursor() as cur:
        cur.execute(texto)
    op.execute("SET search_path TO DEFAULT")
