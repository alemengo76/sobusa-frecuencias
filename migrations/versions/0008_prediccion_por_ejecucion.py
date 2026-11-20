"""Trazabilidad entre predicciones y recomendaciones (P1). Ver DECISIONES.md §15.

prediction pasa a tener una clave única por ejecución, prediction_set guarda un solo dueño por
contenido (huella huella-pred/1) y recommendation exige prediction_id en las filas nuevas
(CHECK ... NOT VALID). franja_minutos = 15 queda como regla de la base. No modifica filas existentes.
El downgrade se niega si la clave antigua no puede representar las predicciones guardadas.

La migración de sentido IDA/VUELTA y segmentos B1-B5 (no autorizada) sería la 0009.

Revision ID: 0008
Revises: 0007
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_util import ejecutar_archivo  # noqa: E402

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ejecutar_archivo("0008_prediccion_por_ejecucion.sql")


def downgrade() -> None:
    ejecutar_archivo("0008_prediccion_por_ejecucion_downgrade.sql")
