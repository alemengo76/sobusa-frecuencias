"""Tablero de SOBUSA · Frecuencias de despacho (Streamlit), conectado a la API /api/v1.

Uso:  streamlit run dashboard/tablero.py   (con la API en API_URL; por defecto
http://localhost:8000/api/v1)

Arquitectura: Usuario → Streamlit → API FastAPI → servicios → PostgreSQL. El tablero no se conecta a
la base de datos, no ejecuta SQL y no recalcula predicciones, recomendaciones, métricas ni SHAP: solo
consume la API (dashboard/cliente.py) y presenta sus resultados con la identidad visual de la maqueta
v2 (dashboard/estilo.py, dashboard/graficos.py). Las exportaciones usan dashboard/exportacion.py.

Secciones (barra lateral): Resumen · Demanda y frecuencias · Alertas · Calidad de datos ·
Evaluación (M1) · Modelos y explicaciones.
"""
import hashlib
import os
import re
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

try:                                   # pytest y python -m importan el paquete dashboard
    from dashboard import cliente as cl
    from dashboard import estilo as es
    from dashboard import explicacion as xp
    from dashboard import exportacion as ex
    from dashboard import graficos as gr
except ModuleNotFoundError:            # streamlit run agrega dashboard/ a sys.path, no la raíz
    import cliente as cl
    import estilo as es
    import explicacion as xp
    import exportacion as ex
    import graficos as gr

API = os.environ.get("API_URL", cl.API_POR_DEFECTO)
SOLO_LECTURA = os.environ.get("SOBUSA_DEMO_READ_ONLY") == "1"
NOMBRES = {2: "2 · SILENCIO", 16: "16 · CRA 54", 21: "21 · CALLE 72"}
BOGOTA = timezone(timedelta(hours=-5))   # America/Bogota no tiene horario de verano
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

# Estado de la exportación de alertas en st.session_state (nombres conservados de la versión anterior)
CLAVE_EXPORTACION = "exportacion_alertas"
CLAVE_AVISO_EXPORTACION = "exportacion_alertas_aviso"
CLAVE_EJECUCIONES = "ejecuciones_sesion"
CLAVE_EXPLICAR = "prediction_id_a_explicar"
CLAVE_FILAS_PREDICCION = "predicciones_filas"   # prediction_id -> ejecución y modelo de la fila vista
CAMPOS_FILA_PREDICCION = ("prediction_id", "execution_id", "model_id", "model_version")
LIMITE_GRAFICA = 2_000                   # filas máximas leídas para graficar un día

SECCIONES = ("Resumen", "Demanda y frecuencias", "Alertas", "Calidad de datos", "Evaluación (M1)",
             "Modelos y explicaciones")
TITULOS = {"Resumen": "Resumen", "Demanda y frecuencias": "Demanda y frecuencias", "Alertas": "Alertas",
           "Calidad de datos": "Calidad de datos (G3)", "Evaluación (M1)": "Predicción frente a realidad (M1)",
           "Modelos y explicaciones": "Modelos y explicaciones"}
BAJADAS = {
    "Resumen": "Modelo en servicio y alertas abiertas de las tres rutas.",
    "Demanda y frecuencias": "Pasajeros previstos por franja y buses recomendados para atenderlos.",
    "Alertas": "Situaciones que requieren atención: revisar, asignar y cerrar.",
    "Calidad de datos": "Registros rechazados o en cuarentena por lote, con su causa.",
    "Evaluación (M1)": "Cuánto se equivocó la predicción cuando llegaron los datos reales.",
    "Modelos y explicaciones": "Versiones del modelo, su activación y por qué predicen lo que predicen.",
}

st.set_page_config(page_title="SOBUSA · Frecuencias", layout="wide")
st.markdown(es.CSS, unsafe_allow_html=True)


# =====================================================================================================
# utilidades de sesión, caché y formato
# =====================================================================================================

def _html(fragmento: str) -> None:
    st.markdown(fragmento, unsafe_allow_html=True)


def _firma(*partes) -> str:
    """Firma estable de unos filtros: cambia la clave de los widgets de página (vuelve a la página 1)."""
    return hashlib.sha256(repr(partes).encode("utf-8")).hexdigest()[:12]


def _generacion() -> int:
    return st.session_state.get("_generacion", 0)


def _cache(clave: tuple, funcion):
    """Resultado de una consulta guardado mientras no cambie la generación de datos. Los errores no
    se guardan. «Actualizar datos» y cualquier operación que escribe aumentan la generación."""
    cache = st.session_state.setdefault("_cache", {})
    k = (_generacion(),) + tuple(clave)
    if k in cache:
        return cache[k]
    valor = funcion()
    for viejo in [x for x in cache if x[0] != _generacion()]:
        cache.pop(viejo, None)
    while len(cache) >= 120:                    # las más antiguas primero (orden de inserción)
        cache.pop(next(iter(cache)))
    cache[k] = valor
    return valor


def _invalidar_exportacion(motivo: str, conjuntos=None) -> None:
    """Descarta las exportaciones preparadas (todas o las indicadas) y deja el motivo para mostrarlo."""
    nombres = conjuntos or [c.nombre for c in ex.CONJUNTOS.values()]
    for nombre in nombres:
        if st.session_state.pop(f"exportacion_{nombre}", None) is not None:
            st.session_state[f"exportacion_{nombre}_aviso"] = motivo


def _invalidar_datos(motivo: str) -> None:
    """Tras una operación que escribe: nada de lo consultado antes se reutiliza."""
    st.session_state["_generacion"] = _generacion() + 1
    st.session_state.pop("_cache", None)
    _invalidar_exportacion(motivo)


def _fecha(valor, con_hora: bool = True) -> str:
    """ISO 8601 de la API -> «01-oct-2025 07:00» en hora de Bogotá. None -> «Sin dato»."""
    if valor is None or valor == "":
        return es.SIN_DATO
    t = pd.to_datetime(valor, utc=True, errors="coerce")
    if pd.isna(t):
        return str(valor)
    t = t.tz_convert(BOGOTA)
    texto = f"{t.day:02d}-{MESES[t.month - 1]}-{t.year}"
    return texto + (f" {t:%H:%M}" if con_hora else "")


def _hora(valor) -> str:
    t = pd.to_datetime(valor, utc=True, errors="coerce")
    return es.SIN_DATO if pd.isna(t) else f"{t.tz_convert(BOGOTA):%H:%M}"


def _inicio_dia(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=BOGOTA)


def _rango(desde_dia, hasta_dia) -> tuple:
    """Días inclusivos -> (desde, hasta) con zona horaria; hasta es exclusivo (00:00 del día siguiente)."""
    desde = _inicio_dia(desde_dia) if desde_dia else None
    hasta = _inicio_dia(hasta_dia + timedelta(days=1)) if hasta_dia else None
    return desde, hasta


def _iso(dt) -> str | None:
    return None if dt is None else dt.isoformat()


def _tabla(filas: list[dict], columnas: list[tuple]) -> pd.DataFrame:
    """Tabla para st.dataframe con encabezados en español. columnas: (campo, título, tipo) con tipo
    'fecha', 'hora', 'num' o 'texto'. Los faltantes quedan vacíos, nunca en 0."""
    datos = {}
    for campo, titulo, tipo in columnas:
        valores = [f.get(campo) for f in filas]
        if tipo == "fecha":
            valores = [_fecha(v) if v not in (None, "") else None for v in valores]
        elif tipo == "hora":
            valores = [_hora(v) if v not in (None, "") else None for v in valores]
        elif tipo == "num":
            valores = [v if isinstance(v, int) and not isinstance(v, bool) else gr._f(v) for v in valores]
        elif tipo == "texto":
            valores = [None if es.es_faltante(v) else str(v) for v in valores]
        elif tipo == "bool":
            valores = [None if v is None else ("Sí" if v else "No") for v in valores]
        datos[titulo] = pd.Series(valores, dtype=object)
    return pd.DataFrame(datos)


def _mostrar_tabla(df: pd.DataFrame) -> None:
    st.dataframe(df, width="stretch", hide_index=True)


def _error(e: Exception, contexto: str, escritura: bool = False) -> None:
    """Muestra un error de la API sin traza ni URL; el texto de la API se escapa (Markdown).

    En una escritura distingue «La operación falló» (la API respondió con un error: no se aplicó o se
    revirtió) de «Resultado no confirmado» (sin respuesta, ilegible o incompleta: pudo aplicarse)."""
    if isinstance(e, cl.ErrorApi):
        if e.no_confirmada:
            st.warning(f"Resultado no confirmado. {contexto} {es.md(e.texto())}")
        else:
            prefijo = "La operación falló. " if escritura else ""
            st.error(f"{prefijo}{contexto} {es.md(e.texto())}")
    elif isinstance(e, ValueError):
        st.error(f"{contexto} {es.md(e)}")
    else:
        st.error(f"{contexto} Error inesperado ({type(e).__name__}).")


def _mensaje_error(e: Exception) -> str:
    """Texto para el usuario, sin URL ni traza (errores de exportación)."""
    causa = e.__cause__
    if isinstance(causa, cl.ErrorApi):
        return f"{es.md(e)} — {es.md(causa.texto())}"
    if isinstance(causa, requests.Timeout):
        return f"{es.md(e)} — la API no respondió a tiempo."
    if isinstance(causa, requests.ConnectionError):
        return f"{es.md(e)} — no se pudo conectar con la API."
    if isinstance(causa, requests.HTTPError):
        codigo = getattr(causa.response, "status_code", "desconocido")
        return f"{es.md(e)} — la API respondió con el código {codigo}."
    if isinstance(e, (ex.ErrorExportacion, ValueError)):
        return es.md(e)
    return f"Error inesperado ({type(e).__name__})."


# ------------------------------------------------------------------ paginación

def _paginador(clave: str, etiqueta: str, firma: str) -> tuple[int, int]:
    """Tamaño y número de página. La clave del número incluye la firma de los filtros: si cambian,
    el widget es otro y vuelve a la página 1."""
    c1, c2, _ = st.columns([1, 1, 3])
    with c1:
        tamano = st.selectbox(f"Filas por página ({etiqueta})", (25, 50, 100, 200, 500), index=1,
                              key=f"tam_{clave}")
    with c2:
        pagina = st.number_input(f"Página ({etiqueta})", min_value=1, value=1, step=1,
                                 key=f"pag_{clave}_{firma}_{tamano}")
    return int(pagina), int(tamano)


def _pie_pagina(p: cl.Pagina, etiqueta: str) -> None:
    if p.total == 0:
        st.info(f"No hay {etiqueta} con estos filtros (la API informó un total de 0).")
        return
    if p.pagina > p.paginas:
        st.warning(f"La página {p.pagina} no existe: hay {p.paginas} páginas de {etiqueta} "
                   f"({p.total} en total). Vuelva a una página entre 1 y {p.paginas}.")
        return
    a = (p.pagina - 1) * p.tamano + 1
    b = a + len(p.datos) - 1
    st.caption(f"Filas {a}–{b} de {p.total} {etiqueta} · página {p.pagina} de {p.paginas} "
               "(paginacion.total de la API). Use «Página» para avanzar o retroceder.")


# ------------------------------------------------------------------ exportación genérica

def _nombre_archivo(texto: str) -> str:
    """Nombre de archivo sin rutas ni caracteres especiales (el lote o un filtro lo escribe el usuario)."""
    limpio = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(texto)).strip("._") or "exportacion"
    return limpio[:80]


