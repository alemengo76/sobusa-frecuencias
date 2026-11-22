"""PATCH de gestión de alertas en dashboard/tablero.py e invalidación de la exportación (RF-011, RF-017).

El tablero dibuja su interfaz al nivel del módulo, así que cada prueba ejecuta el guion completo con
runpy.run_path, con:
- un Streamlit falso ESTRICTO (no hace falta tenerlo instalado): implementa solo lo que usa el
  tablero y cualquier otro atributo lanza un error explícito;
- requests.get / requests.post / requests.patch reemplazados por funciones de la prueba, y cualquier
  otra solicitud bloqueada en requests.Session.request;
- respuestas requests.Response reales, para que raise_for_status() y json() lancen las excepciones
  verdaderas de requests.
No usa red, base de datos, credenciales ni .env, y no modifica dashboard/tablero.py ni exportacion.py.
"""
import contextlib
import json
import runpy
import sys
import types
from pathlib import Path

import numpy  # noqa: F401  (se cargan antes de ejecutar el tablero)
import openpyxl  # noqa: F401
import pandas  # noqa: F401
import pytest
import requests

from dashboard import exportacion as ex

TABLERO = Path(__file__).resolve().parents[2] / "dashboard" / "tablero.py"
API = "http://api-falsa.invalid/api/v1"
AID = 1201                                    # primera alerta de la tabla: la que elige el formulario
CUERPO_ESPERADO = {"estado": "reconocida", "usuario": "usuario-prueba", "comentario": None,
                   "usuario_asignado": None}
CLAVE_EXPORTACION = "exportacion_alertas"
CLAVE_AVISO = "exportacion_alertas_aviso"
AVISO_PATCH = "Se intentó actualizar una alerta: se descartó la exportación preparada."
TEXTO_EXITO = "La alerta se actualizó correctamente."
TEXTO_NO_CONFIRMADA = ("No se pudo confirmar la actualización. "
                       "Revise el estado de la alerta antes de reintentar.")


# ------------------------------------------------------------------------- Streamlit falso estricto

class StFalso(types.ModuleType):
    """Implementa explícitamente lo que usa dashboard/tablero.py. Los widgets devuelven valores
    controlados por la prueba y todo lo que se dibuja queda en `eventos` como (tipo, primer argumento)."""

    def __init__(self, estado: dict, pulsados=(), valores=None):
        super().__init__("streamlit")
        self.session_state = estado
        self.pulsados = set(pulsados)
        self.valores = valores or {}
        self.eventos = []

    def __getattr__(self, nombre):
        raise AttributeError(f"Streamlit falso: st.{nombre} no está simulado; agréguelo a StFalso "
                             "si el tablero empezó a usarlo")

    def _registrar(self, tipo, valor=None):
        self.eventos.append((tipo, valor))

    def de_tipo(self, tipo):
        return [valor for t, valor in self.eventos if t == tipo]

    # configuración y textos
    def set_page_config(self, **kwargs): self._registrar("set_page_config", kwargs)
    def title(self, texto, **kwargs): self._registrar("title", texto)
    def subheader(self, texto, **kwargs): self._registrar("subheader", texto)
    def caption(self, texto, **kwargs): self._registrar("caption", texto)
    def write(self, *objetos, **kwargs): self._registrar("write", objetos)
    def error(self, texto, **kwargs): self._registrar("error", texto)
    def info(self, texto, **kwargs): self._registrar("info", texto)
    def success(self, texto, **kwargs): self._registrar("success", texto)
    def warning(self, texto, **kwargs): self._registrar("warning", texto)
    def json(self, datos, **kwargs): self._registrar("json", datos)
    def text(self, texto, **kwargs): self._registrar("text", texto)

    def markdown(self, texto, **kwargs): self._registrar("markdown", texto)
    def rerun(self): self._registrar("rerun", None)

    # datos y gráficos
    def dataframe(self, datos, **kwargs): self._registrar("dataframe", datos)
    def vega_lite_chart(self, especificacion, **kwargs): self._registrar("vega_lite_chart", especificacion)
    def line_chart(self, datos, **kwargs): self._registrar("line_chart", datos)
    def bar_chart(self, datos, **kwargs): self._registrar("bar_chart", datos)
    def download_button(self, etiqueta, datos, **kwargs):
        self._registrar("download_button", etiqueta)
        self._registrar("download_file", kwargs.get("file_name"))

    # contenedores
    @property
    def sidebar(self): return contextlib.nullcontext()
    def tabs(self, nombres): return [contextlib.nullcontext() for _ in nombres]
    def columns(self, spec, **kwargs):
        return [contextlib.nullcontext() for _ in range(spec if isinstance(spec, int) else len(spec))]
    def container(self, **kwargs): return contextlib.nullcontext()
    def expander(self, etiqueta, **kwargs): return contextlib.nullcontext()
    def form(self, clave, **kwargs): return contextlib.nullcontext()
    def spinner(self, texto, **kwargs):
        self._registrar("spinner", texto)
        return contextlib.nullcontext()

    # widgets
    def selectbox(self, etiqueta, opciones, index=0, **kwargs):
        self._registrar("selectbox", (etiqueta, list(opciones)))
        return self.valores.get(etiqueta, list(opciones)[index])
    def radio(self, etiqueta, opciones, index=0, **kwargs):
        return self.valores.get(etiqueta, list(opciones)[index])
    def date_input(self, etiqueta, valor=None, **kwargs): return self.valores.get(etiqueta, valor)
    def text_input(self, etiqueta, **kwargs): return self.valores.get(etiqueta, "")
    def text_area(self, etiqueta, **kwargs): return self.valores.get(etiqueta, "")
    def number_input(self, etiqueta, min_value=None, max_value=None, value=None, step=None, **kwargs):
        self._registrar("number_input", kwargs.get("key"))
        return self.valores.get(etiqueta, value)
    def checkbox(self, etiqueta, value=False, **kwargs): return self.valores.get(etiqueta, value)
    def multiselect(self, etiqueta, opciones, default=None, **kwargs):
        return self.valores.get(etiqueta, list(default or []))
    def button(self, etiqueta, **kwargs): return etiqueta in self.pulsados
    def form_submit_button(self, etiqueta, **kwargs): return etiqueta in self.pulsados


# ------------------------------------------------------------------------- respuestas HTTP reales

def respuesta(codigo: int, contenido: bytes, url: str = f"{API}/alerts/{AID}/status") -> requests.Response:
    r = requests.models.Response()
    r.status_code = codigo
    r._content = contenido
    r.encoding = "utf-8"
    r.url = url
    return r


def respuesta_json(codigo: int, cuerpo, url: str = f"{API}/alerts/{AID}/status") -> requests.Response:
    return respuesta(codigo, json.dumps(cuerpo).encode("utf-8"), url)


class RespuestaConValueError(requests.Response):
    """HTTP 200 cuyo json() lanza un ValueError común. Con requests real, json() lanza
    requests.exceptions.JSONDecodeError; este estímulo es la única forma de alcanzar la rama
    `except ValueError` del tablero sin modificar producción."""

    def json(self, **kwargs):
        raise ValueError("valor no procesable")


def alerta(i: int) -> dict:
    """Alerta sintética con todas las columnas de ex.ALERTAS.columnas, más las que la API también
    devuelve y la lista blanca excluye."""
    return {"alert_id": i, "execution_id": "00000000-0000-0000-0000-000000000001", "codigo": "ALT-001",
            "severidad": "alta", "estado": "abierta", "idruta": 16,
            "franja_inicio": "2025-12-03T06:00:00-05:00", "horizonte_min": 60, "valor_observado": 120.5,
            "valor_esperado": 80.0, "umbral": 100, "umbral_version": 1, "puntuacion_anomalia": 1.2,
            "explicacion": "Demanda alta", "recomendacion": "Reforzar", "creada_en": "2025-12-03T05:00:00-05:00",
            "actualizada_en": "2025-12-03T05:30:00-05:00", "usuario_asignado": None, "clave_dedup": f"k{i}"}


ALERTAS = [alerta(i) for i in (AID, 1200, 1199)]

# Registro de modelos simulado (GET /models): incluye artefacto_ruta para comprobar que no se muestra.
MODELOS = [
    {"model_id": 2, "nombre": "gbm_fe31", "version": "gbm_fe31_fixture", "tipo": "candidato",
     "estado": "activo", "semilla": 2026, "hiperparametros": {"metodo": "gbm_fe31", "max_iter": 40},
     "version_datos": "viaje_fuente:marca_20260123T115816", "feature_version": "sobusa-fe-3.1",
     "metricas": {"evaluacion": "2025-04..06", "cobertura_intervalo_80": 0.802},
     "artefacto_ruta": "local/fixture/modelos/gbm.pkl", "artefacto_sha256": "f" * 64,
     "creado_en": "2026-11-07T06:54:52-05:00", "activado_en": "2026-11-07T07:00:00-05:00",
     "activado_por": "equipo"},
    {"model_id": 1, "nombre": "dia_hora_4sem", "version": "dia_hora_4sem_v1", "tipo": "baseline",
     "estado": "retirado", "semilla": None, "hiperparametros": {"metodo": "dia_hora", "semanas": 4},
     "version_datos": "v1", "feature_version": "base-1", "metricas": {},
     "artefacto_ruta": None, "artefacto_sha256": None, "creado_en": "2026-11-02T08:00:00-05:00",
     "activado_en": None, "activado_por": None},
    {"model_id": 3, "nombre": "gbm_fe31", "version": "gbm_fe31_candidato", "tipo": "candidato",
     "estado": "candidato", "semilla": 7, "hiperparametros": {"metodo": "gbm_fe31"}, "version_datos": "v2",
     "feature_version": "sobusa-fe-3.1", "metricas": {}, "artefacto_ruta": "local/fixture/c.pkl",
     "artefacto_sha256": None, "creado_en": "2026-11-15T08:00:00-05:00", "activado_en": None, "activado_por": None},
    {"model_id": 4, "nombre": "gbm_fe31", "version": "gbm_fe31_rechazado", "tipo": "candidato",
     "estado": "rechazado", "semilla": 8, "hiperparametros": {"metodo": "gbm_fe31"}, "version_datos": "v2",
     "feature_version": "sobusa-fe-3.1", "metricas": {}, "artefacto_ruta": None,
     "artefacto_sha256": None, "creado_en": "2026-11-16T08:00:00-05:00", "activado_en": None, "activado_por": None},
]


