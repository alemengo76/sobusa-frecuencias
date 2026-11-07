"""Carga de la serie por franja desde la salida del cuaderno de feature engineering.

Usa salidas_feature_engineering/auditoria_franjas_conjunta.parquet, columna
pax_elegibles_completo_snapshot (proxy por franja de 15 min, NA si incompleta).
"""
from pathlib import Path

import pandas as pd

COLUMNA_PROXY = "pax_elegibles_completo_snapshot"
CORTES = {"validacion_desde": "2025-01-01", "prueba_desde": "2025-07-01"}


def cargar_series(ruta_parquet: Path) -> dict[int, pd.Series]:
    a = pd.read_parquet(ruta_parquet, columns=["id_ruta", "origen", COLUMNA_PROXY])
    series = {}
    for r, g in a.groupby("id_ruta"):
        s = g.set_index(pd.to_datetime(g.origen))[COLUMNA_PROXY].astype(float).sort_index()
        s = s[~s.index.duplicated()]
        series[int(r)] = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="15min"))
    return series


def particion(idx: pd.DatetimeIndex) -> pd.Series:
    p = pd.Series("entrenamiento", index=idx)
    p[idx >= pd.Timestamp(CORTES["validacion_desde"])] = "validacion"
    p[idx >= pd.Timestamp(CORTES["prueba_desde"])] = "prueba"
    return p