def _exportar(conjunto: ex.Conjunto, filtros: dict, descripcion: str, nombre_archivo: str, *,
              get=None, boton: str = "Preparar exportación") -> None:
    """Prepara (solo al pulsar) y ofrece CSV y Excel con TODAS las filas de los filtros activos."""
    clave_estado = f"exportacion_{conjunto.nombre}"
    clave_aviso = f"{clave_estado}_aviso"
    clave = tuple(sorted((k, _iso(v) if isinstance(v, datetime) else v) for k, v in filtros.items()))
    guardada = st.session_state.get(clave_estado)
    if guardada is not None and guardada["clave"] != clave:
        _invalidar_exportacion("Los filtros cambiaron: se descartó la exportación preparada.", [conjunto.nombre])
        guardada = None

    st.caption(descripcion)
    if st.button(boton, key=f"preparar_exportacion_{conjunto.nombre}"):
        st.session_state.pop(clave_estado, None)          # nunca queda un archivo anterior
        st.session_state.pop(clave_aviso, None)
        guardada = None
        try:
            with st.spinner("Preparando la exportación: leyendo todas las páginas…"):
                res = ex.traer_todo(get or cl.get_para_exportar, conjunto, filtros)
                csv = ex.a_csv(res.datos, columna_id=conjunto.columna_id)
                xlsx = ex.a_xlsx(res.datos, ex.metadatos(res), columna_id=conjunto.columna_id)
        except Exception as e:  # noqa: BLE001 - cualquier fallo: ningún archivo y mensaje claro
            st.error(f"No se generó ningún archivo. {_mensaje_error(e)}")
        else:
            guardada = {"clave": clave, "total": res.total, "advertencias": res.advertencias,
                        "filtros": dict(res.filtros), "csv": csv, "xlsx": xlsx,
                        "preparada_en": datetime.now(BOGOTA)}
            st.session_state[clave_estado] = guardada

    aviso = st.session_state.get(clave_aviso)
    if guardada is None:
        if aviso:
            st.info(aviso)
        return
    filtros_txt = es.md("; ".join(f"{k}={v}" for k, v in sorted(guardada.get("filtros", {}).items())) or "ninguno")
    nombre_archivo = _nombre_archivo(nombre_archivo)
    st.success(f"Exportación lista: {guardada['total']} filas "
               f"(preparada a las {guardada['preparada_en']:%H:%M:%S}). Filtros: {filtros_txt}.")
    for advertencia in guardada["advertencias"]:
        st.warning(es.md(advertencia))
    st.caption(ex.AVISO_INSTANTANEA)
    st.download_button("Descargar CSV", guardada["csv"], file_name=f"{nombre_archivo}.csv",
                       mime="text/csv", key=f"descargar_{conjunto.nombre}_csv")
    st.download_button("Descargar Excel", guardada["xlsx"], file_name=f"{nombre_archivo}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       key=f"descargar_{conjunto.nombre}_xlsx")


def _exportar_alertas(filtros: dict) -> None:
    descripcion = ("Incluye todas las alertas que cumplen los filtros activos, no solo la página que "
                   "muestra la tabla. No incluye el usuario asignado ni los comentarios.")
    nombre = "alertas_" + "_".join(f"{k}{v}" for k, v in sorted(filtros.items())
                                   if v is not None and not isinstance(v, datetime))
    _exportar(ex.ALERTAS, filtros, descripcion, nombre)


# ------------------------------------------------------------------ operaciones que escriben

OPERACIONES = {"frecuencias": "calcular frecuencias", "recomendaciones": "calcular recomendaciones",
               "activacion": "activar un modelo"}


def _bloqueada(nombre: str) -> bool:
    """True si hay un envío anterior de esta operación cuyo resultado no se confirmó."""
    if not st.session_state.get(f"_envio_{nombre}"):
        return False
    operacion = OPERACIONES.get(nombre, "actualizar esta alerta" if nombre.startswith("alerta") else nombre)
    st.warning(f"Hay un envío anterior de «{operacion}» cuyo resultado no se pudo confirmar: pudo haberse "
               "aplicado. Consulte el estado antes de reintentar; así se evita aplicarlo dos veces.")
    if st.button("Ya revisé el estado: permitir un nuevo envío", key=f"liberar_{nombre}"):
        st.session_state.pop(f"_envio_{nombre}", None)
        return False
    return True


def _enviar(nombre: str, funcion):
    """Ejecuta una escritura una sola vez. Devuelve (respuesta, error). Si el resultado no se confirma
    (timeout, conexión perdida, 2xx ilegible), la operación queda bloqueada hasta que el usuario lo
    revise."""
    st.session_state[f"_envio_{nombre}"] = True
    try:
        r = funcion()
    except cl.ErrorApi as e:
        if not e.no_confirmada:
            st.session_state.pop(f"_envio_{nombre}", None)
        return None, e
    st.session_state.pop(f"_envio_{nombre}", None)
    return r, None


def _avisar_y_recargar(tipo: str, texto: str, datos=None) -> None:
    """Guarda el resultado de una operación y vuelve a ejecutar el guion: así el listado y las consultas
    se recargan con los datos nuevos y el mensaje se muestra arriba en la siguiente ejecución."""
    st.session_state.setdefault("_avisos", []).append((tipo, texto, datos))
    st.rerun()


def _registrar_ejecucion(registro: dict) -> None:
    lista = st.session_state.setdefault(CLAVE_EJECUCIONES, [])
    lista.insert(0, registro)
    del lista[20:]


# =====================================================================================================
# barra lateral y cabecera
# =====================================================================================================

if "_ir_a" in st.session_state:          # navegación pedida por otra sección (antes de crear el radio)
    st.session_state["seccion"] = st.session_state.pop("_ir_a")

with st.sidebar:
    _html(es.logo_lateral())
    st.session_state.setdefault("seccion", SECCIONES[1])
    if SOLO_LECTURA:
        st.caption("Demostración de solo lectura")
        credencial_escritura = ""
    else:
        credencial_escritura = st.text_input(
            "Credencial Bearer", type="password", key="credencial_escritura",
            help="Operador o administrador para despachar. Administrador para recalcular, gestionar alertas o activar modelos.",
        ).strip()
    seccion = st.radio("Sección", SECCIONES, key="seccion", label_visibility="collapsed")
    if st.button("Actualizar datos", key="actualizar_datos",
                 help="Vuelve a consultar la API (descarta lo guardado en esta sesión)."):
        st.session_state["_generacion"] = _generacion() + 1
        st.session_state.pop("_cache", None)
        st.session_state.pop("_consultado", None)
    ver_fuentes = st.checkbox("Mostrar origen técnico", value=False, key="ver_fuentes",
                              help="Muestra en cada tarjeta el endpoint de la API del que salen los datos.")
    _consultado = st.session_state.setdefault("_consultado", datetime.now(BOGOTA))
    _html(es.pie_lateral(f"{_consultado:%H:%M}"))
if not ver_fuentes:
    _html(es.OCULTAR_FUENTES)


def _modelos() -> list[dict]:
    cuerpo = _cache(("models",), lambda: cl.obtener("models"))
    datos = cuerpo.get("datos")
    if not isinstance(datos, list) or any(not isinstance(m, dict) for m in datos):
        raise cl.ErrorApi("estructura", "La respuesta de GET /models no tiene 'datos'.")
    return datos


try:
    MODELOS = _modelos()
    ERROR_MODELOS = None
except (cl.ErrorApi, ValueError) as e:
    MODELOS, ERROR_MODELOS = None, e

ACTIVO = next((m for m in (MODELOS or []) if m.get("estado") == "activo"), None)
pildoras = [es.pildora(None, "API disponible" if ERROR_MODELOS is None else "API no disponible",
                       punto="" if ERROR_MODELOS is None else "mal")]
pildoras.append(es.pildora("Modelo activo", ACTIVO.get("version") if ACTIVO else
                           ("Ninguno" if MODELOS is not None else es.SIN_DATO), mono=True))
_html(es.cabecera(TITULOS[seccion], pildoras, antetitulo=es.fecha_larga(datetime.now(BOGOTA)),
                  bajada=BAJADAS.get(seccion)))
if ERROR_MODELOS is not None:
    st.caption(f"No se pudo consultar el registro de modelos: {es.md(ERROR_MODELOS)}")
for _tipo, _texto, _datos in st.session_state.pop("_avisos", []):   # resultado de la operación anterior
    {"ok": st.success, "aviso": st.warning}.get(_tipo, st.info)(_texto)
    if _datos is not None:
        with st.expander("Respuesta de la API"):
            st.json(_datos)


# =====================================================================================================
# A. DEMANDA Y FRECUENCIAS
# =====================================================================================================

def _formulario_ejecucion(dia: date, idruta: int) -> None:
    """POST /frequency/executions: crea una ejecución nueva (predicción + recomendación + alertas)."""
    with st.expander("Calcular frecuencias del día (crea una ejecución nueva)", expanded=False):
        _html(es.marca_escritura("Acción que escribe · POST /frequency/executions"))
        if _bloqueada("frecuencias"):
            return
        with st.form("form_frecuencias", clear_on_submit=True):
            dia_e = st.date_input("Día a calcular", dia, key="dia_ejecucion")
            rutas = st.multiselect("Rutas", list(NOMBRES), default=[idruta], format_func=NOMBRES.get,
                                   key="rutas_ejecucion")
            horizontes = st.multiselect("Horizontes (min)", list(cl.HORIZONTES), default=list(cl.HORIZONTES),
                                        key="horizontes_ejecucion")
            base = st.radio("Base de demanda", cl.BASES_DEMANDA, index=0, horizontal=True,
                            key="base_ejecucion",
                            help="puntual: predicción; superior: límite superior del intervalo.")
            confirma = st.checkbox("Confirmo que quiero crear una ejecución nueva", key="confirma_ejecucion")
            enviar = st.form_submit_button("Calcular frecuencias", type="primary")
        if not enviar:
            return
        if not confirma:
            st.warning("Marque la confirmación para crear la ejecución. No se envió nada.")
            return
        if not rutas or not horizontes:
            st.warning("Elija al menos una ruta y un horizonte. No se envió nada.")
            return
        cuerpo = {"dia": str(dia_e), "idrutas": sorted(int(r) for r in rutas),
                  "horizontes": sorted(int(h) for h in horizontes), "base_demanda": base}
        with st.spinner("Calculando: predicción, recomendación y alertas…"):
            r, err = _enviar("frecuencias", lambda: cl.validar_ejecucion_frecuencias(cl.solicitar(
                "POST", "frequency/executions", json=cuerpo, timeout=cl.TIEMPO_EJECUCION,
                modifica=True, credencial=credencial_escritura)))
        # Con o sin respuesta, lo consultado antes puede haber cambiado
        _invalidar_datos("Se intentó calcular frecuencias: se descartó la exportación preparada.")
        if err is not None:
            _error(err, "Calcular frecuencias.", escritura=True)
            return
        ids = cl.ids_de_respuesta_frecuencias(r)
        _registrar_ejecucion({"tipo": "frecuencias", "dia": cuerpo["dia"], "idrutas": cuerpo["idrutas"],
                              "horizontes": cuerpo["horizontes"], "base_demanda": base,
                              "registrada_en": datetime.now(BOGOTA).isoformat(), **ids, "respuesta": r})
        if r["estado"] == "exitosa":
            texto = (f"Ejecución exitosa: {r['predicciones']} predicciones, {r['recomendaciones']} "
                     f"recomendaciones, {r['no_viables']} no viables, {r['alertas_nuevas']} alertas nuevas.")
            if ids["execution_id_predicciones"] != ids["execution_id_prediccion"]:
                texto += (" Reutilizó predicciones idénticas de la ejecución "
                          f"{ids['execution_id_predicciones']}; el tablero las consulta con ese id (P1).")
            _avisar_y_recargar("ok", texto, r)
        else:
            _avisar_y_recargar("aviso", "La ejecución terminó con estado «fallida» (la API no encontró datos "
                               "de demanda para el día): no generó recomendaciones ni alertas.", r)


def _formulario_recomendaciones() -> None:
    """POST /recommendations/calculate sobre una ejecución de predicción identificada."""
    with st.expander("Recalcular recomendaciones sobre una ejecución de predicción", expanded=False):
        _html(es.marca_escritura("Acción que escribe · POST /recommendations/calculate"))
        if _bloqueada("recomendaciones"):
            return
        conocidas = []
        for x in st.session_state.get(CLAVE_EJECUCIONES, []):
            pid = x.get("execution_id_prediccion") or (x.get("execution_id") if x.get("tipo") == "prediccion" else None)
            if pid and pid not in conocidas:
                conocidas.append(pid)
        with st.form("form_recomendaciones", clear_on_submit=True):
            elegida = st.selectbox("Ejecución de predicción de esta sesión", [None] + conocidas,
                                   format_func=lambda x: "— escribir el id —" if x is None else x,
                                   key="pred_origen_sesion")
            escrita = st.text_input("…o id de la ejecución de predicción", key="pred_origen_texto",
                                    help="execution_id de POST /predictions/run o execution_id_prediccion "
                                         "de una ejecución de frecuencias.")
            base = st.radio("Base de demanda", cl.BASES_DEMANDA, index=0, horizontal=True, key="base_recomendacion")
            confirma = st.checkbox("Confirmo que quiero calcular recomendaciones nuevas", key="confirma_recomendacion")
            enviar = st.form_submit_button("Calcular recomendaciones", type="primary")
        if not enviar:
            return
        origen = (escrita or "").strip() or elegida
        if not origen:
            st.warning("Identifique la ejecución de predicción de origen. No se envió nada.")
            return
        if not cl.es_uuid(origen):
            st.warning("El id de la ejecución de predicción no tiene formato UUID. No se envió nada.")
            return
        if not confirma:
            st.warning("Marque la confirmación para calcular. No se envió nada.")
            return
        cuerpo = {"execution_id_prediccion": origen, "base_demanda": base}
        with st.spinner("Calculando recomendaciones…"):
            r, err = _enviar("recomendaciones", lambda: cl.validar_calculo_recomendaciones(cl.solicitar(
                "POST", "recommendations/calculate", json=cuerpo, timeout=cl.TIEMPO_EJECUCION,
                modifica=True, credencial=credencial_escritura)))
        _invalidar_datos("Se intentó calcular recomendaciones: se descartó la exportación preparada.")
        if err is not None:
            _error(err, "Calcular recomendaciones.", escritura=True)
            return
        _registrar_ejecucion({"tipo": "recomendacion", "execution_id": r["execution_id"],
                              "recomendaciones_de": r["execution_id"], "execution_id_prediccion": origen,
                              "execution_id_predicciones": None, "base_demanda": base,
                              "registrada_en": datetime.now(BOGOTA).isoformat(), "respuesta": r})
        _avisar_y_recargar("ok", f"Recomendaciones calculadas: {r['recomendaciones']} ({r['no_viables']} no "
                           f"viables). Ejecución {r['execution_id']}.", r)


def _leer_dia(conjunto: ex.Conjunto, filtros: dict) -> list[dict]:
    """Todas las filas de un día (para gráficas), con la lectura paginada verificada de exportacion."""
    clave = (conjunto.nombre,) + tuple(sorted((k, _iso(v) if isinstance(v, datetime) else v)
                                              for k, v in filtros.items()))
    res = _cache(clave, lambda: ex.traer_todo(cl.get_para_exportar, conjunto, filtros, limite_filas=LIMITE_GRAFICA))
    return res.datos.to_dict("records")


def _validar_filas_despacho(base, eleccion, preds, recs) -> None:
    def franja(fila):
        return pd.to_datetime(fila["franja_inicio"], utc=True)

    for filas, clave in ((preds, "pred"), (recs, "rec")):
        if any(fila["execution_id"] != eleccion[clave] or fila["idruta"] != base["idruta"]
               or fila["horizonte_min"] != base["horizonte_min"]
               or not base["desde"] <= franja(fila) < base["hasta"] for fila in filas):
            raise ValueError("La API devolvió filas ajenas al despacho o a sus filtros.")
    por_id = {fila["prediction_id"]: fila for fila in preds}
    if (len(por_id) != len(preds)
        or len({(fila["model_id"], fila["model_version"]) for fila in preds}) > 1
        or (eleccion["pred"] and eleccion["rec"] and any(
            fila["prediction_id"] not in por_id or franja(fila) != franja(por_id[fila["prediction_id"]])
            for fila in recs
        ))):
        raise ValueError("Las predicciones y recomendaciones del despacho no son coherentes.")


def _resolver_manual(eid: str) -> dict:
    """Ids de predicciones y recomendaciones de una ejecución escrita a mano (GET /frequency/executions)."""
    detalle = _cache(("ejecucion", eid), lambda: cl.obtener(f"frequency/executions/{eid}"))
    ids = cl.ids_de_detalle(detalle)
    if ids["recomendaciones_de"] and not ids["predicciones_de"] and ids["prediccion_origen"]:
        origen = _cache(("ejecucion", ids["prediccion_origen"]),
                        lambda: cl.obtener(f"frequency/executions/{ids['prediccion_origen']}"))
        ids["predicciones_de"] = cl.ids_de_detalle(origen)["predicciones_de"]
    return {**ids, "detalle": detalle}


def _opciones_ejecucion(dia_txt: str, idruta: int, preds: list[dict], recs: list[dict]) -> list[dict]:
    """Ejecuciones con filas en el día: las de esta sesión y las que aparecen en los datos de la API.
    Para las encontradas en los datos, la dueña de las predicciones se deduce por prediction_id."""
    opciones, vistos = [], set()
    pred_por_id = {p.get("prediction_id"): p.get("execution_id") for p in preds}
    con_filas = {p.get("execution_id") for p in preds} | {r.get("execution_id") for r in recs}
    for x in st.session_state.get(CLAVE_EJECUCIONES, []):
        if x.get("dia") not in (None, dia_txt) or (x.get("idrutas") and idruta not in x["idrutas"]):
            continue
        rec = x.get("recomendaciones_de") or (x.get("execution_id") if x.get("tipo") == "frecuencias" else None)
        if x.get("estado") not in (None, "exitosa"):
            continue
        clave = (rec, x.get("execution_id_predicciones"))
        if clave in vistos or not ({rec, x.get("execution_id_predicciones")} & con_filas):
            continue                       # sin filas en este día, ruta y horizonte
        vistos.add(clave)
        opciones.append({"etiqueta": f"Esta sesión · {es.corto(rec or x.get('execution_id'))} · "
                                     f"{x.get('base_demanda', '')}",
                         "rec": rec, "pred": x.get("execution_id_predicciones"), "origen": "sesion"})
    por_ejecucion = {}
    for r in recs:
        por_ejecucion.setdefault(r.get("execution_id"), []).append(r)
    for eid, filas in sorted(por_ejecucion.items(), key=lambda kv: max(str(f.get("generated_at")) for f in kv[1]),
                             reverse=True):
        duenas = {pred_por_id.get(f.get("prediction_id")) for f in filas} - {None}
        pred = next(iter(duenas)) if len(duenas) == 1 else None
        if (eid, pred) in vistos:
            continue
        vistos.add((eid, pred))
        generada = _fecha(max(str(f.get("generated_at")) for f in filas))
        opciones.append({"etiqueta": f"Recomendaciones {es.corto(eid)} · {generada}",
                         "rec": eid, "pred": pred, "origen": "datos"})
    con_rec = {o["pred"] for o in opciones}
    for eid in sorted({p.get("execution_id") for p in preds} - con_rec - {None}):
        opciones.append({"etiqueta": f"Solo predicciones {es.corto(eid)}", "rec": None, "pred": eid,
                         "origen": "datos"})
    return opciones


def _contexto_demanda():
    """Barra de contexto (ruta, horizonte, día, ejecución). Devuelve el contexto elegido."""
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1.1, 1.2, 1, 2.2])
        with c1:
            idruta = st.selectbox("Ruta", list(NOMBRES), format_func=NOMBRES.get, index=1, key="ruta_demanda")
        with c2:
            horizonte = st.radio("Horizonte (min)", list(cl.HORIZONTES), index=1, horizontal=True,
                                 key="horizonte_demanda")
        with c3:
            dia = st.date_input("Día", date(2025, 10, 1), key="dia_demanda")
        with c4:
            manual = st.text_input("Ejecución (id, opcional)", key="ejecucion_manual",
                                   help="Pegue un execution_id para ver exactamente esa ejecución. Vacío: "
                                        "se listan las ejecuciones con filas en el día.").strip()
        if not SOLO_LECTURA:
            _formulario_ejecucion(dia, idruta)
            _formulario_recomendaciones()
    return idruta, horizonte, dia, manual