class GetFalso:
    """GET de pantalla (predicciones, recomendaciones, alertas) y de ex.traer_todo, con el contrato de la
    API: {"datos": [...], "paginacion": {"pagina", "tamano", "total"}}, respetando pagina y tamano."""

    def __init__(self):
        self.llamadas = []

    def __call__(self, url, params=None, timeout=None, **otros):
        assert otros == {}, f"argumentos inesperados en GET: {otros}"
        ruta, p = url.rsplit("/", 1)[-1], dict(params or {})
        self.llamadas.append((ruta, p, timeout))
        pagina, tamano = int(p.get("pagina", 1)), int(p.get("tamano", 50))
        if ruta == "alerts":
            seleccion = [a for a in ALERTAS
                         if str(a["idruta"]) == str(p.get("idruta")) and a["estado"] == p.get("estado")]
        elif ruta in ("predictions", "recommendations"):
            seleccion = []
        elif ruta == "models":                    # cabecera del tablero (modelo activo)
            return respuesta_json(200, {"datos": MODELOS}, url)
        else:
            raise AssertionError(f"GET inesperado: {url}")
        cuerpo = {"datos": seleccion[(pagina - 1) * tamano:pagina * tamano],
                  "paginacion": {"pagina": pagina, "tamano": tamano, "total": len(seleccion)}}
        return respuesta_json(200, cuerpo, url)

    def paginas_exportadas(self):
        """Páginas pedidas por la exportación (tamaño máximo); la tabla y los conteos piden menos."""
        return [p["pagina"] for ruta, p, _ in self.llamadas
                if ruta == "alerts" and "pagina" in p and int(p.get("tamano", 0)) == ex.TAMANO_PAGINA_MAX]


class PatchFalso:
    """requests.patch simulado: registra cada llamada junto con el estado de la exportación EN ESE
    INSTANTE, y después devuelve la respuesta indicada o lanza la excepción indicada."""

    def __init__(self, efecto, estado: dict):
        self.efecto, self.estado, self.llamadas = efecto, estado, []

    def __call__(self, url, json=None, timeout=None, **otros):
        self.llamadas.append({"url": url, "json": json, "timeout": timeout, "otros": otros,
                              "exportacion_presente": CLAVE_EXPORTACION in self.estado,
                              "aviso_en_ese_momento": self.estado.get(CLAVE_AVISO)})
        if isinstance(self.efecto, BaseException):
            raise self.efecto
        return self.efecto()


def post_prohibido(*args, **kwargs):
    raise AssertionError("estas pruebas no deben ejecutar el POST de frecuencias")


# ------------------------------------------------------------------------- ejecución del tablero

@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    """Bloquea toda solicitud HTTP real y apunta API_URL a un dominio inválido."""
    def prohibido(*args, **kwargs):
        raise AssertionError("solicitud HTTP real bloqueada")
    monkeypatch.setattr(requests.sessions.Session, "request", prohibido)
    monkeypatch.setenv("API_URL", API)


def ejecutar(monkeypatch, estado: dict, pulsados=(), patch=None, get=None) -> StFalso:
    """Ejecuta el tablero una vez (una 'recarga' de Streamlit) con el estado de sesión dado."""
    st = StFalso(estado, pulsados, valores={"Sección": "Alertas", "Usuario": "usuario-prueba",
                                             "Confirmo el cambio en la alerta": True})
    monkeypatch.setitem(sys.modules, "streamlit", st)         # se restaura solo esta clave
    monkeypatch.setattr(requests, "get", get or GetFalso())
    monkeypatch.setattr(requests, "post", post_prohibido)
    monkeypatch.setattr(requests, "patch", patch or PatchFalso(AssertionError("PATCH inesperado"), estado))
    runpy.run_path(str(TABLERO), run_name="__main__")
    return st


def guardar(monkeypatch, efecto, estado=None):
    """Ejecuta el tablero pulsando «Guardar» con el efecto dado para el PATCH."""
    estado = {} if estado is None else estado
    patch = PatchFalso(efecto, estado)
    st = ejecutar(monkeypatch, estado, pulsados={"Guardar"}, patch=patch)
    return st, patch


def comprobar_solicitud(patch: PatchFalso):
    assert len(patch.llamadas) == 1
    llamada = patch.llamadas[0]
    assert llamada["url"] == f"{API}/alerts/{AID}/status"
    assert llamada["json"] == CUERPO_ESPERADO
    assert llamada["timeout"] == 10
    assert llamada["otros"] == {}


def comprobar_sin_exito(st: StFalso):
    assert TEXTO_EXITO not in st.de_tipo("success")
    assert st.de_tipo("json") == []


def comprobar_error_unico(st: StFalso) -> str:
    errores = st.de_tipo("error")
    assert len(errores) == 1, errores
    return errores[0]


# ------------------------------------------------------------------------- datos de prueba

def test_las_alertas_sinteticas_tienen_todas_las_columnas_exportables():
    for a in ALERTAS:
        assert set(ex.ALERTAS.columnas) <= set(a)


def test_el_streamlit_falso_rechaza_metodos_no_simulados():
    with pytest.raises(AttributeError, match="no está simulado"):
        StFalso({}).metric("x", 1)


# ------------------------------------------------------------------------- PATCH

def test_sin_pulsar_guardar_no_se_ejecuta_el_patch(monkeypatch):
    estado = {}
    patch = PatchFalso(AssertionError("PATCH inesperado"), estado)
    st = ejecutar(monkeypatch, estado, patch=patch)
    assert patch.llamadas == []
    assert st.de_tipo("error") == []
    assert TEXTO_EXITO not in st.de_tipo("success")


def test_patch_exitoso_muestra_exito_y_resultado(monkeypatch):
    cuerpo = {"alert_id": AID, "estado": "reconocida"}
    st, patch = guardar(monkeypatch, lambda: respuesta_json(200, cuerpo))
    comprobar_solicitud(patch)
    assert st.de_tipo("success").count(TEXTO_EXITO) == 1
    assert st.de_tipo("json") == [cuerpo]
    assert st.de_tipo("error") == []


@pytest.mark.parametrize("codigo, texto_codigo", [
    (404, "La alerta solicitada no existe."),
    (409, "La operación no está permitida por el estado actual de la alerta."),
    (422, "Los datos enviados no cumplen las validaciones."),
    (500, "La API encontró un error interno."),
    (418, "La API rechazó la solicitud."),
], ids=["404", "409", "422", "500", "otro_418"])
def test_error_http_muestra_codigo_mensaje_y_correlation_id(monkeypatch, codigo, texto_codigo):
    cuerpo = {"error": {"codigo": "x", "mensaje": "Transición no permitida", "detalle": None,
                        "correlation_id": "cid-123"}}
    st, patch = guardar(monkeypatch, lambda: respuesta_json(codigo, cuerpo))
    comprobar_solicitud(patch)
    assert comprobar_error_unico(st) == (
        f"No se pudo actualizar la alerta (HTTP {codigo}). {texto_codigo}"
        " Mensaje de la API: Transición no permitida Identificador de seguimiento: cid-123")
    comprobar_sin_exito(st)


@pytest.mark.parametrize("contenido", [
    b"<html>Bad Gateway</html>",
    b'{"detail": "Not Found"}',
    b'{"error": "texto en vez de objeto"}',
    b'["lista"]',
], ids=["html", "detail_fastapi", "error_texto", "lista"])
def test_error_http_con_cuerpo_no_uniforme_no_agrega_datos_de_la_api(monkeypatch, contenido):
    st, patch = guardar(monkeypatch, lambda: respuesta(502, contenido))
    comprobar_solicitud(patch)
    assert comprobar_error_unico(st) == "No se pudo actualizar la alerta (HTTP 502). La API rechazó la solicitud."
    comprobar_sin_exito(st)


def test_200_sin_json_valido_advierte_que_la_actualizacion_pudo_aplicarse(monkeypatch):
    st, patch = guardar(monkeypatch, lambda: respuesta(200, b"<html>ok</html>"))
    comprobar_solicitud(patch)
    assert comprobar_error_unico(st) == (
        "La API respondió con HTTP 200, pero no se pudo leer su respuesta. "
        "La actualización pudo haberse aplicado. Revise el estado de la alerta antes de reintentar.")
    comprobar_sin_exito(st)


@pytest.mark.parametrize("excepcion", [
    requests.ConnectionError("caída"),
    requests.exceptions.SSLError("ssl"),
], ids=["conexion", "ssl"])
def test_error_de_conexion_pide_revisar_antes_de_reintentar(monkeypatch, excepcion):
    st, patch = guardar(monkeypatch, excepcion)
    comprobar_solicitud(patch)
    assert comprobar_error_unico(st) == TEXTO_NO_CONFIRMADA
    comprobar_sin_exito(st)


@pytest.mark.parametrize("excepcion", [
    requests.exceptions.ConnectTimeout("conexión lenta"),     # hereda de ConnectionError y de Timeout
    requests.exceptions.ReadTimeout("lectura lenta"),
], ids=["conexion", "lectura"])
def test_tiempo_de_espera_pide_revisar_antes_de_reintentar(monkeypatch, excepcion):
    st, patch = guardar(monkeypatch, excepcion)
    comprobar_solicitud(patch)
    assert comprobar_error_unico(st) == TEXTO_NO_CONFIRMADA
    comprobar_sin_exito(st)


