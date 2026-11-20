"""Cliente HTTP del tablero (dashboard/cliente.py): errores, paginación e identificadores de ejecución.

Sin red, sin base de datos y sin Streamlit: requests.get/post/patch se reemplazan por funciones de la
prueba que devuelven requests.Response reales o lanzan las excepciones reales de requests.
"""
import json

import pytest
import requests

from dashboard import cliente as cl

API = "http://api-falsa.invalid/api/v1"


@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    def prohibido(*args, **kwargs):
        raise AssertionError("solicitud HTTP real bloqueada")
    monkeypatch.setattr(requests.sessions.Session, "request", prohibido)
    monkeypatch.setenv("API_URL", API)


def respuesta(codigo, contenido, url=f"{API}/x"):
    r = requests.models.Response()
    r.status_code, r.url, r.encoding = codigo, url, "utf-8"
    r._content = contenido if isinstance(contenido, bytes) else json.dumps(contenido).encode("utf-8")
    return r


class Falso:
    """Reemplazo de requests.get/post/patch: registra la llamada y devuelve o lanza lo indicado."""

    def __init__(self, efecto):
        self.efecto, self.llamadas = efecto, []

    def __call__(self, url, **kwargs):
        self.llamadas.append((url, kwargs))
        if isinstance(self.efecto, BaseException):
            raise self.efecto
        return self.efecto


def instalar(monkeypatch, metodo, efecto):
    falso = Falso(efecto)
    monkeypatch.setattr(requests, metodo, falso)
    return falso