def _seccion_demanda() -> None:
    idruta, horizonte, dia, manual = _contexto_demanda()
    desde, hasta = _rango(dia, dia)
    base = {"idruta": idruta, "horizonte_min": horizonte, "desde": desde, "hasta": hasta}

    if manual:
        if not cl.es_uuid(manual):
            st.warning("El id de la ejecución no tiene formato UUID (p. ej. 3f2c…-…). No se consultó la API.")
            return
        try:
            ids = _resolver_manual(manual)
        except cl.ErrorApi as e:
            _error(e, "No se pudo consultar la ejecución indicada.")
            return
        if ids["estado"] != "exitosa":
            st.warning(f"La ejecución {manual} tiene estado «{es.md(ids['estado'])}»: no tiene filas que mostrar.")
            return
        eleccion = {"etiqueta": f"Ejecución indicada {es.corto(manual)}", "rec": ids["recomendaciones_de"],
                    "pred": ids["predicciones_de"], "origen": "manual", "detalle": ids["detalle"]}
        if not eleccion["rec"] and not eleccion["pred"]:
            st.warning(f"La ejecución {manual} es de tipo «{ids['tipo']}»: no tiene predicciones ni "
                       "recomendaciones que mostrar.")
            return
        try:
            preds = _leer_dia(ex.PREDICCIONES, {**base, "execution_id": eleccion["pred"]}) if eleccion["pred"] else []
            recs = _leer_dia(ex.RECOMENDACIONES, {**base, "execution_id": eleccion["rec"]}) if eleccion["rec"] else []
        except Exception as e:  # noqa: BLE001
            st.error(f"No se pudieron leer los datos del día. {_mensaje_error(e)}")
            return
    else:
        try:
            todas_p = _leer_dia(ex.PREDICCIONES, base)
            todas_r = _leer_dia(ex.RECOMENDACIONES, base)
        except ex.LimiteExcedido:
            st.error(f"Hay más de {LIMITE_GRAFICA} filas en el día: escriba el id de la ejecución que "
                     "quiere ver.")
            return
        except Exception as e:  # noqa: BLE001
            st.error(f"No se pudieron leer los datos del día. {_mensaje_error(e)}")
            return
        opciones = _opciones_ejecucion(str(dia), idruta, todas_p, todas_r)
        if not opciones:
            _html(es.vacio(f"No hay predicciones ni recomendaciones de la ruta {NOMBRES[idruta]} a "
                           f"{horizonte} min para el {_fecha(_iso(desde), False)}."
                           + ("" if SOLO_LECTURA else
                              " Puede crear una ejecución con «Calcular frecuencias del día».")))
            return
        if len(opciones) > 1:
            st.caption(f"Hay {len(opciones)} ejecuciones con filas en este día. Se muestra una sola a la vez "
                       "para no mezclar modelos ni cortes de datos.")
        indice = st.selectbox("Ejecución a mostrar", range(len(opciones)),
                              format_func=lambda i: opciones[i]["etiqueta"], key="eleccion_ejecucion")
        eleccion = opciones[indice]
        preds = [p for p in todas_p if eleccion["pred"] and p.get("execution_id") == eleccion["pred"]]
        recs = [r for r in todas_r if eleccion["rec"] and r.get("execution_id") == eleccion["rec"]]

    if SOLO_LECTURA:
        try:
            _validar_filas_despacho(base, eleccion, preds, recs)
        except (ValueError, TypeError, KeyError) as e:
            st.error(f"No se pudo verificar el despacho. {_mensaje_error(e)}")
            return

    _pildoras_contexto(dia, eleccion, preds)
    vista = st.radio("Vista", ("01 · Análisis y visualizaciones", "02 · Decisión operativa"), index=0,
                     horizontal=True, key="vista_demanda", label_visibility="collapsed")
    if vista.startswith("01"):
        _vista_analisis(idruta, horizonte, dia, eleccion, preds, recs)
    else:
        _vista_decision(idruta, horizonte, dia, recs, preds)


def _pildoras_contexto(dia, eleccion, preds) -> None:
    cortes = sorted({str(p.get("data_cutoff")) for p in preds if p.get("data_cutoff")})
    datos_hasta = _fecha(cortes[0]) if len(cortes) == 1 else (
        "varios cortes" if cortes else es.SIN_DATO)
    _html(es.franja_pildoras([es.pildora("Día consultado", _fecha(_iso(_inicio_dia(dia)), False)),
                              es.pildora("Recomendaciones", es.corto(eleccion.get("rec")), mono=True),
                              es.pildora("Predicciones", es.corto(eleccion.get("pred")), mono=True),
                              es.pildora("Datos hasta", datos_hasta)]))


def _kpis_analisis(horizonte, preds, recs) -> None:
    def maximo(filas, campo):
        validas = [f for f in filas if gr._f(f.get(campo)) is not None]
        return max(validas, key=lambda f: gr._f(f[campo])) if validas else None

    pm = maximo(preds, "demanda_pred")
    rm = maximo([r for r in recs if r.get("es_viable") is True], "buses_hora_recomendado")
    vm = maximo(recs, "vehiculos_requeridos")
    nv = [r for r in recs if r.get("es_viable") is False]
    topes = {gr._f(r.get("flota_tope")) for r in recs} - {None}
    tope = next(iter(topes)) if len(topes) == 1 else None
    _html(es.kpis([
        {"l": "Demanda máxima prevista", "v": es.num(pm["demanda_pred"]) if pm else es.SIN_DATO,
         "u": f"pasajeros en {horizonte} min",
         "ctx": (f"franja {_hora(pm['franja_inicio'])} · intervalo {es.num(pm.get('limite_inferior'))}–"
                 f"{es.num(pm.get('limite_superior'))}") if pm else "sin predicciones",
         "fte": "GET /predictions · demanda_pred"},
        {"l": "Máx. buses/hora recomendados", "v": es.num(rm["buses_hora_recomendado"]) if rm else es.SIN_DATO,
         "u": "bus/h", "ctx": (f"franja {_hora(rm['franja_inicio'])} · cada "
                               f"{es.num(rm.get('headway_recomendado_min'), 2)} min · solo franjas viables")
         if rm else "sin franjas viables", "fte": "GET /recommendations · buses_hora_recomendado"},
        {"l": "Máx. vehículos requeridos", "v": es.num(vm["vehiculos_requeridos"]) if vm else es.SIN_DATO,
         "u": f"de {es.num(tope)} en flota" if tope is not None else "sin tope de flota registrado",
         "mal": bool(vm and tope is not None and gr._f(vm["vehiculos_requeridos"]) > tope),
         "ctx": f"franja {_hora(vm['franja_inicio'])} · incluye franjas no viables" if vm else "",
         "fte": "GET /recommendations · vehiculos_requeridos"},
        {"l": "Franjas no viables", "v": str(len(nv)) if recs else es.SIN_DATO, "u": f"de {len(recs)}",
         "mal": bool(nv), "ctx": "la flota o el headway mínimo no alcanzan" if nv else
         ("todas viables" if recs else "sin recomendaciones"), "fte": "GET /recommendations · es_viable"},
    ]))


def _vista_analisis(idruta, horizonte, dia, eleccion, preds, recs) -> None:
    with st.container(border=True):
        _kpis_analisis(horizonte, preds, recs)
    c1, c2 = st.columns([2, 1])
    with c1, st.container(border=True):
        _html(es.encabezado("Demanda prevista", f"pasajeros en los próximos {horizonte} min, por franja de "
                            "15 min", "GET /predictions"))
        if preds:
            st.vega_lite_chart(gr.demanda(preds, horizonte), width="stretch", theme=None)
            niveles = sorted({str(gr._f(p.get("nivel_intervalo"))) for p in preds})
            _html(es.leyenda([("predicción", es.VERDE, None),
                              (f"intervalo (nivel nominal registrado {', '.join(niveles)})", es.VT2, None)]))
            st.caption("El nivel nominal es el que registra la API; no es una cobertura medida. La cobertura "
                       "observada frente a datos reales está en «Evaluación (M1)».")
        else:
            _html(es.vacio("Esta ejecución no tiene predicciones propias para la ruta, horizonte y día; "
                           "no se muestran otras en su lugar."))
    with c2, st.container(border=True):
        _html(es.encabezado("Viabilidad y cambios", None, "GET /recommendations"))
        if recs:
            deltas = [gr._f(r.get("delta_buses_hora")) for r in recs if r.get("es_viable") is True]
            partes = [("Aumenta", sum(1 for d in deltas if d is not None and d > 0), es.VERDE),
                      ("Sin cambio", sum(1 for d in deltas if d == 0), es.GRIS),
                      ("Reduce", sum(1 for d in deltas if d is not None and d < 0), "#7FA3B8"),
                      ("Sin frecuencia actual", sum(1 for d in deltas if d is None), es.AT2),
                      ("No viable", sum(1 for r in recs if r.get("es_viable") is False), es.CORAL)]
            st.vega_lite_chart(gr.anillo(partes, str(len(recs)), "franjas"), width="stretch", theme=None)
            _html(es.leyenda([(n, c, v) for n, v, c in partes if v]))
            st.caption("Conteo de franjas por es_viable y por el signo de delta_buses_hora.")
        else:
            _html(es.vacio("Sin recomendaciones para esta selección."))
    with st.container(border=True):
        _html(es.encabezado("Frecuencia de despacho", "buses por hora: recomendados por la API frente a la "
                            "programación actual", "GET /recommendations"))
        if recs:
            st.vega_lite_chart(gr.frecuencia(recs), width="stretch", theme=None)
            _html(es.leyenda([("recomendada", es.VT2, None), ("actual", es.ACERO, None),
                              ("no viable · sin recomendación", es.CO2, None)]))
        else:
            _html(es.vacio("Sin recomendaciones para esta selección."))
    c1, c2 = st.columns([2, 1])
    with c1, st.container(border=True):
        _html(es.encabezado("Resultados por franja", "consulta paginada de la API", None))
        tipo = st.radio("Tabla", ("Recomendaciones", "Predicciones"), horizontal=True, key="tabla_resultados",
                        label_visibility="collapsed")
        filtros = {"idruta": idruta, "horizonte_min": horizonte, "desde": _rango(dia, dia)[0],
                   "hasta": _rango(dia, dia)[1],
                   "execution_id": eleccion.get("rec") if tipo == "Recomendaciones" else eleccion.get("pred")}
        if filtros["execution_id"] is None:
            _html(es.vacio(f"La ejecución elegida no tiene {tipo.lower()}."))
        elif tipo == "Recomendaciones":
            _tabla_recomendaciones(filtros)
        else:
            _tabla_predicciones(filtros)
    with c2, st.container(border=True):
        _tarjeta_trazabilidad(eleccion, horizonte, preds, recs)


