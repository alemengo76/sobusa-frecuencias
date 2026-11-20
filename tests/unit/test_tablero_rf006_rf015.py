"""RF-006 (incertidumbre) y RF-015 (explicación) en dashboard/tablero.py y dashboard/explicacion.py.

Dos niveles:
- funciones de dashboard/explicacion.py, directamente;
- el tablero completo con runpy.run_path, un Streamlit falso ESTRICTO (implementa solo lo que usa el
  tablero) y una API falsa que respeta los filtros, el orden y la paginación de la API real.
No usa red, base de datos, credenciales ni .env.

La API falsa compara instantes, como PostgreSQL al comparar timestamptz: un valor sin zona se toma
en America/Bogota, la zona de sesión de la API (app/db/conexion.py). No verificado contra la base real.
"""
import contextlib
import json
import random
import runpy
import sys
import types
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy  # noqa: F401  (se cargan antes de ejecutar el tablero)
import pandas as pd
import pytest
import requests

from dashboard import explicacion as xp

TABLERO = Path(__file__).resolve().parents[2] / "dashboard" / "tablero.py"
API = "http://api-falsa.invalid/api/v1"
E_PRED_A, E_PRED_B = "aaaaaaaa-0000-0000-0000-00000000000a", "bbbbbbbb-0000-0000-0000-00000000000b"
E_FREC_A, E_FREC_B = "cccccccc-0000-0000-0000-00000000000c", "dddddddd-0000-0000-0000-00000000000d"
F1, F2, F3 = "2025-10-01T06:00:00-05:00", "2025-10-01T06:15:00-05:00", "2025-10-01T06:30:00-05:00"
F_OTRO_DIA = "2025-10-02T06:00:00-05:00"


# ------------------------------------------------------------------------------------ datos

def prediccion(i, franja, *, eid=E_PRED_A, pred=100.0, li=80.0, ls=120.0, idruta=16, h=60,
               model_id=3, version="gbm_fe31_v1", generada="2025-10-01T01:00:00-05:00",
               cutoff="2025-10-01T00:00:00-05:00"):
    return {"prediction_id": i, "execution_id": eid, "idruta": idruta, "sentido": "CICLO",
            "segmento_id": "TOTAL", "franja_inicio": franja, "franja_minutos": 15, "horizonte_min": h,
            "demanda_pred": pred, "limite_inferior": li, "limite_superior": ls, "nivel_intervalo": 0.8,
            "definicion_tiempo": "salida_programada", "model_id": model_id, "model_version": version,
            "feature_version": "sobusa-fe-3.1", "data_cutoff": cutoff, "generated_at": generada,
            "latency_ms": 12}


def recomendacion(i, franja, *, eid=E_FREC_A, idruta=16, h=60, viable=True, **cambios):
    fila = {"recommendation_id": i, "execution_id": eid, "prediction_id": None, "idruta": idruta,
            "sentido": "CICLO", "segmento_id": "TOTAL", "franja_inicio": franja, "horizonte_min": h,
            "demanda_hora": 100.0, "base_demanda": "puntual", "factor_carga_max": 0.435,
            "carga_max_estimada_hora": 43.5, "capacidad_nominal": 55.0,
            "capacidad_fuente": "derivado_plazas", "ocupacion_objetivo": 0.75,
            "capacidad_efectiva": 41.25, "buses_hora_recomendado": 2 if viable else None,
            "headway_recomendado_min": 30.0 if viable else None, "ciclo_min": 238.8,
            "vehiculos_requeridos": 8, "flota_tope": 99, "buses_hora_actual": 4.0,
            "headway_actual_min": 15.0, "delta_buses_hora": -2.0 if viable else None,
            "es_viable": viable, "motivo_no_viable": None if viable else "requiere 120 vehículos y el "
            "tope de flota es 99", "parametros_version": "param_v2:laboral",
            "generated_at": "2025-10-01T01:00:05-05:00"}
    fila.update(cambios)
    return fila


def ejecucion_frecuencias(eid_pred):
    return {"tipo": "frecuencias", "estado": "exitosa", "parametros": {"prediccion": eid_pred},
            "resumen": {"execution_id_prediccion": eid_pred}}


def _instante_bogota(valor):
    t = pd.Timestamp(valor)
    return t if t.tzinfo else t.tz_localize("America/Bogota")


class ApiFalsa:
    """GET con el contrato de la API: filtros de igualdad, desde/hasta sobre franja_inicio,
    ORDER BY id DESC y paginación. Registra cada llamada."""

    def __init__(self, predicciones=(), recomendaciones=(), ejecuciones=None, alertas=(), fallar=(),
                 modelos=(), explicaciones=None):
        self.tablas = {"predictions": ("prediction_id", list(predicciones)),
                       "recommendations": ("recommendation_id", list(recomendaciones)),
                       "alerts": ("alert_id", list(alertas))}
        self.ejecuciones = ejecuciones or {}
        self.fallar = set(fallar)
        self.modelos = list(modelos)                 # filas de GET /models
        self.explicaciones = explicaciones or {}     # prediction_id -> (código HTTP, cuerpo)
        self.llamadas = []

    def __call__(self, url, params=None, timeout=None, **otros):
        assert otros == {}, otros
        ruta = url[len(API) + 1:]
        assert timeout == (120 if ruta.endswith("/explanation") else 10)
        p = dict(params or {})
        self.llamadas.append((ruta, p))
        if ruta in self.fallar:
            return respuesta(500, b'{"error": {"mensaje": "x"}}', url)
        if ruta == "models":
            return respuesta(200, json.dumps({"datos": self.modelos}).encode(), url)
        if ruta.startswith("predictions/") and ruta.endswith("/explanation"):
            pid = int(ruta.split("/")[1])
            codigo, cuerpo = self.explicaciones.get(
                pid, (404, {"error": {"codigo": "no_encontrado", "mensaje": f"Predicción {pid} no existe"}}))
            contenido = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
            return respuesta(codigo, contenido, url)
        if ruta.startswith("frequency/executions/"):
            eid = ruta.rsplit("/", 1)[1]
            if eid not in self.ejecuciones:
                return respuesta(404, b'{"error": {"codigo": "no_encontrado"}}', url)
            return respuesta(200, json.dumps(self.ejecuciones[eid]).encode(), url)
        orden, filas = self.tablas[ruta]
        for k in ("idruta", "horizonte_min", "execution_id", "estado"):
            if p.get(k) is not None:
                filas = [f for f in filas if str(f.get(k)) == str(p[k])]
        if p.get("desde"):
            filas = [f for f in filas if _instante_bogota(f["franja_inicio"]) >= _instante_bogota(p["desde"])]
        if p.get("hasta"):
            filas = [f for f in filas if _instante_bogota(f["franja_inicio"]) < _instante_bogota(p["hasta"])]
        filas = sorted(filas, key=lambda f: f[orden], reverse=True)
        pagina, tamano = int(p.get("pagina", 1)), int(p.get("tamano", 50))
        cuerpo = {"datos": filas[(pagina - 1) * tamano:pagina * tamano],
                  "paginacion": {"pagina": pagina, "tamano": tamano, "total": len(filas)}}
        return respuesta(200, json.dumps(cuerpo).encode(), url)

    def de(self, ruta):
        return [p for r, p in self.llamadas if r == ruta]


