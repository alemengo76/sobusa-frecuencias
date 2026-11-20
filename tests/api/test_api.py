"""Pruebas de la API con unittest.mock (sin base de datos).

Verifican: los 10 endpoints existen en OpenAPI, errores uniformes, Correlation-Id,
paginación y validación de entrada.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.api import main  # noqa: E402
from app.core.errores import Conflicto  # noqa: E402

cliente = TestClient(main.app, raise_server_exceptions=False)


@contextmanager
def conexion_falsa():
    yield MagicMock()


ENDPOINTS = {
    ("post", "/api/v1/frequency/executions"), ("get", "/api/v1/frequency/executions/{execution_id}"),
    ("post", "/api/v1/predictions/run"), ("get", "/api/v1/predictions"),
    ("post", "/api/v1/recommendations/calculate"), ("get", "/api/v1/recommendations"),
    ("get", "/api/v1/alerts"), ("patch", "/api/v1/alerts/{alert_id}/status"),
    ("get", "/api/v1/models"), ("post", "/api/v1/models/{model_id}/activate"),
}


def test_contrato_openapi_tiene_los_10_endpoints():
    esquema = cliente.get("/openapi.json").json()
    assert esquema["openapi"].startswith("3.")
    publicados = {(m, ruta) for ruta, ops in esquema["paths"].items() for m in ops}
    assert ENDPOINTS <= publicados


def test_correlation_id_se_propaga():
    r = cliente.get("/salud", headers={"X-Correlation-Id": "prueba-123"})
    assert r.headers["X-Correlation-Id"] == "prueba-123"
    assert "X-Correlation-Id" in cliente.get("/salud").headers


def test_error_de_validacion_es_uniforme():
    r = cliente.post("/api/v1/predictions/run", json={"idrutas": [99], "dia": "2025-10-01"})
    assert r.status_code == 422
    cuerpo = r.json()["error"]
    assert cuerpo["codigo"] == "solicitud_invalida" and cuerpo["correlation_id"]


@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.repo.obtener_ejecucion", return_value=None)
def test_ejecucion_inexistente_da_404_uniforme(_):
    r = cliente.get("/api/v1/frequency/executions/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404 and r.json()["error"]["codigo"] == "no_encontrado"


@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.repo.consultar", return_value=([{"alert_id": 1}], 120))
def test_paginacion(consultar):
    r = cliente.get("/api/v1/alerts?estado=abierta&pagina=2&tamano=50")
    assert r.status_code == 200
    assert r.json()["paginacion"] == {"pagina": 2, "tamano": 50, "total": 120, "paginas": 3}
    args = consultar.call_args.args
    assert args[1] == "alert" and args[2]["estado"] == "abierta" and args[5:] == (2, 50)


def test_tamano_maximo_de_pagina():
    assert cliente.get("/api/v1/alerts?tamano=5000").status_code == 422


@patch("app.api.main.servicios.ejecutar_frecuencias",
       return_value={"execution_id": "x", "estado": "exitosa", "recomendaciones": 3})
def test_ejecucion_de_frecuencias(servicio):
    r = cliente.post("/api/v1/frequency/executions", json={"dia": "2025-10-01", "idrutas": [16]})
    assert r.status_code == 201 and r.json()["execution_id"] == "x"
    assert servicio.call_args.args[0] == [16]


@patch("app.api.main.servicios.cambiar_estado_alerta", side_effect=Conflicto("No se puede pasar"))
def test_transicion_invalida_da_409(_):
    r = cliente.patch("/api/v1/alerts/1/status", json={"estado": "abierta", "usuario": "ana"})
    assert r.status_code == 409 and r.json()["error"]["codigo"] == "conflicto"


@patch("app.api.main.escribir", conexion_falsa)
@patch("app.api.main.repo.activar_modelo", return_value={"model_id": 2, "modelo_anterior": 1})
def test_activar_modelo(_):
    r = cliente.post("/api/v1/models/2/activate", json={"usuario": "ana", "motivo": "mejor WAPE"})
    assert r.status_code == 200 and r.json()["modelo_anterior"] == 1


@patch("app.api.main.servicios.ejecutar_frecuencias", side_effect=RuntimeError("falla"))
def test_error_inesperado_no_filtra_detalles(_):
    r = cliente.post("/api/v1/frequency/executions", json={"dia": "2025-10-01"})
    assert r.status_code == 500 and "falla" not in r.text


# ------------------------------------------------------------------ M1. predicción contra real

def _evaluacion_falsa(*_a, **_k):
    import pandas as pd
    from app.ml import comparacion_real as cr
    from tests.unit.test_comparacion_real import MARCA, franjas, pred
    pares = cr.emparejar(pd.DataFrame([pred(), pred(prediction_id=2, horizonte_min=30),
                                       pred(prediction_id=3, horizonte_min=45)]), franjas(), MARCA, 3)
    return {"pares": pares, "parametros": {"reglas": cr.VERSION_REGLAS}, "modelos": {},
            "huella": "h" * 64}


def test_m1_endpoints_publicados_sin_romper_los_10():
    rutas = cliente.get("/openapi.json").json()["paths"]
    assert "get" in rutas["/api/v1/predictions/accuracy"]
    assert "get" in rutas["/api/v1/predictions/accuracy/pairs"]
    assert ENDPOINTS <= {(m, r) for r, ops in rutas.items() for m in ops}


@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.evaluacion_real.evaluar", side_effect=_evaluacion_falsa)
def test_m1_metricas(evaluar):
    r = cliente.get("/api/v1/predictions/accuracy?model_id=7&idruta=16&agrupar_por=horizonte_min"
                    "&desde=2025-10-01T00:00:00&hasta=2025-10-09T00:00:00")
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["conteo_estados"]["valido"] == 2 and cuerpo["conteo_estados"]["inconsistente"] == 1
    # el horizonte inválido (45) aparece como grupo propio, contado pero sin métricas
    assert [g["horizonte_min"] for g in cuerpo["datos"]] == [30, 45, 60]
    assert cuerpo["datos"][1]["n_validas"] == 0 and cuerpo["datos"][1]["mae"] is None
    assert all(g["model_id"] == 7 for g in cuerpo["datos"])
    assert cuerpo["total"][0]["n_validas"] == 2 and cuerpo["huella"] == "h" * 64
    assert isinstance(cuerpo["total"][0]["n_validas"], int)
    args = evaluar.call_args.args
    assert args[1:5] == (7, None, 16, None)


@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.evaluacion_real.evaluar", side_effect=_evaluacion_falsa)
def test_m1_pares_paginados_y_filtrados(_):
    r = cliente.get("/api/v1/predictions/accuracy/pairs?estado=valido&pagina=1&tamano=1")
    assert r.status_code == 200
    c = r.json()
    assert c["paginacion"] == {"pagina": 1, "tamano": 1, "total": 2, "paginas": 2}
    assert c["datos"][0]["estado"] == "valido" and c["datos"][0]["demanda_real"] is not None
    assert c["huella"] == "h" * 64


@pytest.mark.parametrize("consulta", [
    "accuracy?idruta=99", "accuracy?horizonte_min=45", "accuracy?agrupar_por=segmento",
    "accuracy/pairs?estado=raro", "accuracy?execution_id=no-es-uuid", "accuracy/pairs?tamano=5000",
])
def test_m1_validacion_uniforme(consulta):
    r = cliente.get(f"/api/v1/predictions/{consulta}")
    assert r.status_code == 422 and r.json()["error"]["codigo"] == "solicitud_invalida"


@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.repo.ejecucion_duena", return_value=None)
@patch("app.evaluacion_real.lr.contar_predicciones", return_value=0)
def test_m1_ejecucion_inexistente_404(*_):
    r = cliente.get("/api/v1/predictions/accuracy?execution_id=00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404 and r.json()["error"]["codigo"] == "no_encontrado"