def _recordar_fila_prediccion(fila) -> None:
    """Guarda ejecución y modelo de una predicción vista, para contrastar luego su explicación (RF-015)."""
    if not isinstance(fila, dict) or fila.get("prediction_id") is None:
        return
    filas = st.session_state.setdefault(CLAVE_FILAS_PREDICCION, {})
    filas[int(fila["prediction_id"])] = {c: fila.get(c) for c in CAMPOS_FILA_PREDICCION}
    if len(filas) > 1_000:                      # acotado: se descartan las más antiguas
        for k in list(filas)[: len(filas) - 1_000]:
            filas.pop(k, None)


def _tabla_predicciones(filtros: dict) -> None:
    solo_no_none = {k: v for k, v in filtros.items() if v is not None}
    pagina, tamano = _paginador("predicciones", "predicciones", _firma(sorted(map(str, solo_no_none.items()))))
    params = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in solo_no_none.items()}
    try:
        p = _cache(("pag_pred", tuple(sorted(params.items())), pagina, tamano),
                   lambda: cl.obtener_pagina("predictions", {**params, "pagina": pagina, "tamano": tamano}))
    except (cl.ErrorApi, ValueError) as e:
        _error(e, "No se pudieron consultar las predicciones.")
        return
    if p.datos:
        _mostrar_tabla(_tabla(p.datos, [
            ("prediction_id", "ID predicción", "texto"), ("franja_inicio", "Franja", "fecha"),
            ("horizonte_min", "Horizonte (min)", "num"), ("demanda_pred", "Predicción", "num"),
            ("limite_inferior", "Límite inferior", "num"), ("limite_superior", "Límite superior", "num"),
            ("nivel_intervalo", "Nivel nominal", "num"), ("model_version", "Versión del modelo", "texto"),
            ("feature_version", "Variables", "texto"), ("data_cutoff", "Datos hasta", "fecha"),
            ("generated_at", "Generada", "fecha"), ("execution_id", "Ejecución", "texto")]))
        st.session_state["predicciones_vistas"] = [f.get("prediction_id") for f in p.datos][:200]
        for f in p.datos[:200]:
            _recordar_fila_prediccion(f)
    _pie_pagina(p, "predicciones")
    _exportar(ex.PREDICCIONES, solo_no_none, "CSV y Excel con todas las predicciones de los filtros activos.",
              f"predicciones_ruta{filtros['idruta']}_h{filtros['horizonte_min']}")


def _tabla_recomendaciones(filtros: dict, solo_no_viables: bool = False) -> None:
    c1, _ = st.columns([1, 3])
    with c1:
        no_viables = st.checkbox("Solo no viables", value=solo_no_viables, key="solo_no_viables")
    filtros = {**filtros, "es_viable": False if no_viables else None}
    solo_no_none = {k: v for k, v in filtros.items() if v is not None}
    pagina, tamano = _paginador("recomendaciones", "recomendaciones", _firma(sorted(map(str, solo_no_none.items()))))
    params = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in solo_no_none.items()}
    try:
        p = _cache(("pag_rec", tuple(sorted(params.items())), pagina, tamano),
                   lambda: cl.obtener_pagina("recommendations", {**params, "pagina": pagina, "tamano": tamano}))
    except (cl.ErrorApi, ValueError) as e:
        _error(e, "No se pudieron consultar las recomendaciones.")
        return
    if p.datos:
        filas = [{**f, "estado_txt": ("■ Viable" if f.get("es_viable") else "▲ No viable")} for f in p.datos]
        _mostrar_tabla(_tabla(filas, [
            ("franja_inicio", "Franja", "fecha"), ("estado_txt", "Estado", "texto"),
            ("horizonte_min", "Horizonte (min)", "num"), ("demanda_hora", "Demanda/h", "num"),
            ("buses_hora_actual", "Bus/h actual", "num"), ("buses_hora_recomendado", "Bus/h recomendado", "num"),
            ("delta_buses_hora", "Diferencia", "num"), ("headway_recomendado_min", "Headway (min)", "num"),
            ("vehiculos_requeridos", "Vehículos", "num"), ("flota_tope", "Tope de flota", "num"),
            ("motivo_no_viable", "Motivo no viable", "texto"), ("recommendation_id", "ID", "texto"),
            ("prediction_id", "ID predicción", "texto")]))
    _pie_pagina(p, "recomendaciones")
    filtros_exp = {k: ("true" if v else "false") if k == "es_viable" else v for k, v in solo_no_none.items()}
    _exportar(ex.RECOMENDACIONES, filtros_exp, "CSV y Excel con todas las recomendaciones de los filtros activos.",
              f"recomendaciones_ruta{filtros['idruta']}_h{filtros['horizonte_min']}")


def _tarjeta_trazabilidad(eleccion, horizonte, preds, recs) -> None:
    _html(es.encabezado("Trazabilidad", None, "GET /frequency/executions/{id}"))
    modelos = sorted({str(p.get("model_version")) for p in preds if p.get("model_version")})
    bases = sorted({str(r.get("base_demanda")) for r in recs if r.get("base_demanda")})
    parametros = sorted({str(r.get("parametros_version")) for r in recs if r.get("parametros_version")})
    _html(es.lista([
        ("Recomendaciones", eleccion.get("rec") or es.SIN_DATO, None, None),
        ("Predicciones", eleccion.get("pred") or es.SIN_DATO, None, None),
        ("Modelo", ", ".join(modelos) or es.SIN_DATO, None, None),
        ("Base · horizonte", f"{', '.join(bases) or es.SIN_DATO} · {horizonte} min", None, None),
        ("Parámetros", ", ".join(parametros) or es.SIN_DATO, None, None),
    ]))
    eid = eleccion.get("rec") or eleccion.get("pred")
    if eid and st.button("Ver detalle de la ejecución", key="ver_detalle_ejecucion"):
        st.session_state["detalle_ejecucion"] = eid
    if eid and st.session_state.get("detalle_ejecucion") == eid:
        try:
            d = _cache(("ejecucion", eid), lambda: cl.obtener(f"frequency/executions/{eid}"))
        except cl.ErrorApi as e:
            _error(e, "No se pudo consultar la ejecución.")
            return
        _html(es.datos([("Tipo", d.get("tipo")), ("Estado", d.get("estado")),
                        ("Inicio", _fecha(d.get("iniciada_en"))), ("Fin", _fecha(d.get("finalizada_en"))),
                        ("Correlation-Id", d.get("correlation_id")), ("Error", d.get("error") or "—")]))
        with st.expander("Parámetros y resumen (execution_log)"):
            st.json({"parametros": d.get("parametros"), "resumen": d.get("resumen")})
    if eleccion.get("rec") and not eleccion.get("pred"):
        st.caption("No se pudo identificar la ejecución dueña de las predicciones de estas recomendaciones.")


def _franja_txt(r) -> str:
    return f"{_hora(r.get('franja_inicio'))} · {'viable' if r.get('es_viable') else 'no viable'}"


def _vista_decision(idruta, horizonte, dia, recs, preds) -> None:
    if not recs:
        _html(es.vacio("Esta ejecución no tiene recomendaciones para la ruta, horizonte y día."))
        return
    nv = [r for r in recs if r.get("es_viable") is False]
    au = sorted([r for r in recs if r.get("es_viable") and (gr._f(r.get("delta_buses_hora")) or 0) > 0],
                key=lambda r: -gr._f(r["delta_buses_hora"]))
    re_ = sorted([r for r in recs if r.get("es_viable") and (gr._f(r.get("delta_buses_hora")) or 0) < 0],
                 key=lambda r: gr._f(r["delta_buses_hora"]))
    vm = max(recs, key=lambda r: gr._f(r.get("vehiculos_requeridos")) or -1)
    topes = {gr._f(r.get("flota_tope")) for r in recs} - {None}
    tope = next(iter(topes)) if len(topes) == 1 else None
    versiones = sorted({str(r.get("parametros_version")) for r in recs})
    with st.container(border=True):
        _html(es.kpis([
            {"l": "Franjas no viables", "v": str(len(nv)), "u": "franjas", "mal": bool(nv),
             "ctx": "la flota o el headway mínimo no alcanzan" if nv else "todas viables",
             "fte": "es_viable · motivo_no_viable"},
            {"l": "Máxima necesidad de vehículos", "v": es.num(vm.get("vehiculos_requeridos")),
             "u": f"vehículos · franja {_hora(vm.get('franja_inicio'))}",
             "mal": bool(tope is not None and (gr._f(vm.get("vehiculos_requeridos")) or 0) > tope),
             "ctx": "incluye las franjas no viables", "fte": "vehiculos_requeridos"},
            {"l": "Flota disponible", "v": es.num(tope) if tope is not None else es.SIN_DATO,
             "u": "vehículos (tope)" if tope is not None else "sin tope registrado",
             "ctx": f"parámetros {', '.join(versiones)}", "fte": "flota_tope"},
            {"l": "Cambios de frecuencia", "v": str(len(au)), "u": f"aumentan · {len(re_)} reducen",
             "ctx": f"mayor aumento {es.num(au[0]['delta_buses_hora'])} bus/h a las {_hora(au[0]['franja_inicio'])}"
             if au else "sin aumentos", "fte": "delta_buses_hora"},
        ]))
    with st.container(border=True):
        _html(es.encabezado("Franjas que requieren atención", "no viables primero", "GET /recommendations", "c"))
        grupos = [("▲ No viable", nv), ("■ Mayor aumento", au[:5]), ("◆ Reducción", re_[:5])]
        filas = [{**r, "grupo": g} for g, rs in grupos for r in rs]
        if filas:
            _mostrar_tabla(_tabla(filas, [
                ("grupo", "Grupo", "texto"), ("franja_inicio", "Franja", "hora"),
                ("buses_hora_actual", "Actual (bus/h)", "num"),
                ("buses_hora_recomendado", "Recomendada (bus/h)", "num"),
                ("delta_buses_hora", "Diferencia", "num"), ("vehiculos_requeridos", "Vehículos", "num"),
                ("flota_tope", "Tope", "num"), ("motivo_no_viable", "Motivo", "texto")]))
        else:
            _html(es.vacio("Ninguna franja no viable ni con cambios de frecuencia."))
    ordenadas = sorted(recs, key=lambda r: str(r.get("franja_inicio")))
    candidatas = (nv + au[:5] + re_[:5]) or ordenadas
    por_id = {r.get("recommendation_id"): r for r in ordenadas}
    opciones = [r.get("recommendation_id") for r in candidatas] + [
        r.get("recommendation_id") for r in ordenadas if r not in candidatas]
    rid = st.selectbox("Franja a revisar", opciones, format_func=lambda i: _franja_txt(por_id[i]),
                       key="franja_decision")
    r = por_id[rid]
    pred = next((p for p in preds if p.get("prediction_id") == r.get("prediction_id")), None)
    c1, c2 = st.columns([5, 7])
    with c1, st.container(border=True):
        _detalle_decision(r, pred, idruta, horizonte, dia)
    with c2, st.container(border=True):
        _calculo_decision(r)


def _detalle_decision(r, pred, idruta, horizonte, dia) -> None:
    viable = r.get("es_viable") is True
    _html(es.encabezado(f"Decisión · franja {_hora(r.get('franja_inicio'))}",
                        f"ruta {NOMBRES[idruta]} · {_fecha(_iso(_inicio_dia(dia)), False)} · {horizonte} min",
                        f"rec. {r.get('recommendation_id')}", "" if viable else "c"))
    tope = gr._f(r.get("flota_tope"))
    if viable:
        texto = (f"Viable: la API recomienda {es.num(r.get('buses_hora_recomendado'))} buses por hora "
                 f"(cada {es.num(r.get('headway_recomendado_min'), 2)} min)")
        if gr._f(r.get("buses_hora_actual")) is not None:
            texto += (f"; hoy hay {es.num(r.get('buses_hora_actual'), 2)} "
                      f"(diferencia {es.num(r.get('delta_buses_hora'), 2)}).")
        else:
            texto += "; no hay frecuencia actual registrada para comparar."
        _html(es.conclusion(texto, "ok"))
        if tope is None:
            _html(es.conclusion("Sin tope de flota registrado: la API no verificó la flota para esta franja.", "av"))
    else:
        _html(es.conclusion(f"No viable: {r.get('motivo_no_viable') or es.SIN_DATO}", "nv"))
    _html(es.datos([
        ("Demanda prevista", f"{es.num(pred.get('demanda_pred'))} pas." if pred else es.SIN_DATO),
        ("Frecuencia actual", f"{es.num(r.get('buses_hora_actual'), 2)} bus/h"
         if gr._f(r.get("buses_hora_actual")) is not None else None),
        ("Frecuencia recomendada", f"{es.num(r.get('buses_hora_recomendado'))} bus/h" if viable else None),
        ("Headway", f"{es.num(r.get('headway_recomendado_min'), 2)} min" if viable else None),
        ("Vehículos requeridos", es.num(r.get("vehiculos_requeridos"))),
        ("Flota disponible", es.num(tope) if tope is not None else None),
        ("Base de demanda", r.get("base_demanda")), ("Predicción", r.get("prediction_id")),
    ]))
    if not viable:
        _html(es.nota("La API no guarda buses/h ni headway para franjas no viables; el tablero no los calcula."))
    if r.get("prediction_id") is not None and st.button("Explicar esta predicción (RF-015)",
                                                        key="explicar_desde_decision"):
        st.session_state[CLAVE_EXPLICAR] = int(r["prediction_id"])
        st.session_state["_pid_pendiente"] = int(r["prediction_id"])
        _recordar_fila_prediccion(pred)
        st.session_state["_ir_a"] = "Modelos y explicaciones"
        st.rerun()


