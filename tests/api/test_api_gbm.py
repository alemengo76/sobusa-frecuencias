"""E01 por la API: POST /predictions/run predice con el GBM entrenado y cae a la línea base.

Sin base de datos (unittest.mock) y con un GBM pequeño entrenado sobre viajes sintéticos.
"""
import json
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


EID = "00000000-0000-0000-0000-000000000001"


def _es_insert_de_predicciones(sentencia) -> bool:
    return str(sentencia).lstrip().startswith("INSERT INTO prediction (")


@contextmanager
def base_falsa():
    """Base vacía simulada: la ejecución queda dueña de la huella (nadie más la reservó) y el
    INSERT ... RETURNING de insertar_predicciones devuelve un prediction_id por cada fila enviada,
    que es lo que hace PostgreSQL. Las demás sentencias no devuelven nada que se use."""
    c = MagicMock()

    def ejecutar(sentencia, parametros=None):
        resultado = MagicMock()
        if _es_insert_de_predicciones(sentencia):
            enviadas = json.loads(parametros["filas"])
            resultado.all.return_value = [(i,) for i in range(1, len(enviadas) + 1)]
        return resultado

    c.execute.side_effect = ejecutar

    @contextmanager
    def escribir():
        yield c

    with patch.object(servicios, "escribir", escribir), \
         patch.object(servicios.repo, "crear_ejecucion", return_value=EID), \
         patch.object(servicios.repo, "reservar_conjunto", return_value=True), \
         patch.object(servicios.repo, "cerrar_ejecucion"):
        yield c


def filas_insertadas(c) -> list[dict]:
    """Las filas que insertar_predicciones envió a PostgreSQL (parámetro :filas, en JSON)."""
    (llamada,) = [k for k in c.execute.call_args_list if _es_insert_de_predicciones(k.args[0])]
    return json.loads(llamada.args[1]["filas"])


def test_api_predice_con_el_gbm(entorno):  # noqa: F811
    with repo_falso(entorno, entorno["gbm"]), base_falsa() as c:
        r = cliente.post("/api/v1/predictions/run", json=CUERPO)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == "exitosa" and cuerpo["predicciones"] > 0
    assert cuerpo["metodo_activo"] == "gbm_fe31" and cuerpo["respaldo"] is False
    assert cuerpo["predicciones_guardadas"] == cuerpo["predicciones"]
    assert cuerpo["execution_id_predicciones"] == EID and "reutiliza" not in cuerpo
    filas = filas_insertadas(c)
    assert len(filas) == cuerpo["predicciones"]
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
    assert cuerpo["predicciones_guardadas"] == cuerpo["predicciones"]
    assert {f["model_id"] for f in filas_insertadas(c)} == {1}


@patch("app.api.main.escribir")
@patch("app.api.main.servicios.verificar_activable", side_effect=Conflicto("No se puede activar el modelo 9"))
def test_no_se_activa_un_gbm_con_artefacto_invalido(_, escribir):
    escribir.return_value.__enter__.return_value = MagicMock()
    r = cliente.post("/api/v1/models/9/activate", json={"usuario": "ana", "motivo": "probar"})
    assert r.status_code == 409 and r.json()["error"]["codigo"] == "conflicto"