@pytest.mark.parametrize("excepcion", [
    requests.exceptions.ChunkedEncodingError("cortado"),
    requests.exceptions.TooManyRedirects("redirecciones"),
], ids=["chunked", "redirecciones"])
def test_otro_error_de_requests_es_problema_de_comunicacion(monkeypatch, excepcion):
    st, patch = guardar(monkeypatch, excepcion)
    comprobar_solicitud(patch)
    assert comprobar_error_unico(st) == "Ocurrió un problema de comunicación con la API."
    comprobar_sin_exito(st)


def test_value_error_al_procesar_la_respuesta(monkeypatch):
    def respuesta_con_value_error():
        r = RespuestaConValueError()
        r.status_code, r._content, r.encoding = 200, b"{}", "utf-8"
        return r

    st, patch = guardar(monkeypatch, respuesta_con_value_error)
    comprobar_solicitud(patch)
    assert comprobar_error_unico(st) == "Ocurrió un error al procesar la respuesta de la API."
    comprobar_sin_exito(st)


# ------------------------------------------------------------------------- invalidación de la exportación

@pytest.mark.parametrize("efecto", [
    lambda: respuesta_json(200, {"alert_id": AID, "estado": "reconocida"}),
    requests.ConnectionError("caída"),
    lambda: respuesta_json(409, {"error": {"mensaje": "m", "correlation_id": "c"}}),
], ids=["exito", "conexion", "http_409"])
def test_la_exportacion_preparada_se_invalida_antes_del_patch(monkeypatch, efecto):
    estado = {}

    get = GetFalso()
    st_preparacion = ejecutar(monkeypatch, estado, pulsados={"Preparar exportación"}, get=get)
    assert CLAVE_EXPORTACION in estado, st_preparacion.de_tipo("error")
    assert get.paginas_exportadas() == [1, 1]                 # página 1 y su verificación de cierre
    assert estado[CLAVE_EXPORTACION]["total"] == len(ALERTAS)

    st, patch = guardar(monkeypatch, efecto, estado)

    comprobar_solicitud(patch)
    # orden temporal: al llamar a requests.patch la exportación ya se había descartado y el aviso existía
    assert patch.llamadas[0]["exportacion_presente"] is False
    assert patch.llamadas[0]["aviso_en_ese_momento"] == AVISO_PATCH
    assert CLAVE_EXPORTACION not in estado
    assert estado[CLAVE_AVISO] == AVISO_PATCH
    assert st.de_tipo("download_button") == []
    assert AVISO_PATCH in st.de_tipo("info")


# =========================================================================================
# Integración por secciones (RF-006, RF-015, G3, M1, modelos, exportaciones)
# =========================================================================================

import io as _io                                    # noqa: E402
from datetime import date                           # noqa: E402

from openpyxl import load_workbook                  # noqa: E402

DIA_ISO_DESDE = "2025-10-01T00:00:00-05:00"
DIA_ISO_HASTA = "2025-10-02T00:00:00-05:00"


class ApiTablero:
    """GET simulado por ruta. ``rutas[ruta](params)`` devuelve una Response o una excepción."""

    def __init__(self, rutas=None):
        self.rutas = dict(rutas or {})
        self.llamadas = []

    def __call__(self, url, params=None, timeout=None, **otros):
        assert otros == {}, f"argumentos inesperados en GET: {otros}"
        ruta = url[len(API) + 1:]
        self.llamadas.append((ruta, dict(params or {}), timeout))
        if ruta in self.rutas:
            r = self.rutas[ruta](dict(params or {}))
            if isinstance(r, BaseException):
                raise r
            return r
        if ruta == "models":
            return respuesta_json(200, {"datos": MODELOS}, url)
        raise AssertionError(f"GET inesperado: {ruta}")

    def de(self, ruta):
        return [(p, t) for r, p, t in self.llamadas if r == ruta]


