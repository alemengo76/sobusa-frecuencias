"""Comprueba el cotejo de identificadores sin acceder a PostgreSQL."""
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.calidad import completitud

DIA = date(2025, 11, 19)


@pytest.mark.parametrize(("esperados", "estado", "faltantes", "sobrantes"), [
    ([11, 12], "completo", 0, 0),
    ([11, 13], "parcial", 1, 1),
    ([11, 11], "desconocido", None, None),
    (None, "desconocido", None, None),
])
def test_cotejo_de_programaciones(monkeypatch, esperados, estado, faltantes, sobrantes):
    monkeypatch.setattr(completitud, "_ultima_ingesta", lambda *_: {
        "execution_id": "sintetica", "estado": "exitosa",
        "resumen": {"completa": True, "consulta_valida": True},
    })
    locales = pd.DataFrame([{"idruta": 16, "fecha": DIA, "recibidas": 2,
                            "ids_programaciones": [12, 11], "elegibles": 2, "con_conteo": 2}])

    def leer_local(sql, *_args, **_kwargs):
        assert "array_agg(idprogramacion)" in str(sql)
        return locales

    monkeypatch.setattr(completitud.pd, "read_sql", leer_local)
    fila = {"idruta": 16, "fecha": DIA, "programaciones": 2}
    if esperados is not None:
        fila["ids_programaciones"] = esperados
    referencia = SimpleNamespace(origen="sintetica", leer=lambda *_: pd.DataFrame([fila]))
    [resultado] = completitud.calcular(None, referencia, [16], [DIA], "2025-11-24", 3, 0.9)
    assert resultado["estado"] == estado
    assert resultado["evidencia"]["ids_faltantes"] == faltantes
    assert resultado["evidencia"]["ids_sobrantes"] == sobrantes