def respuesta(codigo, contenido, url):
    r = requests.models.Response()
    r.status_code, r._content, r.encoding, r.url = codigo, contenido, "utf-8", url
    return r


# ------------------------------------------------------------------------- Streamlit falso estricto

class StFalso(types.ModuleType):
    def __init__(self, valores=None, pulsados=()):
        super().__init__("streamlit")
        self.session_state = {}
        self.valores = valores or {}
        self.pulsados = set(pulsados)
        self.eventos = []
        self.opciones = {}

    def __getattr__(self, nombre):
        raise AttributeError(f"Streamlit falso: st.{nombre} no está simulado")

    def _r(self, tipo, valor=None):
        self.eventos.append((tipo, valor))

    def de_tipo(self, tipo):
        return [v for t, v in self.eventos if t == tipo]

    def textos(self):
        return "\n".join(str(v) for t, v in self.eventos
                         if t in ("caption", "info", "warning", "error", "write", "success", "subheader"))

    def set_page_config(self, **k): self._r("set_page_config", k)
    def title(self, t, **k): self._r("title", t)
    def subheader(self, t, **k): self._r("subheader", t)
    def caption(self, t, **k): self._r("caption", t)
    def write(self, *o, **k): self._r("write", o[0] if len(o) == 1 else o)
    def error(self, t, **k): self._r("error", t)
    def info(self, t, **k): self._r("info", t)
    def success(self, t, **k): self._r("success", t)
    def warning(self, t, **k): self._r("warning", t)
    def json(self, d, **k): self._r("json", d)
    def dataframe(self, d, **k): self._r("dataframe", d)
    def line_chart(self, d, **k): self._r("line_chart", d)
    def bar_chart(self, d, **k): self._r("bar_chart", d)

    @property
    def sidebar(self): return contextlib.nullcontext()
    def tabs(self, nombres): return [contextlib.nullcontext() for _ in nombres]
    def form(self, clave, **k): return contextlib.nullcontext()

    def selectbox(self, etiqueta, opciones, index=0, format_func=str, key=None, **k):
        opciones = list(opciones)
        self.opciones[etiqueta] = [format_func(o) for o in opciones]
        return self.valores.get(etiqueta, opciones[index] if opciones else None)

    def radio(self, etiqueta, opciones, index=0, **k):
        return self.valores.get(etiqueta, list(opciones)[index])
    def date_input(self, etiqueta, valor=None, **k): return self.valores.get(etiqueta, valor)
    def text_input(self, etiqueta, **k): return self.valores.get(etiqueta, "")
    def text_area(self, etiqueta, **k): return self.valores.get(etiqueta, "")
    def button(self, etiqueta, **k): return etiqueta in self.pulsados
    def form_submit_button(self, etiqueta, **k): return etiqueta in self.pulsados


@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    def prohibido(*a, **k):
        raise AssertionError("solicitud HTTP real bloqueada")
    monkeypatch.setattr(requests.sessions.Session, "request", prohibido)
    monkeypatch.setenv("API_URL", API)


def ejecutar(monkeypatch, api, valores=None, pulsados=()):
    st = StFalso(valores, pulsados)
    monkeypatch.setitem(sys.modules, "streamlit", st)
    monkeypatch.setattr(requests, "get", api)

    def escritura_prohibida(*a, **k):
        raise AssertionError("estas pruebas no deben escribir en la API")
    monkeypatch.setattr(requests, "post", escritura_prohibida)
    monkeypatch.setattr(requests, "patch", escritura_prohibida)
    runpy.run_path(str(TABLERO), run_name="__main__")
    return st


def tabla_pasos(st):
    """El DataFrame de pasos de la explicación (el que tiene la columna 'concepto')."""
    tablas = [d for d in st.de_tipo("dataframe") if isinstance(d, pd.DataFrame) and "concepto" in d]
    assert len(tablas) == 1, tablas
    return tablas[0].set_index("concepto")


def tabla_intervalos(st):
    (t,) = [d for d in st.de_tipo("dataframe") if "clasificación" in d]
    return t.set_index("clasificación").franjas


def grafico(st):
    (g,) = st.de_tipo("line_chart")
    return g


def get_de(api):
    return lambda ruta, p: api(f"{API}/{ruta}", params=p, timeout=10).json()


# ============================================================================ paginación

def test_rango_dia_en_bogota_con_desplazamiento_y_hasta_exclusivo():
    assert xp.rango_dia(pd.Timestamp("2026-11-19 13:45")) == ("2026-11-19T00:00:00-05:00",
                                                              "2026-11-20T00:00:00-05:00")
    import datetime as dt
    assert xp.rango_dia(dt.date(2025, 10, 1)) == ("2025-10-01T00:00:00-05:00",
                                                  "2025-10-02T00:00:00-05:00")


def test_rango_dia_cubre_exactamente_el_dia_de_bogota():
    desde, hasta = (pd.Timestamp(x) for x in xp.rango_dia(pd.Timestamp("2025-12-31")))
    assert desde.utcoffset() == hasta.utcoffset() == pd.Timedelta(hours=-5)
    assert hasta - desde == pd.Timedelta(days=1)
    assert desde.tz_convert("America/Bogota").strftime("%Y-%m-%d %H:%M") == "2025-12-31 00:00"
    assert hasta.tz_convert("America/Bogota").strftime("%Y-%m-%d %H:%M") == "2026-01-01 00:00"
    bogota = pd.Timestamp("2025-12-31 23:59:59.999999", tz="America/Bogota")
    assert desde <= pd.Timestamp("2025-12-31 00:00", tz="America/Bogota") and bogota < hasta
    assert not pd.Timestamp("2025-12-30 23:59:59", tz="America/Bogota") >= desde
    assert not pd.Timestamp("2026-01-01 00:00", tz="America/Bogota") < hasta


def test_rango_dia_con_zona_toma_la_fecha_de_bogota():
    # 2025-10-02 03:00 UTC es todavía 1 de octubre (22:00) en Bogotá
    assert xp.rango_dia(pd.Timestamp("2025-10-02T03:00:00+00:00"))[0] == "2025-10-01T00:00:00-05:00"


def test_leer_paginas_completa_todas_las_paginas_y_verifica_la_primera():
    api = ApiFalsa(predicciones=[prediccion(i, F1) for i in range(1, 8)])
    filas = xp.leer_paginas(get_de(api), "predictions", {}, "prediction_id", tamano=3)
    assert [f["prediction_id"] for f in filas] == [7, 6, 5, 4, 3, 2, 1]
    assert [p["pagina"] for p in api.de("predictions")] == [1, 2, 3, 1]      # 1 al final: cierre


def test_leer_paginas_sin_filas_tambien_verifica_la_primera():
    api = ApiFalsa()
    assert xp.leer_paginas(get_de(api), "predictions", {}, "prediction_id") == []
    assert [p["pagina"] for p in api.de("predictions")] == [1, 1]