def paginado(filas, igualdad=(), columna_id=None):
    """Como la API: filtros de igualdad, ORDER BY id DESC si se indica, LIMIT/OFFSET y paginas."""
    def manejar(params):
        def igual(valor, parametro):                      # la API recibe booleanos como 'true'/'false'
            return (str(valor).lower() if isinstance(valor, bool) else str(valor)) == str(parametro)
        sel = [f for f in filas if all(igual(f.get(c), params[c]) for c in igualdad if c in params)]
        if columna_id:
            sel = sorted(sel, key=lambda f: f[columna_id], reverse=True)
        p, t = int(params.get("pagina", 1)), int(params.get("tamano", 50))
        cuerpo = {"datos": sel[(p - 1) * t:p * t],
                  "paginacion": {"pagina": p, "tamano": t, "total": len(sel), "paginas": (len(sel) + t - 1) // t}}
        return respuesta_json(200, cuerpo)
    return manejar


class EscrituraFalsa:
    """requests.post / requests.patch simulado."""

    def __init__(self, efecto):
        self.efecto, self.llamadas = efecto, []

    def __call__(self, url, json=None, timeout=None, **otros):
        self.llamadas.append({"ruta": url[len(API) + 1:], "json": json, "timeout": timeout, "otros": otros})
        if isinstance(self.efecto, BaseException):
            raise self.efecto
        return self.efecto()


def correr(monkeypatch, seccion, valores=None, pulsados=(), get=None, post=None, patch=None, estado=None):
    estado = {} if estado is None else estado
    st = StFalso(estado, pulsados, valores={"Sección": seccion, **(valores or {})})
    monkeypatch.setitem(sys.modules, "streamlit", st)
    monkeypatch.setattr(requests, "get", get or ApiTablero())
    monkeypatch.setattr(requests, "post", post or post_prohibido)
    monkeypatch.setattr(requests, "patch", patch or PatchFalso(AssertionError("PATCH inesperado"), estado))
    runpy.run_path(str(TABLERO), run_name="__main__")
    return st


def texto_de(st, *tipos):
    return " ".join(str(v) for t, v in st.eventos if not tipos or t in tipos)


def pred(i, eid, franja, demanda=100.0, **c):
    return {"prediction_id": i, "execution_id": eid, "idruta": 16, "sentido": "CICLO", "segmento_id": "TOTAL",
            "franja_inicio": franja, "franja_minutos": 15, "horizonte_min": 60, "demanda_pred": demanda,
            "limite_inferior": demanda * 0.8, "limite_superior": demanda * 1.2, "nivel_intervalo": 0.8,
            "definicion_tiempo": "salida_programada", "model_id": 2, "model_version": "gbm_fe31_fixture",
            "feature_version": "sobusa-fe-3.1", "data_cutoff": DIA_ISO_DESDE,
            "generated_at": "2026-11-19T08:12:00-05:00", "latency_ms": 900, **c}


def rec(i, eid, pid, franja, viable=True, **c):
    fila = {"recommendation_id": i, "execution_id": eid, "prediction_id": pid, "idruta": 16, "sentido": "CICLO",
            "segmento_id": "TOTAL", "franja_inicio": franja, "horizonte_min": 60, "demanda_hora": 100.0,
            "base_demanda": "puntual", "factor_carga_max": 0.37, "carga_max_estimada_hora": 37.0,
            "capacidad_nominal": 55.0, "capacidad_fuente": "reporte", "ocupacion_objetivo": 0.75,
            "capacidad_efectiva": 41.25, "buses_hora_recomendado": 2 if viable else None,
            "headway_recomendado_min": 30.0 if viable else None, "ciclo_min": 150.0,
            "vehiculos_requeridos": 5 if viable else 30, "flota_tope": 24, "buses_hora_actual": 4.0,
            "headway_actual_min": 15.0, "delta_buses_hora": -2.0 if viable else None, "es_viable": viable,
            "motivo_no_viable": None if viable else "requiere 30 vehículos y el tope de flota es 24",
            "parametros_version": "param_v2:laboral", "generated_at": "2026-11-19T08:12:00-05:00"}
    fila.update(c)
    return fila


FRANJAS = ["2025-10-01T06:00:00-05:00", "2025-10-01T06:15:00-05:00", "2025-10-01T06:30:00-05:00"]
# P1 es la dueña de las predicciones; F1 (frecuencias) reutilizó las de P1 y es dueña de sus recomendaciones.
PREDS = [pred(101 + k, "P1", f, 100.0 + 50 * k) for k, f in enumerate(FRANJAS)] + [
    pred(201, "P0", FRANJAS[0], 999.0, generated_at="2026-11-18T08:00:00-05:00")]
RECS = [rec(11, "F1", 101, FRANJAS[0]), rec(12, "F1", 102, FRANJAS[1], viable=False),
        rec(13, "F1", 103, FRANJAS[2], flota_tope=None),
        rec(21, "R0", 201, FRANJAS[0], generated_at="2026-11-18T08:00:00-05:00")]


def api_demanda(**extra):
    return ApiTablero({"predictions": paginado(PREDS, ("idruta", "horizonte_min", "execution_id"), "prediction_id"),
                       "recommendations": paginado(RECS, ("idruta", "horizonte_min", "execution_id", "es_viable"),
                                                   "recommendation_id"), **extra})


# ------------------------------------------------------------------ A. Demanda: consultas y P1

def test_demanda_lee_el_dia_con_filtros_del_contrato_y_grafica_una_sola_ejecucion(monkeypatch):
    api = api_demanda()
    st = correr(monkeypatch, "Demanda y frecuencias", get=api)
    lectura = api.de("predictions")[0][0]
    assert lectura == {"idruta": "16", "horizonte_min": "60", "desde": DIA_ISO_DESDE, "hasta": DIA_ISO_HASTA,
                       "pagina": 1, "tamano": 500}
    graficas = st.de_tipo("vega_lite_chart")
    demanda = next(g for g in graficas if g["layer"][0]["mark"]["type"] == "area")
    assert [v["pred"] for v in demanda["data"]["values"]] == [100.0, 150.0, 200.0]      # solo P1, sin P0
    tabla = api.de("recommendations")[-1][0]
    assert tabla["execution_id"] == "F1" and tabla["pagina"] == 1 and tabla["tamano"] == 50
    assert st.de_tipo("error") == []


def test_demanda_consulta_predicciones_con_la_ejecucion_duena_y_no_con_la_solicitud(monkeypatch):
    estado = {"ejecuciones_sesion": [{"tipo": "frecuencias", "dia": "2025-10-01", "idrutas": [16],
                                      "estado": "exitosa", "execution_id": "F1", "execution_id_prediccion": "P2",
                                      "execution_id_predicciones": "P1", "recomendaciones_de": "F1"}]}
    api = api_demanda()
    correr(monkeypatch, "Demanda y frecuencias", valores={"Tabla": "Predicciones"}, get=api, estado=estado)
    tabla = [p for p, _ in api.de("predictions") if p.get("tamano") == 50]
    assert tabla and tabla[-1]["execution_id"] == "P1"                    # dueña (P1), no la solicitud P2
    assert all(p.get("execution_id") != "P2" for p, _ in api.de("predictions"))


def test_recomendaciones_solo_no_viables_envia_es_viable_false(monkeypatch):
    api = api_demanda()
    st = correr(monkeypatch, "Demanda y frecuencias", valores={"Solo no viables": True}, get=api)
    tabla = api.de("recommendations")[-1][0]
    assert tabla["es_viable"] == "false" and tabla["execution_id"] == "F1"
    df = st.de_tipo("dataframe")[-1]
    assert df["Estado"].tolist() == ["▲ No viable"]
    assert df["Motivo no viable"].tolist() == ["requiere 30 vehículos y el tope de flota es 24"]


def test_decision_explica_la_regla_sin_afirmar_lo_que_la_api_no_verifico(monkeypatch):
    texto = texto_de(correr(monkeypatch, "Demanda y frecuencias", get=api_demanda(),
                            valores={"Vista": "02 · Decisión operativa", "Franja a revisar": 13}), "markdown")
    assert "Sin tope de flota registrado: la API no verificó la flota" in texto
    assert "mínimo 1 bus/h" in texto and "DECISIONES.md §3" in texto and "no las variables internas del GBM" in texto
    texto = texto_de(correr(monkeypatch, "Demanda y frecuencias", get=api_demanda(),
                            valores={"Vista": "02 · Decisión operativa", "Franja a revisar": 12}), "markdown")
    assert "No viable: requiere 30 vehículos y el tope de flota es 24" in texto
    assert "el tablero no los calcula" in texto


def test_sin_datos_del_dia_se_informa_y_no_se_muestra_otra_ejecucion(monkeypatch):
    api = ApiTablero({"predictions": paginado([]), "recommendations": paginado([])})
    st = correr(monkeypatch, "Demanda y frecuencias", get=api)
    assert "No hay predicciones ni recomendaciones" in texto_de(st, "markdown")
    assert st.de_tipo("vega_lite_chart") == [] and st.de_tipo("error") == []


# ------------------------------------------------------------------ errores de consulta

@pytest.mark.parametrize("efecto, esperado", [
    (requests.exceptions.ReadTimeout("lento"), "no respondió a tiempo"),
    (requests.ConnectionError("caída"), "No se pudo conectar"),
    (lambda: respuesta_json(500, {"error": {"codigo": "error_interno", "mensaje": "falló",
                                            "detalle": None, "correlation_id": "cid-500"}}), "cid-500"),
    (lambda: respuesta_json(404, {"error": {"codigo": "no_encontrado", "mensaje": "x", "detalle": None,
                                            "correlation_id": "cid-404"}}), "404"),
    (lambda: respuesta_json(409, {"error": {"codigo": "conflicto", "mensaje": "x", "detalle": None,
                                            "correlation_id": "c"}}), "409"),
    (lambda: respuesta_json(422, {"error": {"codigo": "solicitud_invalida", "mensaje": "lote vacío",
                                            "detalle": None, "correlation_id": "c"}}), "lote vacío"),
    (lambda: respuesta(200, b"<html>"), "no se pudo leer"),
    (lambda: respuesta_json(200, {"datos": []}), "'datos' y 'paginacion'"),
], ids=["timeout", "conexion", "500", "404", "409", "422", "json_invalido", "sin_paginacion"])
def test_un_error_de_consulta_se_muestra_y_no_se_convierte_en_tabla_vacia(monkeypatch, efecto, esperado):
    manejar = (lambda p: efecto) if isinstance(efecto, BaseException) else (lambda p: efecto())
    api = ApiTablero({"quality/errors": manejar})
    st = correr(monkeypatch, "Calidad de datos", valores={"Lote (obligatorio)": "L1"}, get=api)
    errores = st.de_tipo("error")
    assert len(errores) == 1 and esperado in errores[0]
    assert "api-falsa.invalid" not in errores[0]
    assert st.de_tipo("dataframe") == [] and st.de_tipo("download_button") == []


# ------------------------------------------------------------------ alertas: filtros y paginación

def alertas_numeradas(n):
    return [alerta(i) for i in range(1, n + 1)]


def test_alertas_envian_todos_los_filtros_combinados(monkeypatch):
    api = ApiTablero({"alerts": paginado(alertas_numeradas(3), ("idruta", "estado", "severidad", "codigo"))})
    correr(monkeypatch, "Alertas", get=api, valores={"Severidad": "alta", "Código": "ALT-001",
                                                     "Desde (franja)": date(2025, 12, 1),
                                                     "Hasta (franja, inclusive)": date(2025, 12, 3)})
    tabla = [p for p, _ in api.de("alerts") if p["tamano"] == 50][-1]
    assert tabla == {"idruta": 16, "estado": "abierta", "severidad": "alta", "codigo": "ALT-001",
                     "desde": "2025-12-01T00:00:00-05:00", "hasta": "2025-12-04T00:00:00-05:00",
                     "pagina": 1, "tamano": 50}


def test_alertas_paginacion_varias_paginas_ultima_y_fuera_de_rango(monkeypatch):
    api = ApiTablero({"alerts": paginado(alertas_numeradas(120), ("idruta", "estado"), "alert_id")})
    st = correr(monkeypatch, "Alertas", get=api, valores={"Página (alertas)": 3})
    assert [p for p, _ in api.de("alerts") if p["tamano"] == 50][-1]["pagina"] == 3
    assert any("Filas 101–120 de 120 alertas · página 3 de 3" in c for c in st.de_tipo("caption"))
    assert len(st.de_tipo("dataframe")[0]) == 20
    st = correr(monkeypatch, "Alertas", get=api, valores={"Página (alertas)": 4})
    assert any("La página 4 no existe" in w for w in st.de_tipo("warning"))
    assert st.de_tipo("dataframe") == []


def test_alertas_cambiar_filtros_vuelve_a_la_pagina_1(monkeypatch):
    api = ApiTablero({"alerts": paginado(alertas_numeradas(3), ("idruta", "estado"))})
    clave_1 = [k for k in correr(monkeypatch, "Alertas", get=api).de_tipo("number_input") if "alertas" in k][0]
    clave_2 = [k for k in correr(monkeypatch, "Alertas", get=api, valores={"Estado": "cerrada"})
               .de_tipo("number_input") if "alertas" in k][0]
    assert clave_1 != clave_2                         # widget distinto: arranca en la página 1


def test_alertas_conteos_por_estado_salen_de_paginacion_total(monkeypatch):
    filas = [alerta(1), {**alerta(2), "estado": "cerrada"}, {**alerta(3), "estado": "cerrada"}]
    api = ApiTablero({"alerts": paginado(filas, ("idruta", "estado"))})
    st = correr(monkeypatch, "Alertas", get=api)
    conteos = [p for p, _ in api.de("alerts") if p["tamano"] == 1]
    assert {p["estado"] for p in conteos} == set(("abierta", "reconocida", "asignada", "cerrada", "descartada"))
    html = texto_de(st, "markdown")
    assert "Cerrada</div><div class=\"v\"><b>2</b>" in html


def test_patch_sin_confirmacion_no_se_envia(monkeypatch):
    estado = {}
    patch = PatchFalso(AssertionError("PATCH inesperado"), estado)
    st = correr(monkeypatch, "Alertas", pulsados={"Guardar"}, patch=patch, estado=estado, get=GetFalso(),
                valores={"Usuario": "u"})
    assert patch.llamadas == []
    assert any("Marque la confirmación" in w for w in st.de_tipo("warning"))


@pytest.mark.parametrize("valores, falta", [
    ({"Usuario": ""}, "el usuario"),
    ({"Usuario": "u", "Nuevo estado": "asignada"}, "a quién se asigna"),
    ({"Usuario": "u", "Nuevo estado": "abierta"}, "un comentario"),
], ids=["sin_usuario", "asignar_sin_destinatario", "comentar_sin_comentario"])
def test_patch_valida_campos_obligatorios_antes_de_enviar(monkeypatch, valores, falta):
    estado = {}
    patch = PatchFalso(AssertionError("PATCH inesperado"), estado)
    st = correr(monkeypatch, "Alertas", pulsados={"Guardar"}, patch=patch, estado=estado, get=GetFalso(),
                valores={"Confirmo el cambio en la alerta": True, **valores})
    assert patch.llamadas == []
    assert any(falta in w for w in st.de_tipo("warning"))


def test_patch_sin_confirmar_resultado_bloquea_un_segundo_envio(monkeypatch):
    estado = {}
    valores = {"Usuario": "u", "Confirmo el cambio en la alerta": True}
    patch = PatchFalso(requests.exceptions.ReadTimeout("lento"), estado)
    st = correr(monkeypatch, "Alertas", pulsados={"Guardar"}, patch=patch, estado=estado, get=GetFalso(),
                valores=valores)
    assert len(patch.llamadas) == 1 and TEXTO_EXITO not in st.de_tipo("success")
    st = correr(monkeypatch, "Alertas", pulsados={"Guardar"}, patch=patch, estado=estado, get=GetFalso(),
                valores=valores)
    assert len(patch.llamadas) == 1                                   # no se reenvió
    assert any("no se pudo confirmar" in w for w in st.de_tipo("warning"))
    assert estado[f"_envio_alerta_{AID}"] is True
    correr(monkeypatch, "Alertas", pulsados={"Ya revisé el estado: permitir un nuevo envío", "Guardar"},
           patch=PatchFalso(lambda: respuesta_json(200, {"alert_id": AID, "estado": "reconocida"}), estado),
           estado=estado, get=GetFalso(), valores=valores)
    assert f"_envio_alerta_{AID}" not in estado


# ------------------------------------------------------------------ ejecución de frecuencias (POST)

CUERPO_FRECUENCIAS = {"dia": "2025-10-01", "idrutas": [16], "horizontes": [30, 60, 120], "base_demanda": "puntual"}
U_F9 = "00000000-0000-4000-8000-0000000000f9"
U_P9 = "00000000-0000-4000-8000-0000000000a9"
U_P1 = "00000000-0000-4000-8000-0000000000a1"
RESPUESTA_FRECUENCIAS = {"execution_id": U_F9, "estado": "exitosa", "execution_id_prediccion": U_P9,
                         "execution_id_predicciones": U_P1, "predicciones": 192, "recomendaciones": 192,
                         "no_viables": 3, "alertas_nuevas": 2}


def test_ejecucion_sin_confirmacion_no_se_envia(monkeypatch):
    post = EscrituraFalsa(AssertionError("POST inesperado"))
    st = correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular frecuencias"}, post=post,
                get=api_demanda())
    assert post.llamadas == [] and any("Marque la confirmación" in w for w in st.de_tipo("warning"))


def test_ejecucion_confirmada_envia_el_contrato_y_guarda_los_ids_devueltos(monkeypatch):
    estado = {"exportacion_alertas": {"clave": (), "total": 1}}
    post = EscrituraFalsa(lambda: respuesta_json(201, RESPUESTA_FRECUENCIAS))
    st = correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular frecuencias"}, post=post,
                get=api_demanda(), estado=estado,
                valores={"Confirmo que quiero crear una ejecución nueva": True})
    assert post.llamadas == [{"ruta": "frequency/executions", "json": CUERPO_FRECUENCIAS, "timeout": 120,
                              "otros": {}}]
    registro = estado["ejecuciones_sesion"][0]
    assert (registro["execution_id"], registro["execution_id_predicciones"]) == (U_F9, U_P1)
    # el resultado se guarda y el guion se vuelve a ejecutar para recargar las consultas
    (tipo, texto, datos), = estado["_avisos"]
    assert tipo == "ok" and "192 predicciones" in texto and "Reutilizó predicciones" in texto and U_P1 in texto
    assert datos == RESPUESTA_FRECUENCIAS and ("rerun", None) in st.eventos
    assert "exportacion_alertas" not in estado and estado["_generacion"] >= 1      # exportación invalidada
    assert "_envio_frecuencias" not in estado                                     # confirmada: sin bloqueo
    st = correr(monkeypatch, "Demanda y frecuencias", get=api_demanda(), estado=estado)
    assert any("192 predicciones" in x for x in st.de_tipo("success")) and "_avisos" not in estado


