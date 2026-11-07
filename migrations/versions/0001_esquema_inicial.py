"""Esquema ai_frecuencias (E04).

El DDL está en migrations/sql/0001_esquema.sql para que el entregable E04 sea legible
y se pueda probar con psql directamente.

Revision ID: 0001
Revises:
"""
import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_util import ejecutar_archivo  # noqa: E402

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLAS = ["demanda_franja", "watermark", "data_quality_error", "notification_log",
          "alert_history", "alert", "umbral", "alert_catalog", "recommendation",
          "parametro_operativo", "prediction", "model_history", "model_registry",
          "execution_log"]


def upgrade() -> None:
    ejecutar_archivo("0001_esquema.sql")


def downgrade() -> None:
    for t in TABLAS:
        op.execute(f"DROP TABLE IF EXISTS ai_frecuencias.{t} CASCADE")
