"""Cliente HTTP del tablero para la API /api/v1 (sin Streamlit, sin acceso a la base de datos).

Centraliza las solicitudes del tablero: tiempos de espera explícitos, comprobación del código HTTP,
lectura del formato uniforme de errores ``{"error": {codigo, mensaje, detalle, correlation_id}}`` y
mensajes entendibles. No muestra trazas, credenciales ni la URL de la API.

``requests.get``, ``requests.post`` y ``requests.patch`` se buscan en el momento de cada llamada, así
que las pruebas pueden reemplazarlos con ``monkeypatch``.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

import requests

API_POR_DEFECTO = "http://localhost:8000/api/v1"
TIEMPO_CONSULTA = 10          # segundos: consultas GET
TIEMPO_EVALUACION = 60        # segundos: M1 y explicaciones reconstruyen datos en el servidor
TIEMPO_ESCRITURA = 10         # segundos: PATCH de alertas y activación de modelos
TIEMPO_EJECUCION = 120        # segundos: ejecución completa de frecuencias y recomendaciones
TAMANO_PAGINA_MAX = 500       # igual al le=500 de la API (app/api/main.py: Tamano)

RUTAS = {2: "SILENCIO", 16: "CRA 54", 21: "CALLE 72"}
HORIZONTES = (30, 60, 120)
BASES_DEMANDA = ("puntual", "superior")
ESTADOS_ALERTA = ("abierta", "reconocida", "asignada", "cerrada", "descartada")
SEVERIDADES = ("baja", "media", "alta")
ALCANCES_CALIDAD = ("auto", "ejecucion", "ventana")
AGRUPACIONES_M1 = ("idruta", "horizonte_min", "execution_id", "dia", "hora", "tipo_dia")
ESTADOS_M1 = ("valido", "no_disponible", "incompleto_cuarentena", "sin_correspondencia",
              "duplicado", "inconsistente")
ESTADOS_MODELO = ("activo", "candidato", "rechazado", "retirado")

# Transiciones de alerta (app/alertas/motor.py: TRANSICIONES). Se usan solo para ofrecer opciones;
# la API valida de nuevo y responde 409 si no se permite.
TRANSICIONES_ALERTA = {
    "abierta": ("reconocida", "asignada", "cerrada", "descartada"),
    "reconocida": ("asignada", "cerrada", "descartada"),
    "asignada": ("reconocida", "cerrada", "descartada"),
    "cerrada": (),
    "descartada": (),
}

TEXTO_CODIGO = {
    401: "Introduce una credencial Bearer válida en el tablero.",
    403: "Tu credencial no tiene el rol requerido para esta operación.",
    404: "El recurso solicitado no existe.",
    409: "La operación entra en conflicto con el estado actual.",
    422: "Los datos enviados no cumplen las validaciones.",
    500: "La API encontró un error interno.",
}


CODIGOS_INTERMEDIARIO = (502, 503, 504)    # pasarela o proxy: la solicitud pudo llegar al backend


def url_api() -> str:
    """Base de la API: variable de entorno API_URL o el valor predeterminado."""
    return os.environ.get("API_URL", API_POR_DEFECTO).rstrip("/")


@dataclass
class ErrorApi(Exception):
    """Fallo de una solicitud a la API, ya clasificado para mostrarlo al usuario.

    tipo: 'timeout', 'conexion', 'comunicacion', 'http', 'json', 'estructura' o 'incompleta'.
    no_confirmada: True si la solicitud modificaba datos y no se sabe si el servidor la aplicó (sin
    respuesta, respuesta ilegible o respuesta 2xx sin los campos que confirman el resultado). Un error
    HTTP de la API (4xx/5xx con su cuerpo) es una operación FALLIDA: la API la rechazó o la revirtió.
    """
    tipo: str
    mensaje: str
    codigo_http: int | None = None
    mensaje_api: str | None = None
    correlation_id: str | None = None
    detalle: object = None
    no_confirmada: bool = False

    def __str__(self) -> str:
        return self.texto()

    def texto(self) -> str:
        partes = [self.mensaje]
        if self.mensaje_api:
            partes.append(f"Mensaje de la API: {self.mensaje_api}")
        if self.correlation_id:
            partes.append(f"Identificador de seguimiento: {self.correlation_id}")
        return " ".join(partes)


@dataclass
class Pagina:
    """Página de una consulta paginada, con la paginación que devolvió la API."""
    datos: list[dict]
    pagina: int
    tamano: int
    total: int
    paginas: int
    extra: dict = field(default_factory=dict)

    @property
    def vacia(self) -> bool:
        return self.total == 0


def _error_de_cuerpo(respuesta) -> tuple[str | None, str | None, object]:
    """(mensaje, correlation_id, detalle) del formato uniforme; None si el cuerpo no lo sigue."""
    try:
        cuerpo = respuesta.json()
    except ValueError:                       # incluye requests.exceptions.JSONDecodeError
        return None, None, None
    if not isinstance(cuerpo, Mapping) or not isinstance(cuerpo.get("error"), Mapping):
        return None, None, None
    error = cuerpo["error"]
    mensaje = error.get("mensaje") if isinstance(error.get("mensaje"), str) else None
    cid = error.get("correlation_id") if isinstance(error.get("correlation_id"), str) else None
    return mensaje, cid, error.get("detalle")


def _limpiar(params: Mapping[str, object] | None) -> dict:
    """Quita los None (la API usa su valor por defecto) y convierte booleanos a 'true'/'false'."""
    salida = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        if isinstance(v, bool):
            salida[k] = "true" if v else "false"
        elif isinstance(v, (list, tuple)):
            salida[k] = list(v)
        else:
            salida[k] = v
    return salida


def opciones_bearer(credencial: str) -> dict:
    if os.environ.get("SOBUSA_DEMO_READ_ONLY") == "1":
        raise ErrorApi("http", "La demostración solo permite consultas.", codigo_http=405)
    if not credencial:
        return {}
    destino = urlsplit(url_api())
    if destino.scheme != "https" and not (destino.scheme == "http" and
                                           destino.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ErrorApi("conexion", "Las credenciales requieren HTTPS o una API en loopback.")
    if any(c.isspace() for c in credencial) or not credencial.isascii():
        raise ErrorApi("http", TEXTO_CODIGO[401], codigo_http=401)
    return {"headers": {"Authorization": f"Bearer {credencial}"}, "allow_redirects": False}


def ocultar_credencial(texto, credencial: str):
    return texto.replace(credencial, "[oculto]") if isinstance(texto, str) and credencial else texto


def solicitar(metodo: str, ruta: str, *, params=None, json=None, timeout: float = TIEMPO_CONSULTA,
              modifica: bool = False, credencial: str = ""):
    """Hace la solicitud y devuelve el JSON decodificado. Lanza ``ErrorApi`` ante cualquier fallo.

    ``modifica=True`` (POST, PATCH): un timeout, una conexión perdida o un 2xx ilegible se informan
    como **no confirmados**: la API pudo haber aplicado el cambio.
    """
    solo_lectura = os.environ.get("SOBUSA_DEMO_READ_ONLY") == "1"
    if solo_lectura and metodo != "GET":
        raise ErrorApi("http", "La demostración solo permite consultas.", codigo_http=405)
    funcion = {"GET": requests.get, "POST": requests.post, "PATCH": requests.patch}[metodo]
    url = f"{url_api()}/{ruta.lstrip('/')}"
    argumentos = {"timeout": timeout}
    if credencial or metodo in ("POST", "PATCH"):
        argumentos.update(opciones_bearer(credencial))
    if params is not None:
        argumentos["params"] = _limpiar(params)
    if json is not None:
        argumentos["json"] = json
    try:
        if solo_lectura:
            if url_api() != "http://127.0.0.1:18010/api/v1":
                raise ErrorApi("conexion", "La demostración requiere la API local en el puerto 18010.")
            with requests.Session() as sesion:
                sesion.trust_env = False
                r = sesion.get(url, allow_redirects=False, **argumentos)
            if 300 <= r.status_code < 400:
                raise ErrorApi("http", "La demostración no sigue redirecciones.", codigo_http=r.status_code)
        else:
            r = funcion(url, **argumentos)
    except requests.Timeout as e:            # antes que ConnectionError: ConnectTimeout hereda de ambos
        raise ErrorApi("timeout", "La API no respondió a tiempo."
                       + (" No se pudo confirmar si el cambio se aplicó; consulte el estado antes de "
                          "reintentar." if modifica else ""), no_confirmada=modifica) from e
    except requests.ConnectionError as e:
        raise ErrorApi("conexion", "No se pudo conectar con la API."
                       + (" No se pudo confirmar si el cambio se aplicó; consulte el estado antes de "
                          "reintentar." if modifica else ""), no_confirmada=modifica) from e
    except requests.RequestException as e:
        raise ErrorApi("comunicacion", "Ocurrió un problema de comunicación con la API.",
                       no_confirmada=modifica) from e

    codigo = r.status_code
    if credencial and 300 <= codigo < 400:
        raise ErrorApi("http", "La API devolvió una redirección. No se volvió a enviar la credencial.",
                       codigo_http=codigo, no_confirmada=modifica)
    if codigo >= 400:
        mensaje_api, cid, detalle = _error_de_cuerpo(r)
        if credencial:
            mensaje_api = ocultar_credencial(mensaje_api, credencial)
            cid = ocultar_credencial(cid, credencial)
            detalle = None
        base = TEXTO_CODIGO.get(codigo, "La API rechazó la solicitud.")
        # La API responde sus errores con el formato uniforme y revierte la transacción. Un 502/503/504
        # o un cuerpo que no es el de la API viene de un intermediario: no se sabe si el cambio se aplicó.
        incierto = modifica and (codigo in CODIGOS_INTERMEDIARIO or cid is None)
        raise ErrorApi("http", f"La API respondió con el código {codigo}. {base}"
                       + (" No se pudo confirmar si el cambio se aplicó; consulte el estado antes de "
                          "reintentar." if incierto else ""),
                       codigo_http=codigo, mensaje_api=mensaje_api, correlation_id=cid, detalle=detalle,
                       no_confirmada=incierto)
    try:
        return r.json()
    except ValueError as e:
        raise ErrorApi("json", f"La API respondió con el código {codigo}, pero su respuesta no se pudo "
                       "leer." + (" El cambio pudo haberse aplicado; consulte el estado antes de "
                                  "reintentar." if modifica else ""),
                       codigo_http=codigo, no_confirmada=modifica) from e


def obtener(ruta: str, params=None, *, timeout: float = TIEMPO_CONSULTA, credencial: str = "") -> dict:
    """GET que exige un objeto JSON."""
    cuerpo = solicitar("GET", ruta, params=params, timeout=timeout, credencial=credencial)
    if not isinstance(cuerpo, Mapping):
        raise ErrorApi("estructura", "La respuesta de la API no tiene el formato esperado "
                       "(no es un objeto JSON).")
    return dict(cuerpo)


def obtener_pagina(ruta: str, params=None, *, timeout: float = TIEMPO_CONSULTA, credencial: str = "") -> Pagina:
    """GET paginado. Comprueba ``datos`` y ``paginacion``; nunca convierte un error en página vacía."""
    params = dict(params or {})
    tamano = params.get("tamano", 50)
    if isinstance(tamano, bool) or not isinstance(tamano, int) or not 1 <= tamano <= TAMANO_PAGINA_MAX:
        raise ValueError(f"'tamano' debe ser un entero entre 1 y {TAMANO_PAGINA_MAX}")
    cuerpo = obtener(ruta, params, timeout=timeout, credencial=credencial)
    datos, pag = cuerpo.get("datos"), cuerpo.get("paginacion")
    if not isinstance(datos, list) or not isinstance(pag, Mapping):
        raise ErrorApi("estructura", "La respuesta de la API no tiene 'datos' y 'paginacion'.")
    campos = {}
    for clave in ("pagina", "tamano", "total", "paginas"):
        valor = pag.get(clave)
        if clave == "paginas" and valor is None:       # opcional: se deduce de total y tamano
            continue
        if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
            raise ErrorApi("estructura", f"La paginación de la API no tiene un '{clave}' válido.")
        campos[clave] = valor
    if campos["tamano"] < 1:
        raise ErrorApi("estructura", "La paginación de la API no tiene un 'tamano' válido.")
    esperadas = -(-campos["total"] // campos["tamano"])
    if campos.setdefault("paginas", esperadas) != esperadas:
        raise ErrorApi("estructura", "La paginación de la API no es coherente (paginas ≠ total / tamano).")
    for clave in ("pagina", "tamano"):
        if clave in params and campos[clave] != params[clave]:
            raise ErrorApi("estructura", f"La API devolvió {clave}={campos[clave]} y se pidió {params[clave]}.")
    if any(not isinstance(f, Mapping) for f in datos):
        raise ErrorApi("estructura", "Una fila de la respuesta no es un objeto JSON.")
    if len(datos) > campos["tamano"]:
        raise ErrorApi("estructura", "La API devolvió más filas que el tamaño de página pedido.")
    extra = {k: v for k, v in cuerpo.items() if k not in ("datos", "paginacion")}
    return Pagina(datos=[dict(f) for f in datos], extra=extra, **campos)


def get_para_exportar(ruta: str, params) -> dict:
    """Adaptador para ``exportacion.traer_todo``: devuelve el JSON y lanza ante errores."""
    return obtener(ruta, params)


# ------------------------------------------------------------------ respuestas de operaciones que escriben

def _es_entero(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _es_id(v) -> bool:
    return isinstance(v, str) and es_uuid(v)


def es_uuid(texto) -> bool:
    """True si el texto es un UUID (formato de execution_id). Evita armar URL con texto arbitrario."""
    try:
        return str(uuid.UUID(str(texto).strip())) == str(texto).strip().lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _incompleta(operacion: str, faltan: list[str]) -> ErrorApi:
    return ErrorApi("incompleta", f"La API respondió a «{operacion}», pero la respuesta no trae "
                    f"{', '.join(faltan)}: no se puede confirmar qué se hizo. La operación pudo haberse "
                    "aplicado; consulte el estado antes de reintentar.", no_confirmada=True)


def validar_ejecucion_frecuencias(r) -> dict:
    """POST /frequency/executions (app/servicios.py: ejecutar_frecuencias). Exitosa: ids de la ejecución,
    de la solicitud de predicción y de la dueña de las predicciones, y los conteos. Fallida: el id de la
    ejecución de predicción y 0 predicciones. Cualquier otra forma queda NO confirmada."""
    if not isinstance(r, Mapping):
        raise _incompleta("calcular frecuencias", ["un objeto JSON"])
    if r.get("estado") == "exitosa":
        faltan = [c for c in ("execution_id", "execution_id_prediccion", "execution_id_predicciones")
                  if not _es_id(r.get(c))]
        faltan += [c for c in ("predicciones", "recomendaciones", "no_viables", "alertas_nuevas")
                   if not _es_entero(r.get(c))]
    elif r.get("estado") == "fallida":
        faltan = [c for c in ("execution_id",) if not _es_id(r.get(c))]
    else:
        faltan = ["un estado reconocido (exitosa o fallida)"]
    if faltan:
        raise _incompleta("calcular frecuencias", faltan)
    return dict(r)


def validar_calculo_recomendaciones(r) -> dict:
    """POST /recommendations/calculate (app/api/main.py): execution_id y los dos conteos."""
    if not isinstance(r, Mapping):
        raise _incompleta("calcular recomendaciones", ["un objeto JSON"])
    faltan = (["execution_id"] if not _es_id(r.get("execution_id")) else []) + [
        c for c in ("recomendaciones", "no_viables") if not _es_entero(r.get(c))]
    if faltan:
        raise _incompleta("calcular recomendaciones", faltan)
    return dict(r)


def validar_activacion(r, model_id: int) -> dict:
    """POST /models/{id}/activate (repositorio.activar_modelo): model_id igual al pedido y
    modelo_anterior (entero o null)."""
    if not isinstance(r, Mapping):
        raise _incompleta("activar el modelo", ["un objeto JSON"])
    faltan = []
    if r.get("model_id") != model_id or not _es_entero(r.get("model_id")):
        faltan.append(f"model_id={model_id}")
    if "modelo_anterior" not in r or not (r["modelo_anterior"] is None or _es_entero(r["modelo_anterior"])):
        faltan.append("modelo_anterior")
    if faltan:
        raise _incompleta("activar el modelo", faltan)
    return dict(r)


def validar_cambio_alerta(r, alert_id: int, estado: str) -> dict:
    """PATCH /alerts/{id}/status (servicios.cambiar_estado_alerta): la alerta con el estado pedido."""
    if not isinstance(r, Mapping):
        raise _incompleta("actualizar la alerta", ["un objeto JSON"])
    faltan = []
    if r.get("alert_id") != alert_id:
        faltan.append(f"alert_id={alert_id}")
    if r.get("estado") != estado:
        faltan.append(f"estado={estado}")
    if faltan:
        raise _incompleta("actualizar la alerta", faltan)
    return dict(r)


# ------------------------------------------------------------------ identificadores de ejecución (P1)

def _texto(valor) -> str | None:
    return valor if isinstance(valor, str) and valor else None


def ids_de_respuesta_frecuencias(r: Mapping) -> dict:
    """Identificadores de la respuesta de POST /frequency/executions (DECISIONES.md §15).

    - ``execution_id``: la ejecución de frecuencias (dueña de recomendaciones y alertas). Si la
      predicción falló, la API devuelve la ejecución de predicción con estado 'fallida'.
    - ``execution_id_predicciones``: dueña de las filas de ``prediction`` (puede ser otra ejecución
      que ya tenía exactamente el mismo contenido).
    """
    exitosa = r.get("estado") == "exitosa"
    return {
        "estado": _texto(r.get("estado")),
        "execution_id": _texto(r.get("execution_id")),
        "execution_id_prediccion": _texto(r.get("execution_id_prediccion")),
        "execution_id_predicciones": _texto(r.get("execution_id_predicciones")) if exitosa else None,
        "recomendaciones_de": _texto(r.get("execution_id")) if exitosa and "recomendaciones" in r else None,
    }


def ids_de_detalle(detalle: Mapping) -> dict:
    """Identificadores a partir de GET /frequency/executions/{id} (execution_log).

    Devuelve qué id filtra predicciones (dueña) y cuál filtra recomendaciones; None si la ejecución
    no tiene ese tipo de filas o la dueña no se puede saber sin otra consulta.
    """
    eid = _texto(str(detalle.get("execution_id"))) if detalle.get("execution_id") else None
    tipo = detalle.get("tipo")
    resumen = detalle.get("resumen") if isinstance(detalle.get("resumen"), Mapping) else {}
    parametros = detalle.get("parametros") if isinstance(detalle.get("parametros"), Mapping) else {}
    exitosa = detalle.get("estado") == "exitosa"
    salida = {"execution_id": eid, "tipo": tipo, "estado": detalle.get("estado"),
              "predicciones_de": None, "recomendaciones_de": None, "prediccion_origen": None}
    if not exitosa:
        return salida
    if tipo == "prediccion":
        salida["predicciones_de"] = (_texto(resumen.get("execution_id_predicciones"))
                                     or _texto(resumen.get("reutiliza")) or eid)
    elif tipo == "frecuencias":
        salida["predicciones_de"] = _texto(resumen.get("execution_id_predicciones"))
        salida["recomendaciones_de"] = eid
        salida["prediccion_origen"] = _texto(resumen.get("execution_id_prediccion"))
    elif tipo == "recomendacion":
        salida["recomendaciones_de"] = eid
        salida["prediccion_origen"] = _texto(parametros.get("execution_id_prediccion"))
    return salida


def exportar_csv(credencial: str) -> bytes:
    """Descarga por el servidor, sin pasar la llave al navegador."""
    try:
        with requests.get(f"{url_api()}/alerts/export", **opciones_bearer(credencial),
                          timeout=20, stream=True) as response:
            if response.status_code != 200 or not response.headers.get("Content-Type", "").startswith("text/csv"):
                raise ErrorApi("http", "No se pudo obtener el CSV completo.", codigo_http=response.status_code)
            output = bytearray()
            for chunk in response.iter_content(65536):
                output.extend(chunk)
                if len(output) > 8 * 1024 * 1024:
                    raise ErrorApi("estructura", "El CSV excede el límite de descarga.")
            return bytes(output)
    except requests.RequestException:
        raise ErrorApi("comunicacion", "No fue posible descargar el CSV.") from None