def test_ejecucion_con_timeout_no_afirma_exito_y_bloquea_el_reenvio(monkeypatch):
    estado = {}
    post = EscrituraFalsa(requests.exceptions.ReadTimeout("lento"))
    valores = {"Confirmo que quiero crear una ejecución nueva": True}
    st = correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular frecuencias"}, post=post,
                get=api_demanda(), estado=estado, valores=valores)
    assert st.de_tipo("success") == [] and any("No se pudo confirmar" in w for w in st.de_tipo("warning"))
    correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular frecuencias"}, post=post,
           get=api_demanda(), estado=estado, valores=valores)
    assert len(post.llamadas) == 1                                     # segundo envío evitado


def test_ejecucion_409_muestra_el_motivo_de_la_api(monkeypatch):
    cuerpo = {"error": {"codigo": "conflicto", "mensaje": "No hay un modelo activo", "detalle": None,
                        "correlation_id": "c409"}}
    post = EscrituraFalsa(lambda: respuesta_json(409, cuerpo))
    st = correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular frecuencias"}, post=post,
                get=api_demanda(), valores={"Confirmo que quiero crear una ejecución nueva": True})
    assert any("No hay un modelo activo" in e and "c409" in e for e in st.de_tipo("error"))


def test_calcular_recomendaciones_exige_la_ejecucion_de_prediccion(monkeypatch):
    respuesta_ok = {"execution_id": U_F9, "recomendaciones": 64, "no_viables": 1}
    post = EscrituraFalsa(lambda: respuesta_json(201, respuesta_ok))
    valores = {"Confirmo que quiero calcular recomendaciones nuevas": True}
    st = correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular recomendaciones"}, post=post,
                get=api_demanda(), valores=valores)
    assert post.llamadas == [] and any("Identifique la ejecución" in w for w in st.de_tipo("warning"))
    st = correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular recomendaciones"}, post=post,
                get=api_demanda(), valores={**valores, "…o id de la ejecución de predicción": "../models"})
    assert post.llamadas == [] and any("UUID" in w for w in st.de_tipo("warning"))
    estado = {}
    correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular recomendaciones"}, post=post, get=api_demanda(),
           estado=estado,
           valores={**valores, "…o id de la ejecución de predicción": f" {U_P9} ", "Base de demanda": "superior"})
    assert post.llamadas[0]["ruta"] == "recommendations/calculate"
    assert post.llamadas[0]["json"] == {"execution_id_prediccion": U_P9, "base_demanda": "superior"}
    assert "64 (1 no viables)" in estado["_avisos"][0][1] and "_envio_recomendaciones" not in estado


# ------------------------------------------------------------------ G3

def deteccion(i, nuevo):
    return {"deteccion_id": i, "evento_id": 7, "lote": "L1_001", "detectado_en": "2026-11-19T02:00:00-05:00",
            "idruta": 2, "causa": "numeropasajeros nulo", "valor_original": None, "tabla_origen": "progvehiculos",
            "idprogramacion": 900 + i, "regla": "pasajeros_nulos", "reglas_version": "comun-1.5",
            "valor_normalizado": None, "primer_lote": "L1_001" if nuevo else "L0_001",
            "primera_deteccion": "2026-11-18T02:00:00-05:00", "nuevo_en_lote": nuevo}


def test_calidad_sin_lote_no_consulta(monkeypatch):
    api = ApiTablero()
    st = correr(monkeypatch, "Calidad de datos", get=api)
    assert api.de("quality/errors") == [] and "Escriba un lote" in texto_de(st, "markdown")


def test_calidad_envia_lote_alcance_y_causa_exacta_y_distingue_nuevas(monkeypatch):
    api = ApiTablero({"quality/errors": paginado([deteccion(1, True), deteccion(2, False)])})
    st = correr(monkeypatch, "Calidad de datos", get=api,
                valores={"Lote (obligatorio)": "L1", "Alcance": "ejecucion",
                         "Causa (texto exacto)": "numeropasajeros nulo"})
    assert api.de("quality/errors")[0][0] == {"lote": "L1", "alcance": "ejecucion", "causa": "numeropasajeros nulo",
                                              "pagina": 1, "tamano": 50}
    df = st.de_tipo("dataframe")[0]
    assert df["Novedad"].tolist() == ["■ Nueva en el lote", "◆ Ya detectada (primer lote L0_001)"]
    assert df["ID detección"].tolist() == ["1", "2"] and df["ID evento"].tolist() == ["7", "7"]   # ids como texto


def test_calidad_exportacion_lee_todas_las_detecciones_con_los_filtros(monkeypatch):
    api = ApiTablero({"quality/errors": paginado([deteccion(1, True), deteccion(2, False)], columna_id="deteccion_id")})
    estado = {}
    st = correr(monkeypatch, "Calidad de datos", get=api, estado=estado, pulsados={"Preparar exportación"},
                valores={"Lote (obligatorio)": "L1"})
    exportadas = [p for p, _ in api.de("quality/errors") if p["tamano"] == 500]
    assert [p["pagina"] for p in exportadas] == [1, 1] and all(p["lote"] == "L1" for p in exportadas)
    assert estado["exportacion_errores_calidad"]["total"] == 2
    assert st.de_tipo("download_button") == ["Descargar CSV", "Descargar Excel"]


# ------------------------------------------------------------------ M1

TOTAL_M1 = {"model_id": 2, "model_version": "gbm_fe31_fixture", "n_predicciones": 10, "n_validas": 8,
            "conteo_estados": {}, "mae": 12.5, "rmse": 15.0, "wape": 0.1385, "smape": 0.15, "sesgo": -1.0,
            "suma_real": 900.0, "suma_pred": 880.0, "cobertura_intervalo": 0.75, "nivel_intervalo_nominal": 0.8,
            "n_ingenuo_7d": 7, "mae_mismas_filas_ingenuo": 12.0, "mae_ingenuo_7d": 15.0,
            "mae_relativo_ingenuo_7d": 0.8}


def respuesta_m1(totales, datos):
    return {"parametros": {"reglas": "m1-1.1", "marca_agua": "2025-12-20T12:00:00-05:00",
                           "corte_consolidacion": "2025-12-17T12:00:00-05:00", "margen_consolidacion_dias": 3,
                           "fuente_real": "demanda_franja", "advertencias": ["modelo 1: sin horario"]},
            "huella": "abc", "modelos": {}, "conteo_estados": {"valido": 8, "no_disponible": 2},
            "conteo_motivos": {"ok": 8, "sin_consolidar": 2}, "total": totales, "datos": datos}


