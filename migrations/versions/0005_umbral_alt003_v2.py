"""Umbral de ALT-003 a 2,0 buses/hora como versión nueva (solo datos).

No es la migración de sentido IDA/VUELTA y segmentos B1-B5 (no autorizada; si se autoriza,
irá en la 0007). Las alertas abiertas se reconcilian en la 0006.

Revision ID: 0005
Revises: 0004
"""
import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_util import ejecutar_archivo  # noqa: E402

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ejecutar_archivo("0005_umbral_alt003_v2.sql")


def downgrade() -> None:
    # Borra la versión creada por esta migración y reactiva la versión anterior más reciente.
    op.execute("DELETE FROM ai_frecuencias.umbral WHERE codigo = 'ALT-003' AND creado_por = 'migracion_0005'")
    op.execute("""
        UPDATE ai_frecuencias.umbral SET vigente = true
        WHERE codigo = 'ALT-003'
          AND version = (SELECT max(version) FROM ai_frecuencias.umbral WHERE codigo = 'ALT-003')
          AND NOT EXISTS (SELECT 1 FROM ai_frecuencias.umbral WHERE codigo = 'ALT-003' AND vigente)""")