class ApiConInsercion:
    """Base de ids decrecientes; inserta un id nuevo justo entre el conteo y la consulta de la llamada
    número ``en_llamada`` (la carrera que el total por página no detecta)."""

    def __init__(self, ids, en_llamada):
        self.ids, self.en_llamada, self.llamadas = list(ids), en_llamada, 0

    def __call__(self, ruta, p):
        self.llamadas += 1
        total = len(self.ids)                                  # count(*)
        if self.llamadas == self.en_llamada:
            self.ids.insert(0, max(self.ids) + 1)              # INSERT confirmado entre ambas
        o = (p["pagina"] - 1) * p["tamano"]
        return {"datos": [{"id": i} for i in self.ids[o:o + p["tamano"]]],
                "paginacion": {"total": total}}


def test_insercion_en_una_pagina_unica_llena_se_detecta():
    get = ApiConInsercion(range(500, 0, -1), en_llamada=1)    # sin verificación: falta el id 1
    with pytest.raises(xp.ErrorLectura, match="la página 1 ya no coincide"):
        xp.leer_paginas(get, "predictions", {}, "id", tamano=500)


def test_insercion_antes_de_la_ultima_pagina_se_detecta():
    get = ApiConInsercion(range(6, 0, -1), en_llamada=2)      # página 2 repetiría el id 4
    with pytest.raises(xp.ErrorLectura, match="repetidos"):
        xp.leer_paginas(get, "predictions", {}, "id", tamano=3)


def test_cambio_del_total_entre_paginas_se_detecta():
    respuestas = iter([{"datos": [{"id": 2}], "paginacion": {"total": 2}},
                       {"datos": [{"id": 1}], "paginacion": {"total": 3}}])
    with pytest.raises(xp.ErrorLectura, match="cambió"):
        xp.leer_paginas(lambda r, p: next(respuestas), "predictions", {}, "id", tamano=1)


def test_identificadores_duplicados_se_detectan():
    paginas = {1: [{"id": 5}, {"id": 4}], 2: [{"id": 4}, {"id": 3}]}
    with pytest.raises(xp.ErrorLectura, match="repetidos"):
        xp.leer_paginas(lambda r, p: {"datos": paginas[p["pagina"]], "paginacion": {"total": 4}},
                        "predictions", {}, "id", tamano=2)


def test_cambio_de_la_primera_pagina_al_cerrar_se_detecta():
    lecturas = iter([[{"id": 3}, {"id": 2}], [{"id": 1}], [{"id": 3}, {"id": 9}]])
    with pytest.raises(xp.ErrorLectura, match="la página 1 ya no coincide"):
        xp.leer_paginas(lambda r, p: {"datos": next(lecturas), "paginacion": {"total": 3}},
                        "predictions", {}, "id", tamano=2)


def test_orden_no_estable_se_detecta():
    paginas = {1: [{"id": 5}, {"id": 2}], 2: [{"id": 4}, {"id": 1}]}
    with pytest.raises(xp.ErrorLectura, match="orden"):
        xp.leer_paginas(lambda r, p: {"datos": paginas[p["pagina"]], "paginacion": {"total": 4}},
                        "predictions", {}, "id", tamano=2)


def test_limite_de_filas_y_filas_sin_id_fallan_sin_devolver_datos():
    with pytest.raises(xp.ErrorLectura, match="límite"):
        xp.leer_paginas(lambda r, p: {"datos": [], "paginacion": {"total": 10}}, "x", {}, "id",
                        max_filas=5)
    with pytest.raises(xp.ErrorLectura, match="no tiene 'id'"):
        xp.leer_paginas(lambda r, p: {"datos": [{"otro": 1}], "paginacion": {"total": 1}}, "x", {}, "id")


def test_tablero_muestra_error_y_no_datos_si_la_lectura_es_incoherente(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(1, F1)])
    original = api.__call__
    llamadas = {"n": 0}

    def con_cambio(url, params=None, timeout=None):
        r = original(url, params=params, timeout=timeout)
        if url.endswith("/predictions"):
            llamadas["n"] += 1
            if llamadas["n"] == 2:                             # verificación de cierre distinta
                cuerpo = r.json()
                cuerpo["datos"][0]["prediction_id"] = 99
                return respuesta(200, json.dumps(cuerpo).encode(), url)
        return r
    st = ejecutar(monkeypatch, con_cambio)
    assert any("ya no coincide" in e for e in st.de_tipo("error"))
    assert st.de_tipo("line_chart") == []


# ================================================================================= fechas

def test_asociacion_con_ambas_fechas_con_zona_compara_el_instante():
    preds = [prediccion(10, F1), prediccion(11, F2)]
    a = xp.prediccion_para(recomendacion(1, "2025-10-01T11:15:00+00:00"), preds)   # = F2
    assert a.prediccion["prediction_id"] == 11 and a.motivo is None


def test_asociacion_con_ambas_fechas_sin_zona_compara_tal_cual():
    preds = [prediccion(10, "2025-10-01T06:00:00"), prediccion(11, "2025-10-01T06:15:00")]
    a = xp.prediccion_para(recomendacion(1, "2025-10-01T06:15:00"), preds)
    assert a.prediccion["prediction_id"] == 11


def test_asociacion_con_una_fecha_con_zona_y_otra_sin_zona_no_se_hace():
    # Sin esta regla, 06:00 sin zona tomada como UTC caería en la franja de las 01:00-05:00.
    preds = [prediccion(10, "2025-10-01T01:00:00-05:00"), prediccion(11, F1)]
    a = xp.prediccion_para(recomendacion(1, "2025-10-01T06:00:00"), preds)
    assert a.prediccion is None and a.motivo == xp.ZONA_MIXTA
    a = xp.prediccion_para(recomendacion(1, F1), [prediccion(10, "2025-10-01T06:00:00")])
    assert a.prediccion is None and a.motivo == xp.ZONA_MIXTA


def test_comparables_solo_con_la_misma_condicion_de_zona():
    con, sin = xp.instante(F1), xp.instante("2025-10-01T06:00:00")
    assert xp.comparables(con, con) and xp.comparables(sin, sin)
    assert not xp.comparables(con, sin) and not xp.comparables(con, None)


def test_separar_ejecuciones_ordena_por_fecha_con_zona():
    filas = [prediccion(1, F1, eid=E_PRED_A, generada="2025-10-01T01:00:00-05:00"),
             prediccion(2, F2, eid=E_PRED_A, generada="2025-10-01T01:00:00-05:00"),
             prediccion(3, F1, eid=E_PRED_B, generada="2025-10-01T09:00:00-05:00")]
    g = xp.separar_ejecuciones(filas)
    assert g.por_fecha and [(e.execution_id, e.filas) for e in g.lista] == [(E_PRED_B, 1), (E_PRED_A, 2)]
    assert g.lista[0].generada_hasta == "2025-10-01T09:00:00-05:00"


def test_separar_ejecuciones_sin_zona_tambien_ordena():
    g = xp.separar_ejecuciones([prediccion(1, F1, eid=E_PRED_A, generada="2025-10-01T01:00:00"),
                                prediccion(2, F1, eid=E_PRED_B, generada="2025-10-01T02:00:00")])
    assert g.por_fecha and g.lista[0].execution_id == E_PRED_B


