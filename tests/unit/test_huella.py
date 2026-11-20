"""Huella huella-pred/1 de un conjunto de predicciones y contrato endurecido (P1, migración 0008)."""
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.ml import contrato
from app.ml.contrato import ContratoInvalido, huella_predicciones, normalizar_predicciones
from app.ml.falsas import generar_predicciones_falsas

DIA = pd.Timestamp("2025-10-01")
RUTAS, HORIZONTES = [2, 16, 21], [30, 60, 120]


def preds(seed=3) -> pd.DataFrame:
    return generar_predicciones_falsas(seed=seed, inicio="2025-10-01 05:00", horas=4).assign(model_id=7)


def h(df, dia=DIA, rutas=RUTAS, horizontes=HORIZONTES) -> str:
    return huella_predicciones(normalizar_predicciones(df), dia, rutas, horizontes)


# ------------------------------------------------------------------ estabilidad

def test_la_huella_no_depende_del_orden_de_filas_ni_de_columnas():
    df = preds()
    revuelto = df.sample(frac=1, random_state=1)[list(reversed(df.columns))]
    assert h(df) == h(revuelto)


def test_la_huella_no_depende_del_tipo_numerico():
    df = preds()
    otro = df.copy()
    otro["demanda_pred"] = [Decimal(repr(float(v))) for v in df.demanda_pred]
    otro["horizonte_min"] = df.horizonte_min.astype("int32")
    otro["idruta"] = df.idruta.astype(float)
    otro["limite_superior"] = df.limite_superior.astype(np.float32).astype(np.float64).round(2)
    assert h(df) == h(otro)


def test_la_huella_no_depende_del_orden_de_la_solicitud():
    assert h(preds()) == h(preds(), rutas=[21, 2, 16, 16], horizontes=[120, 30, 60])


@pytest.mark.parametrize("columna,valor", [("latency_ms", 999), ("generated_at", pd.Timestamp("2030-01-01")),
                                           ("execution_id", "otra"), ("prediction_id", 123)])
def test_lo_que_cambia_en_cada_corrida_no_entra(columna, valor):
    assert h(preds()) == h(preds().assign(**{columna: valor}))


# ------------------------------------------------------------------ sensibilidad

@pytest.mark.parametrize("columna,valor", [
    ("model_id", 8), ("model_version", "otra"), ("feature_version", "otra"),
    ("sentido", "IDA"), ("segmento_id", "B1"), ("definicion_tiempo", "otra"),
    ("nivel_intervalo", 0.9), ("franja_minutos", 30), ("data_cutoff", pd.Timestamp("2025-09-30")),
])
def test_la_huella_cambia_si_cambia_un_campo(columna, valor):
    df = preds()
    otro = df.copy()
    otro.loc[otro.index[0], columna] = valor
    assert h(df) != h(otro)


@pytest.mark.parametrize("columna", ["demanda_pred", "limite_inferior", "limite_superior"])
def test_la_huella_cambia_con_un_centavo(columna):
    df = preds()
    otro = df.copy()
    i = otro.index[5]          # en las falsas, inferior = 0,8·pred y superior = 1,25·pred: hay margen
    otro.loc[i, columna] = round(otro.loc[i, columna] + 0.01, 2)      # SOLO esa columna
    assert (otro.drop(columns=columna) == df.drop(columns=columna)).all().all()
    assert h(df) != h(otro)


def test_la_huella_cambia_con_la_solicitud_y_con_la_franja():
    df = preds()
    assert h(df) != h(df, rutas=[2, 16])
    assert h(df) != h(df, horizontes=[30, 60])
    assert h(df) != h(df, dia=pd.Timestamp("2025-10-02"))
    otro = df.copy()
    otro.loc[otro.index[0], "franja_inicio"] = otro.franja_inicio.iloc[0] + pd.Timedelta(minutes=15)
    otro = otro.drop_duplicates(contrato.CLAVE_PREDICCION)
    assert h(df) != h(otro)


def test_la_huella_cambia_con_el_formato(monkeypatch):
    antes = h(preds())
    monkeypatch.setattr(contrato, "FORMATO_HUELLA", "huella-pred/2")
    assert h(preds()) != antes


# ------------------------------------------------------------------ normalización

