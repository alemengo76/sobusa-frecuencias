"""PROPUESTA (no aplicada): evidencia de completitud por ruta y día. Ver propuestas/migraciones/README.md.

Para aprobarla: copiar este archivo a migrations/versions/ y los dos .sql a migrations/sql/. Si para
entonces existe la 0009 (sentido IDA/VUELTA), cambiar down_revision a "0009".

Revision ID: 0010
Revises: 0008
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_util import ejecutar_archivo  # noqa: E402

revision = "0010"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ejecutar_archivo("0010_completitud_ruta_dia.sql")


def downgrade() -> None:
    ejecutar_archivo("0010_completitud_ruta_dia_downgrade.sql")
