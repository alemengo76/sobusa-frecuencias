"""Genera un ejemplo de la salida de M1 con datos SINTÉTICOS (sin base de datos).

    python scripts/ejemplo_m1_sintetico.py salida.json
"""
import json
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pandas as pd
from fastapi.testclient import TestClient

from app.api import main
from tests.unit.test_comparacion_real import DIA, MARCA, franjas, pred


@contextmanager
def _conexion_falsa():
    yield MagicMock()


def main_(destino: str):
    fr = franjas()
    fr.loc[fr.franja_inicio == DIA + pd.Timedelta(hours=12), "pasajeros"] = None   # cuarentena
    h = pd.Timedelta(hours=1)
    filas = [
        pred(prediction_id=1),                                                   # válido (60 min)
        pred(prediction_id=2, horizonte_min=30, demanda_pred=22.0, limite_inferior=15.0,
             limite_superior=30.0),                                              # válido (30 min)
        pred(prediction_id=3, franja_inicio=DIA + 12 * h),                       # franja NULL
        pred(prediction_id=4, execution_id="e2", generated_at=pd.Timestamp("2025-11-02")),  # repetida
        pred(prediction_id=5, franja_inicio=DIA + 8 * h + pd.Timedelta(minutes=7)),          # no alineada
        pred(prediction_id=6, model_id=3, model_version="baseline-1.0", registro_version="baseline-1.0",
             demanda_pred=36.0, limite_inferior=28.0, limite_superior=44.0),     # otro modelo
    ]
    df = pd.DataFrame(filas).assign(modelo_nombre="sintetico", modelo_estado="activo")
    with patch("app.api.main.consultar_resultados", _conexion_falsa), \
         patch("app.evaluacion_real.obtener_config", return_value=MagicMock(dias_revision=3)), \
         patch("app.evaluacion_real.lr.contar_predicciones", return_value=len(df)), \
         patch("app.evaluacion_real.lr.predicciones", return_value=df), \
         patch("app.evaluacion_real.lr.franjas_reales", return_value=fr), \
         patch("app.evaluacion_real.lr.marca_agua",
               return_value={"ultima_actualizacion": MARCA, "lote": "ingesta_sint_001"}):
        c = TestClient(main.app)
        a = c.get("/api/v1/predictions/accuracy?agrupar_por=horizonte_min").json()
        b = c.get("/api/v1/predictions/accuracy/pairs?tamano=10").json()
    salida = {"GET /api/v1/predictions/accuracy?agrupar_por=horizonte_min": a,
              "GET /api/v1/predictions/accuracy/pairs?tamano=10": b}
    with open(destino, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main_(sys.argv[1] if len(sys.argv) > 1 else "ejemplo_salida_m1.json")
