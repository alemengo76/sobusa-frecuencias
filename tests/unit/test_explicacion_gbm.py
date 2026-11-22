"""Pruebas TreeSHAP con un modelo entrenado solo sobre viajes sintéticos."""
import copy
import itertools
import math
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")
from numpy.lib import recfunctions  # noqa: E402
from sklearn.ensemble._hist_gradient_boosting.predictor import TreePredictor  # noqa: E402
from sklearn.utils._openmp_helpers import _openmp_effective_n_threads  # noqa: E402

from app.fe31.viajes import a_formato_fe  # noqa: E402
from app.ml import explicacion_gbm as XG  # noqa: E402
from app.ml import modelo_gbm as G  # noqa: E402
from tests.unit import sinteticos  # noqa: E402

DIA = sinteticos.INICIO + pd.Timedelta(days=70)


@pytest.fixture(scope="module")
def sintetico():
    modelo, _ = sinteticos.artefacto(sinteticos.viajes())
    return modelo


@pytest.fixture(scope="module")
def filas(sintetico):
    """Variables de FE 3.1 del día DIA para las tres rutas (pipeline real, viajes sintéticos)."""
    return G.variables_dia(a_formato_fe(sinteticos.viajes()), DIA, sintetico.horario)


def preprocesar(modelo, X):
    return np.asarray(modelo._preprocess_X(G.matriz(X)[G.VARIABLES], reset=False), dtype="float64")


# ------------------------------------------------------------------ estructura y columnas

def test_el_artefacto_sintetico_tiene_la_estructura_esperada(sintetico):
    XG.validar_estructura(sintetico.modelo)
    assert sintetico.meta["version"] == "gbm_fe31_prueba"


def test_columnas_arbol_siguen_el_preprocesador_y_no_feature_names_in(sintetico):
    nombres = XG.columnas_arbol(sintetico.modelo)
    categoricas = [v for v in G.VARIABLES if v in G.CATEGORIAS]
    assert nombres == categoricas + [v for v in G.VARIABLES if v not in G.CATEGORIAS]
    assert sorted(nombres) == sorted(G.VARIABLES) and len(nombres) == 29
    assert nombres != G.VARIABLES                          # usar feature_names_in_ sería un error


def test_preprocesador_provee_la_representacion_de_los_arboles(sintetico, filas):
    """Comprueba la representación y la identidad SHAP en los viajes sintéticos."""
    m = sintetico.modelo
    ctx = XG._contexto(m)
    x = G.matriz(filas.iloc[::9])[G.VARIABLES]
    raw = m._raw_predict(x).ravel()
    encoded = preprocesar(m, filas.iloc[::9])
    assert not np.array_equal(encoded, x.to_numpy(dtype="float64"), equal_nan=True)
    reconstructed = [ctx.referencia + XG.treeshap_fila(m, row, ctx).sum() for row in encoded]
    np.testing.assert_allclose(reconstructed, raw, rtol=0, atol=1e-6)


# ------------------------------------------------------------------ recorrido

def test_recorrido_igual_al_de_scikit_learn_en_todos_los_arboles(sintetico, filas):
    m = sintetico.modelo
    X = pd.concat([filas.iloc[::25],
                   filas.iloc[:4].assign(id_ruta="99", marca_calendario="otra"),   # desconocidas -> NaN
                   filas.iloc[4:8].assign(y_rezago_7d=np.nan, y_media_4_semanas=np.nan)])
    A = preprocesar(m, X)
    known, fmap = m._bin_mapper.make_known_categories_bitsets()
    hilos = _openmp_effective_n_threads()
    for it in m._predictors:
        t = it[0]
        nodos = t.nodes.copy()
        nodos["value"] = np.arange(len(nodos), dtype="float64")        # la hoja devuelve su índice
        sk = TreePredictor(nodos, t.binned_left_cat_bitsets, t.raw_left_cat_bitsets).predict(A, known, fmap, hilos)
        assert [XG.hoja(t, A[r], known, fmap) for r in range(len(A))] == sk.astype(int).tolist()


# ------------------------------------------------------------------ identidad y Shapley