def test_separar_ejecuciones_con_zona_mixta_no_ordena_por_fecha():
    g = xp.separar_ejecuciones([prediccion(1, F1, eid=E_PRED_B, generada="2025-10-01T09:00:00"),
                                prediccion(2, F1, eid=E_PRED_A, generada="2025-10-01T01:00:00-05:00")])
    assert not g.por_fecha and "zona horaria" in g.motivo
    assert [e.execution_id for e in g.lista] == [E_PRED_A, E_PRED_B]           # por identificador
    assert all(e.generada_hasta is None for e in g.lista)


def test_tablero_avisa_franja_no_asociable_por_zona_mixta(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(10, "2025-10-01T06:00:00", eid=E_PRED_A)],
                   recomendaciones=[recomendacion(1, F1)],
                   ejecuciones={E_FREC_A: ejecucion_frecuencias(E_PRED_A)})
    st = ejecutar(monkeypatch, api)
    assert any("No se asoció una predicción" in w and "zona horaria" in w for w in st.de_tipo("warning"))
    assert tabla_pasos(st).valor["Demanda del horizonte"] == xp.NO_DISPONIBLE


# ============================================================================== intervalos

def _r2(x):
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


@pytest.mark.parametrize("li, pred, ls, clase", [
    (100, 100, 100, xp.COLAPSADO),
    ("0.00", "0.00", "0.00", xp.COLAPSADO),
    (100, 100, 130, xp.PARCIAL),
    (75.0, 100.0, 130.0, xp.BANDA),
    ("92.59", "123.45", "160.49", xp.BANDA),
    (0.75, 1, 1.3, xp.BANDA),
    (80, 100, 125, xp.NO_DETERMINADO),
    # predicciones pequeñas: con 2 decimales la banda no se distingue de otros intervalos
    (0.03, 0.04, 0.05, xp.NO_DETERMINADO),
    (0, 0.01, 0.02, xp.NO_DETERMINADO),
    (0.04, 0.05, 0.07, xp.NO_DETERMINADO),
    (None, 100, 120, xp.SIN_DATO),
    ("abc", 100, 120, xp.SIN_DATO),
])
def test_clasificar_intervalo_sin_tocar_los_valores(li, pred, ls, clase):
    fila = {"limite_inferior": li, "demanda_pred": pred, "limite_superior": ls}
    copia = dict(fila)
    assert xp.clasificar_intervalo(fila) == clase
    assert fila == copia


def test_toda_banda_provisional_distinguible_se_reconoce():
    random.seed(7)
    for _ in range(20_000):
        p = random.uniform(0.06, 3000)                     # sin redondear, como la calcula la API
        fila = {"demanda_pred": _r2(p), "limite_inferior": _r2(p * 0.75), "limite_superior": _r2(p * 1.3)}
        assert xp.clasificar_intervalo(fila) == xp.BANDA, fila


def test_la_banda_no_se_presenta_como_metodo_confirmado():
    assert xp.ETIQUETA_INTERVALO[xp.BANDA] == "Compatible numéricamente con la banda provisional"
    texto = xp.DESCRIPCION_INTERVALO[xp.BANDA]
    assert "coincidencia numérica" in texto and "no confirma" in texto
    assert "cuantil" not in xp.DESCRIPCION_INTERVALO[xp.NO_DETERMINADO].lower()
    assert "GBM" not in xp.DESCRIPCION_INTERVALO[xp.COLAPSADO]


def test_intervalos_colapsados_en_distintos_modelos(monkeypatch):
    api = ApiFalsa(predicciones=[
        prediccion(1, F1, model_id=3, version="gbm", pred=50, li=50, ls=50),
        prediccion(2, F2, model_id=3, version="gbm", pred=60, li=60, ls=60),
        prediccion(3, F3, model_id=1, version="baseline_dh_v1", pred=0, li=0, ls=0),
        prediccion(4, "2025-10-01T06:45:00-05:00", model_id=1, version="baseline_dh_v1",
                   pred=100, li=75, ls=130)])
    for modelo, colapsadas, banda in ((("3", "gbm"), 2, None), (("1", "baseline_dh_v1"), 1, 1)):
        st = ejecutar(monkeypatch, api, valores={"Modelo": modelo})
        t = tabla_intervalos(st)
        assert t["Colapsado"] == colapsadas
        assert t.get("Compatible numéricamente con la banda provisional") == banda
        assert any(f"{colapsadas} franjas con intervalo colapsado" in w for w in st.de_tipo("warning"))


# ======================================================================= explicacion (unidad)

def test_ejecucion_prediccion_asociada_segun_el_tipo_de_ejecucion():
    assert xp.ejecucion_prediccion_asociada(ejecucion_frecuencias(E_PRED_A)) == E_PRED_A
    assert xp.ejecucion_prediccion_asociada(
        {"tipo": "recomendacion", "parametros": {"execution_id_prediccion": E_PRED_B},
         "resumen": {}}) == E_PRED_B
    assert xp.ejecucion_prediccion_asociada(ejecucion_frecuencias("p1")) is None        # no es UUID
    assert xp.ejecucion_prediccion_asociada({"tipo": "frecuencias", "resumen": {}}) is None
    assert xp.ejecucion_prediccion_asociada(None) is None


def test_prediccion_para_por_id_y_nunca_ambigua():
    preds = [prediccion(10, F1), prediccion(11, F2)]
    assert xp.prediccion_para({"prediction_id": 11}, preds).prediccion["prediction_id"] == 11
    assert xp.prediccion_para(recomendacion(1, F2), preds + [prediccion(12, F2)]).motivo == xp.AMBIGUA
    assert xp.prediccion_para(recomendacion(1, F1, h=30), preds).motivo == xp.NO_ENCONTRADA
    assert xp.prediccion_para(recomendacion(1, F1), []).motivo == xp.SIN_PREDICCIONES


def test_respaldo_registrado_solo_con_lo_que_trae_la_api():
    assert xp.respaldo_registrado({"resumen": {"predicciones": 96, "respaldo": True,
                                               "motivo_respaldo": "m"}}) == {"respaldo": True,
                                                                             "motivo_respaldo": "m"}
    assert xp.respaldo_registrado({"resumen": {"predicciones": 96}}) is None
    assert xp.respaldo_registrado(None) is None


def test_explicar_recomendacion_muestra_los_valores_guardados_aunque_no_cuadren():
    rec = recomendacion(1, F1, demanda_hora=123.45, buses_hora_recomendado=999,
                        headway_recomendado_min=0.06, vehiculos_requeridos=7, capacidad_efectiva="41.25")
    exp = xp.explicar_recomendacion(rec, prediccion(10, F1, pred=61.72))
    valores = {p["concepto"]: p["valor"] for p in exp["pasos"]}
    assert valores["Demanda del horizonte"] == "61.72"
    assert valores["Demanda por hora"] == "123.45"
    assert valores["Buses por hora recomendados"] == "999"
    assert valores["Headway recomendado (min)"] == "0.06"
    assert valores["Vehículos requeridos"] == "7"
    assert valores["Capacidad efectiva"] == "41.25"
    assert valores["Factor de carga máximo"] == "0.435"