def pagina(datos, p=1, t=50, total=None, **extra):
    total = len(datos) if total is None else total
    return {"datos": datos, "paginacion": {"pagina": p, "tamano": t, "total": total,
                                           "paginas": (total + t - 1) // t}, **extra}


# ------------------------------------------------------------------ URL, parámetros y tiempos de espera

def test_usa_api_url_y_tiene_un_valor_predeterminado(monkeypatch):
    assert cl.url_api() == API
    monkeypatch.delenv("API_URL")
    assert cl.url_api() == "http://localhost:8000/api/v1"


def test_get_limpia_los_parametros_y_usa_tiempo_de_espera_explicito(monkeypatch):
    falso = instalar(monkeypatch, "get", respuesta(200, {"ok": True}))
    cl.obtener("alerts", {"idruta": 16, "estado": None, "es_viable": False, "agrupar_por": ["idruta", "hora"]})
    url, kwargs = falso.llamadas[0]
    assert url == f"{API}/alerts"
    assert kwargs == {"timeout": cl.TIEMPO_CONSULTA,
                      "params": {"idruta": 16, "es_viable": "false", "agrupar_por": ["idruta", "hora"]}}


def test_post_envia_json_con_el_tiempo_indicado(monkeypatch):
    falso = instalar(monkeypatch, "post", respuesta(201, {"execution_id": "e1"}))
    cuerpo = {"dia": "2025-10-01", "idrutas": [16], "horizontes": [60], "base_demanda": "puntual"}
    assert cl.solicitar("POST", "frequency/executions", json=cuerpo, timeout=cl.TIEMPO_EJECUCION,
                        modifica=True) == {"execution_id": "e1"}
    assert falso.llamadas == [(f"{API}/frequency/executions", {"timeout": 120, "json": cuerpo})]


# ------------------------------------------------------------------ errores

@pytest.mark.parametrize("excepcion, tipo", [
    (requests.exceptions.ReadTimeout("lento"), "timeout"),
    (requests.exceptions.ConnectTimeout("lento"), "timeout"),     # hereda de Timeout y de ConnectionError
    (requests.ConnectionError("caída"), "conexion"),
    (requests.exceptions.ChunkedEncodingError("cortado"), "comunicacion"),
], ids=["lectura", "conexion_lenta", "conexion", "otro"])
def test_errores_de_red_se_clasifican_sin_mostrar_la_url(monkeypatch, excepcion, tipo):
    instalar(monkeypatch, "get", excepcion)
    with pytest.raises(cl.ErrorApi) as info:
        cl.obtener("predictions")
    assert info.value.tipo == tipo and info.value.no_confirmada is False
    assert "api-falsa.invalid" not in info.value.texto()


@pytest.mark.parametrize("excepcion", [requests.exceptions.ReadTimeout("x"), requests.ConnectionError("x")])
def test_una_escritura_sin_respuesta_queda_como_no_confirmada(monkeypatch, excepcion):
    instalar(monkeypatch, "post", excepcion)
    with pytest.raises(cl.ErrorApi) as info:
        cl.solicitar("POST", "models/2/activate", json={}, modifica=True)
    assert info.value.no_confirmada is True
    assert "No se pudo confirmar" in info.value.texto() and "antes de reintentar" in info.value.texto()


@pytest.mark.parametrize("codigo", [404, 409, 422, 500])
def test_errores_http_con_formato_uniforme_conservan_mensaje_y_correlation_id(monkeypatch, codigo):
    cuerpo = {"error": {"codigo": "conflicto", "mensaje": "Transición no permitida", "detalle": None,
                        "correlation_id": "cid-123"}}
    instalar(monkeypatch, "get", respuesta(codigo, cuerpo))
    with pytest.raises(cl.ErrorApi) as info:
        cl.obtener("alerts")
    e = info.value
    assert (e.tipo, e.codigo_http, e.mensaje_api, e.correlation_id) == ("http", codigo, "Transición no permitida",
                                                                        "cid-123")
    assert cl.TEXTO_CODIGO[codigo] in e.texto() and "Identificador de seguimiento: cid-123" in e.texto()
    assert e.no_confirmada is False                     # la API respondió: se sabe que no se aplicó


def test_error_http_422_conserva_el_detalle_de_los_campos(monkeypatch):
    detalle = [{"campo": "query.lote", "mensaje": "Field required"}]
    instalar(monkeypatch, "get", respuesta(422, {"error": {"codigo": "solicitud_invalida", "mensaje": "m",
                                                          "detalle": detalle, "correlation_id": "c"}}))
    with pytest.raises(cl.ErrorApi) as info:
        cl.obtener("quality/errors")
    assert info.value.detalle == detalle


@pytest.mark.parametrize("contenido", [b"<html>Bad Gateway</html>", b'{"detail": "Not Found"}',
                                       b'{"error": "texto"}', b'["lista"]'])
def test_error_http_sin_formato_uniforme_no_inventa_mensaje(monkeypatch, contenido):
    instalar(monkeypatch, "get", respuesta(502, contenido))
    with pytest.raises(cl.ErrorApi) as info:
        cl.obtener("alerts")
    assert info.value.codigo_http == 502 and info.value.mensaje_api is None and info.value.correlation_id is None


def test_json_invalido_en_lectura_y_en_escritura(monkeypatch):
    instalar(monkeypatch, "get", respuesta(200, b"<html>ok</html>"))
    with pytest.raises(cl.ErrorApi) as info:
        cl.obtener("models")
    assert info.value.tipo == "json" and not info.value.no_confirmada
    instalar(monkeypatch, "patch", respuesta(200, b"no es json"))
    with pytest.raises(cl.ErrorApi) as info:
        cl.solicitar("PATCH", "alerts/1/status", json={}, modifica=True)
    assert info.value.no_confirmada and "pudo haberse aplicado" in info.value.texto()


def test_respuesta_que_no_es_objeto_es_error_de_estructura(monkeypatch):
    instalar(monkeypatch, "get", respuesta(200, ["a"]))
    with pytest.raises(cl.ErrorApi) as info:
        cl.obtener("models")
    assert info.value.tipo == "estructura"


# ------------------------------------------------------------------ páginas

def test_pagina_valida_y_extra(monkeypatch):
    instalar(monkeypatch, "get", respuesta(200, pagina([{"a": 1}, {"a": 2}], p=2, t=2, total=5, huella="h")))
    p = cl.obtener_pagina("predictions/accuracy/pairs", {"pagina": 2, "tamano": 2})
    assert (p.pagina, p.tamano, p.total, p.paginas, len(p.datos)) == (2, 2, 5, 3, 2)
    assert p.extra == {"huella": "h"} and not p.vacia


def test_pagina_vacia_es_explicita_y_no_un_error(monkeypatch):
    instalar(monkeypatch, "get", respuesta(200, pagina([])))
    p = cl.obtener_pagina("predictions", {"tamano": 50})
    assert p.vacia and p.paginas == 0 and p.datos == []


def test_pagina_sin_paginas_lo_deduce(monkeypatch):
    instalar(monkeypatch, "get", respuesta(200, {"datos": [{"a": 1}],
                                                 "paginacion": {"pagina": 1, "tamano": 50, "total": 120}}))
    assert cl.obtener_pagina("alerts", {"tamano": 50}).paginas == 3


@pytest.mark.parametrize("cuerpo", [
    {"datos": []},
    {"paginacion": {"pagina": 1, "tamano": 50, "total": 0, "paginas": 0}},
    {"datos": [], "paginacion": {"pagina": 1, "tamano": 50, "total": -1, "paginas": 0}},
    {"datos": [], "paginacion": {"pagina": 1, "tamano": 50, "total": True, "paginas": 0}},
    {"datos": [], "paginacion": {"pagina": 1, "tamano": 50, "total": 120, "paginas": 9}},
    {"datos": [{"a": 1}, {"a": 2}], "paginacion": {"pagina": 1, "tamano": 1, "total": 2, "paginas": 2}},
    {"datos": ["fila"], "paginacion": {"pagina": 1, "tamano": 50, "total": 1, "paginas": 1}},
], ids=["sin_paginacion", "sin_datos", "total_negativo", "total_booleano", "paginas_incoherente",
        "mas_filas_que_tamano", "fila_no_objeto"])
def test_respuesta_sin_los_campos_esperados_no_se_convierte_en_tabla_vacia(monkeypatch, cuerpo):
    instalar(monkeypatch, "get", respuesta(200, cuerpo))
    with pytest.raises(cl.ErrorApi) as info:
        cl.obtener_pagina("alerts", {"tamano": 50 if "tamano" not in str(cuerpo) else 1})
    assert info.value.tipo == "estructura"


@pytest.mark.parametrize("tamano", [0, 501, True, "50"])
def test_tamano_fuera_del_contrato_se_rechaza_sin_llamar_a_la_api(monkeypatch, tamano):
    falso = instalar(monkeypatch, "get", respuesta(200, pagina([])))
    with pytest.raises(ValueError):
        cl.obtener_pagina("alerts", {"tamano": tamano})
    assert falso.llamadas == []


def test_error_durante_la_lectura_de_una_pagina_se_propaga(monkeypatch):
    instalar(monkeypatch, "get", respuesta(500, {"error": {"codigo": "error_interno", "mensaje": "falló",
                                                          "detalle": None, "correlation_id": "c9"}}))
    with pytest.raises(cl.ErrorApi) as info:
        cl.obtener_pagina("recommendations", {"pagina": 3, "tamano": 50})
    assert info.value.codigo_http == 500 and info.value.correlation_id == "c9"


# ------------------------------------------------------------------ identificadores de ejecución (P1)

def test_respuesta_de_frecuencias_que_reutilizo_predicciones():
    r = {"execution_id": "f1", "estado": "exitosa", "execution_id_prediccion": "p2",
         "execution_id_predicciones": "p1", "predicciones": 64, "recomendaciones": 64}
    assert cl.ids_de_respuesta_frecuencias(r) == {"estado": "exitosa", "execution_id": "f1",
                                                  "execution_id_prediccion": "p2",
                                                  "execution_id_predicciones": "p1", "recomendaciones_de": "f1"}


def test_respuesta_de_frecuencias_fallida_no_ofrece_ids_de_filas():
    r = {"execution_id": "p9", "estado": "fallida", "predicciones": 0}
    ids = cl.ids_de_respuesta_frecuencias(r)
    assert ids["execution_id_predicciones"] is None and ids["recomendaciones_de"] is None


@pytest.mark.parametrize("detalle, esperado", [
    ({"execution_id": "p2", "tipo": "prediccion", "estado": "exitosa",
      "resumen": {"execution_id_predicciones": "p1", "reutiliza": "p1"}},
     {"predicciones_de": "p1", "recomendaciones_de": None}),
    ({"execution_id": "p3", "tipo": "prediccion", "estado": "exitosa", "resumen": {}},
     {"predicciones_de": "p3", "recomendaciones_de": None}),
    ({"execution_id": "f1", "tipo": "frecuencias", "estado": "exitosa",
      "resumen": {"execution_id_prediccion": "p2", "execution_id_predicciones": "p1"}},
     {"predicciones_de": "p1", "recomendaciones_de": "f1", "prediccion_origen": "p2"}),
    ({"execution_id": "r1", "tipo": "recomendacion", "estado": "exitosa",
      "parametros": {"execution_id_prediccion": "p2"}, "resumen": {}},
     {"predicciones_de": None, "recomendaciones_de": "r1", "prediccion_origen": "p2"}),
    ({"execution_id": "f2", "tipo": "frecuencias", "estado": "fallida", "resumen": {}},
     {"predicciones_de": None, "recomendaciones_de": None}),
], ids=["prediccion_reutiliza", "prediccion_propia", "frecuencias", "recomendacion", "fallida"])
def test_ids_de_detalle_de_ejecucion(detalle, esperado):
    ids = cl.ids_de_detalle(detalle)
    for clave, valor in esperado.items():
        assert ids[clave] == valor, clave


def test_transiciones_coinciden_con_el_catalogo_del_backend():
    import ast
    from pathlib import Path
    arbol = ast.parse((Path(__file__).resolve().parents[2] / "app" / "alertas" / "catalogo.py").read_text("utf-8"))
    nodo = next(n for n in arbol.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "TRANSICIONES" for t in n.targets))
    reales = {k.value: {e.value for e in v.elts} if isinstance(v, ast.Set) else set()
              for k, v in zip(nodo.value.keys, nodo.value.values, strict=True)}
    assert {k: set(v) for k, v in cl.TRANSICIONES_ALERTA.items()} == reales


# ------------------------------------------------------------------ respuestas 2xx de operaciones que escriben

U1, U2, U3 = ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002",
              "00000000-0000-4000-8000-000000000003")
