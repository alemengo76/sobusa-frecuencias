"""param_v2: factor de carga medido por ruta y tipo de día (E03). Solo datos.

No es la migración de sentido IDA/VUELTA y segmentos B1-B5 (esa no está autorizada; si se
autoriza, irá en una revisión posterior a 0004).

Revision ID: 0003
Revises: 0002
"""
import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_util import ejecutar_archivo  # noqa: E402

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ejecutar_archivo("0003_param_v2.sql")


def downgrade() -> None:
    # Vuelve a la versión anterior: borra param_v2 y reactiva la última fila 'todos' de cada ruta.
    # Las recomendaciones ya calculadas con param_v2 conservan su parametros_version.
    op.execute("DELETE FROM ai_frecuencias.parametro_operativo WHERE version = 'param_v2'")
    op.execute("""
        UPDATE ai_frecuencias.parametro_operativo p SET vigente = true
        WHERE p.tipo_dia = 'todos' AND p.id = (
            SELECT max(q.id) FROM ai_frecuencias.parametro_operativo q
            WHERE q.idruta = p.idruta AND q.tipo_dia = 'todos')
          AND NOT EXISTS (SELECT 1 FROM ai_frecuencias.parametro_operativo v
                          WHERE v.idruta = p.idruta AND v.tipo_dia = 'todos' AND v.vigente)""")