def _calculo_decision(r) -> None:
    """Cómo se calcula, con los valores GUARDADOS por la API (app/optimizacion/frecuencias.py). No
    recalcula: muestra los campos de la fila y la regla que los relaciona."""
    _html(es.encabezado("Cómo se calcula", f"franja {_hora(r.get('franja_inicio'))} · valores guardados por la API",
                        "GET /recommendations"))
    viable = r.get("es_viable") is True
    tope = gr._f(r.get("flota_tope"))
    veh = gr._f(r.get("vehiculos_requeridos"))
    base = "límite superior" if r.get("base_demanda") == "superior" else "predicción puntual"
    _html(es.nota(f"Regla de optimización con los parámetros {r.get('parametros_version')}; explica la regla de "
                  "frecuencias, no las variables internas del GBM. El factor de carga se aparta de la fórmula "
                  "de la especificación (§10.3) y está justificado en DECISIONES.md §3."))
    _html(es.pasos([
        ("Demanda por hora", f"{es.num(r.get('demanda_hora'), 2)} pas/h",
         f"{base} × 60 / {r.get('horizonte_min')} min", False),
        ("Carga máxima a bordo",
         f"× {es.num(r.get('factor_carga_max'), 3)} = {es.num(r.get('carga_max_estimada_hora'), 2)}",
         "demanda por hora × factor de carga medido", False),
        ("Capacidad efectiva", f"{es.num(r.get('capacidad_nominal'), 1)} × {es.num(r.get('ocupacion_objetivo'), 2)} = "
                               f"{es.num(r.get('capacidad_efectiva'), 2)}",
         f"nominal × ocupación objetivo ({r.get('capacidad_fuente')})", False),
        ("Buses por hora", f"{es.num(r.get('buses_hora_recomendado'))} bus/h" if viable else es.SIN_DATO,
         "⌈carga / capacidad efectiva⌉, mínimo 1 bus/h", False),
        ("Headway", f"{es.num(r.get('headway_recomendado_min'), 2)} min" if viable else es.SIN_DATO,
         "60 / buses por hora; debe ser ≥ headway mínimo", False),
        ("Vehículos requeridos", es.num(veh), f"⌈ciclo {es.num(r.get('ciclo_min'), 1)} min / headway⌉", False),
        ("Restricción de flota", (f"{es.num(veh)} {'≤' if veh is not None and veh <= tope else '>'} {es.num(tope)}"
                                  if tope is not None else "sin tope registrado: no se verificó"),
         "dentro del tope" if viable else (r.get("motivo_no_viable") or ""), not viable),
    ]))
    with st.expander("Qué explica esta regla y qué no"):
        st.caption("La regla operativa explica los buses, el headway y la viabilidad a partir de la demanda. "
                   "Por qué el modelo predijo esa demanda lo explica TreeSHAP, en «Modelos y explicaciones» "
                   "(botón «Explicar esta predicción»). TreeSHAP no explica la cantidad de buses.")
        st.caption("Parámetros que intervienen en la regla y que la API no devuelve con la recomendación:")
        for texto in xp.NO_DISPONIBLES_RECOMENDACION:
            st.caption(f"• {texto}")


# =====================================================================================================
# B. ALERTAS
# =====================================================================================================

AVISO_PATCH = "Se intentó actualizar una alerta: se descartó la exportación preparada."


def _conteo_alertas(filtros: dict) -> int | None:
    """paginacion.total de GET /alerts con tamano=1; None si la consulta falla."""
    params = {k: v for k, v in filtros.items() if v is not None}
    try:
        p = _cache(("conteo_alertas", tuple(sorted(params.items()))),
                   lambda: cl.obtener_pagina("alerts", {**params, "pagina": 1, "tamano": 1}))
    except (cl.ErrorApi, ValueError) as e:
        st.session_state["_ultimo_error_conteo"] = str(e)
        return None
    return p.total


def _seccion_alertas() -> None:
    c1, c2 = st.columns([2, 1])
    with c2, st.container(border=True):
        _html(es.encabezado("Filtros", "admitidos por la API"))
        f1, f2 = st.columns(2)
        with f1:
            idruta = st.selectbox("Ruta", [None] + list(NOMBRES), index=2,
                                  format_func=lambda x: "Todas" if x is None else NOMBRES[x], key="ruta_alertas")
            severidad = st.selectbox("Severidad", [None] + list(cl.SEVERIDADES),
                                     format_func=lambda x: "Todas" if x is None else x, key="severidad_alertas")
            desde_d = st.date_input("Desde (franja)", None, key="desde_alertas")
        with f2:
            estado = st.selectbox("Estado", [None] + list(cl.ESTADOS_ALERTA), index=1,
                                  format_func=lambda x: "Todos" if x is None else x, key="estado_alertas")
            codigo = st.selectbox("Código", [None] + [f"ALT-{i:03d}" for i in range(1, 11)],
                                  format_func=lambda x: "Todos" if x is None else x, key="codigo_alertas")
            hasta_d = st.date_input("Hasta (franja, inclusive)", None, key="hasta_alertas")
    desde, hasta = _rango(desde_d, hasta_d)
    if desde and hasta and desde >= hasta:
        st.warning("«Desde» debe ser anterior o igual a «Hasta».")
        return
    with c1, st.container(border=True):
        _html(es.encabezado("Estado de las alertas", f"{'todas las rutas' if idruta is None else NOMBRES[idruta]}",
                            "GET /alerts · paginacion.total", "c"))
        items = []
        for est in cl.ESTADOS_ALERTA:
            n = _conteo_alertas({"idruta": idruta, "estado": est, "severidad": severidad, "codigo": codigo,
                                 "desde": _iso(desde), "hasta": _iso(hasta)})
            items.append({"l": est.capitalize(), "v": str(n) if n is not None else es.SIN_DATO,
                          "u": "", "ctx": "" if n is not None else "la consulta falló"})
        _html(es.kpis(items))
        if any(i["v"] == es.SIN_DATO for i in items):
            st.caption(f"Algún conteo no se pudo obtener: {es.md(st.session_state.get('_ultimo_error_conteo', ''))}")

    filtros = {"idruta": idruta, "estado": estado, "severidad": severidad, "codigo": codigo,
               "desde": desde, "hasta": hasta}
    activos = {k: v for k, v in filtros.items() if v is not None}
    c1, c2 = st.columns([7, 5])
    with c1, st.container(border=True):
        _html(es.encabezado("Bandeja de alertas generadas", None, "GET /alerts", "c"))
        pagina, tamano = _paginador("alertas", "alertas", _firma(sorted(map(str, activos.items()))))
        params = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in activos.items()}
        try:
            p = _cache(("pag_alertas", tuple(sorted(params.items())), pagina, tamano),
                       lambda: cl.obtener_pagina("alerts", {**params, "pagina": pagina, "tamano": tamano}))
        except (cl.ErrorApi, ValueError) as e:
            _error(e, "No se pudieron consultar las alertas.")
            p = None
        if p is not None:
            if p.datos:
                _mostrar_tabla(_tabla(p.datos, [
                    ("alert_id", "ID", "texto"), ("codigo", "Código", "texto"), ("severidad", "Severidad", "texto"),
                    ("estado", "Estado", "texto"), ("idruta", "Ruta", "num"), ("franja_inicio", "Franja", "fecha"),
                    ("horizonte_min", "Horizonte", "num"), ("valor_observado", "Observado", "num"),
                    ("valor_esperado", "Esperado", "num"), ("umbral", "Umbral", "num"),
                    ("puntuacion_anomalia", "Anomalía", "num"), ("explicacion", "Explicación", "texto"),
                    ("recomendacion", "Recomendación", "texto"), ("usuario_asignado", "Responsable", "texto")]))
            _pie_pagina(p, "alertas")
    with c2, st.container(border=True):
        if p is not None and p.datos:
            _gestion_alerta(p.datos)
        else:
            _html(es.vacio("Seleccione filtros con alertas para ver su detalle y gestionarlas."))
    with st.container(border=True):
        _html(es.encabezado("Exportar alertas", "CSV y Excel con todas las alertas del filtro"))
        _exportar_alertas(filtros)


def _gestion_alerta(alertas: list[dict]) -> None:
    por_id = {a["alert_id"]: a for a in alertas}
    aid = st.selectbox("Alerta", list(por_id), format_func=lambda i: f"{i} · {por_id[i].get('codigo')} · "
                       f"{por_id[i].get('estado')}", key="alerta_detalle")
    a = por_id[aid]
    _html(es.encabezado(f"Alerta {aid} · {a.get('codigo')}",
                        f"severidad {a.get('severidad')} · estado {a.get('estado')}",
                        None, "c"))
    _html(f"<div class='leg'>{es.chip_severidad(a.get('severidad'))}{es.chip_estado_alerta(a.get('estado'))}</div>")
    _html(f"<div class='nota'>Ruta {es.e(a.get('idruta'))} · franja {es.e(_fecha(a.get('franja_inicio')))} · "
          f"{es.e(a.get('horizonte_min'))} min</div>")
    _html(es.leyenda([("Observado", es.CORAL, es.num(a.get("valor_observado"), 2)),
                      ("Esperado", es.ACERO, es.num(a.get("valor_esperado"), 2)),
                      ("Umbral", es.TINTA, es.num(a.get("umbral"), 2))]))
    _html(es.lista([("Explicación", a.get("explicacion"), None, None),
                    ("Recomendación", a.get("recomendacion"), None, None),
                    ("Responsable", a.get("usuario_asignado") or "Sin asignar",
                     f"anomalía {es.num(a.get('puntuacion_anomalia'), 2)}", None)]))
    with st.expander("Texto completo de la explicación y la recomendación"):
        st.text(a.get("explicacion") or es.SIN_DATO)          # texto plano: nunca Markdown
        st.text(a.get("recomendacion") or es.SIN_DATO)

    if SOLO_LECTURA:
        return
    _html(es.encabezado("Gestionar", None, "PATCH /alerts/{id}/status"))
    if _bloqueada(f"alerta_{aid}"):
        return
    validas = cl.TRANSICIONES_ALERTA.get(a.get("estado"), ())
    opciones = list(validas) + [a.get("estado")]          # el último: solo comentar
    with st.form("gestion", clear_on_submit=True):
        nuevo = st.selectbox("Nuevo estado", opciones,
                             format_func=lambda x: f"{x} (solo comentar)" if x == a.get("estado") else x,
                             key="nuevo_estado_alerta")
        usuario = st.text_input("Usuario", key="usuario_alerta")
        asignado = st.text_input("Asignar a (si aplica)", key="asignado_alerta",
                                 help="Obligatorio solo si el nuevo estado es «asignada».")
        comentario = st.text_area("Comentario", key="comentario_alerta",
                                  help="Obligatorio si no cambia el estado.")
        confirma = st.checkbox("Confirmo el cambio en la alerta", key="confirma_alerta")
        guardar = st.form_submit_button("Guardar", type="primary")
    st.caption(f"Válidas desde «{a.get('estado')}»: {', '.join(validas) or 'ninguna'}, o solo comentar. "
               "La API vuelve a validar la transición.")
    if not guardar:
        return
    faltan = []
    if not usuario.strip():
        faltan.append("el usuario")
    if nuevo == a.get("estado") and not comentario.strip():
        faltan.append("un comentario (no cambia el estado)")
    if nuevo == "asignada" and not asignado.strip():
        faltan.append("a quién se asigna")
    if faltan:
        st.warning(f"Falta {', '.join(faltan)}. No se envió nada.")
        return
    if not confirma:
        st.warning("Marque la confirmación para guardar. No se envió nada.")
        return
    _patch_alerta(aid, nuevo, usuario, comentario, asignado)


