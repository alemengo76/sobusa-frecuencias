"""RF-015 (A1): servicios.explicar_prediccion con todas sus verificaciones.

Sin base de datos: el repositorio se reemplaza con unittest.mock (como en test_servicio_prediccion) y las
filas "guardadas" se construyen con el mismo predecir_dia que usa la API, sobre viajes sintéticos y un
GBM pequeño del pipeline de pruebas (sinteticos.artefacto). La lectura real en PostgreSQL de las cuatro
consultas nuevas queda para la integración (no se ejecuta aquí).
"""
import importlib.util
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

if importlib.util.find_spec("sqlalchemy") is None or importlib.util.find_spec("sklearn") is None:
    pytest.skip("requiere sqlalchemy y scikit-learn", allow_module_level=True)

from app import servicios  # noqa: E402
from app.core.errores import Conflicto, NoEncontrado  # noqa: E402
from app.fe31 import FEATURE_VERSION  # noqa: E402
from app.fe31.viajes import a_formato_fe  # noqa: E402
from app.ml import explicacion_gbm as XG  # noqa: E402
from app.ml import modelo_gbm as G  # noqa: E402
from tests.unit import sinteticos  # noqa: E402

DIA = sinteticos.INICIO + pd.Timedelta(days=70)
RUTA, H = 16, (30, 60, 120)
EID = "11111111-1111-1111-1111-111111111111"
BOGOTA = "America/Bogota"
INICIO_EJECUCION = (DIA + pd.Timedelta(hours=1)).tz_localize(BOGOTA).to_pydatetime()
CARGA_PREVIA = (DIA - pd.Timedelta(hours=3)).tz_localize(BOGOTA).to_pydatetime()


@pytest.fixture(scope="module")
def entorno(tmp_path_factory):
    crudo = sinteticos.viajes()
    art, _ = sinteticos.artefacto(crudo)
    carpeta = tmp_path_factory.mktemp("modelos")
    ruta, sha = G.guardar(art, carpeta, "gbm_fe31_prueba")
    G.cargar.cache_clear()
    cargado = G.cargar(str(ruta), sha)
    pred = G.predecir_dia(cargado, a_formato_fe(crudo), DIA, [RUTA], H)
    modelo = {"model_id": 7, "nombre": "gbm_fe31", "version": "gbm_fe31_prueba", "tipo": "candidato",
              "estado": "activo", "hiperparametros": {"metodo": "gbm_fe31"}, "feature_version": FEATURE_VERSION,
              "artefacto_ruta": ruta.name, "artefacto_sha256": sha}
    filas = [{"prediction_id": i, "execution_id": EID, "idruta": RUTA, "sentido": "CICLO", "segmento_id": "TOTAL",
              "franja_inicio": pd.Timestamp(f.franja_inicio).tz_localize(BOGOTA).to_pydatetime(),
              "horizonte_min": int(f.horizonte_min), "demanda_pred": XG.a_precision_bd(f.demanda_pred),
              "limite_inferior": XG.a_precision_bd(f.limite_inferior),
              "limite_superior": XG.a_precision_bd(f.limite_superior), "nivel_intervalo": 0.8,
              "model_id": 7, "model_version": "gbm_fe31_prueba", "feature_version": FEATURE_VERSION,
              "data_cutoff": DIA.tz_localize(BOGOTA).to_pydatetime()}
             for i, f in enumerate(pred.itertuples(), start=1)]
    pedida = next(f for f in filas if f["horizonte_min"] == 60)
    return {"crudo": crudo, "carpeta": str(carpeta), "modelo": modelo, "filas": filas, "pedida": pedida}


def _viajes(crudo):
    def f(_c, idrutas, desde, hasta):
        v = crudo[crudo.idruta.isin(idrutas)]
        return v[(v.fechasalida >= desde) & (v.fechasalida < hasta)]
    return f


@contextmanager
def _conexion():
    yield MagicMock()


@contextmanager
def repo_falso(e, *, prediccion=None, modelo=None, filas=None, crudo=None, ultima_carga=CARGA_PREVIA,
               ejecucion="por_defecto"):
    prediccion = e["pedida"] if prediccion is None else prediccion
    ejecucion = {"execution_id": EID, "tipo": "prediccion", "iniciada_en": INICIO_EJECUCION} \
        if ejecucion == "por_defecto" else ejecucion
    G.cargar.cache_clear()
    with patch("app.db.conexion.consultar_resultados", _conexion), \
         patch.object(servicios.repo, "obtener_prediccion", return_value=prediccion), \
         patch.object(servicios.repo, "obtener_modelo", return_value=e["modelo"] if modelo is None else modelo), \
         patch.object(servicios.repo, "obtener_ejecucion", return_value=ejecucion), \
         patch.object(servicios.repo, "predicciones_ejecucion_ruta",
                      return_value=e["filas"] if filas is None else filas), \
         patch.object(servicios.repo, "ultima_carga_viajes", return_value=ultima_carga), \
         patch.object(servicios.repo, "viajes_fuente", _viajes(e["crudo"] if crudo is None else crudo)), \
         patch.object(servicios, "obtener_config", return_value=MagicMock(modelos_dir=e["carpeta"])):
        yield