FREC_OK = {"execution_id": U1, "estado": "exitosa", "execution_id_prediccion": U2, "execution_id_predicciones": U3,
           "predicciones": 10, "recomendaciones": 10, "no_viables": 0, "alertas_nuevas": 1}


def no_confirmada(funcion, *args):
    with pytest.raises(cl.ErrorApi) as info:
        funcion(*args)
    e = info.value
    assert e.tipo == "incompleta" and e.no_confirmada and "pudo haberse aplicado" in e.texto()
    return e


def test_ejecucion_de_frecuencias_valida_y_fallida_se_confirman():
    assert cl.validar_ejecucion_frecuencias(FREC_OK) == FREC_OK
    fallida = {"execution_id": U2, "estado": "fallida", "predicciones": 0}
    assert cl.validar_ejecucion_frecuencias(fallida) == fallida


@pytest.mark.parametrize("cambios", [{"execution_id": None}, {"execution_id": "abc"}, {"execution_id_predicciones": ""},
                                     {"predicciones": None}, {"no_viables": True}, {"alertas_nuevas": 1.0},
                                     {"estado": None}, {"estado": "en_curso"}])
def test_ejecucion_de_frecuencias_incompleta_no_se_confirma(cambios):
    e = no_confirmada(cl.validar_ejecucion_frecuencias, {**FREC_OK, **cambios})
    assert "calcular frecuencias" in e.texto()


