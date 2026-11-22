"""Completar la capacidad de los vehículos (vehiculos.capacidad = 0 en toda la flota).

Prioridad, sin tocar la columna original:
  1. vehiculos.capacidad, si es > 0
  2. plazas del reporte autorizado, por vehículo
  3. n_sentados + n_depie del catálogo
  4. 'sin_dato' (no se imputa)
"""
import numpy as np
import pandas as pd


def normalizar_placa(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.upper()


def completar_capacidad(vehiculos: pd.DataFrame, capacidad_reporte: pd.DataFrame) -> pd.DataFrame:
    """vehiculos: idvehiculo, placa, capacidad, n_sentados, n_depie.
    capacidad_reporte: placa, plazas (vehiculo_capacidad.csv)."""
    v = vehiculos.copy()
    v["placa_norm"] = normalizar_placa(v["placa"])
    rep = capacidad_reporte[["placa", "plazas"]].copy()
    rep["placa_norm"] = normalizar_placa(rep["placa"])
    rep = rep.drop_duplicates("placa_norm")[["placa_norm", "plazas"]].rename(
        columns={"plazas": "plazas_reporte"})
    v = v.merge(rep, on="placa_norm", how="left", validate="many_to_one")

    cap = pd.to_numeric(v["capacidad"], errors="coerce").fillna(0)
    plazas_cat = (pd.to_numeric(v.get("n_sentados"), errors="coerce").fillna(0)
                  + pd.to_numeric(v.get("n_depie"), errors="coerce").fillna(0))
    condiciones = [cap > 0, v["plazas_reporte"].fillna(0) > 0, plazas_cat > 0]
    v["capacidad_completa"] = np.select(condiciones, [cap, v["plazas_reporte"], plazas_cat],
                                        default=np.nan)
    v["capacidad_fuente"] = np.select(
        condiciones, ["catalogo_capacidad", "reporte_extreme", "catalogo_plazas"],
        default="sin_dato")
    return v.drop(columns=["placa_norm"])