def test_explicar_recomendacion_no_viable_con_superior_y_respaldo_todos():
    rec = recomendacion(1, F1, viable=False, base_demanda="superior",
                        parametros_version="param_v2:todos")
    exp = xp.explicar_recomendacion(rec, prediccion(10, F1, pred=100, ls=130.5))
    valores = {p["concepto"]: p["valor"] for p in exp["pasos"]}
    assert valores["Demanda del horizonte"] == "130.5"
    assert valores["Buses por hora recomendados"] == xp.NO_DISPONIBLE
    assert valores["Viable"] == "no"
    texto = " ".join(exp["regla"])
    assert "límite superior" in texto and "'todos'" in texto and "no viable" in texto


def test_explicar_recomendacion_sin_prediccion_declara_lo_que_falta_y_por_que():
    exp = xp.explicar_recomendacion(recomendacion(1, F1), None, xp.ZONA_MIXTA)
    valores = {p["concepto"]: p["valor"] for p in exp["pasos"]}
    assert valores["Demanda del horizonte"] == xp.NO_DISPONIBLE
    faltan = " ".join(exp["no_disponibles"])
    assert "zona horaria" in faltan
    for parametro in ("factor_carga_fuente", "headway_minimo_min", "Mínimo de buses por hora"):
        assert parametro in faltan


def test_los_textos_declaran_el_alcance_real_de_la_explicacion():
    texto = " ".join(xp.LIMITACIONES_PREDICCION).lower()
    assert "solo para predicciones del gbm" in texto and "si la api la verifica" in texto
    assert "no es un intervalo de confianza" in texto
    alcance = xp.ALCANCE_RF015.lower()
    for parte in ("permutación (modelo de evaluación", "ganancia (artefacto de producción)",
                  "reconstruida y verificada", "sin garantía",
                  "línea base no tienen explicación individual"):
        assert parte in alcance


# ======================================================================== tablero: predicción

def test_predicciones_solo_del_dia_y_de_una_ejecucion_con_descartes_informados(monkeypatch):
    api = ApiFalsa(predicciones=[
        prediccion(1, F1, eid=E_PRED_A, pred=100, generada="2025-10-01T01:00:00-05:00"),
        prediccion(2, F2, eid=E_PRED_A, pred=101, generada="2025-10-01T01:00:00-05:00"),
        prediccion(3, F1, eid=E_PRED_B, pred=555, generada="2025-10-01T09:00:00-05:00"),
        prediccion(4, F_OTRO_DIA, eid=E_PRED_B, pred=777, generada="2025-10-01T09:00:00-05:00"),
        prediccion(5, F1, eid=E_PRED_A, pred=888, h=30),
    ])
    st = ejecutar(monkeypatch, api)
    pedido = api.de("predictions")[0]
    assert pedido["desde"] == "2025-10-01T00:00:00-05:00"
    assert pedido["hasta"] == "2025-10-02T00:00:00-05:00"
    assert pedido["idruta"] == 16 and pedido["horizonte_min"] == 60
    assert st.opciones["Ejecución de predicción"][0].startswith(E_PRED_B)       # la más reciente
    assert grafico(st)["demanda_pred"].tolist() == [555.0]
    assert any(f"Ejecución de predicciones: {E_PRED_B} · 1 franjas mostradas · descartadas: 2 filas de "
               "otras ejecuciones y 0 de otros modelos" in c for c in st.de_tipo("caption"))

    st = ejecutar(monkeypatch, api, valores={"Ejecución de predicción": E_PRED_A})
    assert grafico(st)["demanda_pred"].tolist() == [100.0, 101.0]
    assert any(f"{E_PRED_A} · 2 franjas mostradas · descartadas: 1 filas" in c
               for c in st.de_tipo("caption"))


def test_predicciones_muestran_valores_version_y_nivel_de_la_api(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(1, F1, pred="123.45", li="92.59", ls="160.49"),
                                 prediccion(2, F2, pred=80, li=80, ls=80)])
    st = ejecutar(monkeypatch, api)
    tabla = next(d for d in st.de_tipo("dataframe") if "nivel_intervalo" in d).set_index("franja_inicio")
    assert tabla.demanda_pred[F1] == "123.45" and tabla.limite_superior[F1] == "160.49"
    assert tabla.demanda_pred[F2] == 80
    textos = st.textos()
    for esperado in ("gbm_fe31_v1", "model_id: 3", "nivel_intervalo: 0.8", "sobusa-fe-3.1",
                     "data_cutoff: 2025-10-01T00:00:00-05:00", E_PRED_A):
        assert esperado in textos
    t = tabla_intervalos(st)
    assert t["Compatible numéricamente con la banda provisional"] == 1 and t["Colapsado"] == 1
    assert "Explicación del modelo de demanda" in textos


def test_varios_modelos_identifican_franjas_ocultas_y_respaldo(monkeypatch):
    resumen = {"predicciones": 2, "model_id": 3, "modelo_activo": 3, "metodo_activo": "gbm_fe31",
               "respaldo": True, "rutas_respaldo": [16], "modelo_respaldo": 1,
               "motivo_respaldo": "el GBM no produjo filas para las rutas [16]",
               "filas_por_modelo": {"3": 1, "1": 1}}
    api = ApiFalsa(predicciones=[prediccion(1, F1, model_id=3, version="gbm", pred=100),
                                 prediccion(2, F2, model_id=1, version="baseline_dh_v1", pred=200)],
                   ejecuciones={E_PRED_A: {"tipo": "prediccion", "resumen": resumen}})
    for modelo, esperado, oculta in ((("3", "gbm"), [100.0], F2), (("1", "baseline_dh_v1"), [200.0], F1)):
        st = ejecutar(monkeypatch, api, valores={"Modelo": modelo})
        assert any("más de un modelo" in w for w in st.de_tipo("warning"))
        assert grafico(st)["demanda_pred"].tolist() == esperado
        (ocultas,) = [d for d in st.de_tipo("dataframe") if list(d.columns) == ["franja_inicio", "model_id",
                                                                                "model_version"]]
        assert ocultas.franja_inicio.tolist() == [oculta]
        assert any("1 franjas de esta ejecución quedan fuera" in w for w in st.de_tipo("warning"))
        assert any("y 1 de otros modelos" in c for c in st.de_tipo("caption"))
        (respaldo,) = st.de_tipo("json")
        assert respaldo == {k: resumen[k] for k in xp.CLAVES_RESPALDO}          # tal cual, sin "predicciones"


def test_sin_respaldo_registrado_no_se_inventa(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(1, F1)],
                   ejecuciones={E_PRED_A: {"tipo": "prediccion", "resumen": {"predicciones": 1}}})
    st = ejecutar(monkeypatch, api)
    assert st.de_tipo("json") == []
    assert "no registra el modelo activo ni el respaldo" in st.textos()