def test_m1_no_consulta_hasta_pulsar_y_luego_envia_los_filtros(monkeypatch):
    api = ApiTablero({"predictions/accuracy": lambda p: respuesta_json(200, respuesta_m1(
        [TOTAL_M1], [{**TOTAL_M1, "idruta": 16, "horizonte_min": 60}]))})
    correr(monkeypatch, "Evaluación (M1)", get=api)
    assert api.de("predictions/accuracy") == []
    st = correr(monkeypatch, "Evaluación (M1)", get=api, pulsados={"Calcular métricas"})
    params, timeout = api.de("predictions/accuracy")[0]
    assert params == {"model_id": 2, "desde": DIA_ISO_DESDE, "hasta": DIA_ISO_HASTA,
                      "agrupar_por": ["idruta", "horizonte_min"]}
    assert timeout == 60
    html = texto_de(st, "markdown")
    assert "13,85 %" in html and "en 7 filas con ingenuo" in html and "nominal 80 %" in html
    assert "modelo 1: sin horario" in st.de_tipo("warning")


def test_m1_nunca_mezcla_modelos(monkeypatch):
    otro = {**TOTAL_M1, "model_id": 1, "model_version": "dia_hora_4sem_v1", "wape": 0.2}
    datos = [{**TOTAL_M1, "idruta": 16, "horizonte_min": 60}, {**otro, "idruta": 16, "horizonte_min": 60},
             {**otro, "idruta": 2, "horizonte_min": 60}]
    api = ApiTablero({"predictions/accuracy": lambda p: respuesta_m1_json(TOTAL_M1, otro, datos)})
    st = correr(monkeypatch, "Evaluación (M1)", get=api, pulsados={"Calcular métricas"},
                valores={"Modelo": None, "Modelo a mostrar": 1})
    assert "model_id" not in api.de("predictions/accuracy")[0][0]
    assert any("2 modelos" in i for i in st.de_tipo("info"))
    grupos = st.de_tipo("dataframe")[-1]
    assert len(grupos) == 2 and grupos["WAPE"].tolist() == [0.2, 0.2]          # solo el modelo 1


def respuesta_m1_json(t1, t2, datos):
    return respuesta_json(200, respuesta_m1([t1, t2], datos))


def test_m1_pares_muestra_estado_y_motivo_y_filtra_por_estado(monkeypatch):
    par = {"prediction_id": 101, "execution_id": "P1", "model_id": 2, "model_version": "v", "feature_version": "f",
           "idruta": 16, "franja_inicio": FRANJAS[0], "fin_ventana": FRANJAS[2], "horizonte_min": 60,
           "data_cutoff": DIA_ISO_DESDE, "generated_at": DIA_ISO_DESDE, "demanda_pred": 100.0,
           "limite_inferior": 80.0, "limite_superior": 120.0, "demanda_real": None, "error": None,
           "dentro_intervalo": None, "demanda_ingenuo_7d": 90.0, "programaciones_ventana": 4,
           "estado": "no_disponible", "motivo": "sin_consolidar",
           "detalle_motivo": "la ventana termina después del corte de consolidación"}

    def pares(params):
        cuerpo = json.loads(paginado([par], ("estado",))(params).content)
        return respuesta_json(200, {**cuerpo, "parametros": {}, "huella": "h"})
    api = ApiTablero({"predictions/accuracy/pairs": pares})
    st = correr(monkeypatch, "Evaluación (M1)", get=api, pulsados={"Consultar pares"},
                valores={"Vista M1": "Detalle de pares", "Estado del par": "no_disponible"})
    params, timeout = api.de("predictions/accuracy/pairs")[0]
    assert params["estado"] == "no_disponible" and params["pagina"] == 1 and timeout == 60
    df = st.de_tipo("dataframe")[0]
    assert df["Motivo"].tolist() == ["sin_consolidar"] and df["Real"].tolist() == [None]


# ------------------------------------------------------------------ modelos y explicación

def test_modelos_no_exponen_la_ruta_del_artefacto(monkeypatch):
    st = correr(monkeypatch, "Modelos y explicaciones")
    todo = texto_de(st) + " ".join(str(df.to_dict()) for df in st.de_tipo("dataframe"))
    assert "/srv/secreto" not in todo and "artefacto_ruta" not in todo
    assert "■ activo" in st.de_tipo("dataframe")[0]["Estado"].tolist()


CONFIRMA_ACTIVAR = "Confirmo que quiero activar este modelo y retirar el activo"
CONFIRMA_REVERSION = "Si el modelo elegido está retirado: confirmo que esto revierte el modelo activo"


def test_activacion_no_ofrece_rechazados_ni_el_activo(monkeypatch):
    st = correr(monkeypatch, "Modelos y explicaciones")
    opciones = dict(st.de_tipo("selectbox"))["Modelo a activar"]
    assert opciones == [1, 3]                       # retirado (reversión) y candidato; ni 2 (activo) ni 4 (rechazado)
    assert any("1 modelo(s) rechazado(s)" in c for c in st.de_tipo("caption"))


def test_activacion_exige_confirmacion_y_envia_usuario_y_motivo(monkeypatch):
    post = EscrituraFalsa(lambda: respuesta_json(200, {"model_id": 3, "modelo_anterior": 2}))
    valores = {"Usuario": "persona-a", "Motivo": "candidato aprobado tras M1", "Modelo a activar": 3}
    st = correr(monkeypatch, "Modelos y explicaciones", pulsados={"Activar modelo"}, post=post, valores=valores)
    assert post.llamadas == [] and any("Marque la confirmación" in w for w in st.de_tipo("warning"))
    estado = {}
    st = correr(monkeypatch, "Modelos y explicaciones", pulsados={"Activar modelo"}, post=post, estado=estado,
                valores={**valores, CONFIRMA_ACTIVAR: True})
    assert post.llamadas == [{"ruta": "models/3/activate", "timeout": 10, "otros": {},
                              "json": {"usuario": "persona-a", "motivo": "candidato aprobado tras M1"}}]
    assert "Modelo activo: 3. Modelo anterior: 2" in estado["_avisos"][0][1] and ("rerun", None) in st.eventos
    assert "_envio_activacion" not in estado


def test_reactivar_un_retirado_exige_la_confirmacion_de_reversion(monkeypatch):
    post = EscrituraFalsa(lambda: respuesta_json(200, {"model_id": 1, "modelo_anterior": 2}))
    valores = {"Usuario": "u", "Motivo": "reversión", "Modelo a activar": 1, CONFIRMA_ACTIVAR: True}
    st = correr(monkeypatch, "Modelos y explicaciones", pulsados={"Activar modelo"}, post=post, valores=valores)
    assert post.llamadas == [] and any("está retirado" in w for w in st.de_tipo("warning"))
    correr(monkeypatch, "Modelos y explicaciones", pulsados={"Activar modelo"}, post=post,
           valores={**valores, CONFIRMA_REVERSION: True})
    assert [c["ruta"] for c in post.llamadas] == ["models/1/activate"]


def test_activacion_409_muestra_el_motivo_de_la_api(monkeypatch):
    cuerpo = {"error": {"codigo": "conflicto", "mensaje": "No se puede activar el modelo 1: sin artefacto",
                        "detalle": None, "correlation_id": "c1"}}
    post = EscrituraFalsa(lambda: respuesta_json(409, cuerpo))
    estado = {}
    st = correr(monkeypatch, "Modelos y explicaciones", pulsados={"Activar modelo"}, post=post, estado=estado,
                valores={"Usuario": "u", "Motivo": "prueba", "Modelo a activar": 3, CONFIRMA_ACTIVAR: True})
    assert any(e.startswith("La operación falló") and "sin artefacto" in e and "c1" in e
               for e in st.de_tipo("error"))
    assert st.de_tipo("success") == [] and "_envio_activacion" not in estado      # rechazo definitivo: sin bloqueo


EID_EXPLICACION = "7a1c0e5e-0000-4000-8000-000000000771"


def explicacion_sintetica(pid=771201, eid=EID_EXPLICACION, model_id=2, version="gbm_fe31_fixture",
                          **cambios):
    """Explicación sintética COHERENTE con el contrato de la API (ExplicacionPrediccion): 29 aportes,
    factor = exp(aporte), referencia + suma = predicción en escala log. Datos inventados para pruebas."""
    import math
    aportes = [{"variable": "y_media_4_semanas", "valor": 498.0, "valor_modelo": 498.0, "aporte_log": 0.91},
               {"variable": "tipo_dia", "valor": "laboral", "valor_modelo": 0.0, "aporte_log": -0.05}]
    aportes += [{"variable": f"var_sintetica_{k:02d}", "valor": float(k), "valor_modelo": float(k),
                 "aporte_log": round(((-1) ** k) * 0.001 * k, 6)} for k in range(1, 28)]
    for a in aportes:
        a["factor"] = math.exp(a["aporte_log"])
    ref = 5.1
    pred_log = ref + math.fsum(a["aporte_log"] for a in aportes)
    base = {"prediction_id": pid, "execution_id": eid, "model_id": model_id, "model_version": version,
            "feature_version": "sobusa-fe-3.1", "artefacto_sha256": "f" * 64, "scikit_learn": "1.9.1",
            "idruta": 16, "franja_inicio": FRANJAS[0], "horizonte_min": 60, "data_cutoff": DIA_ISO_DESDE,
            "demanda_pred": round(math.exp(pred_log), 2), "estado": "reconstruida_y_verificada",
            "advertencia": "Reconstruida, no garantizada", "metodo": "TreeSHAP", "escala": "log",
            "referencia_log": ref, "referencia_pasajeros": math.exp(ref),
            "prediccion_log": pred_log, "prediccion_pasajeros": math.exp(pred_log), "aportes": aportes,
            "verificaciones": {"identidad_treeshap": {"error_absoluto": 1e-12, "tolerancia": 1e-9},
                               "reproduccion": {"predicciones_comparadas": 3, "horizontes": [30, 60, 120]}},
            "importancia_global_artefacto": {"metodo": "ganancia", "model_version": version, "nota": "global",
                                             "variables": [{"variable": "y_media_4_semanas", "ganancia": 10.0,
                                                            "proporcion": 0.6, "divisiones": 40}]}}
    return {**base, **cambios}


EXPLICACION = explicacion_sintetica()