def test_redondeo_igual_que_numeric_10_2():
    df = preds().head(2).copy()
    df["limite_inferior"] = [1.0, 2.0]
    df["demanda_pred"] = [1.005, 2.675]          # ROUND_HALF_UP sobre el decimal más corto del float
    df["limite_superior"] = [3.0, 4.0]
    n = normalizar_predicciones(df)
    assert list(n.demanda_pred) == [Decimal("1.01"), Decimal("2.68")]
    assert all(isinstance(v, Decimal) and v.as_tuple().exponent == -2 for v in n.demanda_pred)


def test_menos_cero_es_cero():
    a, b = preds().head(1).copy(), preds().head(1).copy()
    a[["limite_inferior", "demanda_pred"]] = 0.0
    b[["limite_inferior", "demanda_pred"]] = -0.0
    assert str(normalizar_predicciones(b).demanda_pred.iloc[0]) == "0.00"
    assert h(a, rutas=[2]) == h(b, rutas=[2])


@pytest.mark.parametrize("valor", [np.nan, np.inf, -np.inf, None])
def test_no_finitos_y_nulos_se_rechazan(valor):
    df = preds()
    df["limite_superior"] = df.limite_superior.astype(object)
    df.loc[df.index[0], "limite_superior"] = valor
    with pytest.raises(ContratoInvalido):
        normalizar_predicciones(df)


def test_valor_que_no_cabe_en_la_base_se_rechaza():
    df = preds()
    df.loc[df.index[0], ["demanda_pred", "limite_superior"]] = 1e9
    with pytest.raises(ContratoInvalido, match="no cabe"):
        normalizar_predicciones(df)


def test_fechas_con_zona_equivalen_a_la_hora_local():
    df = preds()
    con_zona = df.assign(franja_inicio=df.franja_inicio.dt.tz_localize("America/Bogota").dt.tz_convert("UTC"))
    assert h(df) == h(con_zona)


@pytest.mark.parametrize("desplazamiento", [pd.Timedelta(seconds=1), pd.Timedelta(minutes=5),
                                            pd.Timedelta(milliseconds=1)])
def test_franja_desalineada_se_rechaza(desplazamiento):
    df = preds()
    df.loc[df.index[0], "franja_inicio"] = df.franja_inicio.iloc[0] + desplazamiento
    with pytest.raises(ContratoInvalido):
        normalizar_predicciones(df)


def test_texto_con_separador_se_rechaza():
    df = preds().assign(model_version="v\x1f1")
    with pytest.raises(ContratoInvalido, match="separador"):
        normalizar_predicciones(df)


def test_huella_exige_valores_normalizados():
    with pytest.raises(ContratoInvalido, match="normalizar"):
        huella_predicciones(preds(), DIA, RUTAS, HORIZONTES)


def test_normalizar_no_modifica_la_entrada():
    df = preds()
    antes = df.copy()
    normalizar_predicciones(df)
    pd.testing.assert_frame_equal(df, antes)


# ------------------------------------------------------------------ contrato endurecido

def test_contrato_exige_franja_de_15_minutos():
    df = generar_predicciones_falsas(seed=1, horas=2).assign(franja_minutos=30)
    with pytest.raises(ContratoInvalido, match="franja_minutos"):
        contrato.validar_predicciones(df)


def test_contrato_rechaza_infinitos():
    df = generar_predicciones_falsas(seed=1, horas=2)
    df.loc[df.index[0], "limite_superior"] = np.inf
    with pytest.raises(ContratoInvalido, match="no finitos"):
        contrato.validar_predicciones(df)


def test_contrato_rechaza_dos_modelos_para_la_misma_franja():
    df = generar_predicciones_falsas(seed=1, horas=2)
    otro = df.head(1).assign(model_version="respaldo")
    with pytest.raises(ContratoInvalido, match="duplicadas"):
        contrato.validar_predicciones(pd.concat([df, otro], ignore_index=True))


def test_contrato_rechaza_franja_desalineada():
    df = generar_predicciones_falsas(seed=1, horas=2)
    df.loc[df.index[0], "franja_inicio"] += pd.Timedelta(minutes=7)
    with pytest.raises(ContratoInvalido, match="múltiplos"):
        contrato.validar_predicciones(df)