def test_sin_predicciones_del_dia_informa_y_no_usa_otro_dia(monkeypatch):
    st = ejecutar(monkeypatch, ApiFalsa(predicciones=[prediccion(1, F_OTRO_DIA)]))
    assert st.de_tipo("line_chart") == []
    assert any("Sin predicciones" in i and "2025-10-01" in i for i in st.de_tipo("info"))


def test_error_de_la_api_en_predicciones_no_rompe_el_resto_del_tablero(monkeypatch):
    api = ApiFalsa(alertas=[{"alert_id": 1, "idruta": 16, "estado": "abierta", "codigo": "ALT-001",
                             "severidad": "alta", "franja_inicio": F1, "valor_observado": 1,
                             "valor_esperado": 2, "explicacion": "e", "recomendacion": "r",
                             "usuario_asignado": None}],
                   fallar={"predictions"})
    st = ejecutar(monkeypatch, api)
    assert any("No se pudieron leer predictions" in e for e in st.de_tipo("error"))
    assert any("alert_id" in d for d in st.de_tipo("dataframe"))              # alertas sigue igual


# ===================================================================== tablero: recomendación

def escenario_recomendacion(prediccion_asociada=True):
    """Recomendación de E_FREC_A, cuya ejecución de predicción es E_PRED_A. E_PRED_B tiene
    predicciones de las mismas franjas: nunca deben usarse para explicar E_FREC_A."""
    preds = [prediccion(20, F1, eid=E_PRED_B, pred=999, generada="2025-10-01T09:00:00-05:00")]
    if prediccion_asociada:
        preds.append(prediccion(10, F1, eid=E_PRED_A, pred=61.72))
    return ApiFalsa(
        predicciones=preds,
        recomendaciones=[recomendacion(1, F1, eid=E_FREC_A, demanda_hora=123.45,
                                       buses_hora_recomendado=999)],
        ejecuciones={E_FREC_A: ejecucion_frecuencias(E_PRED_A)})


def test_recomendacion_se_explica_con_los_valores_de_la_api_sin_recalcular(monkeypatch):
    api = escenario_recomendacion()
    st = ejecutar(monkeypatch, api)
    pasos = tabla_pasos(st).valor
    assert pasos["Demanda del horizonte"] == "61.72"                          # de E_PRED_A, no de E_PRED_B
    assert pasos["Demanda por hora"] == "123.45"
    assert pasos["Buses por hora recomendados"] == "999"                       # no se recalcula
    assert pasos["Capacidad efectiva"] == "41.25"
    assert pasos["Factor de carga máximo"] == "0.435"
    asociadas = [p for p in api.de("predictions") if p.get("execution_id")]
    assert asociadas and all(p["execution_id"] == E_PRED_A for p in asociadas)
    assert all(p["desde"] == "2025-10-01T00:00:00-05:00" and p["horizonte_min"] == 60
               for p in asociadas)
    textos = st.textos()
    assert f"Ejecución de recomendaciones: {E_FREC_A}" in textos
    assert f"ejecución de predicciones asociada: {E_PRED_A}" in textos
    assert f"prediction_id 10 de la ejecución {E_PRED_A}" in textos
    assert "no los recalcula" in textos and "headway_minimo_min" in textos
    assert "regla operativa de recomendación, no las del modelo" in textos
    assert xp.ALCANCE_RF015 in textos


def test_ejecucion_sin_predicciones_asociadas_no_usa_otra_ejecucion(monkeypatch):
    st = ejecutar(monkeypatch, escenario_recomendacion(prediccion_asociada=False))
    assert any(E_PRED_A in w and "No se completan con datos de otra ejecución" in w
               for w in st.de_tipo("warning"))
    pasos = tabla_pasos(st).valor
    assert pasos["Demanda del horizonte"] == xp.NO_DISPONIBLE                 # no el 999 de E_PRED_B
    assert pasos["Buses por hora recomendados"] == "999"
    assert xp.MOTIVO_NO_ASOCIADA[xp.SIN_PREDICCIONES] in st.textos()


def test_recomendaciones_de_varias_ejecuciones_no_se_mezclan(monkeypatch):
    api = escenario_recomendacion()
    api.tablas["recommendations"][1].append(
        recomendacion(2, F1, eid=E_FREC_B, buses_hora_recomendado=7,
                      generated_at="2025-10-01T10:00:00-05:00"))
    api.ejecuciones[E_FREC_B] = ejecucion_frecuencias(E_PRED_B)
    st = ejecutar(monkeypatch, api)
    assert st.opciones["Ejecución de recomendación"][0].startswith(E_FREC_B)
    (barras,) = st.de_tipo("bar_chart")
    assert barras["buses_hora_recomendado"].tolist() == [7.0]
    assert tabla_pasos(st).valor["Demanda del horizonte"] == "999"            # su propia predicción
    assert any(f"Ejecución de recomendaciones: {E_FREC_B} · 1 franjas mostradas · descartadas: 1 filas "
               f"de otras ejecuciones · ejecución de predicciones asociada: {E_PRED_B}" in c
               for c in st.de_tipo("caption"))


def test_recomendacion_no_viable_muestra_motivo(monkeypatch):
    api = escenario_recomendacion()
    api.tablas["recommendations"] = ("recommendation_id", [recomendacion(1, F1, viable=False)])
    st = ejecutar(monkeypatch, api)
    assert any("Motivo de no viabilidad: requiere 120 vehículos" in w for w in st.de_tipo("warning"))
    assert any("1 franjas no viables" in w for w in st.de_tipo("warning"))
    assert tabla_pasos(st).valor["Buses por hora recomendados"] == xp.NO_DISPONIBLE


def test_ejecucion_inexistente_se_informa_y_se_explica_sin_prediccion(monkeypatch):
    api = escenario_recomendacion()
    api.ejecuciones = {}
    st = ejecutar(monkeypatch, api)
    assert any(f"No se pudo consultar la ejecución {E_FREC_A}" in w for w in st.de_tipo("warning"))
    assert "ejecución de predicciones asociada: no disponible" in st.textos()
    assert tabla_pasos(st).valor["Demanda del horizonte"] == xp.NO_DISPONIBLE


def test_el_tablero_solo_hace_get_al_cargar(monkeypatch):
    api = escenario_recomendacion()
    ejecutar(monkeypatch, api)            # requests.post/patch lanzan si se llaman
    assert {r.split("/")[0] for r, _ in api.llamadas} <= {"predictions", "recommendations", "alerts",
                                                          "frequency", "models"}
    assert not [r for r, _ in api.llamadas if r.endswith("/explanation")]      # solo al pulsar el botón


# ====================================================== modelo = (model_id, model_version)

def test_mismo_model_id_con_dos_versiones_son_modelos_distintos():
    filas = [prediccion(1, F1, model_id=3, version="gbm_v1", pred=100),
             prediccion(2, F2, model_id=3, version="gbm_v2", pred=200),
             prediccion(3, F3, model_id=3, version="gbm_v2", pred=300)]
    assert xp.modelos(filas) == [(("3", "gbm_v2"), 2), (("3", "gbm_v1"), 1)]
    assert [f["prediction_id"] for f in xp.de_modelo(filas, ("3", "gbm_v1"))] == [1]
    assert [o["franja_inicio"] for o in xp.ocultas_por_modelo(filas, ("3", "gbm_v1"))] == [F2, F3]


