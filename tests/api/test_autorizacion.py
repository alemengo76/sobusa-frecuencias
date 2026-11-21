from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.main import app


@pytest.fixture
def cliente(monkeypatch):
    for rol in ("LECTOR", "OPERADOR", "ADMIN"):
        monkeypatch.setenv(f"SOBUSA_{rol}_TOKEN", rol.lower())
    return TestClient(app)


@pytest.mark.parametrize(("metodo", "ruta"), [
    ("POST", "/api/v1/frequency/executions"),
    ("POST", "/api/v1/predictions/run"),
    ("POST", "/api/v1/recommendations/calculate"),
    ("PATCH", "/api/v1/alerts/1/status"),
    ("POST", "/api/v1/models/1/activate"),
])
@pytest.mark.parametrize(("token", "estado"), [(None, 401), ("incorrecto", 401), ("lector", 403)])
def test_escritura_protegida_antes_de_invocar_servicios(cliente, metodo, ruta, token, estado):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with patch("app.api.main.escribir") as db, patch("app.api.main.servicios") as servicios:
        r = cliente.request(metodo, ruta, json={}, headers=headers)
        assert r.status_code == estado
        assert r.json()["error"]["correlation_id"] == r.headers["X-Correlation-Id"]
        assert not db.called and not servicios.mock_calls
    if estado == 401:
        assert r.headers["WWW-Authenticate"] == "Bearer"


def test_operador_solo_puede_despachar(cliente):
    headers = {"Authorization": "Bearer operador"}
    assert cliente.post("/api/v1/models/1/activate", json={}, headers=headers).status_code == 403
    with patch("app.api.main.servicios.ejecutar_frecuencias", return_value={"estado": "exitosa"}) as ejecutar:
        r = cliente.post("/api/v1/frequency/executions", headers=headers,
                         json={"idrutas": [16], "dia": "2026-03-16"})
    assert r.status_code == 201
    ejecutar.assert_called_once()


def test_token_compartido_falla_en_cerrado(cliente, monkeypatch):
    monkeypatch.setenv("SOBUSA_LECTOR_TOKEN", "operador")
    r = cliente.post("/api/v1/frequency/executions", json={},
                     headers={"Authorization": "Bearer operador"})
    assert r.status_code == 401
