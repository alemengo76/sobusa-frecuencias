"""PATCH de gestión de alertas en dashboard/tablero.py (RF-011).

El tablero dibuja su interfaz al nivel del módulo, así que cada prueba ejecuta el guion completo con
runpy.run_path, con:
- un Streamlit falso ESTRICTO (no hace falta tenerlo instalado): implementa solo lo que usa el
  tablero y cualquier otro atributo lanza un error explícito;
- requests.get / requests.post / requests.patch reemplazados por funciones de la prueba, y cualquier
  otra solicitud bloqueada en requests.Session.request;
- respuestas requests.Response reales, para que raise_for_status() y json() lancen las excepciones
  verdaderas de requests.
No usa red, base de datos, credenciales ni .env, y no modifica dashboard/tablero.py.
"""
import contextlib
import json
import runpy
import sys
import types
from pathlib import Path

import numpy  # noqa: F401  (se cargan antes de ejecutar el tablero)
import pandas  # noqa: F401
import pytest
import requests

TABLERO = Path(__file__).resolve().parents[2] / "dashboard" / "tablero.py"
API = "http://api-falsa.invalid/api/v1"
AID = 1201                                    # primera alerta de la tabla: la que elige el formulario
CUERPO_ESPERADO = {"estado": "reconocida", "usuario": "usuario-prueba", "comentario": None,
                   "usuario_asignado": None}
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

    # datos y gráficos
    def dataframe(self, datos, **kwargs): self._registrar("dataframe", datos)
    def line_chart(self, datos, **kwargs): self._registrar("line_chart", datos)
    def bar_chart(self, datos, **kwargs): self._registrar("bar_chart", datos)

    # contenedores
    @property
    def sidebar(self): return contextlib.nullcontext()
    def tabs(self, nombres): return [contextlib.nullcontext() for _ in nombres]
    def form(self, clave, **kwargs): return contextlib.nullcontext()

    # widgets
    def selectbox(self, etiqueta, opciones, index=0, **kwargs):
        return self.valores.get(etiqueta, list(opciones)[index])
    def radio(self, etiqueta, opciones, index=0, **kwargs):
        return self.valores.get(etiqueta, list(opciones)[index])
    def date_input(self, etiqueta, valor=None, **kwargs): return self.valores.get(etiqueta, valor)
    def text_input(self, etiqueta, **kwargs): return self.valores.get(etiqueta, "")
    def text_area(self, etiqueta, **kwargs): return self.valores.get(etiqueta, "")
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
    """Alerta sintética con las columnas que devuelve la API."""
    return {"alert_id": i, "execution_id": "00000000-0000-0000-0000-000000000001", "codigo": "ALT-001",
            "severidad": "alta", "estado": "abierta", "idruta": 16,
            "franja_inicio": "2025-12-03T06:00:00-05:00", "horizonte_min": 60, "valor_observado": 120.5,
            "valor_esperado": 80.0, "umbral": 100, "umbral_version": 1, "puntuacion_anomalia": 1.2,
            "explicacion": "Demanda alta", "recomendacion": "Reforzar", "creada_en": "2025-12-03T05:00:00-05:00",
            "actualizada_en": "2025-12-03T05:30:00-05:00", "usuario_asignado": None, "clave_dedup": f"k{i}"}


ALERTAS = [alerta(i) for i in (AID, 1200, 1199)]


class GetFalso:
    """GET de pantalla (predicciones, recomendaciones, alertas), con el contrato de la API:
    {"datos": [...], "paginacion": {"pagina", "tamano", "total"}}, respetando pagina y tamano."""

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
        else:
            raise AssertionError(f"GET inesperado: {url}")
        cuerpo = {"datos": seleccion[(pagina - 1) * tamano:pagina * tamano],
                  "paginacion": {"pagina": pagina, "tamano": tamano, "total": len(seleccion)}}
        return respuesta_json(200, cuerpo, url)


class PatchFalso:
    """requests.patch simulado: registra cada llamada y después devuelve la respuesta indicada o lanza
    la excepción indicada."""

    def __init__(self, efecto, estado: dict):
        self.efecto, self.estado, self.llamadas = efecto, estado, []

    def __call__(self, url, json=None, timeout=None, **otros):
        self.llamadas.append({"url": url, "json": json, "timeout": timeout, "otros": otros})
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
    st = StFalso(estado, pulsados, valores={"Usuario": "usuario-prueba"})
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

