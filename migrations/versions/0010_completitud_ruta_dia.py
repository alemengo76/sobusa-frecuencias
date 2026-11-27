"""Evidencia de completitud por ruta y día.

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