def _patch_alerta(aid, nuevo, usuario, comentario, asignado) -> None:
    """PATCH con los mensajes de la versión anterior (probados en tests/unit/test_tablero.py)."""
    try:
        opciones = cl.opciones_bearer(credencial_escritura)
    except cl.ErrorApi as e:
        _error(e, "Actualizar la alerta.", escritura=True)
        return
    _invalidar_exportacion(AVISO_PATCH)
    st.session_state["_generacion"] = _generacion() + 1
    st.session_state.pop("_cache", None)
    bloqueo = f"_envio_alerta_{aid}"
    st.session_state[bloqueo] = True
    confirmado = False
    try:
        r = requests.patch(
            f"{cl.url_api()}/alerts/{aid}/status",
            json={"estado": nuevo, "usuario": usuario, "comentario": comentario or None,
                  "usuario_asignado": asignado or None},
            timeout=10,
            **opciones,
        )
        if credencial_escritura and 300 <= r.status_code < 400:
            st.error("La API devolvió una redirección. Revise la alerta antes de reintentar.")
            return
        r.raise_for_status()
        resultado = r.json()
    except requests.Timeout:
        st.error("No se pudo confirmar la actualización. Revise el estado de la alerta antes de reintentar.")
    except requests.ConnectionError:
        st.error("No se pudo confirmar la actualización. Revise el estado de la alerta antes de reintentar.")
    except requests.HTTPError as e:
        respuesta = e.response
        codigo = respuesta.status_code if respuesta is not None else "desconocido"
        mensajes = {401: cl.TEXTO_CODIGO[401], 403: cl.TEXTO_CODIGO[403],
                    404: "La alerta solicitada no existe.",
                    409: "La operación no está permitida por el estado actual de la alerta.",
                    422: "Los datos enviados no cumplen las validaciones.",
                    500: "La API encontró un error interno."}
        texto_error = (f"No se pudo actualizar la alerta (HTTP {codigo}). "
                       f"{mensajes.get(codigo, 'La API rechazó la solicitud.')}")
        cuerpo_error = {}
        if respuesta is not None:
            try:
                cuerpo_error = respuesta.json()
            except requests.exceptions.JSONDecodeError:
                pass
        error_api = cuerpo_error.get("error", {}) if isinstance(cuerpo_error, dict) else {}
        if isinstance(error_api, dict):
            mensaje_api = cl.ocultar_credencial(error_api.get("mensaje"), credencial_escritura)
            correlation_id = cl.ocultar_credencial(error_api.get("correlation_id"), credencial_escritura)
            if mensaje_api:
                texto_error += f" Mensaje de la API: {es.md(mensaje_api)}"
            if correlation_id:
                texto_error += f" Identificador de seguimiento: {es.md(correlation_id)}"
            # Error propio de la API (formato uniforme, transacción revertida): se sabe que no se aplicó.
            # Un 502/503/504 o un cuerpo ajeno viene de un intermediario: queda sin confirmar (bloqueado).
            confirmado = bool(correlation_id) and codigo not in cl.CODIGOS_INTERMEDIARIO
        st.error(texto_error)
    except requests.exceptions.JSONDecodeError:
        st.error(f"La API respondió con HTTP {r.status_code}, pero no se pudo leer su respuesta. "
                 "La actualización pudo haberse aplicado. Revise el estado de la alerta antes de reintentar.")
    except requests.RequestException:
        st.error("Ocurrió un problema de comunicación con la API.")
    except ValueError:
        st.error("Ocurrió un error al procesar la respuesta de la API.")
    else:
        try:
            cl.validar_cambio_alerta(resultado, aid, nuevo)
        except cl.ErrorApi as e:              # 2xx sin la alerta con el estado pedido: no confirmado
            st.warning(f"Resultado no confirmado. {es.md(e.texto())}")
        else:
            confirmado = True
            st.success("La alerta se actualizó correctamente.")
            st.json(resultado)
            st.caption("La bandeja y los conteos se vuelven a consultar en la próxima interacción.")
    if confirmado:
        st.session_state.pop(bloqueo, None)


# =====================================================================================================
# C. CALIDAD DE DATOS (G3)
# =====================================================================================================

def _seccion_calidad() -> None:
    with st.container(border=True):
        _html(es.encabezado("Errores de calidad por lote", "detecciones de un lote con su evento",
                            "GET /quality/errors"))
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            lote = st.text_input("Lote (obligatorio)", key="lote_calidad",
                                 help="La API no ofrece un listado de lotes: escriba el lote exacto (p. ej. el de "
                                      "una ingesta). Se usa tal cual, sin recortar espacios.")
        with c2:
            alcance = st.selectbox("Alcance", cl.ALCANCES_CALIDAD, index=0, key="alcance_calidad",
                                   help="ventana: el lote exacto · ejecucion: sus ventanas <lote>_NNN · auto: ambos.")
        with c3:
            idruta = st.selectbox("Ruta", [None] + list(NOMBRES),
                                  format_func=lambda x: "Todas" if x is None else NOMBRES[x],
                                  key="ruta_calidad")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            regla = st.text_input("Regla", key="regla_calidad")
        with c2:
            tabla = st.text_input("Tabla de origen", key="tabla_calidad")
        with c3:
            version = st.text_input("Versión de reglas", key="version_calidad")
        with c4:
            causa = st.text_input("Causa (texto exacto)", key="causa_calidad",
                                  help="Comparación exacta, no búsqueda parcial.")
    if not lote:
        _html(es.vacio("Escriba un lote para consultar sus errores de calidad."))
        return
    filtros = {"lote": lote, "alcance": alcance, "idruta": idruta, "regla": regla or None,
               "tabla_origen": tabla or None, "reglas_version": version or None, "causa": causa or None}
    activos = {k: v for k, v in filtros.items() if v is not None}
    with st.container(border=True):
        pagina, tamano = _paginador("calidad", "detecciones", _firma(sorted(activos.items())))
        try:
            p = _cache(("pag_calidad", tuple(sorted(activos.items())), pagina, tamano),
                       lambda: cl.obtener_pagina("quality/errors", {**activos, "pagina": pagina, "tamano": tamano}))
        except (cl.ErrorApi, ValueError) as e:
            _error(e, "No se pudieron consultar los errores de calidad.")
            return
        nuevas = sum(1 for f in p.datos if f.get("nuevo_en_lote") is True)
        _html(es.kpis([{"l": "Detecciones", "v": str(p.total), "u": "en total", "fte": "paginacion.total"},
                       {"l": "Nuevas en el lote", "v": str(nuevas), "u": "en esta página",
                        "ctx": "primera vez que se detecta el evento"},
                       {"l": "Ya detectadas antes", "v": str(len(p.datos) - nuevas), "u": "en esta página",
                        "ctx": "el evento venía de un lote anterior"}]))
        if p.datos:
            filas = [{**f, "novedad": ("■ Nueva en el lote" if f.get("nuevo_en_lote")
                                       else f"◆ Ya detectada (primer lote {f.get('primer_lote')})")} for f in p.datos]
            _mostrar_tabla(_tabla(filas, [
                ("deteccion_id", "ID detección", "texto"), ("evento_id", "ID evento", "texto"),
                ("novedad", "Novedad", "texto"), ("lote", "Lote", "texto"), ("detectado_en", "Detectado", "fecha"),
                ("idruta", "Ruta", "num"), ("regla", "Regla", "texto"), ("causa", "Causa", "texto"),
                ("tabla_origen", "Tabla", "texto"), ("idprogramacion", "idprogramacion", "texto"),
                ("valor_original", "Valor original", "texto"), ("valor_normalizado", "Valor normalizado", "texto"),
                ("reglas_version", "Reglas", "texto"), ("primera_deteccion", "Primera detección", "fecha")]))
            st.caption("Una detección es la aparición de un evento en un lote; varias detecciones pueden "
                       "pertenecer al mismo evento (mismo ID evento).")
        _pie_pagina(p, "detecciones")
        _exportar(ex.ERRORES_CALIDAD, activos, "CSV y Excel con todas las detecciones de los filtros activos.",
                  f"errores_calidad_{lote}"[:80])


# =====================================================================================================
# D. EVALUACIÓN M1
# =====================================================================================================

EXPLICACION_ESTADOS = {
    "valido": "se compara con la demanda real consolidada (entra en las métricas)",
    "no_disponible": "el real aún no está consolidado o no hay marca de agua",
    "incompleto_cuarentena": "falta medición o hubo cuarentena en la ventana",
    "sin_correspondencia": "ventana sin salidas fuera de horario o sin horario para decidir",
    "duplicado": "repetición u otro pronóstico más reciente del mismo modelo",
    "inconsistente": "la predicción no es comparable (ver motivo)",
}
COLORES_ESTADO = {"valido": es.VERDE, "no_disponible": es.GRIS, "incompleto_cuarentena": es.AMBAR,
                  "sin_correspondencia": es.AT2, "duplicado": "#7FA3B8", "inconsistente": es.CORAL}


def _etiqueta_modelo(m) -> str:
    return f"{m.get('model_id')} · {m.get('nombre')} {m.get('version')} ({m.get('estado')})"


def _filtros_m1() -> dict:
    with st.container(border=True):
        _html(es.encabezado("Filtros de la evaluación", "solo predicciones guardadas y reales consolidados",
                            "GET /predictions/accuracy"))
        c1, c2, c3, c4 = st.columns([2, 1, 1, 2])
        ids_modelo = [None] + [m.get("model_id") for m in (MODELOS or [])]
        por_id = {m.get("model_id"): m for m in (MODELOS or [])}
        with c1:
            model_id = st.selectbox("Modelo", ids_modelo, key="modelo_m1",
                                    index=(ids_modelo.index(ACTIVO["model_id"])
                                           if ACTIVO and ACTIVO.get("model_id") in ids_modelo else 0),
                                    format_func=lambda x: "Todos (se muestran por separado)" if x is None
                                    else _etiqueta_modelo(por_id[x]))
        with c2:
            idruta = st.selectbox("Ruta", [None] + list(NOMBRES), key="ruta_m1",
                                  format_func=lambda x: "Todas" if x is None else NOMBRES[x])
        with c3:
            horizonte = st.selectbox("Horizonte", [None] + list(cl.HORIZONTES), key="horizonte_m1",
                                     format_func=lambda x: "Todos" if x is None else f"{x} min")
        with c4:
            execution_id = st.text_input("Ejecución (id, opcional)", key="ejecucion_m1").strip() or None
        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            desde_d = st.date_input("Desde (franja)", date(2025, 10, 1), key="desde_m1")
        with c2:
            hasta_d = st.date_input("Hasta (franja, inclusive)", date(2025, 10, 1), key="hasta_m1")
        with c3:
            agrupar = st.multiselect("Agrupar por", list(cl.AGRUPACIONES_M1), default=["idruta", "horizonte_min"],
                                     key="agrupar_m1")
    desde, hasta = _rango(desde_d, hasta_d)
    return {"model_id": model_id, "idruta": idruta, "horizonte_min": horizonte, "execution_id": execution_id,
            "desde": desde, "hasta": hasta, "agrupar_por": agrupar}


def _seccion_m1() -> None:
    f = _filtros_m1()
    if f["execution_id"] and not cl.es_uuid(f["execution_id"]):
        st.warning("El id de la ejecución no tiene formato UUID. No se consultó la API.")
        return
    if f["desde"] and f["hasta"] and f["desde"] >= f["hasta"]:
        st.warning("«Desde» debe ser anterior o igual a «Hasta».")
        return
    vista = st.radio("Vista M1", ("Métricas", "Detalle de pares"), horizontal=True, key="vista_m1",
                     label_visibility="collapsed")
    base = {k: v for k, v in f.items() if k != "agrupar_por" and v is not None}
    if vista == "Métricas":
        _m1_metricas(base, f["agrupar_por"])
    else:
        _m1_pares(base)


