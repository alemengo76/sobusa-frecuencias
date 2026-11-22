"""RF-015 en el tablero: explicación del modelo de demanda (TreeSHAP) integrada desde 646ce5b.

Las respuestas de GET /predictions/{id}/explanation son SIMULADAS con el contrato del backend
(app/api/esquemas.ExplicacionPrediccion, ya en main). Los datos son sintéticos. Prueban la integración
con el tablero, no contra la API real. Reutilizan el arnés de test_tablero.py.
"""
import json
import math
from pathlib import Path

import pandas as pd
import pytest
import requests

from dashboard import explicacion as xp
from tests.unit.test_tablero import (  # noqa: F401  (sin_red es un fixture autouse)
    EID_EXPLICACION,
    MODELOS,
    ApiTablero,
    api_demanda,
    correr,
    explicacion_sintetica,
    respuesta,
    respuesta_json,
    sin_red,
)

RAIZ = Path(__file__).resolve().parents[2]
from app.ml.modelo_gbm import VARIABLES
SECCION = "Modelos y explicaciones"
BOTON = "Solicitar explicación"
FILA = {"prediction_id": 771201, "execution_id": EID_EXPLICACION, "model_id": 2,
        "model_version": "gbm_fe31_fixture"}
REGISTRO_GBM = {"model_id": 3, "version": "gbm_fe31_v1", "hiperparametros": {"metodo": "gbm_fe31"},
                "metricas": {"importancia_top10": [{"variable": "y_media_4_semanas", "importancia_mae": 306.032},
                                                   {"variable": "horizonte_min", "importancia_mae": 56.393}]}}
REGISTRO_BASE = {"model_id": 1, "version": "baseline_dh_v1", "hiperparametros": {"semanas": 4}, "metricas": {}}


def explicacion_real_de_variables(**cambios):
    """Como explicacion_sintetica pero con los 29 nombres reales de FE 3.1 (valores sintéticos)."""
    aportes = [{"variable": v, "valor": float(i), "valor_modelo": float(i), "aporte_log": (i - 14) / 100,
                "factor": math.exp((i - 14) / 100)} for i, v in enumerate(VARIABLES)]
    ref = 6.0
    pred_log = ref + math.fsum(a["aporte_log"] for a in aportes)
    return explicacion_sintetica(aportes=aportes, referencia_log=ref, referencia_pasajeros=math.exp(ref),
                                 prediccion_log=pred_log, prediccion_pasajeros=math.exp(pred_log), **cambios)


def api_explicacion(cuerpo=None, codigo=200, pid=771201, contenido=None):
    def manejar(_):
        if contenido is not None:
            return respuesta(codigo, contenido)
        return respuesta_json(codigo, explicacion_sintetica() if cuerpo is None else cuerpo)
    return ApiTablero({f"predictions/{pid}/explanation": manejar})


def con_fila(fila=FILA):
    """Estado de sesión como si la predicción se hubiera visto en la tabla de predicciones."""
    return {"predicciones_vistas": [fila["prediction_id"]],
            "predicciones_filas": {fila["prediction_id"]: dict(fila)}}


def tablas(st, columna):
    return [d for d in st.de_tipo("dataframe") if isinstance(d, pd.DataFrame) and columna in d.columns]


def sin_explicacion(st):
    assert tablas(st, "Aporte (log)") == [] and st.de_tipo("download_button") == []
    assert tablas(st, "Proporción") == []


def llamadas(api):
    return [r for r, _, _ in api.llamadas if r.endswith("/explanation")]


# ------------------------------------------------------------------ funciones puras

def test_validar_acepta_la_respuesta_coherente_con_y_sin_fila():
    assert xp.validar_explicacion(explicacion_sintetica(), 771201, FILA) is None
    assert xp.validar_explicacion(explicacion_sintetica(), 771201) is None
    assert xp.validar_explicacion(explicacion_real_de_variables(), 771201, FILA) is None
    assert len(VARIABLES) == xp.N_VARIABLES_MODELO


def test_validar_acepta_el_uuid_en_mayusculas():
    fila = {**FILA, "execution_id": EID_EXPLICACION.upper()}
    assert xp.validar_explicacion(explicacion_sintetica(), 771201, fila) is None


def _aportes_con(mutar):
    x = explicacion_sintetica()
    aportes = [dict(a) for a in x["aportes"]]
    mutar(aportes)
    return {"aportes": aportes}


