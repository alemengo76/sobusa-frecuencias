"""Métricas por ruta, horizonte, franja y pico/valle.

Frontera con el modelo: evaluar(predicciones, reales) -> tabla de métricas.
"""
import numpy as np
import pandas as pd

from app.ml.baseline import tipo_dia


def metricas(real: pd.Series, pred: pd.Series) -> dict:
    m = real.notna() & pred.notna()
    r, p = real[m].to_numpy(float), pred[m].to_numpy(float)
    if len(r) == 0:
        return {"n": 0, "mae": np.nan, "rmse": np.nan, "wape": np.nan, "smape": np.nan}
    err = r - p
    denom = np.abs(r).sum()
    smape_d = np.abs(r) + np.abs(p)
    return {
        "n": int(len(r)),
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err ** 2))),
        "wape": float(np.abs(err).sum() / denom) if denom > 0 else np.nan,
        "smape": float(np.mean(np.divide(2 * np.abs(err), smape_d, out=np.zeros_like(err),
                                          where=smape_d > 0))),
    }


def marcar_pico(entrenamiento: pd.DataFrame, festivos=(), proporcion=0.25) -> pd.DataFrame:
    """Pico = el 25% de franjas con más pasajeros promedio por ruta y tipo de día
    (definición del equipo). Se calcula con entrenamiento y se aplica a todo."""
    e = entrenamiento.dropna(subset=["demanda_real"]).copy()
    e["tipo_dia"] = tipo_dia(e.franja_inicio, festivos)
    e["franja"] = pd.DatetimeIndex(e.franja_inicio).strftime("%H:%M")
    perfil = e.groupby(["idruta", "tipo_dia", "franja"]).demanda_real.mean().reset_index()
    umbral = perfil.groupby(["idruta", "tipo_dia"]).demanda_real.transform(
        lambda s: s.quantile(1 - proporcion))
    perfil["periodo"] = np.where(perfil.demanda_real >= umbral, "pico", "valle")
    return perfil[["idruta", "tipo_dia", "franja", "periodo"]]


def evaluar(df: pd.DataFrame, pico: pd.DataFrame | None = None, festivos=(),
            por=("idruta", "horizonte_min")) -> pd.DataFrame:
    """df: idruta, franja_inicio, horizonte_min, demanda_real, demanda_pred [, modelo]."""
    d = df.copy()
    grupos = list(por)
    if "modelo" in d.columns:
        grupos = ["modelo"] + grupos
    if pico is not None:
        d["tipo_dia"] = tipo_dia(d.franja_inicio, festivos)
        d["franja"] = pd.DatetimeIndex(d.franja_inicio).strftime("%H:%M")
        d = d.merge(pico, on=["idruta", "tipo_dia", "franja"], how="left")
        d["periodo"] = d["periodo"].fillna("valle")
        filas = []
        for clave, g in d.groupby(grupos):
            clave = clave if isinstance(clave, tuple) else (clave,)
            for periodo, gp in [("total", g), *g.groupby("periodo")]:
                filas.append({**dict(zip(grupos, clave)), "periodo": periodo,
                              **metricas(gp.demanda_real, gp.demanda_pred)})
        return pd.DataFrame(filas)
    return (d.groupby(grupos)
             .apply(lambda g: pd.Series(metricas(g.demanda_real, g.demanda_pred)),
                    include_groups=False)
             .reset_index())