def test_ejecucion_fallida_sin_id_no_se_confirma():
    no_confirmada(cl.validar_ejecucion_frecuencias, {"estado": "fallida"})
    no_confirmada(cl.validar_ejecucion_frecuencias, [FREC_OK])


def test_calculo_de_recomendaciones():
    ok = {"execution_id": U1, "recomendaciones": 5, "no_viables": 2}
    assert cl.validar_calculo_recomendaciones(ok) == ok
    for malo in ({"execution_id": U1}, {**ok, "execution_id": 7}, {**ok, "no_viables": "2"}, None):
        no_confirmada(cl.validar_calculo_recomendaciones, malo)


def test_activacion_exige_el_mismo_modelo_y_modelo_anterior():
    assert cl.validar_activacion({"model_id": 3, "modelo_anterior": None}, 3)["modelo_anterior"] is None
    assert cl.validar_activacion({"model_id": 3, "modelo_anterior": 2}, 3)["model_id"] == 3
    for malo in ({"model_id": 4, "modelo_anterior": 2}, {"model_id": 3}, {"model_id": True, "modelo_anterior": 2},
                 {"model_id": 3, "modelo_anterior": "2"}, "ok"):
        no_confirmada(cl.validar_activacion, malo, 3)


def test_cambio_de_alerta_exige_la_alerta_con_el_estado_pedido():
    assert cl.validar_cambio_alerta({"alert_id": 9, "estado": "cerrada", "historial": []}, 9, "cerrada")
    for malo in ({"alert_id": 9}, {"alert_id": 8, "estado": "cerrada"}, {"alert_id": 9, "estado": "abierta"}, []):
        no_confirmada(cl.validar_cambio_alerta, malo, 9, "cerrada")


