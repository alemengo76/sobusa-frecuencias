"""RF-015: explicación del MODELO en el tablero (consume GET /predictions/{id}/explanation del backend A1).

Las respuestas del endpoint son SIMULADAS con el contrato del backend A1 (rama feature/rf015-treeshap):
esto prueba la integración visual, no la integración real con la API, que queda pendiente hasta
integrar ambos PR. Reutiliza el arnés de test_tablero_rf006_rf015 (API y Streamlit falsos).
"""
import json
import math
from pathlib import Path

import pandas as pd
import pytest
import requests

from dashboard import explicacion as xp
from tests.unit.test_tablero_rf006_rf015 import (  # noqa: F401  (sin_red es un fixture autouse)
    E_PRED_A, E_PRED_B, F1, F2, F3, ApiFalsa, ejecutar, prediccion, sin_red)

RAIZ = Path(__file__).resolve().parents[2]
META_OFICIAL = RAIZ / "modelos" / "gbm_fe31_20261107T065452.json"
VARIABLES = json.loads(META_OFICIAL.read_text(encoding="utf-8"))["variables"]
BOTON = "Consultar explicación"
REGISTRO_GBM = {
    "model_id": 3, "version": "gbm_fe31_v1", "tipo": "candidato", "hiperparametros": {"metodo": "gbm_fe31"},
    "metricas": {"evaluacion": "2025-04-01..2025-06-30 (filas comunes a todos los métodos)",
                 "importancia_top10": [{"variable": "y_media_4_semanas", "importancia_mae": 306.032},
                                       {"variable": "horizonte_min", "importancia_mae": 56.393}]}}
REGISTRO_BASE = {"model_id": 1, "version": "baseline_dh_v1", "tipo": "baseline",
                 "hiperparametros": {"semanas": 4}, "metricas": {}}


def explicacion_valida(fila, **cambios):
    """Respuesta con el contrato del backend A1 (ExplicacionPrediccion), coherente con ``fila``."""
    aportes = [{"variable": v, "valor": float(i), "valor_modelo": float(i), "aporte_log": (i - 14) / 100,
                "factor": math.exp((i - 14) / 100)} for i, v in enumerate(VARIABLES)]
    aportes.sort(key=lambda a: abs(a["aporte_log"]), reverse=True)
    referencia = 6.0
    pred_log = referencia + math.fsum(a["aporte_log"] for a in aportes)
    cuerpo = {
        "prediction_id": fila["prediction_id"], "execution_id": fila["execution_id"],
        "model_id": fila["model_id"], "model_version": fila["model_version"],
        "feature_version": "sobusa-fe-3.1",
        "artefacto_sha256": "a" * 64,
        "scikit_learn": "1.9.1", "idruta": fila["idruta"], "franja_inicio": fila["franja_inicio"],
        "horizonte_min": fila["horizonte_min"], "data_cutoff": fila["data_cutoff"],
        "demanda_pred": fila["demanda_pred"], "estado": "reconstruida_y_verificada",
        "advertencia": "Explicación reconstruida y verificada, no garantizada.",
        "metodo": "treeshap_dependiente_del_camino",
        "escala": "log", "referencia_log": referencia, "referencia_pasajeros": math.exp(referencia),
        "prediccion_log": pred_log, "prediccion_pasajeros": math.exp(pred_log), "aportes": aportes,
        "verificaciones": {"reproduccion": {"predicciones_comparadas": 195},
                           "identidad_treeshap": {"error_absoluto": 1e-15, "tolerancia": 1e-9}},
        "importancia_global_artefacto": {
            "metodo": "ganancia_de_division", "model_version": fila["model_version"], "nota": "global",
            "variables": [{"variable": "y_media_4_semanas", "ganancia": 2.6e9, "proporcion": 0.8448,
                           "divisiones": 3384}]}}
    cuerpo.update(cambios)
    return cuerpo


PRED_1, PRED_2 = prediccion(1, F1, pred=100.0), prediccion(2, F2, pred=120.0)


def api_con(explicaciones=None, modelos=(REGISTRO_GBM,), predicciones=(PRED_1, PRED_2), **otros):
    return ApiFalsa(predicciones=list(predicciones), modelos=list(modelos),
                    explicaciones=explicaciones or {}, **otros)


def llamadas_explicacion(api):
    return [r for r, _ in api.llamadas if r.endswith("/explanation")]


def tablas(st, columna):
    return [d for d in st.de_tipo("dataframe") if isinstance(d, pd.DataFrame) and columna in d]


def sin_explicacion(st):
    """Ninguna parte de una explicación individual quedó en pantalla."""
    assert tablas(st, "aporte (escala log)") == []
    assert tablas(st, "proporción de la ganancia") == []
    assert not [i for i in st.de_tipo("info") if str(i).startswith("Estado:")]
    assert xp.TEXTO_ESCALA not in st.textos()


# ------------------------------------------------------------------ secciones y conceptos separados