def test_tablero_separa_versiones_con_el_mismo_model_id(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(1, F1, model_id=3, version="gbm_v1", pred=100),
                                 prediccion(2, F2, model_id=3, version="gbm_v2", pred=200),
                                 prediccion(3, F3, model_id=3, version="gbm_v2", pred=300)])
    casos = ((("3", "gbm_v1"), [100.0], [F2, F3], "gbm_v2"),
             (("3", "gbm_v2"), [200.0, 300.0], [F1], "gbm_v1"))
    for modelo, mostradas, ocultas_esperadas, version_oculta in casos:
        st = ejecutar(monkeypatch, api, valores={"Modelo": modelo})
        assert any("más de un modelo" in w for w in st.de_tipo("warning"))
        assert grafico(st)["demanda_pred"].tolist() == mostradas
        (ocultas,) = [d for d in st.de_tipo("dataframe")
                      if list(d.columns) == ["franja_inicio", "model_id", "model_version"]]
        assert sorted(ocultas.franja_inicio) == ocultas_esperadas
        assert set(ocultas.model_version) == {version_oculta}
        tabla = next(d for d in st.de_tipo("dataframe") if "nivel_intervalo" in d)
        assert set(tabla.model_version) == {modelo[1]}                          # sin mezclar versiones
        assert not set(tabla.franja_inicio) & set(ocultas_esperadas)
        assert f"model_version: {modelo[1]}" in st.textos()


# ============================================================= filtro diario en hora de Bogotá

def test_la_consulta_recibe_el_dia_de_bogota_con_desplazamiento(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(1, F1)])
    ejecutar(monkeypatch, api, valores={"Día": pd.Timestamp("2026-11-19")})
    (primera, *_) = api.de("predictions")
    assert primera == {"idruta": 16, "horizonte_min": 60, "desde": "2026-11-19T00:00:00-05:00",
                       "hasta": "2026-11-20T00:00:00-05:00", "pagina": 1, "tamano": 500}
    for params in api.de("predictions") + api.de("recommendations"):
        assert params["desde"].endswith("-05:00") and params["hasta"].endswith("-05:00")


def test_el_filtro_diario_no_trae_el_dia_anterior_ni_el_siguiente(monkeypatch):
    dentro = {"2025-10-01T00:00:00-05:00": 1.0,          # primer instante del día
              "2025-10-01T23:45:00-05:00": 2.0,          # última franja
              "2025-10-02T04:30:00+00:00": 3.0}          # = 23:30 del 1 en Bogotá, escrita en UTC
    fuera = {"2025-09-30T23:45:00-05:00": 91.0,          # día anterior
             "2025-10-02T00:00:00-05:00": 92.0,          # día siguiente (hasta es exclusivo)
             "2025-10-01T04:59:00+00:00": 93.0}          # = 23:59 del 30 en Bogotá, escrita en UTC
    filas = [prediccion(i, franja, pred=v, li=v, ls=v)
             for i, (franja, v) in enumerate({**dentro, **fuera}.items(), start=1)]
    st = ejecutar(monkeypatch, ApiFalsa(predicciones=filas))
    assert sorted(grafico(st)["demanda_pred"].tolist()) == sorted(dentro.values())


def test_franjas_con_y_sin_zona_en_la_misma_ejecucion_no_se_grafican(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(1, F1), prediccion(2, "2025-10-01T06:15:00")])
    st = ejecutar(monkeypatch, api)
    assert st.de_tipo("line_chart") == []
    assert any("mezclan fechas con y sin zona horaria" in w for w in st.de_tipo("warning"))


# ======================================================== execution_id nulo, ausente o vacío

def _sin_execution_id(fila):
    fila = dict(fila)
    del fila["execution_id"]
    return fila


INVALIDOS = [None, "", "   ", "None", "no-es-uuid"]


@pytest.mark.parametrize("valor", INVALIDOS + [float("nan"), numpy.nan, pd.NA, pd.NaT, True])
def test_id_ejecucion_rechaza_nulos_vacios_y_no_uuid(valor):
    assert xp.id_ejecucion(valor) is None


def test_varias_filas_sin_execution_id_incluido_nan_no_forman_una_ejecucion(monkeypatch):
    nulos = [None, float("nan"), None]
    api = ApiFalsa(predicciones=[prediccion(i, f, eid=v) for i, (f, v) in enumerate(zip((F1, F2, F3), nulos),
                                                                                    start=1)])
    g = xp.separar_ejecuciones(api.tablas["predictions"][1])
    assert g.lista == [] and g.excluidas == 3
    st = ejecutar(monkeypatch, api)
    assert "frequency/executions/None" not in [r for r, _ in api.llamadas]
    assert not [r for r, _ in api.llamadas if r.startswith("frequency/")]
    assert st.de_tipo("line_chart") == []
    assert any("Ninguna fila tiene un execution_id válido" in w for w in st.de_tipo("warning"))


def test_filas_sin_execution_id_valido_no_forman_una_ejecucion():
    filas = ([prediccion(i, F1, eid=v) for i, v in enumerate(INVALIDOS, start=1)]
             + [_sin_execution_id(prediccion(9, F2)), prediccion(10, F3, eid=E_PRED_A)])
    g = xp.separar_ejecuciones(filas)
    assert [(e.execution_id, e.filas) for e in g.lista] == [(E_PRED_A, 1)]
    assert g.excluidas == len(INVALIDOS) + 1
    for invalido in INVALIDOS:
        assert xp.de_ejecucion(filas, invalido) == []                # nunca agrupa los inválidos
    assert [f["prediction_id"] for f in xp.de_ejecucion(filas, E_PRED_A)] == [10]


def test_tablero_excluye_y_cuenta_filas_sin_execution_id(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(1, F1, eid=None, pred=10),
                                 _sin_execution_id(prediccion(2, F2, pred=20)),
                                 prediccion(3, F3, eid="", pred=30),
                                 prediccion(4, F1, eid=E_PRED_A, pred=40)])
    st = ejecutar(monkeypatch, api)
    assert any("Se excluyeron 3 filas sin execution_id válido" in w for w in st.de_tipo("warning"))
    assert grafico(st)["demanda_pred"].tolist() == [40.0]
    assert any(f"Ejecución de predicciones: {E_PRED_A} · 1 franjas mostradas · descartadas: 0 filas de "
               "otras ejecuciones" in c for c in st.de_tipo("caption"))
    assert "None" not in " ".join(st.de_tipo("caption"))
    assert [r for r, _ in api.llamadas if r.startswith("frequency/")] == [f"frequency/executions/{E_PRED_A}"]