@pytest.mark.parametrize("cambios, fila, motivo", [
    ({"prediction_id": 2}, FILA, "prediction_id"),
    ({"execution_id": "00000000-0000-4000-8000-000000000002"}, FILA, "ejecución"),
    ({"model_version": "gbm_fe31_otro"}, FILA, "model_version"),
    ({"model_id": 9}, FILA, "model_id"),
    ({"execution_id": "no-es-uuid"}, None, "execution_id"),
    ({"estado": "parcial"}, FILA, "estado"),
    ({"aportes": []}, FILA, "29 aportes"),
    ({"aportes": None}, FILA, "29 aportes"),
    (_aportes_con(lambda a: a.pop()), FILA, "29 aportes"),
    (_aportes_con(lambda a: a.__setitem__(5, {**a[5], "variable": a[4]["variable"]})), FILA, "repetidas"),
    (_aportes_con(lambda a: a.__setitem__(3, {**a[3], "aporte_log": float("nan")})), FILA, "no numéricos"),
    (_aportes_con(lambda a: a.__setitem__(3, {**a[3], "aporte_log": "0.1"})), FILA, "no numéricos"),
    (_aportes_con(lambda a: a.__setitem__(3, {**a[3], "factor": 7.0})), FILA, "exp(aporte_log)"),
    ({"prediccion_log": 99.0}, FILA, "no suman"),
    ({"prediccion_pasajeros": 1.0}, FILA, "prediccion_pasajeros"),
    ({"referencia_pasajeros": float("inf")}, FILA, "referencia_pasajeros"),
    ({"referencia_log": None}, FILA, "referencia_log"),
    ({"verificaciones": None}, FILA, "verificaciones"),
    ({"verificaciones": {}}, FILA, "verificaciones"),
    ({"verificaciones": {"identidad_treeshap": {"error_absoluto": 1e-3, "tolerancia": 1e-9}}}, FILA, "identidad"),
    ({"importancia_global_artefacto": None}, FILA, "importancia global"),
    ({"importancia_global_artefacto": {"variables": []}}, FILA, "importancia global"),
], ids=["otra_prediccion", "otra_ejecucion", "otra_version", "otro_modelo", "eid_invalido_sin_fila", "estado",
        "sin_aportes", "aportes_nulos", "28_aportes", "variable_repetida", "aporte_nan", "aporte_texto",
        "factor_incoherente", "suma", "pasajeros_incoherentes", "referencia_inf", "sin_referencia",
        "sin_verificaciones", "verificaciones_vacias", "identidad_fuera", "sin_importancia", "importancia_vacia"])
def test_validar_rechaza_respuestas_incompletas_o_inconsistentes(cambios, fila, motivo):
    resultado = xp.validar_explicacion(explicacion_sintetica(**cambios), 771201, fila)
    assert resultado is not None and motivo in resultado


def test_validar_rechaza_lo_que_no_es_un_objeto():
    assert xp.validar_explicacion([1, 2], 1) == "la respuesta no es un objeto"
    assert xp.validar_explicacion(None, 1) == "la respuesta no es un objeto"


def test_importancia_permutacion_solo_con_datos_validos():
    assert xp.importancia_permutacion(REGISTRO_GBM) == REGISTRO_GBM["metricas"]["importancia_top10"]
    assert xp.importancia_permutacion(REGISTRO_BASE) is None
    assert xp.importancia_permutacion({"metricas": {"importancia_top10": [{"variable": "x"}]}}) is None
    assert xp.importancia_permutacion({"metricas": {"importancia_top10": [{"variable": "x",
                                                                          "importancia_mae": "nan"}]}}) is None
    assert xp.importancia_permutacion(None) is None


def test_modelo_registrado_exige_id_y_version():
    assert xp.modelo_registrado([REGISTRO_GBM], 3, "gbm_fe31_v1") is REGISTRO_GBM
    assert xp.modelo_registrado([REGISTRO_GBM], "3", "gbm_fe31_v1") is REGISTRO_GBM
    assert xp.modelo_registrado([REGISTRO_GBM], 3, "otra") is None
    assert xp.modelo_registrado([REGISTRO_GBM, dict(REGISTRO_GBM)], 3, "gbm_fe31_v1") is None
    assert xp.modelo_registrado(None, 3, "gbm_fe31_v1") is None
    assert xp.es_gbm(REGISTRO_GBM) and not xp.es_gbm(REGISTRO_BASE) and not xp.es_gbm(None)