def test_sin_pulsar_no_se_consulta_y_se_muestra_la_importancia_global(monkeypatch):
    api = api_con()
    st = ejecutar(monkeypatch, api)
    assert llamadas_explicacion(api) == []
    subtitulos = st.de_tipo("subheader")
    for s in ("Explicación del modelo de demanda", "Importancia global del modelo",
              "Explicación individual de una predicción"):
        assert s in subtitulos
    (perm,) = tablas(st, "importancia_mae")
    assert perm.to_dict("records") == REGISTRO_GBM["metricas"]["importancia_top10"]   # tal cual
    textos = st.textos()
    assert "MODELO DE EVALUACIÓN" in textos and "no del artefacto de producción" in textos
    assert "2025-04-01..2025-06-30" in textos
    sin_explicacion(st)


def test_la_regla_operativa_queda_separada_del_modelo(monkeypatch):
    st = ejecutar(monkeypatch, api_con())
    textos = st.textos()
    assert "La regla operativa que recomienda buses y frecuencia se explica en la pestaña" in textos


# ------------------------------------------------------------------ explicación válida

def test_explicacion_valida_se_muestra_completa_con_sus_valores(monkeypatch):
    cuerpo = explicacion_valida(PRED_1)
    api = api_con({1: (200, cuerpo)})
    st = ejecutar(monkeypatch, api, valores={"Predicción a explicar": "1"}, pulsados={BOTON})
    assert llamadas_explicacion(api) == ["predictions/1/explanation"]
    (aportes,) = tablas(st, "aporte (escala log)")
    assert len(aportes) == 29
    assert aportes["variable"].tolist() == [a["variable"] for a in cuerpo["aportes"]]
    # sin recalcular: los mismos valores que devolvió la API
    assert aportes["aporte (escala log)"].tolist() == [a["aporte_log"] for a in cuerpo["aportes"]]
    assert aportes["factor"].tolist() == [a["factor"] for a in cuerpo["aportes"]]
    primera = cuerpo["aportes"][0]
    sentido = "la aumenta" if primera["aporte_log"] > 0 else "la disminuye"
    esperado = f"multiplica la predicción por {primera['factor']:.4f} ({sentido})"
    assert aportes["interpretación"].iloc[0] == esperado
    textos = st.textos()
    for esperado in ("Predicción 1", f"ejecución {E_PRED_A}", "modelo gbm_fe31_v1", "model_id 3",
                     "Estado: reconstruida_y_verificada", "no garantizada",
                     xp.TEXTO_ESCALA, xp.TEXTO_REFERENCIA,
                     "195 predicciones de la ejecución y la ruta reproducidas",
                     f"Referencia: {cuerpo['referencia_pasajeros']} pasajeros"):
        assert esperado in textos, esperado
    (ganancia,) = tablas(st, "proporción de la ganancia")
    assert ganancia["variable"].tolist() == ["y_media_4_semanas"]
    assert "Importancia global por ganancia del artefacto de producción" in st.de_tipo("subheader")
    assert xp.TEXTO_GANANCIA.split(":")[0] in textos
    assert tablas(st, "importancia_mae") and tablas(st, "importancia_mae")[0] is not ganancia   # distintas


def test_se_consulta_la_prediccion_elegida_y_no_otra(monkeypatch):
    api = api_con({1: (200, explicacion_valida(PRED_1)), 2: (200, explicacion_valida(PRED_2))})
    st = ejecutar(monkeypatch, api, valores={"Predicción a explicar": "2"}, pulsados={BOTON})
    assert llamadas_explicacion(api) == ["predictions/2/explanation"]
    assert "Predicción 2" in st.textos() and "Predicción 1 " not in st.textos()


def test_solo_se_ofrecen_predicciones_del_modelo_y_la_ejecucion_mostrados(monkeypatch):
    otro_modelo = prediccion(3, F3, model_id=1, version="baseline_dh_v1")
    otra_ejecucion = prediccion(4, F1, eid=E_PRED_B, generada="2025-09-30T01:00:00-05:00")
    api = api_con(predicciones=(PRED_1, PRED_2, otro_modelo, otra_ejecucion))
    st = ejecutar(monkeypatch, api, valores={"Modelo": ("3", "gbm_fe31_v1"),
                                             "Ejecución de predicción": E_PRED_A})
    assert [o.split("prediction_id ")[1] for o in st.opciones["Predicción a explicar"]] == ["1", "2"]


# ------------------------------------------------------------------ rechazos de la API

@pytest.mark.parametrize("codigo, mensaje", [
    (404, "Predicción 1 no existe"),
    (409, "La predicción 1 es del modelo baseline_dh_v1: las predicciones de línea base no se explican"),
    (409, "Reproducción rechazada: 3 de 195 predicciones recalculadas no coinciden"),
    (409, "Viajes de la ruta 16 del período se cargaron de nuevo después del inicio de la ejecución"),
    (500, "Error interno; use el correlation_id para rastrearlo"),
], ids=["404", "409_linea_base", "409_reproduccion", "409_procedencia", "500"])
def test_rechazo_de_la_api_muestra_el_motivo_y_ninguna_explicacion(monkeypatch, codigo, mensaje):
    cuerpo = {"error": {"codigo": "x", "mensaje": mensaje, "detalle": None, "correlation_id": "c"}}
    st = ejecutar(monkeypatch, api_con({1: (codigo, cuerpo)}), valores={"Predicción a explicar": "1"},
                  pulsados={BOTON})
    (error,) = [e for e in st.de_tipo("error") if "predicción 1" in e]
    assert f"(HTTP {codigo})" in error and mensaje in error and "No se muestra ninguna explicación" in error
    sin_explicacion(st)


