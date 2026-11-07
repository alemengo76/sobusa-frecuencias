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