def test_aportes_ordenados_por_magnitud_con_interpretacion():
    filas = xp.filas_aportes(explicacion_sintetica())
    assert [f["variable"] for f in filas[:2]] == ["y_media_4_semanas", "tipo_dia"]
    assert filas[0]["efecto"] == "aumenta" and "la aumenta" in filas[0]["interpretacion"]
    assert filas[1]["efecto"] == "disminuye" and "la disminuye" in filas[1]["interpretacion"]
    r = xp.resumen_aportes(explicacion_sintetica())
    assert r["mayor_sube"] == "y_media_4_semanas" and r["mayor_baja"] == "tipo_dia"
    assert r["suben"] + r["bajan"] == 29


def test_las_dos_importancias_tienen_textos_distintos_y_no_se_mezclan():
    assert "PERMUTACIÓN" not in xp.TEXTO_GANANCIA and "ARTEFACTO DE PRODUCCIÓN" in xp.TEXTO_GANANCIA
    assert "MODELO DE EVALUACIÓN" in xp.TEXTO_PERMUTACION
    assert "No explica cuántos buses" in xp.TEXTO_ALCANCE


# ------------------------------------------------------------------ integración con el tablero

def test_seleccion_desde_la_tabla_consulta_esa_prediccion_y_la_contrasta(monkeypatch):
    api = api_explicacion()
    st = correr(monkeypatch, SECCION, get=api, estado=con_fila(),
                valores={"…o una de la última tabla de predicciones": 771201}, pulsados={BOTON})
    assert llamadas(api) == ["predictions/771201/explanation"]
    (tabla,) = tablas(st, "Aporte (log)")
    assert len(tabla) == 29 and {"Efecto", "Interpretación"} <= set(tabla.columns)
    assert st.de_tipo("error") == []
    assert not any("se escribió a mano" in c for c in st.de_tipo("caption"))
    assert st.de_tipo("download_button") == ["Descargar aportes (CSV)", "Descargar aportes (Excel)"]
    assert len(tablas(st, "Proporción")) == 1                     # importancia por ganancia del artefacto


def test_id_escrito_a_mano_se_valida_y_se_advierte_que_no_se_contrasto(monkeypatch):
    st = correr(monkeypatch, SECCION, get=api_explicacion(), valores={"ID de la predicción": 771201},
                pulsados={BOTON})
    assert len(tablas(st, "Aporte (log)")) == 1
    assert any("se escribió a mano" in c for c in st.de_tipo("caption"))


@pytest.mark.parametrize("cambios, motivo", [
    ({"execution_id": "00000000-0000-4000-8000-000000000002"}, "ejecución"),
    ({"model_version": "otra"}, "model_version"),
    ({"aportes": explicacion_sintetica()["aportes"][:2]}, "29 aportes"),
    ({"prediccion_log": 1.0}, "no suman"),
    ({"prediction_id": 5}, "prediction_id"),
], ids=["otra_ejecucion", "otro_modelo", "incompleta", "no_suma", "otra_prediccion"])
def test_respuesta_inconsistente_no_muestra_ninguna_parte(monkeypatch, cambios, motivo):
    api = api_explicacion(explicacion_sintetica(**cambios))
    st = correr(monkeypatch, SECCION, get=api, estado=con_fila(),
                valores={"…o una de la última tabla de predicciones": 771201}, pulsados={BOTON})
    (error,) = st.de_tipo("error")
    assert motivo.replace("_", "\\_") in error and "No se muestra ninguna parte" in error   # texto escapado (Markdown)
    sin_explicacion(st)


@pytest.mark.parametrize("codigo, tipo, texto", [
    (409, "info", "no puede explicarse"), (404, "warning", "no existe"),
    (500, "error", "No se pudo obtener la explicación"), (503, "error", "No se pudo obtener la explicación"),
], ids=["409", "404", "500", "503"])
def test_errores_http_del_endpoint(monkeypatch, codigo, tipo, texto):
    cuerpo = {"error": {"codigo": "x", "mensaje": "m", "detalle": None, "correlation_id": "cid-1"}}
    st = correr(monkeypatch, SECCION, get=api_explicacion(cuerpo, codigo), estado=con_fila(),
                valores={"…o una de la última tabla de predicciones": 771201}, pulsados={BOTON})
    assert any(texto in m for m in st.de_tipo(tipo))
    sin_explicacion(st)


def test_respuesta_200_que_no_es_json_no_muestra_explicacion(monkeypatch):
    st = correr(monkeypatch, SECCION, get=api_explicacion(contenido=b"<html>ok</html>"), estado=con_fila(),
                valores={"…o una de la última tabla de predicciones": 771201}, pulsados={BOTON})
    assert len(st.de_tipo("error")) == 1
    sin_explicacion(st)