def test_error_de_red_no_muestra_explicacion(monkeypatch):
    api = api_con({1: (200, explicacion_valida(PRED_1))})

    def con_caida(url, params=None, timeout=None, **otros):
        if url.endswith("/explanation"):
            raise requests.ConnectionError("caída")
        return api(url, params=params, timeout=timeout, **otros)
    st = ejecutar(monkeypatch, con_caida, valores={"Predicción a explicar": "1"}, pulsados={BOTON})
    assert any("No se pudo consultar la explicación de la predicción 1 (ConnectionError)" in e
               for e in st.de_tipo("error"))
    sin_explicacion(st)


@pytest.mark.parametrize("cambio, motivo", [
    ({"prediction_id": 2}, "prediction_id"),
    ({"execution_id": E_PRED_B}, "ejecución"),
    ({"model_version": "gbm_fe31_otro"}, "model_version"),
    ({"model_id": 9}, "model_id"),
    ({"estado": "parcial"}, "estado"),
    ({"aportes": []}, "29 aportes"),
    ({"prediccion_log": 99.0}, "no suman"),
    ({"verificaciones": None}, "verificaciones"),
    ({"importancia_global_artefacto": None}, "importancia global"),
], ids=["otra_prediccion", "otra_ejecucion", "otra_version", "otro_modelo", "estado", "incompleta",
        "suma", "sin_verificaciones", "sin_importancia"])
def test_respuesta_incoherente_o_de_otra_prediccion_se_descarta(monkeypatch, cambio, motivo):
    st = ejecutar(monkeypatch, api_con({1: (200, explicacion_valida(PRED_1, **cambio))}),
                  valores={"Predicción a explicar": "1"}, pulsados={BOTON})
    (error,) = [e for e in st.de_tipo("error") if "Se descartó la respuesta" in e]
    assert motivo in error and "No se muestra ninguna explicación" in error
    sin_explicacion(st)


def test_respuesta_200_sin_json_se_descarta(monkeypatch):
    st = ejecutar(monkeypatch, api_con({1: (200, b"<html>ok</html>")}),
                  valores={"Predicción a explicar": "1"}, pulsados={BOTON})
    assert any("Se descartó la respuesta" in e for e in st.de_tipo("error"))
    sin_explicacion(st)


# ------------------------------------------------------------------ línea base y registro

def test_linea_base_no_ofrece_explicacion_individual(monkeypatch):
    base = prediccion(1, F1, model_id=1, version="baseline_dh_v1")
    api = api_con(modelos=(REGISTRO_BASE,), predicciones=(base,))
    st = ejecutar(monkeypatch, api, pulsados={BOTON})
    assert xp.TEXTO_LINEA_BASE in st.de_tipo("info")
    assert "Predicción a explicar" not in st.opciones and llamadas_explicacion(api) == []
    assert "Importancia por permutación: no disponible" in st.textos()
    sin_explicacion(st)


def test_sin_registro_de_modelos_la_api_decide_y_se_avisa(monkeypatch):
    api = api_con({1: (200, explicacion_valida(PRED_1))}, fallar={"models"})
    st = ejecutar(monkeypatch, api, valores={"Predicción a explicar": "1"}, pulsados={BOTON})
    assert any("No se pudo consultar el registro de modelos" in w for w in st.de_tipo("warning"))
    assert "Importancia por permutación: no disponible" in st.textos()
    assert len(tablas(st, "aporte (escala log)")) == 1                     # la API verificó la predicción


# ------------------------------------------------------------------ funciones puras

def test_validar_explicacion_acepta_la_respuesta_coherente():
    assert xp.validar_explicacion(explicacion_valida(PRED_1), PRED_1) is None


def test_importancia_permutacion_solo_con_datos_validos():
    assert xp.importancia_permutacion(REGISTRO_GBM) == REGISTRO_GBM["metricas"]["importancia_top10"]
    assert xp.importancia_permutacion(REGISTRO_BASE) is None
    assert xp.importancia_permutacion({"metricas": {"importancia_top10": [{"variable": "x"}]}}) is None
    assert xp.importancia_permutacion(None) is None


def test_modelo_registrado_exige_id_y_version():
    assert xp.modelo_registrado([REGISTRO_GBM], ("3", "gbm_fe31_v1")) is REGISTRO_GBM
    assert xp.modelo_registrado([REGISTRO_GBM], ("3", "otra")) is None
    assert xp.modelo_registrado([REGISTRO_GBM, dict(REGISTRO_GBM)], ("3", "gbm_fe31_v1")) is None
    assert xp.es_gbm(REGISTRO_GBM) and not xp.es_gbm(REGISTRO_BASE) and not xp.es_gbm(None)