def _m1_metricas(base: dict, agrupar: list) -> None:
    if not agrupar:
        st.warning("Elija al menos una agrupación.")
        return
    params = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in base.items()}
    clave = ("m1", tuple(sorted(params.items())), tuple(agrupar))
    if st.button("Calcular métricas", type="primary", key="calcular_m1",
                 help="La API reconstruye el emparejamiento predicción-real; puede tardar."):
        st.session_state["m1_pedido"] = clave
    if st.session_state.get("m1_pedido") != clave:
        _html(es.vacio("Pulse «Calcular métricas» para consultar la evaluación con estos filtros."))
        return
    try:
        with st.spinner("Calculando la evaluación en la API…"):
            r = _cache(clave, lambda: cl.obtener("predictions/accuracy", {**params, "agrupar_por": list(agrupar)},
                                                 timeout=cl.TIEMPO_EVALUACION))
    except cl.ErrorApi as e:
        _error(e, "No se pudo calcular la evaluación.")
        return
    faltan = [k for k in ("parametros", "conteo_estados", "total", "datos") if k not in r]
    if faltan:
        st.error(f"La respuesta de la evaluación no trae: {', '.join(faltan)}.")
        return
    par = r.get("parametros") or {}
    for adv in par.get("advertencias") or []:
        st.warning(es.md(adv))
    conteo = r.get("conteo_estados") or {}
    with st.container(border=True):
        _html(es.encabezado("Estado de las predicciones evaluadas", f"reglas {par.get('reglas')}",
                            f"huella {es.corto(r.get('huella'), 6)}"))
        _html(es.leyenda([(est, COLORES_ESTADO.get(est, es.GRIS), conteo.get(est, 0)) for est in cl.ESTADOS_M1]))
        _html(es.datos([("Marca de agua", _fecha(par.get("marca_agua"))),
                        ("Corte de consolidación", _fecha(par.get("corte_consolidacion"))),
                        ("Margen (días)", par.get("margen_consolidacion_dias")),
                        ("Fuente real", par.get("fuente_real"))]))
        with st.expander("Qué significa cada estado y motivos de exclusión"):
            _mostrar_tabla(pd.DataFrame({"Estado": list(EXPLICACION_ESTADOS),
                                         "Significado": list(EXPLICACION_ESTADOS.values()),
                                         "Predicciones": [conteo.get(k, 0) for k in EXPLICACION_ESTADOS]}))
            motivos = r.get("conteo_motivos") or {}
            if motivos:
                _mostrar_tabla(pd.DataFrame({"Motivo": list(motivos), "Predicciones": list(motivos.values())}))
            st.caption("Solo el estado «valido» entra en las métricas. Los demás no se convierten en cero.")
    totales = [t for t in (r.get("total") or []) if isinstance(t, dict)]
    if not totales:
        _html(es.vacio("No hay predicciones en este filtro."))
        return
    claves_modelo = [(t.get("model_id"), t.get("model_version")) for t in totales]
    if len(claves_modelo) > 1:
        st.info(f"El filtro incluye {len(claves_modelo)} modelos o versiones. Las métricas nunca se mezclan: "
                "se muestra uno a la vez.")
    i = st.selectbox("Modelo a mostrar", range(len(totales)), key="modelo_mostrar_m1",
                     format_func=lambda k: f"{totales[k].get('model_id')} · {totales[k].get('model_version')}")
    t = totales[i]
    with st.container(border=True):
        _html(es.encabezado(f"Desempeño · modelo {t.get('model_id')} ({t.get('model_version')})",
                            "solo predicciones válidas", "total"))
        _html(es.kpis([
            {"l": "Predicciones válidas", "v": f"{t.get('n_validas', 0)}", "u": f"de {t.get('n_predicciones', 0)}"},
            {"l": "WAPE", "v": es.pct(t.get("wape")), "ctx": "error absoluto ponderado por la demanda real"},
            {"l": "MAE", "v": es.num(t.get("mae"), 2), "u": "pasajeros", "ctx": f"RMSE {es.num(t.get('rmse'), 2)}"},
            {"l": "sMAPE", "v": es.pct(t.get("smape")), "ctx": f"sesgo {es.num(t.get('sesgo'), 2)} pas."},
        ]))
        _html(es.kpis([
            {"l": "Cobertura observada del intervalo", "v": es.pct(t.get("cobertura_intervalo")),
             "u": f"nominal {es.pct(t.get('nivel_intervalo_nominal'), 0)}",
             "ctx": "proporción de reales dentro de [inferior, superior]"},
            {"l": "MAE relativo al ingenuo de 7 días", "v": es.num(t.get("mae_relativo_ingenuo_7d"), 3),
             "u": f"en {t.get('n_ingenuo_7d', 0)} filas con ingenuo",
             "ctx": f"modelo {es.num(t.get('mae_mismas_filas_ingenuo'), 2)} vs ingenuo "
                    f"{es.num(t.get('mae_ingenuo_7d'), 2)} "
                    "en las mismas filas; < 1 = mejor que el ingenuo"},
        ]))
        if t.get("advertencia"):
            st.warning(es.md(t["advertencia"]))
        st.caption("No compare WAPE entre grupos o modelos con conjuntos de observaciones distintos; para comparar "
                   "con el ingenuo use el MAE relativo calculado sobre las mismas filas.")
    grupos = [g for g in (r.get("datos") or []) if g.get("model_id") == t.get("model_id")
              and g.get("model_version") == t.get("model_version")]
    with st.container(border=True):
        _html(es.encabezado("Métricas por grupo", f"agrupado por {', '.join(agrupar)} · mismo modelo", "datos"))
        if grupos:
            if set(agrupar) == {"idruta", "horizonte_min"}:
                metrica = st.radio("Métrica del mapa", ("wape", "smape", "mae", "rmse"), horizontal=True,
                                   key="metrica_mapa_m1", format_func=str.upper)
                filas = [{**g, "nombre_ruta": cl.RUTAS.get(g.get("idruta"), "")} for g in grupos]
                st.vega_lite_chart(gr.mapa_calor(filas, metrica, metrica in ("wape", "smape")),
                                   width="stretch", theme=None)
                st.caption("Más oscuro = más error (escala relativa a las celdas mostradas).")
            cols = [(g, g, "texto") for g in agrupar if g != "execution_id"] + (
                [("execution_id", "execution_id", "texto")] if "execution_id" in agrupar else [])
            _mostrar_tabla(_tabla(grupos, cols + [
                ("n_validas", "Válidas", "num"), ("n_predicciones", "Predicciones", "num"), ("mae", "MAE", "num"),
                ("rmse", "RMSE", "num"), ("wape", "WAPE", "num"), ("smape", "sMAPE", "num"), ("sesgo", "Sesgo", "num"),
                ("cobertura_intervalo", "Cobertura", "num"), ("nivel_intervalo_nominal", "Nominal", "num"),
                ("mae_relativo_ingenuo_7d", "MAE rel. ingenuo", "num"), ("n_ingenuo_7d", "Filas ingenuo", "num"),
                ("advertencia", "Advertencia", "texto")]))
        else:
            _html(es.vacio("Sin grupos para este modelo."))


def _m1_pares(base: dict) -> None:
    c1, _ = st.columns([1, 3])
    with c1:
        estado = st.selectbox("Estado del par", [None] + list(cl.ESTADOS_M1), key="estado_pares_m1",
                              format_func=lambda x: "Todos" if x is None else x)
    filtros = {**base, "estado": estado} if estado else dict(base)
    params = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in filtros.items()}
    with st.container(border=True):
        _html(es.encabezado("Detalle de pares predicción-real", "por qué cada predicción entra o no en las métricas",
                            "GET /predictions/accuracy/pairs"))
        pagina, tamano = _paginador("pares", "pares", _firma(sorted(params.items())))
        if st.button("Consultar pares", key="consultar_pares_m1", type="primary"):
            st.session_state["pares_pedido"] = (tuple(sorted(params.items())), pagina, tamano)
        pedido = st.session_state.get("pares_pedido")
        if not pedido or pedido[0] != tuple(sorted(params.items())):
            _html(es.vacio("Pulse «Consultar pares» (la API reconstruye la evaluación en cada página)."))
            return
        try:
            p = _cache(("pag_pares", tuple(sorted(params.items())), pagina, tamano),
                       lambda: cl.obtener_pagina("predictions/accuracy/pairs",
                                                 {**params, "pagina": pagina, "tamano": tamano},
                                                 timeout=cl.TIEMPO_EVALUACION))
        except (cl.ErrorApi, ValueError) as e:
            _error(e, "No se pudieron consultar los pares.")
            return
        if p.datos:
            _mostrar_tabla(_tabla(p.datos, [
                ("prediction_id", "ID predicción", "texto"), ("estado", "Estado", "texto"),
                ("motivo", "Motivo", "texto"), ("detalle_motivo", "Detalle", "texto"),
                ("franja_inicio", "Franja", "fecha"), ("horizonte_min", "Horizonte", "num"), ("idruta", "Ruta", "num"),
                ("demanda_pred", "Predicción", "num"), ("demanda_real", "Real", "num"), ("error", "Error", "num"),
                ("limite_inferior", "Inferior", "num"), ("limite_superior", "Superior", "num"),
                ("dentro_intervalo", "Dentro del intervalo", "bool"), ("demanda_ingenuo_7d", "Ingenuo 7 d", "num"),
                ("programaciones_ventana", "Salidas en la ventana", "num"), ("model_id", "Modelo", "num"),
                ("model_version", "Versión", "texto"), ("data_cutoff", "Datos hasta", "fecha"),
                ("execution_id", "Ejecución", "texto")]))
            st.caption("Real vacío = no disponible o excluido; nunca se reemplaza por 0.")
        _pie_pagina(p, "pares")
        exp_filtros = {k: v for k, v in filtros.items() if v is not None}
        _exportar(ex.PARES_M1, exp_filtros, "CSV y Excel con todos los pares de los filtros, con estado y motivo.",
                  "pares_m1", get=lambda ruta, prm: cl.obtener(ruta, prm, timeout=cl.TIEMPO_EVALUACION))


# =====================================================================================================
# E. MODELOS Y EXPLICACIONES
# =====================================================================================================

CLAVES_INTERNAS = ("artefacto_ruta", "ruta_artefacto", "path", "dir", "archivo", "ruta_archivo")


def _sin_rutas(obj):
    """Quita claves que pueden revelar rutas del sistema de archivos (artefacto_ruta, *path*, *dir*)."""
    if isinstance(obj, dict):
        return {k: _sin_rutas(v) for k, v in obj.items()
                if not any(x in str(k).lower() for x in ("path", "dir")) and str(k) not in CLAVES_INTERNAS}
    if isinstance(obj, list):
        return [_sin_rutas(x) for x in obj]
    return obj


def _seccion_modelos() -> None:
    if ERROR_MODELOS is not None:
        _error(ERROR_MODELOS, "No se pudo consultar el registro de modelos.")
    modelos = MODELOS or []
    c1, c2 = st.columns([8, 4])
    with c1, st.container(border=True):
        _html(es.encabezado("Registro de modelos", "activo, candidatos, rechazados y retirados", "GET /models"))
        if modelos:
            filas = [{**m, "estado_txt": {"activo": "■ activo", "candidato": "● candidato", "rechazado": "▲ rechazado",
                                          "retirado": "◆ retirado"}.get(m.get("estado"), m.get("estado"))}
                     for m in modelos]
            _mostrar_tabla(_tabla(filas, [
                ("model_id", "ID", "texto"), ("estado_txt", "Estado", "texto"), ("nombre", "Nombre", "texto"),
                ("version", "Versión", "texto"), ("tipo", "Tipo", "texto"), ("feature_version", "Variables", "texto"),
                ("version_datos", "Versión de datos", "texto"), ("semilla", "Semilla", "texto"),
                ("creado_en", "Creado", "fecha"), ("activado_en", "Activado", "fecha"),
                ("activado_por", "Activado por", "texto")]))
        elif ERROR_MODELOS is None:
            _html(es.vacio("No hay modelos registrados."))
    with c2, st.container(border=True):
        if not SOLO_LECTURA:
            _activacion(modelos)
    if modelos:
        por_id = {m.get("model_id"): m for m in modelos}
        with st.container(border=True):
            mid = st.selectbox("Ficha técnica del modelo", list(por_id), key="ficha_modelo",
                               index=(list(por_id).index(ACTIVO["model_id"])
                                      if ACTIVO and ACTIVO.get("model_id") in por_id else 0),
                               format_func=lambda x: _etiqueta_modelo(por_id[x]))
            _ficha(por_id[mid])
    with st.container(border=True):
        _explicacion()


def _ficha(m: dict) -> None:
    _html(es.encabezado("Ficha técnica", "registro del modelo en la API", "GET /models"))
    _html(es.datos([("Nombre", m.get("nombre")), ("Versión", m.get("version")), ("Tipo", m.get("tipo")),
                    ("Estado", m.get("estado")), ("Variables", m.get("feature_version")),
                    ("Versión de datos", m.get("version_datos")), ("Semilla", m.get("semilla")),
                    ("SHA-256 del artefacto", m.get("artefacto_sha256")), ("Creado", _fecha(m.get("creado_en"))),
                    ("Activado", _fecha(m.get("activado_en")) if m.get("activado_en") else None)]))
    with st.expander("Hiperparámetros"):
        st.json(_sin_rutas(m.get("hiperparametros") or {}))
    _importancia_permutacion(m)
    metricas = _sin_rutas(m.get("metricas") or {})
    with st.expander("Métricas registradas al entrenar"):
        if metricas:
            st.caption("Tal como se registraron al entrenar (evaluación histórica del proceso de entrenamiento). "
                       "No son la evaluación frente a datos reales: para eso use «Evaluación (M1)». Algunas, como "
                       "la cobertura del intervalo, pueden venir de un modelo de evaluación distinto de este "
                       "artefacto: no las lea como desempeño en producción.")
            st.json(metricas)
        else:
            st.caption("Este modelo no tiene métricas registradas.")


def _importancia_permutacion(m: dict) -> None:
    """Importancia global por permutación (metricas.importancia_top10 de GET /models). Se calculó con el
    modelo de evaluación del entrenamiento: no se mezcla con la importancia por ganancia del artefacto."""
    _html(es.zona("Importancia global por permutación"))
    filas = xp.importancia_permutacion(m)
    if filas is None:
        _html(es.vacio("Este modelo no registra importancia por permutación (metricas.importancia_top10)."
                       if xp.es_gbm(m) else
                       "Modelo de línea base: no tiene importancia de variables ni explicación TreeSHAP."))
        return
    st.caption(xp.TEXTO_PERMUTACION)
    st.vega_lite_chart(gr.barras_horizontales(filas, "importancia_mae", "variable",
                                              "aumento del MAE al permutar (pasajeros)"),
                       width="stretch", theme=None)
    _mostrar_tabla(_tabla(filas, [("variable", "Variable", "texto"),
                                  ("importancia_mae", "Aumento del MAE (pasajeros)", "num")]))
    st.caption("La importancia por ganancia del artefacto de producción es otra medida: aparece con la "
               "explicación individual de una predicción.")


def _activacion(modelos: list[dict]) -> None:
    _html(es.encabezado("Activar un modelo", "cambia el modelo activo y retira el anterior", None, "c"))
    _html(es.marca_escritura("Acción que escribe · POST /models/{id}/activate"))
    if ERROR_MODELOS is not None or not modelos:
        _html(es.vacio("Sin listado de modelos no se ofrece la activación."))
        return
    if _bloqueada("activacion"):
        return
    # La API y el repositorio también rechazan la activación de modelos rechazados.
    elegibles = [m for m in modelos if m.get("estado") in ("candidato", "retirado")]
    rechazados = sum(1 for m in modelos if m.get("estado") == "rechazado")
    if rechazados:
        st.caption(f"{rechazados} modelo(s) rechazado(s) no se ofrecen para activar.")
    if not elegibles:
        _html(es.vacio("No hay modelos candidatos ni retirados para activar."))
        return
    por_id = {m.get("model_id"): m for m in elegibles}
    with st.form("form_activacion", clear_on_submit=True):
        mid = st.selectbox("Modelo a activar", list(por_id), format_func=lambda x: _etiqueta_modelo(por_id[x]),
                           key="modelo_activar")
        usuario = st.text_input("Usuario", key="usuario_activacion")
        motivo = st.text_area("Motivo", key="motivo_activacion", help="Al menos 3 caracteres; queda en la bitácora.")
        confirma = st.checkbox("Confirmo que quiero activar este modelo y retirar el activo", key="confirma_activacion")
        reversion = st.checkbox("Si el modelo elegido está retirado: confirmo que esto revierte el modelo activo",
                                key="confirma_reversion")
        enviar = st.form_submit_button("Activar modelo", type="primary")
    st.caption("Para revertir, active de nuevo el modelo anterior (retirado) con este mismo formulario.")
    if not enviar:
        return
    if not usuario.strip() or len(motivo.strip()) < 3:
        st.warning("El usuario y un motivo de al menos 3 caracteres son obligatorios. No se envió nada.")
        return
    if not confirma:
        st.warning("Marque la confirmación para activar. No se envió nada.")
        return
    if por_id[mid].get("estado") == "retirado" and not reversion:
        st.warning(f"El modelo {mid} está retirado: activarlo revierte el modelo activo. Marque también la "
                   "confirmación de reversión. No se envió nada.")
        return
    anterior = ACTIVO.get("model_id") if ACTIVO else None
    r, err = _enviar("activacion", lambda: cl.validar_activacion(cl.solicitar(
        "POST", f"models/{mid}/activate", json={"usuario": usuario, "motivo": motivo},
        timeout=cl.TIEMPO_ESCRITURA, modifica=True, credencial=credencial_escritura), mid))
    _invalidar_datos("Se intentó cambiar el modelo activo: se descartó la exportación preparada.")
    if err is not None:
        if err.codigo_http == 409 and not err.no_confirmada:
            st.error(f"La operación falló: la API no permite activar el modelo {mid}. {es.md(err.texto())}")
        else:
            _error(err, "Activar el modelo.", escritura=True)
        return
    previo = r["modelo_anterior"]
    _avisar_y_recargar("ok", f"Modelo activo: {r['model_id']}. Modelo anterior: "
                       f"{previo if previo is not None else 'ninguno'} (antes de enviar, el registro mostraba "
                       f"{anterior if anterior is not None else 'ninguno'}). El listado ya está actualizado.", r)


