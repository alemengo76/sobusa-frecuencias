"""RF-015 (A1): GET /api/v1/predictions/{prediction_id}/explanation (sin base de datos).

El servicio se reemplaza con unittest.mock: aquí se prueba el contrato HTTP (esquema, 404, 409, 422 y
500 uniformes, sin cuerpo parcial). Las verificaciones del servicio están en
tests/unit/test_servicio_explicacion.py.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.api import main  # noqa: E402
from app.core.errores import Conflicto, NoEncontrado  # noqa: E402
from app.ml.explicacion_gbm import ExplicacionNoValida  # noqa: E402

cliente = TestClient(main.app, raise_server_exceptions=False)
RUTA = "/api/v1/predictions/{}/explanation"
INSTANTE = datetime(2025, 10, 1, 11, 0, tzinfo=timezone.utc)


def respuesta_valida():
    return {"prediction_id": 5, "execution_id": "11111111-1111-1111-1111-111111111111", "model_id": 2,
            "model_version": "gbm_fe31_fixture", "feature_version": "sobusa-fe-3.1",
            "artefacto_sha256": "f" * 64, "scikit_learn": "1.9.1", "idruta": 16,
            "franja_inicio": INSTANTE, "horizonte_min": 60, "data_cutoff": INSTANTE, "demanda_pred": 267.94,
            "estado": "reconstruida_y_verificada", "advertencia": "no garantizada", "metodo": "treeshap",
            "escala": "log", "referencia_log": 6.35, "referencia_pasajeros": 573.6, "prediccion_log": 5.59,
            "prediccion_pasajeros": 267.94,
            "aportes": [{"variable": "y_media_4_semanas", "valor": 250.0, "valor_modelo": 250.0,
                         "aporte_log": -1.2, "factor": 0.3}],
            "verificaciones": {"reproduccion": {"predicciones_comparadas": 195}},
            "importancia_global_artefacto": {"metodo": "ganancia_de_division", "model_version": "v",
                                             "nota": "global", "variables": []}}


def test_el_endpoint_esta_publicado_en_openapi():
    ruta = cliente.get("/openapi.json").json()["paths"]["/api/v1/predictions/{prediction_id}/explanation"]
    assert set(ruta["get"]["responses"]) == {"200", "404", "409", "422", "500"}


@patch("app.api.main.servicios.explicar_prediccion")
def test_explicacion_valida_da_200_con_el_esquema(servicio):
    servicio.return_value = respuesta_valida()
    r = cliente.get(RUTA.format(5))
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["estado"] == "reconstruida_y_verificada" and cuerpo["prediction_id"] == 5
    servicio.assert_called_once_with(5)


@pytest.mark.parametrize("error, estado, codigo", [
    (NoEncontrado("Predicción", 999), 404, "no_encontrado"),
    (Conflicto("La predicción es del modelo baseline_dh_v1: línea base"), 409, "conflicto"),
    (Conflicto("Reproducción rechazada: 3 de 195 predicciones"), 409, "conflicto"),
    (ExplicacionNoValida("La suma de aportes no reproduce la predicción"), 500, "error_interno"),
], ids=["404", "409_linea_base", "409_reproduccion", "500_identidad"])
def test_rechazos_son_uniformes_y_sin_explicacion_parcial(error, estado, codigo):
    with patch("app.api.main.servicios.explicar_prediccion", side_effect=error):
        r = cliente.get(RUTA.format(999))
    assert r.status_code == estado
    cuerpo = r.json()
    assert set(cuerpo) == {"error"} and cuerpo["error"]["codigo"] == codigo and cuerpo["error"]["correlation_id"]
    assert "aportes" not in r.text


def test_identificador_no_entero_da_422():
    r = cliente.get(RUTA.format("abc"))
    assert r.status_code == 422 and r.json()["error"]["codigo"] == "solicitud_invalida"


@patch("app.api.main.servicios.explicar_prediccion")
def test_respuesta_incompleta_del_servicio_no_se_publica(servicio):
    """Si el servicio devolviera algo sin los campos obligatorios, la API falla (500), no lo recorta."""
    incompleta = respuesta_valida()
    del incompleta["aportes"]
    servicio.return_value = incompleta
    r = cliente.get(RUTA.format(5))
    assert r.status_code == 500 and "aportes" not in r.text
