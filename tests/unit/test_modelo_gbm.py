"""E01: gradient boosting FE 3.1 integrado (variables, artefacto, predicción válida, sin entrenar)."""
import hashlib
import pickle
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.fe31 import FEATURE_VERSION, REGLAS_VERSION
from app.fe31.viajes import a_formato_fe
from app.ml import modelo_gbm as G
from tests.unit import sinteticos

DIA = sinteticos.INICIO + pd.Timedelta(days=70)


@pytest.fixture(scope="module")
def datos():
    crudo = sinteticos.viajes()
    art, X = sinteticos.artefacto(crudo)
    return crudo, art, X


@pytest.fixture()
def guardado(datos, tmp_path):
    _, art, _ = datos
    ruta, sha = G.guardar(art, tmp_path, "gbm_prueba")
    G.cargar.cache_clear()
    return ruta, sha


def test_variables_en_linea_iguales_a_entrenamiento(datos):
    """Las variables que calcula la API para el día D (solo con viajes anteriores a D) son
    idénticas a las del conjunto de entrenamiento completo para ese día."""
    crudo, art, X = datos
    P = a_formato_fe(crudo)
    for dia in (DIA, DIA + pd.Timedelta(days=5), DIA + pd.Timedelta(days=6)):
        en_linea = G.variables_dia(P, dia, art.horario)
        offline = X[X.fecha.eq(dia)]
        k = ["id_ruta", "horizonte_min", "origen"]
        a = offline.set_index(k, drop=False)[G.VARIABLES].sort_index()
        b = en_linea.set_index(k, drop=False)[G.VARIABLES].sort_index()
        assert a.index.equals(b.index)
        pd.testing.assert_frame_equal(G.matriz(a).reset_index(drop=True), G.matriz(b).reset_index(drop=True))


def test_sin_fuga_los_viajes_del_dia_no_cambian_la_prediccion(datos):
    crudo, art, _ = datos
    base = G.predecir_dia(art, a_formato_fe(crudo), DIA, (2, 16, 21), (30, 60, 120))
    alterado = crudo.copy()
    alterado.loc[alterado.fechasalida >= DIA, "numeropasajeros"] = 9999
    otra = G.predecir_dia(art, a_formato_fe(alterado), DIA, (2, 16, 21), (30, 60, 120))
    pd.testing.assert_frame_equal(base, otra)


def test_predicciones_validas(datos):
    crudo, art, _ = datos
    p = G.predecir_dia(art, a_formato_fe(crudo), DIA, (2, 16, 21), (30, 60, 120))
    assert set(p.idruta) == {2, 16, 21} and set(p.horizonte_min) == {30, 60, 120}
    num = p[["demanda_pred", "limite_inferior", "limite_superior"]].to_numpy()
    assert np.isfinite(num).all() and (num >= 0).all()
    assert (p.limite_inferior <= p.demanda_pred).all() and (p.demanda_pred <= p.limite_superior).all()
    assert not p.duplicated(["idruta", "franja_inicio", "horizonte_min"]).any()
    assert p.franja_inicio.dt.normalize().eq(DIA).all()


def test_artefacto_se_guarda_y_se_carga_igual(datos, guardado):
    crudo, art, _ = datos
    ruta, sha = guardado
    assert sha == hashlib.sha256(Path(ruta).read_bytes()).hexdigest()
    cargado = G.cargar(str(ruta), sha)
    assert cargado.meta["feature_version"] == FEATURE_VERSION == "sobusa-fe-3.1"
    assert cargado.meta["reglas_version"] == REGLAS_VERSION == "comun-1.5"
    assert cargado.meta["objetivo"] == "y_pasajeros"
    P = a_formato_fe(crudo)
    pd.testing.assert_frame_equal(G.predecir_dia(art, P, DIA, (16,), (60,)),
                                  G.predecir_dia(cargado, P, DIA, (16,), (60,)))


def test_artefacto_se_carga_una_sola_vez(guardado):
    ruta, sha = guardado
    G.cargar(str(ruta), sha)
    G.cargar(str(ruta), sha)
    assert G.cargar.cache_info().hits >= 1


