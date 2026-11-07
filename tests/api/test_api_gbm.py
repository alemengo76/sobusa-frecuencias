"""E01 por la API: POST /predictions/run predice con el GBM entrenado y cae a la línea base.

Sin base de datos (unittest.mock) y con un GBM pequeño entrenado sobre viajes sintéticos.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import servicios  # noqa: E402
from app.api import main  # noqa: E402
from app.core.errores import Conflicto  # noqa: E402
from tests.unit.test_servicio_prediccion import BASE, DIA, entorno, repo_falso  # noqa: E402,F401

cliente = TestClient(main.app, raise_server_exceptions=False)
CUERPO = {"idrutas": [2, 16, 21], "dia": DIA.strftime("%Y-%m-%d"), "horizontes": [30, 60, 120]}


@contextmanager
def base_falsa():
    c = MagicMock()

    @contextmanager
    def escribir():
        yield c

    with patch.object(servicios, "escribir", escribir), \
         patch.object(servicios.repo, "crear_ejecucion", return_value="00000000-0000-0000-0000-000000000001"), \
         patch.object(servicios.repo, "cerrar_ejecucion"):
        yield c


def test_api_predice_con_el_gbm(entorno):  # noqa: F811
    with repo_falso(entorno, entorno["gbm"]), base_falsa() as c:
        r = cliente.post("/api/v1/predictions/run", json=CUERPO)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == "exitosa" and cuerpo["predicciones"] > 0
    assert cuerpo["metodo_activo"] == "gbm_fe31" and cuerpo["respaldo"] is False
    filas = c.execute.call_args.args[1]
    assert {f["model_id"] for f in filas} == {7}
    assert {f["feature_version"] for f in filas} == {"sobusa-fe-3.1"}


def test_api_usa_la_linea_base_si_el_gbm_falla(entorno):  # noqa: F811
    malo = {**entorno["gbm"], "artefacto_ruta": "borrado.pkl"}
    with repo_falso(entorno, malo), base_falsa() as c:
        r = cliente.post("/api/v1/predictions/run", json=CUERPO)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["respaldo"] is True and cuerpo["modelo_respaldo"] == BASE["model_id"]
    assert "No existe" in cuerpo["motivo_respaldo"]
    assert {f["model_id"] for f in c.execute.call_args.args[1]} == {1}


@patch("app.api.main.escribir")
@patch("app.api.main.servicios.verificar_activable", side_effect=Conflicto("No se puede activar el modelo 9"))
def test_no_se_activa_un_gbm_con_artefacto_invalido(_, escribir):
    escribir.return_value.__enter__.return_value = MagicMock()
    r = cliente.post("/api/v1/models/9/activate", json={"usuario": "ana", "motivo": "probar"})
    assert r.status_code == 409 and r.json()["error"]["codigo"] == "conflicto"
