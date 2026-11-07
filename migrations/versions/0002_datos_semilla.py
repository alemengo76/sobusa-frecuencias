"""Datos semilla: catálogo ALT-001..010, umbrales v1, parámetros operativos y modelo base.

Revision ID: 0002
Revises: 0001
"""
import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_util import ejecutar_archivo  # noqa: E402

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ejecutar_archivo("0002_semilla.sql")


def downgrade() -> None:
    for t in ["model_registry", "parametro_operativo", "umbral", "alert_catalog"]:
        op.execute(f"DELETE FROM ai_frecuencias.{t}")
