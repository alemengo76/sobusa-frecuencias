"""Pruebas de la API con unittest.mock (sin base de datos).

Verifican: los 11 endpoints existen en OpenAPI, errores uniformes, Correlation-Id,
paginación y validación de entrada.
"""
from contextlib import contextmanager
from datetime import datetime
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
    ("get", "/api/v1/quality/errors"),
}


def test_contrato_openapi_tiene_los_11_endpoints():
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


# ------------------------------------------------------------------ G3: GET /quality/errors

URL_CALIDAD = "/api/v1/quality/errors"
SIN_FILTROS = {"idruta": None, "regla": None, "tabla_origen": None, "reglas_version": None}
FILA_CALIDAD = {  # datos sintéticos con los 15 campos de ErrorCalidad
    "deteccion_id": 7, "evento_id": 3, "lote": "ingesta_20261119T080000_001",
    "detectado_en": datetime(2026, 11, 19, 8, 0, 5), "idruta": 16,
    "causa": "valor fuera de rango", "valor_original": "-4", "tabla_origen": "progvehiculos",
    "idprogramacion": 123, "regla": "regla_sintetica", "reglas_version": "comun-1.5",
    "valor_normalizado": "-4", "primer_lote": "ingesta_20261118T080000_002",
    "primera_deteccion": datetime(2026, 11, 18, 8, 0, 9), "nuevo_en_lote": False,
}


@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.repo.errores_calidad_por_lote", return_value=([FILA_CALIDAD], 101))
def test_errores_calidad_respuesta_con_datos_y_paginacion(repo_calidad):
    r = cliente.get(URL_CALIDAD, params={"lote": "ingesta_20261119T080000", "pagina": 3, "tamano": 50})
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["paginacion"] == {"pagina": 3, "tamano": 50, "total": 101, "paginas": 3}
    assert len(cuerpo["datos"]) == 1
    dato = cuerpo["datos"][0]
    assert set(dato) == set(FILA_CALIDAD)
    assert dato["deteccion_id"] == 7 and dato["idprogramacion"] == 123
    assert dato["detectado_en"] == "2026-11-19T08:00:05" and dato["nuevo_en_lote"] is False
    assert repo_calidad.call_count == 1


@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.repo.errores_calidad_por_lote", return_value=([], 0))
def test_errores_calidad_valores_por_defecto_y_resultado_vacio(repo_calidad):
    r = cliente.get(URL_CALIDAD, params={"lote": "L"})
    assert r.status_code == 200
    assert r.json() == {"datos": [], "paginacion": {"pagina": 1, "tamano": 50, "total": 0, "paginas": 0}}
    assert repo_calidad.call_args.args[1:] == ("L", "auto", SIN_FILTROS, 1, 50)
    assert repo_calidad.call_args.kwargs == {}


@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.repo.errores_calidad_por_lote", return_value=([], 0))
def test_errores_calidad_filtros_y_paginacion_llegan_al_repositorio(repo_calidad):
    r = cliente.get(URL_CALIDAD, params={
        "lote": "ingesta_20261119T080000", "alcance": "ejecucion", "idruta": 21,
        "regla": "regla_sintetica", "tabla_origen": "progvehiculos", "reglas_version": "comun-1.5",
        "pagina": 4, "tamano": 20})
    assert r.status_code == 200
    assert repo_calidad.call_args.args[1:] == (
        "ingesta_20261119T080000", "ejecucion",
        {"idruta": 21, "regla": "regla_sintetica", "tabla_origen": "progvehiculos",
         "reglas_version": "comun-1.5"},
        4, 20)


@pytest.mark.parametrize("alcance", ["auto", "ejecucion", "ventana"])
@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.repo.errores_calidad_por_lote", return_value=([], 0))
def test_errores_calidad_alcances_validos(repo_calidad, alcance):
    assert cliente.get(URL_CALIDAD, params={"lote": "L", "alcance": alcance}).status_code == 200
    assert repo_calidad.call_args.args[2] == alcance


@patch("app.api.main.repo.errores_calidad_por_lote")
def test_errores_calidad_lote_obligatorio(repo_calidad):
    r = cliente.get(URL_CALIDAD)
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "solicitud_invalida" and "query.lote" in r.text
    repo_calidad.assert_not_called()


@pytest.mark.parametrize("alcance", ["", "AUTO", "Ventana", "otro"])
@patch("app.api.main.repo.errores_calidad_por_lote")
def test_errores_calidad_alcance_invalido_da_422(repo_calidad, alcance):
    r = cliente.get(URL_CALIDAD, params={"lote": "L", "alcance": alcance})
    assert r.status_code == 422 and r.json()["error"]["codigo"] == "solicitud_invalida"
    repo_calidad.assert_not_called()


@pytest.mark.parametrize("campo,maximo", [("lote", 120), ("regla", 100), ("tabla_origen", 100),
                                          ("reglas_version", 50)])
@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.repo.errores_calidad_por_lote", return_value=([], 0))
def test_errores_calidad_limites_de_longitud(repo_calidad, campo, maximo):
    def pedir(valor):
        return cliente.get(URL_CALIDAD, params={"lote": "L", campo: valor}).status_code

    assert pedir("x" * maximo) == 200
    args = repo_calidad.call_args.args
    assert (args[1] if campo == "lote" else args[3][campo]) == "x" * maximo
    llamadas = repo_calidad.call_count
    assert pedir("x" * (maximo + 1)) == 422
    assert pedir("") == 422
    assert repo_calidad.call_count == llamadas


@pytest.mark.parametrize("params", [{"lote": "L", "idruta": "abc"}, {"lote": "L", "pagina": 0},
                                    {"lote": "L", "tamano": 0}, {"lote": "L", "tamano": 501}])
@patch("app.api.main.repo.errores_calidad_por_lote")
def test_errores_calidad_idruta_y_paginacion_invalidas(repo_calidad, params):
    assert cliente.get(URL_CALIDAD, params=params).status_code == 422
    repo_calidad.assert_not_called()


@patch("app.api.main.consultar_resultados", conexion_falsa)
@patch("app.api.main.repo.errores_calidad_por_lote", side_effect=RuntimeError("detalle interno"))
def test_errores_calidad_error_inesperado_es_uniforme(_):
    r = cliente.get(URL_CALIDAD, params={"lote": "L"})
    assert r.status_code == 500 and r.json()["error"]["codigo"] == "error_interno"
    assert "detalle interno" not in r.text


def test_errores_calidad_en_openapi():
    op = cliente.get("/openapi.json").json()["paths"]["/api/v1/quality/errors"]["get"]
    assert op["tags"] == ["calidad"]
    esquema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert esquema["$ref"].endswith("/PaginaErroresCalidad")
    alcance = next(p for p in op["parameters"] if p["name"] == "alcance")
    assert set(alcance["schema"]["enum"]) == {"auto", "ejecucion", "ventana"}
    assert alcance["schema"]["default"] == "auto"