@pytest.mark.parametrize("texto, esperado", [(U1, True), (U1.upper(), True), (f" {U1} ", True), ("../models", False),
                                             ("1", False), (None, False), (U1 + "/x", False)])
def test_es_uuid(texto, esperado):
    assert cl.es_uuid(texto) is esperado


def test_pagina_devuelta_distinta_de_la_pedida(monkeypatch):
    instalar(monkeypatch, "get", respuesta(200, pagina([{"a": 1}], p=1, t=50, total=120)))
    with pytest.raises(cl.ErrorApi, match="se pidió 2"):
        cl.obtener_pagina("alerts", {"pagina": 2, "tamano": 50})
    instalar(monkeypatch, "get", respuesta(200, pagina([{"a": 1}], p=1, t=25, total=120)))
    with pytest.raises(cl.ErrorApi, match="tamano"):
        cl.obtener_pagina("alerts", {"pagina": 1, "tamano": 50})


@pytest.mark.parametrize("codigo, cuerpo, incierto", [
    (500, {"error": {"codigo": "error_interno", "mensaje": "m", "detalle": None, "correlation_id": "c"}}, False),
    (409, {"error": {"codigo": "conflicto", "mensaje": "m", "detalle": None, "correlation_id": "c"}}, False),
    (502, b"<html>Bad Gateway</html>", True),
    (504, {"error": {"codigo": "x", "mensaje": "m", "detalle": None, "correlation_id": "c"}}, True),
    (500, b"Internal Server Error", True),
], ids=["500_api", "409_api", "502_proxy", "504", "500_sin_formato"])
def test_un_error_de_intermediario_en_una_escritura_queda_sin_confirmar(monkeypatch, codigo, cuerpo, incierto):
    instalar(monkeypatch, "post", respuesta(codigo, cuerpo))
    with pytest.raises(cl.ErrorApi) as info:
        cl.solicitar("POST", "models/3/activate", json={}, modifica=True)
    assert info.value.no_confirmada is incierto and info.value.codigo_http == codigo
    instalar(monkeypatch, "get", respuesta(codigo, cuerpo))
    with pytest.raises(cl.ErrorApi) as info:
        cl.obtener("models")
    assert info.value.no_confirmada is False                      # una lectura nunca queda «sin confirmar»