def test_explicacion_verificada_muestra_aportes_y_permite_exportarlos(monkeypatch):
    api = ApiTablero({"predictions/771201/explanation": lambda p: respuesta_json(200, EXPLICACION)})
    st = correr(monkeypatch, "Modelos y explicaciones", get=api, pulsados={"Solicitar explicación"},
                valores={"ID de la predicción": 771201})
    assert api.de("predictions/771201/explanation")[0][1] == 60
    tabla = next(df for df in st.de_tipo("dataframe") if "Aporte (log)" in df.columns)
    assert len(tabla) == 29                                   # las 29 variables, de mayor a menor magnitud
    assert tabla["Variable"].tolist()[:2] == ["y_media_4_semanas", "tipo_dia"]
    assert "Reconstruida, no garantizada" in st.de_tipo("warning")
    assert st.de_tipo("download_button") == ["Descargar aportes (CSV)", "Descargar aportes (Excel)"]


@pytest.mark.parametrize("codigo, tipo, texto", [
    (409, "info", "no puede explicarse con el mecanismo disponible"),
    (404, "warning", "no existe"),
], ids=["409", "404"])
def test_explicacion_no_disponible_no_es_un_error_inesperado(monkeypatch, codigo, tipo, texto):
    cuerpo = {"error": {"codigo": "conflicto", "mensaje": "La predicción es del modelo dia_hora", "detalle": None,
                        "correlation_id": "cx"}}
    api = ApiTablero({"predictions/5/explanation": lambda p: respuesta_json(codigo, cuerpo)})
    st = correr(monkeypatch, "Modelos y explicaciones", get=api, pulsados={"Solicitar explicación"},
                valores={"ID de la predicción": 5})
    assert st.de_tipo("error") == [] and any(texto in m for m in st.de_tipo(tipo))
    assert st.de_tipo("download_button") == []


def test_explicacion_sin_estado_verificado_no_se_muestra(monkeypatch):
    api = ApiTablero({"predictions/5/explanation": lambda p: respuesta_json(200, {**EXPLICACION, "estado": "parcial"})})
    st = correr(monkeypatch, "Modelos y explicaciones", get=api, pulsados={"Solicitar explicación"},
                valores={"ID de la predicción": 5})
    assert len(st.de_tipo("error")) == 1 and st.de_tipo("download_button") == []


# ------------------------------------------------------------------ exportaciones desde la interfaz

def test_exportar_predicciones_usa_la_ejecucion_duena_y_lee_todas_las_paginas(monkeypatch):
    api = api_demanda()
    estado = {}
    st = correr(monkeypatch, "Demanda y frecuencias", get=api, estado=estado, pulsados={"Preparar exportación"},
                valores={"Tabla": "Predicciones"})
    exportadas = [p for p, _ in api.de("predictions") if p.get("execution_id") == "P1" and p["tamano"] == 500]
    assert [p["pagina"] for p in exportadas] == [1, 1]
    guardada = estado["exportacion_predicciones"]
    assert guardada["total"] == 3 and guardada["filtros"]["execution_id"] == "P1"
    libro = load_workbook(_io.BytesIO(guardada["xlsx"]))
    assert [c.value for c in libro["datos"][1]][0] == "prediction_id"
    assert st.de_tipo("download_button") == ["Descargar CSV", "Descargar Excel"]


def test_cambiar_un_filtro_descarta_la_exportacion_preparada(monkeypatch):
    api = api_demanda()
    estado = {}
    correr(monkeypatch, "Demanda y frecuencias", get=api, estado=estado, pulsados={"Preparar exportación"})
    assert "exportacion_recomendaciones" in estado
    st = correr(monkeypatch, "Demanda y frecuencias", get=api, estado=estado, valores={"Solo no viables": True})
    assert "exportacion_recomendaciones" not in estado
    assert "Los filtros cambiaron: se descartó la exportación preparada." in st.de_tipo("info")


# ------------------------------------------------------------------ resumen

def test_resumen_cuenta_alertas_y_no_presenta_metricas_registradas_como_desempeno(monkeypatch):
    filas = [alerta(1), {**alerta(2), "estado": "asignada"}, {**alerta(3), "estado": "reconocida", "idruta": 2}]
    api = ApiTablero({"alerts": paginado(filas, ("idruta", "estado"))})
    st = correr(monkeypatch, "Resumen", get=api)
    html = texto_de(st, "markdown")
    assert "0.802" not in html and "0,802" not in html and "cobertura_intervalo_80" not in html
    assert "gbm_fe31_fixture" in html
    anillo = st.de_tipo("vega_lite_chart")[0]
    assert {v["nombre"]: v["valor"] for v in anillo["layer"][0]["data"]["values"]} == {
        "Abierta": 1, "Reconocida": 1, "Asignada": 1}
    assert st.de_tipo("error") == []


def test_resumen_sin_api_muestra_sin_dato_y_no_ceros(monkeypatch):
    api = ApiTablero({"alerts": lambda p: requests.ConnectionError("caída"),
                      "models": lambda p: requests.ConnectionError("caída")})
    st = correr(monkeypatch, "Resumen", get=api)
    html = texto_de(st, "markdown")
    assert "API no disponible" in html and "Sin dato" in html
    assert st.de_tipo("vega_lite_chart") == []


# ------------------------------------------------------------------ revisión integral: estados inciertos

@pytest.mark.parametrize("cuerpo", [
    {"estado": "exitosa"},
    {**RESPUESTA_FRECUENCIAS, "execution_id_predicciones": None},
    {**RESPUESTA_FRECUENCIAS, "execution_id": "F9"},                     # no es UUID
    {**RESPUESTA_FRECUENCIAS, "recomendaciones": "192"},
    {**RESPUESTA_FRECUENCIAS, "estado": "en_curso"},
    ["no", "es", "objeto"],
], ids=["solo_estado", "sin_duena", "id_no_uuid", "conteo_texto", "estado_desconocido", "lista"])
def test_ejecucion_2xx_incompleta_queda_no_confirmada_y_bloquea_el_reenvio(monkeypatch, cuerpo):
    estado = {}
    post = EscrituraFalsa(lambda: respuesta_json(201, cuerpo))
    valores = {"Confirmo que quiero crear una ejecución nueva": True}
    st = correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular frecuencias"}, post=post,
                get=api_demanda(), estado=estado, valores=valores)
    avisos = st.de_tipo("warning")
    assert any(w.startswith("Resultado no confirmado") and "pudo haberse aplicado" in w for w in avisos)
    assert st.de_tipo("success") == [] and "_avisos" not in estado and "ejecuciones_sesion" not in estado
    assert estado["_envio_frecuencias"] is True
    correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular frecuencias"}, post=post,
           get=api_demanda(), estado=estado, valores=valores)
    assert len(post.llamadas) == 1


def test_ejecucion_fallida_confirmada_libera_el_bloqueo_y_lo_informa(monkeypatch):
    estado = {}
    cuerpo = {"execution_id": U_P9, "estado": "fallida", "predicciones": 0}
    post = EscrituraFalsa(lambda: respuesta_json(201, cuerpo))
    correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular frecuencias"}, post=post, get=api_demanda(),
           estado=estado, valores={"Confirmo que quiero crear una ejecución nueva": True})
    (tipo, texto, _), = estado["_avisos"]
    assert tipo == "aviso" and "fallida" in texto and "_envio_frecuencias" not in estado


def test_ejecucion_rechazada_por_la_api_es_fallida_y_no_bloquea(monkeypatch):
    estado = {}
    cuerpo = {"error": {"codigo": "error_interno", "mensaje": "Error interno", "detalle": None, "correlation_id": "c5"}}
    post = EscrituraFalsa(lambda: respuesta_json(500, cuerpo))
    st = correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular frecuencias"}, post=post, get=api_demanda(),
                estado=estado, valores={"Confirmo que quiero crear una ejecución nueva": True})
    assert any(e.startswith("La operación falló") and "c5" in e for e in st.de_tipo("error"))
    assert "_envio_frecuencias" not in estado


@pytest.mark.parametrize("cuerpo", [{"execution_id": U_F9},
                                    {"execution_id": "R5", "recomendaciones": 1, "no_viables": 0},
                                    {"recomendaciones": 1, "no_viables": 0}])
def test_recomendaciones_2xx_incompletas_quedan_no_confirmadas(monkeypatch, cuerpo):
    estado = {}
    post = EscrituraFalsa(lambda: respuesta_json(201, cuerpo))
    st = correr(monkeypatch, "Demanda y frecuencias", pulsados={"Calcular recomendaciones"}, post=post,
                get=api_demanda(), estado=estado,
                valores={"Confirmo que quiero calcular recomendaciones nuevas": True,
                         "…o id de la ejecución de predicción": U_P9})
    assert any(w.startswith("Resultado no confirmado") for w in st.de_tipo("warning"))
    assert estado["_envio_recomendaciones"] is True and "_avisos" not in estado


@pytest.mark.parametrize("cuerpo", [{"model_id": 1, "modelo_anterior": 2}, {"model_id": 3}, {"ok": True},
                                    {"model_id": "3", "modelo_anterior": 2}])
def test_activacion_2xx_incompleta_o_de_otro_modelo_queda_no_confirmada(monkeypatch, cuerpo):
    estado = {}
    post = EscrituraFalsa(lambda: respuesta_json(200, cuerpo))
    st = correr(monkeypatch, "Modelos y explicaciones", pulsados={"Activar modelo"}, post=post, estado=estado,
                valores={"Usuario": "u", "Motivo": "prueba", "Modelo a activar": 3, CONFIRMA_ACTIVAR: True})
    assert any(w.startswith("Resultado no confirmado") for w in st.de_tipo("warning"))
    assert estado["_envio_activacion"] is True and "_avisos" not in estado
    st = correr(monkeypatch, "Modelos y explicaciones", pulsados={"Activar modelo"}, post=post, estado=estado,
                valores={"Usuario": "u", "Motivo": "prueba", "Modelo a activar": 3, CONFIRMA_ACTIVAR: True})
    assert len(post.llamadas) == 1 and any("activar un modelo" in w for w in st.de_tipo("warning"))


