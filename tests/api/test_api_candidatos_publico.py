"""Contrato del candidato con un proveedor sintético y sin repositorio empresarial."""
import importlib

from fastapi.testclient import TestClient

from app import servicios_candidatos
from app.api import main


def test_canal_candidato_optativo(monkeypatch):
    monkeypatch.delenv("DATABASE_URL_READ", raising=False)
    monkeypatch.delenv("DATABASE_URL_WRITE", raising=False)
    monkeypatch.delenv("SOBUSA_CANDIDATO_LOCAL", raising=False)
    original = importlib.reload(main).app.openapi()["paths"]
    assert len(original) == 15
    monkeypatch.setenv("SOBUSA_CANDIDATO_LOCAL", "1")
    monkeypatch.setenv("SOBUSA_DATOS_SINTETICOS", "1")
    monkeypatch.setenv("SOBUSA_OPERADOR_TOKEN", "solo-prueba-local")
    monkeypatch.setattr(servicios_candidatos, "inferir", lambda _: {
        "metadata": dict(fecha="2025-05-01", version=servicios_candidatos.VERSION,
                         feature_version="sintetica", snapshot="sintetico", corte_nominal="2025-05-01"),
        "filas": [dict(idruta=2, franja_inicio="2025-05-01T08:00:00", horizonte_min=60,
                       tipo_dia="laboral", demanda_rrap_v4=40, demanda_lightgbm_l3=60, demanda_pred=50)],
    })
    app = importlib.reload(main).app
    try:
        rutas = app.openapi()["paths"]
        assert len(rutas) == 19 and all(rutas[p] == v for p, v in original.items())
        with TestClient(app, headers={"Authorization": "Bearer solo-prueba-local"}) as cliente:
            payload = dict(modelo="sobusa_v1", version=servicios_candidatos.VERSION,
                           fecha="2025-05-01", rutas=[2], horizontes=[60])
            assert cliente.post("/api/v1/candidate-runs", json=dict(payload, modelo="gbm_fe31")).status_code == 422
            r = cliente.post("/api/v1/candidate-runs", json=payload)
            assert r.status_code == 201 and r.json()["simulaciones_60"] == 1
            url = "/api/v1/candidate-runs/" + r.json()["run_id"]
            fila = cliente.get(url + "/predictions?tamano=1").json()["datos"][0]
            assert fila["demanda_pred"] == (fila["demanda_rrap_v4"] + fila["demanda_lightgbm_l3"]) / 2
            assert "limite_superior" not in fila
            rec = cliente.get(url + "/recommendations?tamano=1").json()
            assert rec["metadata"]["parametros"] == "sintetico_v1"
            assert cliente.get(url + "/recommendations?base_demanda=superior").status_code == 422
    finally:
        monkeypatch.delenv("SOBUSA_CANDIDATO_LOCAL")
        importlib.reload(main)
