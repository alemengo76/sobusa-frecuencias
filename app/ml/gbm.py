"""REEMPLAZADO PARA PRODUCCIÓN por app/ml/modelo_gbm.py (FE 3.1 / comun-1.5, servido por la API).
Se conserva porque sus pruebas (tests/unit/test_gbm.py) documentan el candidato anterior.

Modelo candidato: gradient boosting (HistGradientBoostingRegressor, pérdida Poisson).

Un modelo por horizonte, con la ruta como variable categórica. Variables: calendario
y festivos (conocidos de antemano) + historia D+1 (features_d1). Sin fuga: cada variable
histórica usa solo días anteriores al de la franja de origen.
"""
import hashlib
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance

from app.ml.features import FEATURE_VERSION, acumulado_adelante, calendario, features_d1

SEMILLA = 2026
HIPERPARAMETROS = {"loss": "poisson", "learning_rate": 0.05, "max_iter": 400,
                   "max_leaf_nodes": 31, "min_samples_leaf": 40, "l2_regularization": 1.0,
                   "early_stopping": True, "validation_fraction": 0.1, "random_state": SEMILLA}
VARIABLES = ["idruta", "hora", "dia_semana_iso", "mes", "hora_sin", "hora_cos", "es_fin_semana",
             "es_festivo", "lag_1d", "lag_7d", "media_dh_4sem", "media_dh_8sem", "nivel_28d",
             "media_dia_anterior", "acum_dh_4sem"]


def tabla_modelo(series: dict[int, pd.Series], horizonte: int, festivos=None) -> pd.DataFrame:
    partes = []
    for idruta, s in series.items():
        f = features_d1(s)
        cal = calendario(s.index, festivos)
        # Línea base acumulada como variable: el modelo aprende a corregirla
        base = f["media_dh_4sem"]
        pasos = horizonte // 15
        acum = pd.concat([base.shift(-k) for k in range(pasos)], axis=1).sum(axis=1, min_count=pasos)
        d = pd.concat([cal, f], axis=1).assign(
            idruta=idruta, acum_dh_4sem=acum, y=acumulado_adelante(s, horizonte),
            franja_inicio=s.index, horizonte_min=horizonte)
        partes.append(d)
    return pd.concat(partes, ignore_index=True)


def entrenar(tabla: pd.DataFrame) -> HistGradientBoostingRegressor:
    e = tabla.dropna(subset=["y"])
    m = HistGradientBoostingRegressor(categorical_features=[VARIABLES.index("idruta")],
                                      **HIPERPARAMETROS)
    m.fit(e[VARIABLES], e["y"])
    return m


def predecir(m, tabla: pd.DataFrame) -> np.ndarray:
    return np.clip(m.predict(tabla[VARIABLES]), 0, None)


def importancia_global(m, tabla: pd.DataFrame, n=5000) -> pd.DataFrame:
    v = tabla.dropna(subset=["y"]).sample(min(n, tabla.y.notna().sum()), random_state=SEMILLA)
    r = permutation_importance(m, v[VARIABLES], v["y"], n_repeats=5, random_state=SEMILLA,
                               scoring="neg_mean_absolute_error")
    return (pd.DataFrame({"variable": VARIABLES, "importancia_mae": r.importances_mean,
                          "desviacion": r.importances_std})
              .sort_values("importancia_mae", ascending=False))


def explicacion_local(m, fila: pd.DataFrame, referencia: pd.DataFrame) -> pd.DataFrame:
    """Cuánto cambia UNA predicción si cada variable toma su valor típico (mediana de
    entrenamiento). Es una explicación por oclusión; reemplazable por SHAP si se instala."""
    base = predecir(m, fila)[0]
    filas = []
    for v in VARIABLES:
        x = fila.copy()
        x[v] = referencia[v].median()
        filas.append({"variable": v, "valor": fila[v].iloc[0], "valor_tipico": referencia[v].median(),
                      "aporte": base - predecir(m, x)[0]})
    return pd.DataFrame(filas).sort_values("aporte", key=abs, ascending=False).assign(prediccion=base)


def guardar_artefacto(m, carpeta: Path, nombre: str, metadatos: dict) -> dict:
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / f"{nombre}.pkl"
    ruta.write_bytes(pickle.dumps(m))
    sha = hashlib.sha256(ruta.read_bytes()).hexdigest()
    meta = {**metadatos, "artefacto_ruta": str(ruta), "artefacto_sha256": sha, "semilla": SEMILLA,
            "hiperparametros": HIPERPARAMETROS, "feature_version": FEATURE_VERSION,
            "variables": VARIABLES}
    (carpeta / f"{nombre}.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return meta