def test_caida_de_red_no_interrumpe_el_tablero(monkeypatch):
    api = ApiTablero({"predictions/771201/explanation": lambda p: requests.ConnectionError("sin red")})
    st = correr(monkeypatch, SECCION, get=api, estado=con_fila(),
                valores={"…o una de la última tabla de predicciones": 771201}, pulsados={BOTON})
    assert len(st.de_tipo("error")) == 1
    sin_explicacion(st)
    assert any("Registro de modelos" in str(m) for m in st.de_tipo("markdown"))   # el resto de la sección sigue


def test_linea_base_no_llama_a_la_api_ni_inventa_explicacion(monkeypatch):
    fila = {"prediction_id": 55, "execution_id": EID_EXPLICACION, "model_id": 1, "model_version": "dia_hora_4sem_v1"}
    api = ApiTablero()                                              # cualquier llamada a /explanation fallaría
    st = correr(monkeypatch, SECCION, get=api, estado=con_fila(fila),
                valores={"…o una de la última tabla de predicciones": 55}, pulsados={BOTON})
    assert xp.TEXTO_LINEA_BASE in st.de_tipo("info") and llamadas(api) == []
    sin_explicacion(st)


def test_modelo_no_registrado_deja_decidir_a_la_api(monkeypatch):
    fila = {**FILA, "model_id": 99, "model_version": "desconocido"}
    api = api_explicacion(explicacion_sintetica(model_id=99, model_version="desconocido"))
    st = correr(monkeypatch, SECCION, get=api, estado=con_fila(fila),
                valores={"…o una de la última tabla de predicciones": 771201}, pulsados={BOTON})
    assert llamadas(api) == ["predictions/771201/explanation"] and len(tablas(st, "Aporte (log)")) == 1


def test_sin_pulsar_no_se_consulta_la_explicacion(monkeypatch):
    api = api_explicacion()
    correr(monkeypatch, SECCION, get=api, estado=con_fila())
    assert llamadas(api) == []


def test_ficha_muestra_permutacion_separada_de_la_ganancia(monkeypatch):
    modelos = [{**MODELOS[0], "metricas": {**MODELOS[0]["metricas"],
                                           "importancia_top10": REGISTRO_GBM["metricas"]["importancia_top10"]}},
               *MODELOS[1:]]
    monkeypatch.setattr("tests.unit.test_tablero.MODELOS", modelos)
    st = correr(monkeypatch, SECCION)
    (perm,) = tablas(st, "Aumento del MAE (pasajeros)")
    assert perm["Variable"].tolist() == ["y_media_4_semanas", "horizonte_min"]
    assert xp.TEXTO_PERMUTACION in st.de_tipo("caption")
    assert tablas(st, "Proporción") == []                          # la ganancia solo llega con una explicación


def test_ficha_sin_importancia_lo_dice_sin_inventarla(monkeypatch):
    st = correr(monkeypatch, SECCION)                              # el activo no trae importancia_top10
    assert tablas(st, "Aumento del MAE (pasajeros)") == []
    assert any("no registra importancia por permutación" in str(m) for m in st.de_tipo("markdown"))


def test_regla_operativa_aclara_que_treeshap_no_explica_los_buses(monkeypatch):
    st = correr(monkeypatch, "Demanda y frecuencias", get=api_demanda(),
                valores={"Vista": "02 · Decisión operativa"})
    captions = st.de_tipo("caption")
    assert any("TreeSHAP no explica la cantidad de buses" in c for c in captions)
    assert all(any(t in c for c in captions) for t in xp.NO_DISPONIBLES_RECOMENDACION)


def test_explicar_desde_la_decision_guarda_la_fila_para_contrastarla(monkeypatch):
    estado = {}
    correr(monkeypatch, "Demanda y frecuencias", get=api_demanda(), estado=estado,
           valores={"Vista": "02 · Decisión operativa", "Franja a revisar": 11},
           pulsados={"Explicar esta predicción (RF-015)"})
    assert estado.get("_pid_pendiente") == 101
    assert estado["predicciones_filas"][101] == {"prediction_id": 101, "execution_id": "P1", "model_id": 2,
                                                 "model_version": "gbm_fe31_fixture"}


def test_tabla_de_predicciones_recuerda_ejecucion_y_modelo_de_cada_fila(monkeypatch):
    estado = {}
    correr(monkeypatch, "Demanda y frecuencias", get=api_demanda(), estado=estado, valores={"Tabla": "Predicciones"})
    filas = estado["predicciones_filas"]
    assert filas and all(set(f) == {"prediction_id", "execution_id", "model_id", "model_version"}
                         for f in filas.values())
    assert set(filas) == set(estado["predicciones_vistas"])