def test_sin_ninguna_ejecucion_valida_no_se_grafica_ni_se_consulta(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(1, F1, eid=None), prediccion(2, F2, eid="")],
                   recomendaciones=[recomendacion(1, F1, eid=None),
                                    _sin_execution_id(recomendacion(2, F2))])
    st = ejecutar(monkeypatch, api)
    assert st.de_tipo("line_chart") == [] and st.de_tipo("bar_chart") == []
    assert not [r for r, _ in api.llamadas if r.startswith("frequency/")]
    assert not [p for p in api.de("predictions") if "execution_id" in p]
    assert sum("Ninguna fila tiene un execution_id válido" in w for w in st.de_tipo("warning")) == 2
    assert not [c for c in st.de_tipo("caption") if "Ejecución de" in c]      # nunca "Ejecución None"


def test_ejecucion_asociada_invalida_no_se_consulta(monkeypatch):
    api = escenario_recomendacion()
    api.ejecuciones[E_FREC_A] = ejecucion_frecuencias("None")
    st = ejecutar(monkeypatch, api)
    assert not [p for p in api.de("predictions") if "execution_id" in p]
    assert any("identificador no es válido" in w for w in st.de_tipo("warning"))
    assert tabla_pasos(st).valor["Demanda del horizonte"] == xp.NO_DISPONIBLE


# ==================================================================== franjas duplicadas

def llamadas_frequency(api):
    return [r for r, _ in api.llamadas if r.startswith("frequency/")]


@pytest.mark.parametrize("cambio", [
    {"segmento_id": "S1"},
    {"data_cutoff": "2025-09-30T00:00:00-05:00"},
], ids=["segmento_id", "data_cutoff"])
def test_predicciones_duplicadas_no_se_grafican_ni_consultan_la_api(monkeypatch, cambio):
    otra = {**prediccion(2, F1, pred=999), **cambio}
    api = ApiFalsa(predicciones=[prediccion(1, F1, pred=100), otra, prediccion(3, F2)])
    st = ejecutar(monkeypatch, api)
    assert st.de_tipo("line_chart") == []
    assert llamadas_frequency(api) == []                           # ni siquiera el respaldo
    assert any("2 predicciones comparten franja" in w for w in st.de_tipo("warning"))
    (tabla,) = [d for d in st.de_tipo("dataframe")
                if list(d.columns) == list(xp.COLUMNAS_DUPLICADO_PREDICCION)]
    assert sorted(tabla.prediction_id) == [1, 2]                     # ambas, sin elegir
    columna = next(iter(cambio))
    assert set(tabla[columna]) == {prediccion(1, F1)[columna], cambio[columna]}


def test_mismo_instante_con_distinto_desplazamiento_es_duplicado(monkeypatch):
    api = ApiFalsa(predicciones=[prediccion(1, F1), prediccion(2, "2025-10-01T11:00:00+00:00")])
    st = ejecutar(monkeypatch, api)
    assert st.de_tipo("line_chart") == []
    (tabla,) = [d for d in st.de_tipo("dataframe") if "prediction_id" in d and "segmento_id" in d]
    assert sorted(tabla.franja_inicio) == ["2025-10-01T06:00:00-05:00", "2025-10-01T11:00:00+00:00"]


def test_duplicados_solo_entre_fechas_comparables():
    assert xp.clave_instante(F1) == xp.clave_instante("2025-10-01T11:00:00+00:00")
    assert xp.clave_instante(F1) != xp.clave_instante("2025-10-01T06:00:00")    # con zona vs sin zona
    assert xp.clave_instante("no es fecha") is None
    clave, cols = xp.CLAVE_FRANJA_PREDICCION, xp.COLUMNAS_DUPLICADO_PREDICCION
    assert xp.franjas_duplicadas([prediccion(1, F1), prediccion(2, "2025-10-01T06:00:00")], clave, cols) == []
    assert xp.franjas_duplicadas([prediccion(1, F1), prediccion(2, F1, h=30)], clave, cols) == []
    assert xp.franjas_duplicadas([prediccion(1, F1), prediccion(2, F1, version="otra")], clave, cols) == []
    assert len(xp.franjas_duplicadas([prediccion(1, F1), prediccion(2, F1)], clave, cols)) == 2


@pytest.mark.parametrize("segunda", [F1, "2025-10-01T11:00:00+00:00"],
                         ids=["misma_cadena", "otro_desplazamiento"])
def test_recomendaciones_duplicadas_no_se_grafican_explican_ni_consultan(monkeypatch, segunda):
    api = escenario_recomendacion()
    api.tablas["recommendations"] = ("recommendation_id", [
        recomendacion(1, F1, buses_hora_recomendado=2), recomendacion(2, segunda, buses_hora_recomendado=9)])
    st = ejecutar(monkeypatch, api)
    assert st.de_tipo("bar_chart") == []
    assert not [d for d in st.de_tipo("dataframe") if "concepto" in d]          # sin explicación
    assert f"frequency/executions/{E_FREC_A}" not in llamadas_frequency(api)
    assert not [p for p in api.de("predictions") if "execution_id" in p]
    (tabla,) = [d for d in st.de_tipo("dataframe")
                if list(d.columns) == list(xp.COLUMNAS_DUPLICADO_RECOMENDACION)]
    assert sorted(tabla.recommendation_id) == [1, 2]
    assert any("2 recomendaciones comparten franja" in w for w in st.de_tipo("warning"))
    assert "no consultada (datos ambiguos)" in st.textos()



# =========================================================== normalización y caso sin duplicados

def test_clave_instante_normaliza_en_bogota():
    esperado = ("con_zona", pd.Timestamp(F1).tz_convert("America/Bogota"))
    assert xp.clave_instante("2025-10-01T11:00:00Z") == esperado
    assert str(xp.clave_instante("2025-10-01T11:00:00+00:00")[1].tz) == "America/Bogota"
    assert xp.clave_instante("2025-10-01 06:00:00-0500") == xp.clave_instante(F1)       # otro formato


def test_sin_duplicados_se_grafica_y_se_explica_normalmente(monkeypatch):
    api = escenario_recomendacion()
    api.tablas["predictions"][1].extend([prediccion(11, F2, pred=70), prediccion(12, F3, pred=80)])
    api.tablas["recommendations"] = ("recommendation_id", [
        recomendacion(1, F1, buses_hora_recomendado=2), recomendacion(2, F2, buses_hora_recomendado=3),
        recomendacion(3, F3, buses_hora_recomendado=4)])
    franja_2 = xp.clave_instante(F2)
    st = ejecutar(monkeypatch, api, valores={"Ejecución de predicción": E_PRED_A,
                                             "Franja a explicar": franja_2})
    assert not [w for w in st.de_tipo("warning") if "comparten franja" in w]
    assert grafico(st)["demanda_pred"].tolist() == [61.72, 70.0, 80.0]
    (barras,) = st.de_tipo("bar_chart")
    assert barras["buses_hora_recomendado"].tolist() == [2.0, 3.0, 4.0]
    assert st.opciones["Franja a explicar"] == [F1, F2, F3]                        # se ven tal como llegaron
    pasos = tabla_pasos(st).valor
    assert pasos["Buses por hora recomendados"] == "3" and pasos["Demanda del horizonte"] == "70"
    assert f"frequency/executions/{E_FREC_A}" in [r for r, _ in api.llamadas]