def _explicacion() -> None:
    _html(es.encabezado("Explicación individual de una predicción (RF-015)", "aportes TreeSHAP verificados por la API",
                        "GET /predictions/{id}/explanation"))
    st.caption("Solo para predicciones del GBM FE 3.1 que la API puede reconstruir y verificar. "
               "El tablero no calcula SHAP: muestra lo que devuelve la API, después de comprobarlo.")
    _html(es.nota(xp.TEXTO_ALCANCE))
    vistas = [x for x in st.session_state.get("predicciones_vistas", []) if x is not None]
    pendiente = st.session_state.pop("_pid_pendiente", None)
    if pendiente is not None:                     # viene de «Explicar esta predicción»: se pide de inmediato
        st.session_state["pid_explicacion"] = pendiente
        st.session_state["pid_tabla"] = None
        st.session_state["explicacion_pedida"] = pendiente
    c1, c2 = st.columns([1, 1])
    with c1:
        pid = st.number_input("ID de la predicción", min_value=1, step=1, key="pid_explicacion",
                              **({} if "pid_explicacion" in st.session_state else {"value": 1}))
    with c2:
        de_tabla = st.selectbox("…o una de la última tabla de predicciones", [None] + vistas, key="pid_tabla",
                                format_func=lambda x: "—" if x is None else str(x))
    elegido = int(de_tabla) if de_tabla is not None else int(pid)
    if st.button("Solicitar explicación", type="primary", key="solicitar_explicacion"):
        st.session_state[CLAVE_EXPLICAR] = elegido
        st.session_state["explicacion_pedida"] = elegido
    if st.session_state.get("explicacion_pedida") != elegido:
        return
    fila = (st.session_state.get(CLAVE_FILAS_PREDICCION) or {}).get(elegido)
    if fila is not None and MODELOS is not None:
        registro = xp.modelo_registrado(MODELOS, fila.get("model_id"), fila.get("model_version"))
        if registro is not None and not xp.es_gbm(registro):
            st.info(xp.TEXTO_LINEA_BASE)          # sin llamada: la API respondería 409
            return
    try:
        with st.spinner("La API reconstruye y verifica la predicción…"):
            x = _cache(("explicacion", elegido), lambda: cl.obtener(f"predictions/{elegido}/explanation",
                                                                   timeout=cl.TIEMPO_EVALUACION))
    except cl.ErrorApi as e:
        if e.codigo_http == 409:
            st.info("Esta predicción no puede explicarse con el mecanismo disponible (solo predicciones del GBM "
                    f"FE 3.1 reproducibles y verificadas). {e.texto()}")
        elif e.codigo_http == 404:
            st.warning(f"La predicción {elegido} no existe. {e.texto()}")
        else:
            _error(e, "No se pudo obtener la explicación.")
        return
    motivo = xp.validar_explicacion(x, elegido, fila)
    if motivo is not None:
        st.error(f"La API devolvió una explicación que no se puede mostrar: {es.md(motivo)}. "
                 "No se muestra ninguna parte de ella.")
        return
    if fila is None:
        st.caption(f"La predicción {elegido} se escribió a mano: se comprobó que la respuesta es de esa "
                   "predicción, pero no se pudo contrastar su ejecución y su modelo con la fila de "
                   "predicciones (la API no consulta predicciones por id). Para la comprobación completa, "
                   "elíjala en la tabla de predicciones o con «Explicar esta predicción».")
    _html(es.datos([("Predicción", x.get("prediction_id")), ("Ejecución", x.get("execution_id")),
                    ("Modelo", f"{x.get('model_id')} · {x.get('model_version')}"),
                    ("Variables", x.get("feature_version")), ("Ruta", NOMBRES.get(x.get("idruta"), x.get("idruta"))),
                    ("Franja", _fecha(x.get("franja_inicio"))), ("Horizonte", f"{x.get('horizonte_min')} min"),
                    ("Datos hasta", _fecha(x.get("data_cutoff"))), ("scikit-learn", x.get("scikit_learn")),
                    ("SHA-256", es.corto(x.get("artefacto_sha256"), 8))]))
    r = xp.resumen_aportes(x)
    _html(es.kpis([
        {"l": "Demanda predicha", "v": es.num(x.get("demanda_pred"), 2), "u": "pasajeros (guardada)"},
        {"l": "Referencia", "v": es.num(x.get("referencia_pasajeros"), 2), "u": "pasajeros",
         "ctx": f"log {es.num(x.get('referencia_log'), 4)}"},
        {"l": "Predicción reconstruida", "v": es.num(x.get("prediccion_pasajeros"), 2), "u": "pasajeros",
         "ctx": f"log {es.num(x.get('prediccion_log'), 4)} = referencia + suma de aportes"},
        {"l": "Variables", "v": f"{r['suben']} ↑ · {r['bajan']} ↓", "u": f"de {xp.N_VARIABLES_MODELO}",
         "ctx": f"más sube: {r['mayor_sube'] or '—'} · más baja: {r['mayor_baja'] or '—'}"}]))
    if x.get("advertencia"):
        st.warning(es.md(x["advertencia"]))
    st.caption(f"{es.md(xp.TEXTO_ESCALA)} {es.md(xp.TEXTO_REFERENCIA)}")
    aportes = xp.filas_aportes(x)
    st.vega_lite_chart(gr.barras_horizontales(aportes[:15], "aporte_log", "variable", "aporte (escala log)",
                                              divergente=True), width="stretch", theme=None)
    st.caption("Verde: la variable aumenta la predicción · azul: la disminuye. Se muestran las 15 de mayor "
               "magnitud; la tabla trae las 29.")
    _mostrar_tabla(_tabla(aportes, [("variable", "Variable", "texto"), ("valor", "Valor de la variable", "texto"),
                                    ("valor_modelo", "Valor recibido por el modelo", "num"),
                                    ("aporte_log", "Aporte (log)", "num"), ("factor", "Factor multiplicativo", "num"),
                                    ("efecto", "Efecto", "texto"), ("interpretacion", "Interpretación", "texto")]))
    _html(es.zona("Verificaciones de la API"))
    _html(es.datos([(t, v if not isinstance(v, (list, tuple)) else ", ".join(map(str, v)))
                    for t, v in xp.resumen_verificaciones(x)]))
    with st.expander("Verificaciones completas (JSON)"):
        st.json(x.get("verificaciones") or {})
    imp = x["importancia_global_artefacto"]
    _html(es.zona("Importancia global por ganancia (artefacto de producción)"))
    st.caption(f"{es.md(xp.TEXTO_GANANCIA)} Método: {es.md(imp.get('metodo'))} · versión "
               f"{es.md(imp.get('model_version'))}. {es.md(imp.get('nota'))}")
    variables = xp.filas_importancia_ganancia(x)
    st.vega_lite_chart(gr.barras_horizontales(variables[:15], "proporcion", "variable",
                                              "proporción de la ganancia", formato=".1%"),
                       width="stretch", theme=None)
    with st.expander("Tabla de importancia por ganancia"):
        _mostrar_tabla(_tabla(variables, [("variable", "Variable", "texto"), ("proporcion", "Proporción", "num"),
                                          ("ganancia", "Ganancia", "num"), ("divisiones", "Divisiones", "num")]))
    st.caption("La importancia por permutación (modelo de evaluación) es otra medida: está en la ficha técnica "
               "del modelo.")
    try:
        tabla, meta = ex.tabla_explicacion(x)
        csv = ex.a_csv(tabla)
        xlsx = ex.a_xlsx(tabla, meta)
    except Exception as e:  # noqa: BLE001
        st.error(f"No se generó la exportación de la explicación. {_mensaje_error(e)}")
        return
    st.download_button("Descargar aportes (CSV)", csv, file_name=f"explicacion_{elegido}.csv", mime="text/csv",
                       key="descargar_explicacion_csv")
    st.download_button("Descargar aportes (Excel)", xlsx, file_name=f"explicacion_{elegido}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       key="descargar_explicacion_xlsx")


# =====================================================================================================
# F. RESUMEN
# =====================================================================================================

def _seccion_resumen() -> None:
    conteos = {est: _conteo_alertas({"estado": est}) for est in cl.ESTADOS_ALERTA}
    c1, c2 = st.columns([8, 4])
    with c1, st.container(border=True):
        _html(es.encabezado("Modelo activo", "registro de modelos", "GET /models"))
        if ACTIVO:
            _html(es.datos([("Nombre", ACTIVO.get("nombre")), ("Versión", ACTIVO.get("version")),
                            ("Variables", ACTIVO.get("feature_version")), ("Tipo", ACTIVO.get("tipo")),
                            ("Activado", _fecha(ACTIVO.get("activado_en")) if ACTIVO.get("activado_en") else None),
                            ("Activado por", ACTIVO.get("activado_por"))]))
        else:
            _html(es.vacio("No hay modelo activo o no se pudo consultar el registro."))
        st.caption("El desempeño frente a datos reales se mide en «Evaluación (M1)», por modelo, ruta y horizonte. "
                   "Las métricas registradas al entrenar están en la ficha técnica de «Modelos y explicaciones» y "
                   "no se resumen aquí: algunas provienen de un modelo de evaluación distinto del artefacto activo.")
        if st.button("Ir a Evaluación (M1)", key="ir_m1"):
            st.session_state["_ir_a"] = "Evaluación (M1)"
            st.rerun()
    with c2, st.container(border=True):
        _html(es.encabezado("Operación", None, "GET /models · /alerts"))
        abiertas = conteos.get("abierta")
        gestion = (None if conteos.get("reconocida") is None or conteos.get("asignada") is None
                   else conteos["reconocida"] + conteos["asignada"])
        _html(es.lista([
            ("API", "disponible" if ERROR_MODELOS is None else "no disponible", None,
             None if ERROR_MODELOS is None else es.CORAL),
            ("Modelo activo", ACTIVO.get("version") if ACTIVO else es.SIN_DATO, None, None),
            ("Alertas abiertas", "todas las rutas", es.SIN_DATO if abiertas is None else str(abiertas), es.CORAL),
            ("En gestión", "reconocidas o asignadas", es.SIN_DATO if gestion is None else str(gestion), es.AMBAR)]))
    c1, c2 = st.columns([8, 4])
    with c2, st.container(border=True):
        _html(es.encabezado("Alertas por estado", "todas las rutas", "GET /alerts", "c"))
        colores = {"abierta": es.CORAL, "reconocida": "#F3C98E", "asignada": es.AMBAR, "cerrada": es.VT2,
                   "descartada": es.GRIS}
        if all(v is not None for v in conteos.values()):
            partes = [(k.capitalize(), v, colores[k]) for k, v in conteos.items()]
            st.vega_lite_chart(gr.anillo(partes, str(sum(conteos.values())), "alertas"), width="stretch", theme=None)
            _html(es.leyenda([(n, c, v) for n, v, c in partes]))
        else:
            _html(es.vacio("Sin dato: alguna consulta de alertas falló."))
            st.caption(es.md(st.session_state.get("_ultimo_error_conteo", "")))
        st.caption("Cada número sale de paginacion.total con un filtro de estado.")
    with c1:
        _html(es.zona("Por ruta · alertas"))
        cols = st.columns(3)
        for col, r in zip(cols, NOMBRES, strict=True):
            with col, st.container(border=True):
                _html(es.encabezado(cl.RUTAS[r], f"ruta {r}", "GET /alerts"))
                a = _conteo_alertas({"idruta": r, "estado": "abierta"})
                rec = _conteo_alertas({"idruta": r, "estado": "reconocida"})
                asi = _conteo_alertas({"idruta": r, "estado": "asignada"})
                g = None if rec is None or asi is None else rec + asi
                _html(es.lista([("Alertas abiertas", "", es.SIN_DATO if a is None else str(a), es.CORAL),
                                ("En gestión", "", es.SIN_DATO if g is None else str(g), es.AMBAR)]))


# =====================================================================================================
# despacho
# =====================================================================================================

if seccion == "Resumen":
    _seccion_resumen()
elif seccion == "Demanda y frecuencias":
    _seccion_demanda()
elif seccion == "Alertas":
    _seccion_alertas()
elif seccion == "Calidad de datos":
    _seccion_calidad()
elif seccion == "Evaluación (M1)":
    _seccion_m1()
else:
    _seccion_modelos()
