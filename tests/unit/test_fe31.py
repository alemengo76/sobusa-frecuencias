"""El FE del prototipo es copia exacta del FE de referencia (fe_v4_comun15)."""
import hashlib
import sys
from pathlib import Path

import pytest

import app.fe31 as fe

AQUI = Path(fe.__file__).parent
REFERENCIA = Path(__file__).resolve().parents[3] / "fe_v4_comun15"


@pytest.mark.parametrize("archivo", sorted(fe.HUELLAS))
def test_copia_exacta(archivo):
    assert hashlib.sha256((AQUI / archivo).read_bytes()).hexdigest() == fe.HUELLAS[archivo]


@pytest.mark.parametrize("archivo", sorted(fe.HUELLAS))
def test_igual_a_la_carpeta_de_referencia_si_esta_disponible(archivo):
    original = REFERENCIA / archivo
    if not original.exists():
        pytest.skip(f"no está {original}")
    assert original.read_bytes() == (AQUI / archivo).read_bytes()


def test_un_solo_modulo_de_reglas():
    assert sys.modules["reglas_comunes"] is fe.rc
    assert fe.ce.etiqueta_operativa is fe.rc.etiqueta_operativa
    assert fe.REGLAS_VERSION == "comun-1.5" and fe.FEATURE_VERSION == "sobusa-fe-3.1"