# ------------------------------------------------------------------ caso válido

def test_explicacion_reconstruida_y_verificada(entorno):
    with repo_falso(entorno):
        r = servicios.explicar_prediccion(entorno["pedida"]["prediction_id"])
    p = entorno["pedida"]
    assert r["estado"] == "reconstruida_y_verificada" and "no garantizada" in r["advertencia"]
    assert (r["prediction_id"], r["execution_id"], r["model_id"], r["model_version"]) == \
           (p["prediction_id"], EID, 7, "gbm_fe31_prueba")
    assert r["verificaciones"]["reproduccion"]["predicciones_comparadas"] == len(entorno["filas"])
    assert r["verificaciones"]["identidad_treeshap"]["error_absoluto"] <= 1e-9
    assert abs(r["referencia_log"] + sum(a["aporte_log"] for a in r["aportes"]) - r["prediccion_log"]) <= 1e-9
    assert XG.a_precision_bd(r["prediccion_pasajeros"]) == p["demanda_pred"]
    assert len(r["aportes"]) == 29
    assert r["importancia_global_artefacto"]["metodo"] == "ganancia_de_division"
    assert "permutación" in r["importancia_global_artefacto"]["nota"]


# ------------------------------------------------------------------ 404 y línea base

def test_prediccion_inexistente_da_404(entorno):
    with repo_falso(entorno, prediccion=False), \
         patch.object(servicios.repo, "obtener_prediccion", return_value=None), pytest.raises(NoEncontrado):
        servicios.explicar_prediccion(999)


@pytest.mark.parametrize("hiper", [{"semanas": 4}, {"metodo": "dia_hora_4sem"}, None])
def test_prediccion_de_linea_base_se_rechaza_sin_cargar_artefacto(entorno, hiper):
    base = {"model_id": 1, "version": "baseline_dh_v1", "tipo": "baseline", "hiperparametros": hiper}
    p = {**entorno["pedida"], "model_id": 1, "model_version": "baseline_dh_v1"}
    with repo_falso(entorno, prediccion=p, modelo=base), patch.object(G, "cargar") as cargar, \
         pytest.raises(Conflicto, match="línea base"):
        servicios.explicar_prediccion(p["prediction_id"])
    cargar.assert_not_called()


# ------------------------------------------------------------------ identidad del modelo

@pytest.mark.parametrize("caso", ["hash", "sin_hash", "version_registro", "feature_version", "sklearn",
                                  "artefacto_ausente"])
def test_identidad_del_modelo_incompatible_se_rechaza(entorno, caso):
    modelo, p = dict(entorno["modelo"]), dict(entorno["pedida"])
    if caso == "hash":
        modelo["artefacto_sha256"] = "0" * 64
    elif caso == "sin_hash":
        modelo["artefacto_sha256"] = None
    elif caso == "version_registro":
        modelo["version"] = "gbm_fe31_otra"
    elif caso == "feature_version":
        p["feature_version"] = "sobusa-fe-2.1-d1"
    elif caso == "artefacto_ausente":
        modelo["artefacto_ruta"] = "borrado.pkl"
    with repo_falso(entorno, prediccion=p, modelo=modelo), pytest.raises(Conflicto):
        if caso == "sklearn":
            with patch.object(G.sklearn, "__version__", "0.0.0"):
                servicios.explicar_prediccion(p["prediction_id"])
        else:
            servicios.explicar_prediccion(p["prediction_id"])


def test_version_del_artefacto_distinta_del_registro_se_rechaza(entorno):
    real = G.cargar

    def cargar_con_otra_version(ruta, sha):
        a = real(ruta, sha)
        return G.Artefacto(a.modelo, a.horario, a.intervalos, {**a.meta, "version": "gbm_fe31_otra"})
    with repo_falso(entorno), patch.object(G, "cargar", cargar_con_otra_version), \
         pytest.raises(Conflicto, match="version del artefacto"):
        servicios.explicar_prediccion(entorno["pedida"]["prediction_id"])


