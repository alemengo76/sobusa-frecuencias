"""Errores de calidad como eventos + detecciones (sin duplicados entre lotes). Ver DECISIONES.md §8.

data_quality_error pasa a tener un evento por tabla_origen + idprogramacion + regla +
reglas_version + valor (primera y última detección); cada detección por lote queda en
data_quality_deteccion. La migración comprueba que evento + detección reconstruyen exactamente
la tabla anterior. El downgrade vuelve a una fila por detección (los ids de antes de la 0007 se
recuperan; las detecciones posteriores reciben ids nuevos) y se niega si la forma antigua no
puede representar los datos.

No es la migración de sentido IDA/VUELTA y segmentos B1-B5 (no autorizada; si se autoriza, 0009: la 0008 es la de trazabilidad de predicciones).

Revision ID: 0007
Revises: 0006
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_util import ejecutar_archivo  # noqa: E402

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ejecutar_archivo("0007_dq_eventos.sql")


def downgrade() -> None:
    ejecutar_archivo("0007_dq_eventos_downgrade.sql")
