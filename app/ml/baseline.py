"""Líneas base obligatorias (promedio día-hora y última semana) y el "método Extreme".

Todas respetan el contrato D+1: cada franja usa solo datos de semanas anteriores.
La predicción para el horizonte H es el acumulado de las franjas en [t, t+H).
"""
import numpy as np
import pandas as pd

from app.ml.features import features_d1, validar_malla

METODOS = ("dia_hora_4sem", "dia_hora_8sem", "ultima_semana")


def _por_franja(s: pd.Series, metodo: str) -> pd.Series:
    f = features_d1(s)
    columna = {"dia_hora_4sem": "media_dh_4sem", "dia_hora_8sem": "media_dh_8sem",
               "ultima_semana": "lag_7d"}[metodo]
    return f[columna]


def acumular(base_franja: pd.Series, horizonte_min: int) -> pd.Series:
    pasos = horizonte_min // 15
    ventana = pd.concat([base_franja.shift(-k) for k in range(pasos)], axis=1)
    return ventana.sum(axis=1, min_count=pasos)


def predecir(s: pd.Series, metodo: str, horizontes=(30, 60, 120)) -> pd.DataFrame:
    """s: proxy por franja de UNA ruta (índice = origen, malla de 15 min)."""
    if metodo not in METODOS:
        raise ValueError(f"Método desconocido: {metodo}")
    s = validar_malla(s)
    base = _por_franja(s, metodo)
    partes = [pd.DataFrame({"franja_inicio": s.index, "horizonte_min": h,
                            "demanda_pred": acumular(base, h).to_numpy()})
              for h in horizontes]
    return pd.concat(partes, ignore_index=True)


def tipo_dia(idx, festivos=()) -> np.ndarray:
    idx = pd.DatetimeIndex(idx)
    es_festivo = pd.Index(idx.date).isin(list(festivos))
    return np.select([es_festivo | (idx.dayofweek == 6), idx.dayofweek == 5],
                     ["domingo_festivo", "sabado"], "laboral")


class MetodoExtreme:
    """Perfil histórico como el de timbradas_consolidado: promedio por tipo de día y
    franja, calculado SOLO con el periodo de entrenamiento (sin fuga)."""

    def __init__(self, festivos=()):
        self.festivos = festivos
        self.perfil = None

    def ajustar(self, s_entrenamiento: pd.Series):
        s = s_entrenamiento.dropna()
        clave = pd.DataFrame({"tipo": tipo_dia(s.index, self.festivos),
                              "franja": s.index.strftime("%H:%M"), "y": s.to_numpy()})
        self.perfil = clave.groupby(["tipo", "franja"]).y.mean()
        return self

    def predecir(self, idx: pd.DatetimeIndex, horizontes=(30, 60, 120)) -> pd.DataFrame:
        if self.perfil is None:
            raise RuntimeError("Primero llame a ajustar()")
        claves = pd.MultiIndex.from_arrays([tipo_dia(idx, self.festivos), idx.strftime("%H:%M")])
        base = pd.Series(self.perfil.reindex(claves).to_numpy(), index=idx)
        return pd.concat([pd.DataFrame({"franja_inicio": idx, "horizonte_min": h,
                                        "demanda_pred": acumular(base, h).to_numpy()})
                          for h in horizontes], ignore_index=True)


def ajustar_intervalos(validacion: pd.DataFrame, cuantiles=(0.10, 0.90),
                       por=("idruta", "horizonte_min", "hora")) -> pd.DataFrame:
    """Cuantiles del error (real - pred) en validación, por grupo.

    validacion necesita: columnas de `por`, demanda_real y demanda_pred."""
    v = validacion.dropna(subset=["demanda_real", "demanda_pred"]).copy()
    v["error"] = v["demanda_real"] - v["demanda_pred"]
    q = v.groupby(list(por)).error.quantile(list(cuantiles)).unstack()
    q.columns = ["error_inferior", "error_superior"]
    return q.reset_index()


def aplicar_intervalos(pred: pd.DataFrame, errores: pd.DataFrame,
                       por=("idruta", "horizonte_min", "hora")) -> pd.DataFrame:
    out = pred.merge(errores, on=list(por), how="left")
    out["limite_inferior"] = (out.demanda_pred + out.error_inferior.fillna(0)).clip(lower=0)
    out["limite_superior"] = out.demanda_pred + out.error_superior.fillna(0)
    # El contrato exige inferior <= pred <= superior
    out["limite_inferior"] = np.minimum(out.limite_inferior, out.demanda_pred)
    out["limite_superior"] = np.maximum(out.limite_superior, out.demanda_pred)
    return out.drop(columns=["error_inferior", "error_superior"])