@pytest.mark.parametrize("cuerpo", [{"alert_id": AID}, {"alert_id": AID, "estado": "cerrada"},
                                    {"alert_id": 9999, "estado": "reconocida"}, ["x"]])
def test_patch_2xx_que_no_confirma_el_cambio_no_se_presenta_como_exito(monkeypatch, cuerpo):
    estado = {}
    patch = PatchFalso(lambda: respuesta_json(200, cuerpo), estado)
    st = correr(monkeypatch, "Alertas", pulsados={"Guardar"}, patch=patch, estado=estado, get=GetFalso(),
                valores={"Usuario": "u", "Confirmo el cambio en la alerta": True})
    assert TEXTO_EXITO not in st.de_tipo("success") and st.de_tipo("json") == []
    assert any(w.startswith("Resultado no confirmado") for w in st.de_tipo("warning"))
    assert estado[f"_envio_alerta_{AID}"] is True


def test_el_bloqueo_de_una_alerta_no_bloquea_las_demas(monkeypatch):
    estado = {f"_envio_alerta_{AID}": True}
    patch = PatchFalso(lambda: respuesta_json(200, {"alert_id": 1200, "estado": "reconocida"}), estado)
    st = correr(monkeypatch, "Alertas", pulsados={"Guardar"}, patch=patch, estado=estado, get=GetFalso(),
                valores={"Usuario": "u", "Confirmo el cambio en la alerta": True, "Alerta": 1200})
    assert len(patch.llamadas) == 1 and patch.llamadas[0]["url"].endswith("/alerts/1200/status")
    assert TEXTO_EXITO in st.de_tipo("success")
    st = correr(monkeypatch, "Alertas", pulsados={"Guardar"}, patch=patch, estado=estado, get=GetFalso(),
                valores={"Usuario": "u", "Confirmo el cambio en la alerta": True})
    assert len(patch.llamadas) == 1 and any("actualizar esta alerta" in w for w in st.de_tipo("warning"))


# ------------------------------------------------------------------ revisión integral: seguridad y coherencia

def test_el_texto_de_la_api_no_se_interpreta_como_markdown(monkeypatch):
    cuerpo = {"error": {"codigo": "x", "mensaje": "![rastreo](http://evil.invalid/x.png) *negrita* $x$",
                        "detalle": None, "correlation_id": "c-1"}}
    api = ApiTablero({"quality/errors": lambda p: respuesta_json(422, cuerpo)})
    st = correr(monkeypatch, "Calidad de datos", valores={"Lote (obligatorio)": "L1"}, get=api)
    (error,) = st.de_tipo("error")
    assert r"\!\[rastreo\]" in error and r"\*negrita\*" in error and r"\$x\$" in error and "c-1" in error


def test_explicacion_y_recomendacion_de_una_alerta_se_muestran_como_texto_plano(monkeypatch):
    filas = [{**alerta(1), "explicacion": "[clic](http://evil.invalid)", "recomendacion": "**x**"}]
    st = correr(monkeypatch, "Alertas", get=ApiTablero({"alerts": paginado(filas, ("idruta", "estado"))}))
    assert st.de_tipo("text") == ["[clic](http://evil.invalid)", "**x**"]      # st.text no interpreta Markdown
    assert all("evil.invalid" not in str(v) for t, v in st.eventos if t == "write")


@pytest.mark.parametrize("texto", ["../models", "abc", "1; DROP", "00000000-0000-0000-0000-00000000000g"])
def test_id_de_ejecucion_manual_que_no_es_uuid_no_llega_a_la_url(monkeypatch, texto):
    api = api_demanda()
    st = correr(monkeypatch, "Demanda y frecuencias", get=api, valores={"Ejecución (id, opcional)": texto})
    assert not [r for r, _, _ in api.llamadas if r.startswith("frequency/executions")]
    assert any("UUID" in w for w in st.de_tipo("warning"))
    api = ApiTablero()
    st = correr(monkeypatch, "Evaluación (M1)", get=api, pulsados={"Calcular métricas"},
                valores={"Ejecución (id, opcional)": texto})
    assert api.de("predictions/accuracy") == [] and any("UUID" in w for w in st.de_tipo("warning"))


def test_id_manual_de_una_ejecucion_sin_filas_lo_explica(monkeypatch):
    detalle = {"execution_id": U_P9, "tipo": "ingesta", "estado": "exitosa", "parametros": {}, "resumen": {}}
    api = api_demanda(**{f"frequency/executions/{U_P9}": lambda p: respuesta_json(200, detalle)})
    st = correr(monkeypatch, "Demanda y frecuencias", get=api, valores={"Ejecución (id, opcional)": U_P9})
    assert any("tipo «ingesta»" in w for w in st.de_tipo("warning")) and st.de_tipo("vega_lite_chart") == []


def test_ejecuciones_de_la_sesion_sin_filas_en_el_dia_no_se_ofrecen(monkeypatch):
    estado = {"ejecuciones_sesion": [
        {"tipo": "recomendacion", "execution_id": U_F9, "recomendaciones_de": U_F9, "execution_id_prediccion": U_P9,
         "execution_id_predicciones": None},
        {"tipo": "frecuencias", "dia": "2025-10-01", "idrutas": [16], "estado": "exitosa", "execution_id": "F1",
         "execution_id_prediccion": "P2", "execution_id_predicciones": "P1", "recomendaciones_de": "F1"}]}
    st = correr(monkeypatch, "Demanda y frecuencias", get=api_demanda(), estado=estado)
    etiquetas = [o for e, o in st.de_tipo("selectbox") if e == "Ejecución a mostrar"][0]
    assert len(etiquetas) == 2                        # F1 (sesión) y R0 (datos); la de U_F9 no tiene filas


def test_explicar_desde_la_decision_pide_la_explicacion_de_esa_prediccion(monkeypatch):
    api = ApiTablero({"predictions/771201/explanation": lambda p: respuesta_json(200, EXPLICACION)})
    estado = {"_pid_pendiente": 771201}
    st = correr(monkeypatch, "Modelos y explicaciones", get=api, estado=estado,
                valores={"ID de la predicción": 771201})
    assert len(api.de("predictions/771201/explanation")) == 1           # sin pulsar «Solicitar explicación»
    assert estado["pid_explicacion"] == 771201 and "_pid_pendiente" not in estado
    assert st.de_tipo("download_button") == ["Descargar aportes (CSV)", "Descargar aportes (Excel)"]


def test_una_pagina_distinta_de_la_pedida_es_un_error_y_no_se_muestra(monkeypatch):
    def otra_pagina(params):
        return respuesta_json(200, {"datos": [alerta(1)], "paginacion": {"pagina": 1, "tamano": 50, "total": 120,
                                                                         "paginas": 3}})
    st = correr(monkeypatch, "Alertas", get=ApiTablero({"alerts": otra_pagina}), valores={"Página (alertas)": 2})
    assert any("pagina=1 y se pidió 2" in e for e in st.de_tipo("error"))
    assert st.de_tipo("dataframe") == []


def test_el_nombre_del_archivo_exportado_no_lleva_rutas(monkeypatch):
    api = ApiTablero({"quality/errors": paginado([deteccion(1, True)], columna_id="deteccion_id")})
    st = correr(monkeypatch, "Calidad de datos", get=api, pulsados={"Preparar exportación"},
                valores={"Lote (obligatorio)": "../../etc/x lote"})
    nombres = st.de_tipo("download_file")
    assert nombres == ["errores_calidad_.._.._etc_x_lote.csv", "errores_calidad_.._.._etc_x_lote.xlsx"]
    assert all("/" not in n and " " not in n for n in nombres)


def test_decision_no_repite_franjas_cuando_no_hay_reducciones(monkeypatch):
    recs = [rec(11, "F1", 101, FRANJAS[0], delta_buses_hora=2.0), rec(12, "F1", 102, FRANJAS[1], viable=False),
            rec(13, "F1", 103, FRANJAS[2], delta_buses_hora=0.0)]
    api = ApiTablero({"predictions": paginado(PREDS, ("idruta", "horizonte_min", "execution_id")),
                      "recommendations": paginado(recs, ("idruta", "horizonte_min", "execution_id"))})
    st = correr(monkeypatch, "Demanda y frecuencias", get=api, valores={"Vista": "02 · Decisión operativa"})
    opciones = [o for e, o in st.de_tipo("selectbox") if e == "Franja a revisar"][0]
    assert sorted(opciones) == [11, 12, 13] and len(opciones) == len(set(opciones))


def test_las_tablas_conservan_los_enteros_y_los_faltantes(monkeypatch):
    st = correr(monkeypatch, "Demanda y frecuencias", get=api_demanda())
    df = st.de_tipo("dataframe")[-1]
    assert df["Horizonte (min)"].tolist() == [60, 60, 60]
    assert all(type(x) is int for x in df["Horizonte (min)"])
    assert df["Bus/h recomendado"].tolist()[1] is None                    # no viable: sin dato, no 0


@pytest.mark.parametrize("codigo, cuerpo, bloquea", [
    (409, {"error": {"codigo": "conflicto", "mensaje": "m", "detalle": None, "correlation_id": "c"}}, False),
    (502, {"error": {"codigo": "x", "mensaje": "m", "detalle": None, "correlation_id": "c"}}, True),
    (500, {"detail": "Internal"}, True),
], ids=["409_api", "502", "500_sin_formato"])
def test_patch_con_error_de_intermediario_mantiene_el_bloqueo(monkeypatch, codigo, cuerpo, bloquea):
    estado = {}
    patch = PatchFalso(lambda: respuesta_json(codigo, cuerpo), estado)
    st = correr(monkeypatch, "Alertas", pulsados={"Guardar"}, patch=patch, estado=estado, get=GetFalso(),
                valores={"Usuario": "u", "Confirmo el cambio en la alerta": True})
    assert len(st.de_tipo("error")) == 1
    assert (f"_envio_alerta_{AID}" in estado) is bloquea
