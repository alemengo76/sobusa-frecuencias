"""Pruebas de ML del candidato: sin fuga y sin regresión frente a la línea base."""
import numpy as np
import pandas as pd

from app.ml import baseline, evaluacion, gbm


def serie_semanal(dias=150, semilla=0, nivel=60):
    idx = pd.date_range("2024-01-01", periods=96 * dias, freq="15min")
    h = idx.hour + idx.minute / 60
    perfil = 1 + 1.5 * np.exp(-((h - 7) ** 2) / 2) + np.exp(-((h - 17.5) ** 2) / 3)
    semana = np.where(idx.dayofweek >= 5, 0.6, 1.0)
    tendencia = 1 + 0.2 * np.arange(len(idx)) / len(idx)
    rng = np.random.default_rng(semilla)
    s = pd.Series(rng.poisson(nivel * perfil * semana * tendencia).astype(float), index=idx)
    s[(h < 4.5) | (h > 22.5)] = 0
    return s


def test_tabla_modelo_sin_fuga():
    s = serie_semanal(dias=70)
    t = s.index[96 * 60 + 60]
    columnas = [c for c in gbm.VARIABLES if c not in ("acum_dh_4sem",)]
    a = gbm.tabla_modelo({16: s}, 60, festivos={}).set_index("franja_inicio").loc[t, columnas]
    s2 = s.copy()
    s2[s2.index >= t.normalize()] = 1e6
    b = gbm.tabla_modelo({16: s2}, 60, festivos={}).set_index("franja_inicio").loc[t, columnas]
    assert a.equals(b)


def test_no_regresion_frente_a_linea_base():
    series = {16: serie_semanal(semilla=1), 2: serie_semanal(semilla=2, nivel=30)}
    tabla = gbm.tabla_modelo(series, 60, festivos={})
    corte = pd.Timestamp("2024-04-15")
    m = gbm.entrenar(tabla[tabla.franja_inicio < corte])
    ev = tabla[tabla.franja_inicio >= corte].dropna(subset=["y", "acum_dh_4sem"])
    wape_gbm = evaluacion.metricas(ev.y, pd.Series(gbm.predecir(m, ev), index=ev.index))["wape"]
    wape_base = evaluacion.metricas(ev.y, ev.acum_dh_4sem)["wape"]
    assert wape_gbm <= wape_base * 1.05, (wape_gbm, wape_base)


def test_explicabilidad():
    series = {16: serie_semanal(dias=90, semilla=3)}
    tabla = gbm.tabla_modelo(series, 60, festivos={})
    m = gbm.entrenar(tabla)
    imp = gbm.importancia_global(m, tabla, n=2000)
    assert set(imp.variable) == set(gbm.VARIABLES)
    fila = tabla.dropna(subset=["y"]).iloc[[5000]]
    loc = gbm.explicacion_local(m, fila, tabla)
    assert len(loc) == len(gbm.VARIABLES) and np.isfinite(loc.aporte).all()


def test_artefacto_con_hash(tmp_path):
    tabla = gbm.tabla_modelo({16: serie_semanal(dias=40)}, 30, festivos={})
    meta = gbm.guardar_artefacto(gbm.entrenar(tabla), tmp_path, "prueba", {"version_datos": "sintetico"})
    assert len(meta["artefacto_sha256"]) == 64 and meta["semilla"] == gbm.SEMILLA
