"""Reconciliación de alertas abiertas tras el horizonte de 60 min y el umbral v2 de ALT-003 (solo datos).

Descarta, con su motivo en alert_history, las alertas abiertas que la regla vigente ya no
generaría; a las que sí la cumplen solo les agrega un comentario (su fila no cambia).
El downgrade solo reabre las que nadie tocó después. Ver DECISIONES.md §14.

No es la migración de sentido IDA/VUELTA y segmentos B1-B5 (no autorizada; si se autoriza,
irá después de esta: 0007).

Revision ID: 0006
Revises: 0005
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_util import ejecutar_archivo  # noqa: E402

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ejecutar_archivo("0006_reconciliar_alertas.sql")


def downgrade() -> None:
    ejecutar_archivo("0006_reconciliar_alertas_downgrade.sql")