def test_referencia_mas_aportes_es_la_prediccion_raw(sintetico, filas):
    X = filas.iloc[[0, len(filas) // 2]]
    resultados = XG.explicar(sintetico, X)
    pred = G.predecir_matriz(sintetico.modelo, X)
    for r, p in zip(resultados, pred):
        assert r["error_identidad"] <= XG.TOLERANCIA
        assert abs(r["referencia_log"] + sum(a["aporte_log"] for a in r["aportes"]) - r["prediccion_log"]) <= 1e-9
        assert math.isclose(r["prediccion_pasajeros"], p, rel_tol=1e-12)
        assert math.isclose(math.prod(a["factor"] for a in r["aportes"]) * r["referencia_pasajeros"],
                            r["prediccion_pasajeros"], rel_tol=1e-9)
        assert sorted(a["variable"] for a in r["aportes"]) == sorted(G.VARIABLES)


def _valor(arbol, x, S, known, fmap, j=0):
    """Función de valor de TreeSHAP dependiente del camino (esperanza condicionada por la cubierta)."""
    n = arbol.nodes[j]
    if n["is_leaf"]:
        return float(n["value"])
    if int(n["feature_idx"]) in S:
        izq = XG.va_a_la_izquierda(n, x, arbol.raw_left_cat_bitsets, known, fmap)
        return _valor(arbol, x, S, known, fmap, int(n["left"] if izq else n["right"]))
    i, d = int(n["left"]), int(n["right"])
    return ((_valor(arbol, x, S, known, fmap, i) * arbol.nodes[i]["count"]
             + _valor(arbol, x, S, known, fmap, d) * arbol.nodes[d]["count"]) / n["count"])


def test_treeshap_igual_a_shapley_exacto_en_arboles_pequenos(sintetico, filas):
    m = sintetico.modelo
    known, fmap = m._bin_mapper.make_known_categories_bitsets()
    A = preprocesar(m, filas.iloc[[0, 100, 400]])
    pequenos = [it[0] for it in m._predictors
                if len({int(n["feature_idx"]) for n in it[0].nodes if not n["is_leaf"]}) <= 11]
    assert pequenos, "el artefacto sintetico no tiene árboles con pocas variables"
    for arbol in pequenos[:2]:
        usadas = sorted({int(n["feature_idx"]) for n in arbol.nodes if not n["is_leaf"]})
        M = len(usadas)
        for x in A:
            phi = np.zeros(29)
            XG._shap_arbol(arbol, x, phi, known, fmap)
            for f in usadas:
                otras = [g for g in usadas if g != f]
                exacto = sum(math.factorial(k) * math.factorial(M - k - 1) / math.factorial(M)
                             * (_valor(arbol, x, set(S) | {f}, known, fmap) - _valor(arbol, x, set(S), known, fmap))
                             for k in range(M) for S in itertools.combinations(otras, k))
                assert abs(exacto - phi[f]) <= 1e-12


# ------------------------------------------------------------------ categorías desconocidas y faltantes

def test_categoria_desconocida_y_faltantes_mantienen_la_identidad(sintetico, filas):
    X = filas.iloc[[10, 11]].assign(id_ruta=["99", "16"], y_rezago_7d=[np.nan, np.nan],
                                    pax_por_viaje_rezago_7d=[np.nan, 5.0])
    resultados = XG.explicar(sintetico, X)
    por_variable = {a["variable"]: a for a in resultados[0]["aportes"]}
    assert por_variable["id_ruta"]["valor"] == "99" and por_variable["id_ruta"]["valor_modelo"] is None
    assert por_variable["y_rezago_7d"]["valor"] is None and por_variable["y_rezago_7d"]["valor_modelo"] is None
    assert all(r["error_identidad"] <= XG.TOLERANCIA for r in resultados)


# ------------------------------------------------------------------ estructura incompatible

@pytest.mark.parametrize("dano", ["count", "campo", "preprocesador", "categoricas", "perdida", "orden",
                                  "categorias_encoder"])
def test_estructura_incompatible_se_rechaza(sintetico, dano):
    m = copy.deepcopy(sintetico.modelo)
    t = m._predictors[0][0]
    if dano == "count":
        t.nodes["count"][0] += 1
    elif dano == "campo":
        t.nodes = recfunctions.drop_fields(t.nodes, "count", usemask=False)
    elif dano == "preprocesador":
        m._preprocessor = None
    elif dano == "categoricas":
        m.is_categorical_ = np.zeros(29, dtype=bool)
    elif dano == "perdida":
        m.loss = "squared_error"
    elif dano == "orden":
        m.feature_names_in_ = np.array(list(reversed(G.VARIABLES)), dtype=object)
    elif dano == "categorias_encoder":
        m._preprocessor.named_transformers_["encoder"].categories_[0] = np.array([0.0, 1.0, 7.0])  # 7: no es código
    with pytest.raises(XG.ExplicacionNoValida):
        XG.validar_estructura(m)
    with pytest.raises(XG.ExplicacionNoValida):
        XG.explicar(G.Artefacto(m, sintetico.horario, sintetico.intervalos, sintetico.meta), pd.DataFrame())


def test_identidad_rota_se_rechaza(sintetico, filas, monkeypatch):
    real = XG.treeshap_fila
    monkeypatch.setattr(XG, "treeshap_fila", lambda m, x, ctx=None: real(m, x, ctx) + 1e-6)
    with pytest.raises(XG.ExplicacionNoValida, match="no reproduce"):
        XG.explicar(sintetico, filas.iloc[[0]])


# ------------------------------------------------------------------ importancia por ganancia

def test_importancia_por_ganancia_del_artefacto(sintetico):
    imp = XG.importancia_ganancia(sintetico.modelo)
    assert sorted(imp.variable) == sorted(G.VARIABLES)
    assert math.isclose(imp.proporcion.sum(), 1.0, rel_tol=1e-12)
    assert (imp.divisiones >= 0).all() and imp.divisiones.sum() > 0


# ------------------------------------------------------------------ reproducción

def _guardadas(recalculadas, tz=True):
    filas = []
    for i, f in enumerate(recalculadas.itertuples(), start=1):
        franja = pd.Timestamp(f.franja_inicio)
        filas.append({"prediction_id": i, "franja_inicio": franja.tz_localize("America/Bogota") if tz else franja,
                      "horizonte_min": int(f.horizonte_min), "demanda_pred": XG.a_precision_bd(f.demanda_pred),
                      "limite_inferior": XG.a_precision_bd(f.limite_inferior),
                      "limite_superior": XG.a_precision_bd(f.limite_superior)})
    return filas


@pytest.fixture(scope="module")
def recalculadas(sintetico):
    return G.predecir_dia(sintetico, a_formato_fe(sinteticos.viajes()), DIA, [16], [30, 60, 120])


def test_reproduccion_identica_se_acepta(recalculadas):
    assert XG.verificar_reproduccion(_guardadas(recalculadas), recalculadas) == len(recalculadas)


@pytest.mark.parametrize("cambio", ["demanda", "limite_inferior", "limite_superior", "falta", "sobra",
                                    "repetida"])
def test_reproduccion_distinta_se_rechaza(recalculadas, cambio):
    g = _guardadas(recalculadas)
    if cambio == "demanda":
        g[5]["demanda_pred"] += Decimal("0.01")
    elif cambio in ("limite_inferior", "limite_superior"):
        g[7][cambio] -= Decimal("0.01")
    elif cambio == "falta":
        g = g[1:]
    elif cambio == "sobra":
        g.append({**g[0], "franja_inicio": g[0]["franja_inicio"] - pd.Timedelta(days=1)})
    elif cambio == "repetida":
        g.append(dict(g[0]))
    with pytest.raises(XG.ReproduccionDistinta):
        XG.verificar_reproduccion(g, recalculadas)


def test_precision_bd_replica_numeric_10_2():
    assert XG.a_precision_bd(12.345) == Decimal("12.35")           # mitad hacia afuera
    assert XG.a_precision_bd(12.344999) == Decimal("12.34")
    assert XG.a_precision_bd(Decimal("7.10")) == Decimal("7.10")
    assert XG.a_precision_bd(0.1 + 0.2) == Decimal("0.30")


def test_variables_de_una_ruta_no_dependen_de_las_otras(sintetico):
    """La reconstrucción lee solo los viajes de la ruta: debe dar lo mismo que con las tres."""
    P = a_formato_fe(sinteticos.viajes())
    todas = G.variables_dia(P, DIA, sintetico.horario)
    sola = G.variables_dia(P[P.id_ruta.astype(str).eq("16")], DIA, sintetico.horario)
    k = ["id_ruta", "horizonte_min", "origen"]
    a = todas[todas.id_ruta.eq("16")].set_index(k, drop=False)[G.VARIABLES].sort_index()
    b = sola.set_index(k, drop=False)[G.VARIABLES].sort_index()
    assert a.index.equals(b.index)
    pd.testing.assert_frame_equal(G.matriz(a).reset_index(drop=True), G.matriz(b).reset_index(drop=True))
