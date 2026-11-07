"""Pruebas de ML: fuga temporal (D+1), líneas base, intervalos, métricas y contrato."""
import numpy as np
import pandas as pd
import pytest

from app.ml import baseline, contrato, evaluacion
from app.ml.falsas import generar_predicciones_falsas
from app.ml.features import acumulado_adelante, calendario, features_d1


def serie(dias=70, semilla=0):
    idx = pd.date_range("2024-01-01", periods=96 * dias, freq="15min")
    rng = np.random.default_rng(semilla)
    return pd.Series(rng.poisson(50, len(idx)).astype(float), index=idx)


# ------------------------------------------------------------------ fuga temporal

def test_features_sin_fuga_mismo_dia():
    s = serie()
    t = s.index[96 * 60 + 60]                       # 15:00 del día 61
    antes = features_d1(s).loc[t]
    s2 = s.copy()
    s2[s2.index >= t.normalize()] = 1e6             # ensuciar todo el día de t
    assert features_d1(s2).loc[t].equals(antes)


def test_linea_base_sin_fuga_en_horizonte_largo():
    s = serie()
    t = s.index[96 * 60 + 88]                       # 22:00: el horizonte de 120 cruza la medianoche
    p = lambda x: baseline.predecir(x, "dia_hora_4sem").set_index(
        ["franja_inicio", "horizonte_min"]).loc[(t, 120), "demanda_pred"]
    s2 = s.copy()
    s2[s2.index >= t.normalize()] = 1e6
    assert p(s) == p(s2)


def test_dia_anterior_si_cuenta():
    s = serie()
    t = s.index[96 * 60 + 60]
    s2 = s.copy()
    s2[t - pd.Timedelta("1D")] = 999.0
    assert features_d1(s2).loc[t, "lag_1d"] == 999.0


def test_hueco_no_es_cero():
    s = serie()
    t = s.index[96 * 60 + 60]
    s2 = s.copy()
    s2[t - pd.Timedelta("1D")] = np.nan
    assert pd.isna(features_d1(s2).loc[t, "lag_1d"])


def test_malla_irregular_falla():
    s = serie()
    with pytest.raises(ValueError):
        features_d1(s.drop(index=s.index[5]))


# ------------------------------------------------------------------ etiquetas y calendario

def test_acumulado_adelante_y_na():
    s = pd.Series([1.0, 2, 3, 4, np.nan, 6], index=pd.date_range("2025-01-01", periods=6, freq="15min"))
    y = acumulado_adelante(s, 30)
    assert y.iloc[0] == 3 and pd.isna(y.iloc[3]) and pd.isna(y.iloc[-1])


def test_calendario_festivo():
    idx = pd.date_range("2025-01-01", periods=2, freq="1D")
    cal = calendario(idx, festivos={pd.Timestamp("2025-01-01").date()})
    assert cal.es_festivo.tolist() == [1, 0]


# ------------------------------------------------------------------ líneas base

def test_ultima_semana_es_el_valor_de_hace_7_dias():
    s = serie()
    pred = baseline.predecir(s, "ultima_semana", horizontes=(30,))
    t = s.index[96 * 30]
    esperado = s[t - pd.Timedelta("7D")] + s[t - pd.Timedelta("7D") + pd.Timedelta("15min")]
    assert pred.set_index("franja_inicio").loc[t, "demanda_pred"] == esperado


def test_metodo_extreme_usa_solo_entrenamiento():
    s = serie()
    m = baseline.MetodoExtreme().ajustar(s[:"2024-01-31"])
    pred = m.predecir(s["2024-02-01":].index, horizontes=(30,))
    assert pred.demanda_pred.notna().mean() > 0.95


def test_intervalos_cumplen_el_contrato():
    rng = np.random.default_rng(0)
    v = pd.DataFrame({"idruta": 16, "horizonte_min": 60, "hora": rng.integers(5, 22, 500),
                      "demanda_pred": rng.uniform(50, 200, 500)})
    v["demanda_real"] = v.demanda_pred + rng.normal(0, 20, 500)
    errores = baseline.ajustar_intervalos(v)
    out = baseline.aplicar_intervalos(v.drop(columns="demanda_real"), errores)
    assert ((out.limite_inferior <= out.demanda_pred) & (out.demanda_pred <= out.limite_superior)).all()
    cobertura = ((v.demanda_real >= out.limite_inferior) & (v.demanda_real <= out.limite_superior)).mean()
    assert 0.7 < cobertura < 0.9


# ------------------------------------------------------------------ métricas

def test_metricas_conocidas():
    m = evaluacion.metricas(pd.Series([10.0, 20, 30]), pd.Series([12.0, 18, 30]))
    assert m["mae"] == pytest.approx(4 / 3)
    assert m["wape"] == pytest.approx(4 / 60)
    assert m["n"] == 3


def test_evaluar_con_pico_y_valle():
    s = serie(dias=40)
    df = baseline.predecir(s, "ultima_semana", horizontes=(60,)).assign(idruta=16)
    df["demanda_real"] = df.demanda_pred * 1.1
    pico = evaluacion.marcar_pico(df.dropna())
    tabla = evaluacion.evaluar(df, pico=pico)
    assert set(tabla.periodo) == {"total", "pico", "valle"}


# ------------------------------------------------------------------ contrato

def test_predicciones_falsas_cumplen_contrato():
    contrato.validar_predicciones(generar_predicciones_falsas(seed=3))


def test_contrato_detecta_limites_invertidos():
    df = generar_predicciones_falsas(seed=3, horas=1)
    df.loc[0, "limite_inferior"] = df.loc[0, "demanda_pred"] + 1
    with pytest.raises(contrato.ContratoInvalido, match="limite_inferior"):
        contrato.validar_predicciones(df)