def test_hash_distinto_no_se_abre(guardado):
    ruta, _ = guardado
    with patch("app.ml.modelo_gbm.pickle.load") as abrir:
        with pytest.raises(G.ModeloNoDisponible, match="hash"):
            G.cargar(str(ruta), "0" * 64)
        abrir.assert_not_called()


def test_artefacto_de_otra_version_de_sklearn_se_rechaza(guardado, tmp_path):
    ruta, _ = guardado
    paquete = pickle.loads(Path(ruta).read_bytes())
    paquete["meta"]["sklearn"] = "0.0.1"
    otro = tmp_path / "viejo.pkl"
    otro.write_bytes(pickle.dumps(paquete))
    with pytest.raises(G.ModeloNoDisponible, match="scikit-learn"):
        G.cargar(str(otro), hashlib.sha256(otro.read_bytes()).hexdigest())


def test_artefacto_inexistente():
    with pytest.raises(G.ModeloNoDisponible, match="No existe"):
        G.cargar("/no/existe.pkl", "x")


def test_historia_insuficiente_falla_para_usar_respaldo(datos):
    crudo, art, _ = datos
    P = a_formato_fe(crudo)
    with pytest.raises(G.ModeloNoDisponible):
        G.predecir_dia(art, P, P.salida_programada.max() + pd.Timedelta(days=60), (16,), (60,))
    with pytest.raises(G.ModeloNoDisponible):
        G.predecir_dia(art, P.iloc[0:0], DIA, (16,), (60,))


def test_predecir_no_entrena(datos):
    crudo, art, _ = datos
    with patch("sklearn.ensemble.HistGradientBoostingRegressor.fit", side_effect=AssertionError("entrenó")):
        G.predecir_dia(art, a_formato_fe(crudo), DIA, (2,), (30,))


def test_categoria_desconocida_queda_como_faltante():
    X = pd.DataFrame({v: [1.0] for v in G.VARIABLES})
    X["id_ruta"], X["tipo_dia"], X["marca_calendario"] = "99", "laboral", "otra"
    M = G.matriz(X)
    assert np.isnan(M.id_ruta.iloc[0]) and np.isnan(M.marca_calendario.iloc[0])
    assert M.tipo_dia.iloc[0] == 0


def test_mejora_frente_a_la_linea_base_en_datos_sinteticos(datos):
    """No regresión: el GBM no debe ser peor que el promedio de 4 semanas en datos fuera de muestra."""
    crudo, _, X = datos
    corte = sinteticos.INICIO + pd.Timedelta(days=60)
    H = X[X.habilitada_supervisada & X.y_media_4_semanas.notna()]
    E, V = H[H.origen < corte], H[H.origen >= corte]
    m = G.entrenar(E, max_iter=80)
    wape = lambda p: np.abs(V.y_pasajeros - p).sum() / V.y_pasajeros.sum()  # noqa: E731
    assert wape(G.predecir_matriz(m, V)) <= wape(V.y_media_4_semanas) * 1.05


def test_predice_aunque_falte_el_rezago_de_7_dias(datos):
    """Si el día de hace una semana quedó de baja calidad (p. ej., Nochebuena), el GBM sigue
    prediciendo con los demás rezagos: así se entrenó. Antes esto caía al respaldo."""
    crudo, art, _ = datos
    malo = crudo.copy()
    hace7 = (malo.fechasalida >= DIA - pd.Timedelta(days=7)) & (malo.fechasalida < DIA - pd.Timedelta(days=6))
    malo.loc[hace7, "numeropasajeros"] = np.nan
    P = a_formato_fe(malo)
    X = G.variables_dia(P, DIA, art.horario)
    assert X.y_rezago_7d.isna().all()
    p = G.predecir_dia(art, P, DIA, (2, 16, 21), (60,))
    assert set(p.idruta) == {2, 16, 21} and np.isfinite(p.demanda_pred).all()