def test_estructura_interna_incompatible_da_409(entorno):
    with repo_falso(entorno), \
         patch.object(XG, "validar_estructura", side_effect=XG.ExplicacionNoValida("sin count")), \
         pytest.raises(Conflicto, match="Estructura interna"):
        servicios.explicar_prediccion(entorno["pedida"]["prediction_id"])


# ------------------------------------------------------------------ procedencia y reproducción

def test_viajes_recargados_despues_de_la_ejecucion_se_rechazan(entorno):
    despues = INICIO_EJECUCION + timedelta(minutes=5)
    with repo_falso(entorno, ultima_carga=despues), pytest.raises(Conflicto, match="cargaron de nuevo"):
        servicios.explicar_prediccion(entorno["pedida"]["prediction_id"])


def test_sin_viajes_del_periodo_se_rechaza(entorno):
    with repo_falso(entorno, ultima_carga=None), pytest.raises(Conflicto, match="No hay viajes"):
        servicios.explicar_prediccion(entorno["pedida"]["prediction_id"])


def test_viajes_modificados_sin_rastro_de_carga_fallan_la_reproduccion(entorno):
    """El cambio no deja un cargado_en posterior (riesgo residual), pero la reproducción lo detecta."""
    crudo = entorno["crudo"].copy()
    antes = (crudo.idruta == RUTA) & (crudo.fechasalida < DIA) & (crudo.fechasalida >= DIA - timedelta(days=7))
    crudo.loc[antes, "numeropasajeros"] = crudo.loc[antes, "numeropasajeros"] * 1.5
    with repo_falso(entorno, crudo=crudo), pytest.raises(Conflicto, match="Reproducción rechazada"):
        servicios.explicar_prediccion(entorno["pedida"]["prediction_id"])


def test_cambio_en_otra_fila_de_la_ejecucion_tambien_rechaza(entorno):
    """No basta con que la fila pedida coincida: se exige la ejecución y la ruta completas."""
    filas = [dict(f) for f in entorno["filas"]]
    otra = next(f for f in filas if f["prediction_id"] != entorno["pedida"]["prediction_id"])
    otra["limite_superior"] = otra["limite_superior"] + 1
    with repo_falso(entorno, filas=filas), pytest.raises(Conflicto, match="Reproducción rechazada"):
        servicios.explicar_prediccion(entorno["pedida"]["prediction_id"])


@pytest.mark.parametrize("caso", ["falta_franja", "otro_modelo", "otro_corte", "sin_ejecucion", "corte_no_dia"])
def test_ejecucion_incoherente_se_rechaza(entorno, caso):
    filas, p, ejecucion = [dict(f) for f in entorno["filas"]], dict(entorno["pedida"]), "por_defecto"
    if caso == "falta_franja":
        filas = [f for f in filas if f["prediction_id"] != filas[-1]["prediction_id"]]
    elif caso == "otro_modelo":
        filas[-1]["model_id"] = 8
    elif caso == "otro_corte":
        filas[-1]["data_cutoff"] = filas[-1]["data_cutoff"] - timedelta(days=1)
    elif caso == "sin_ejecucion":
        ejecucion = None
    elif caso == "corte_no_dia":
        p["data_cutoff"] = p["data_cutoff"] + timedelta(hours=6)
    with repo_falso(entorno, prediccion=p, filas=filas, ejecucion=ejecucion), pytest.raises(Conflicto):
        servicios.explicar_prediccion(p["prediction_id"])


def test_identidad_treeshap_rota_es_error_interno(entorno):
    with repo_falso(entorno), \
         patch.object(XG, "treeshap_fila", side_effect=lambda m, x, ctx=None: np.zeros(29)), \
         pytest.raises(XG.ExplicacionNoValida):
        servicios.explicar_prediccion(entorno["pedida"]["prediction_id"])


# ------------------------------------------------------------------ extremo a extremo por HTTP

def test_la_respuesta_real_del_servicio_cumple_el_esquema_http(entorno):
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    from app.api import main
    cliente = TestClient(main.app, raise_server_exceptions=False)
    pid = entorno["pedida"]["prediction_id"]
    with repo_falso(entorno):
        r = cliente.get(f"/api/v1/predictions/{pid}/explanation")
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == "reconstruida_y_verificada" and len(cuerpo["aportes"]) == 29
    assert cuerpo["execution_id"] == EID and cuerpo["model_version"] == "gbm_fe31_prueba"
    with repo_falso(entorno, ultima_carga=INICIO_EJECUCION + timedelta(days=1)):
        r = cliente.get(f"/api/v1/predictions/{pid}/explanation")
    assert r.status_code == 409 and "aportes" not in r.text
