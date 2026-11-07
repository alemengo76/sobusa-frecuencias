"""Tabla viaje_fuente: viajes ingeridos para el GBM (E01). Solo crea una tabla nueva en ai_frecuencias.

Revision ID: 0004
Revises: 0003
"""
import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_util import ejecutar_archivo  # noqa: E402

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ejecutar_archivo("0004_viaje_fuente.sql")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_frecuencias.viaje_fuente")
