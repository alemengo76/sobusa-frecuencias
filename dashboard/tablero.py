"""Tablero mínimo (Streamlit) que consume la API: predicción, recomendación y alertas.

Predicción (RF-006) y recomendación (RF-015) muestran un solo día, una sola ruta, un solo horizonte,
una sola ejecución y un solo modelo a la vez, con los valores tal como los devuelve la API.

Uso:  streamlit run dashboard/tablero.py   (con la API corriendo en http://localhost:8000)
"""
import os

import pandas as pd
import requests
import streamlit as st

try:                                   # pytest y python -m importan el paquete dashboard
    from dashboard import explicacion as xp
except ModuleNotFoundError:            # streamlit run agrega dashboard/ a sys.path, no la raíz
    import explicacion as xp

API = os.environ.get("API_URL", "http://localhost:8000/api/v1")
NOMBRES = {2: "2 · SILENCIO", 16: "16 · CRA 54", 21: "21 · CALLE 72"}

st.set_page_config(page_title="Sobusa · Frecuencias", layout="wide")
st.title("Frecuencias de despacho · Sobusa")


def traer(ruta: str, **params) -> pd.DataFrame:
    r = requests.get(f"{API}/{ruta}", params={k: v for k, v in params.items() if v is not None},
                     timeout=10)
    r.raise_for_status()
    return pd.DataFrame(r.json()["datos"])


def _get(ruta: str, params) -> dict:
    """Adaptador HTTP de solo lectura: lanza ante errores HTTP y devuelve el JSON."""
    r = requests.get(f"{API}/{ruta}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _leer(ruta: str, params: dict, columna_id: str) -> list[dict] | None:
    """Todas las filas, o None (con el error ya mostrado) si la lectura falla o no es coherente."""
    try:
        return xp.leer_paginas(_get, ruta, params, columna_id)
    except xp.ErrorLectura as e:
        st.error(f"No se pudieron leer {ruta}: {e}")
    except requests.exceptions.JSONDecodeError:
        st.error(f"No se pudieron leer {ruta}: la API no devolvió JSON válido.")
    except requests.RequestException as e:
        st.error(f"No se pudieron leer {ruta}: error de comunicación con la API ({type(e).__name__}).")
    except ValueError:
        st.error(f"No se pudieron leer {ruta}: la API no devolvió JSON válido.")
    return None


def _ejecucion(eid: str) -> dict | None:
    """GET /frequency/executions/{id}; None (con aviso) si no se puede consultar. Con un id que no es
    un UUID no se hace la consulta."""
    if xp.id_ejecucion(eid) is None:
        st.warning(f"Identificador de ejecución no válido ({eid!r}); no se consulta la API.")
        return None
    try:
        return _get(f"frequency/executions/{eid}", {})
    except (requests.RequestException, ValueError) as e:
        st.warning(f"No se pudo consultar la ejecución {eid} ({type(e).__name__}).")
        return None


def _elegir_ejecucion(grupo, etiqueta: str, clave: str) -> str | None:
    """Una sola ejecución: la más reciente por defecto, o la que elija el usuario si hay varias.
    None (con aviso) si ninguna fila tiene execution_id válido."""
    if grupo.excluidas:
        st.warning(f"Se excluyeron {grupo.excluidas} filas sin execution_id válido (nulo, ausente, "
                   "vacío o no UUID): no pertenecen a ninguna ejecución y no se muestran.")
    if not grupo.lista:
        st.warning("Ninguna fila tiene un execution_id válido: no hay ejecución que mostrar.")
        return None
    if not grupo.por_fecha:
        st.warning(f"No se pudieron ordenar las ejecuciones por fecha ({grupo.motivo}); se listan por "
                   "identificador.")
    if len(grupo.lista) == 1:
        return grupo.lista[0].execution_id
    st.warning(f"Hay {len(grupo.lista)} ejecuciones con datos para este día; se muestra solo una.")
    etiquetas = {e.execution_id: f"{e.execution_id} · generada "
                                 f"{e.generada_hasta or 'sin fecha comparable'} · {e.filas} filas"
                 for e in grupo.lista}
    return st.selectbox(etiqueta, list(etiquetas), key=clave, format_func=etiquetas.get)


def _sin_otros_filtros(filas: list[dict]) -> list[dict]:
    """Descarta (y avisa) filas de otra ruta u horizonte; la API ya filtra, es una defensa."""
    ajenas = xp.fuera_de_filtro(filas, idruta, horizonte)
    if ajenas:
        st.warning(f"Se descartaron {ajenas} filas de otra ruta u horizonte.")
    return [f for f in filas if str(f.get("idruta")) == str(idruta)
            and str(f.get("horizonte_min")) == str(horizonte)]


def _hay_duplicadas(filas: list[dict], campos: tuple, columnas: tuple, tipo: str) -> bool:
    """True (con aviso y la tabla de filas afectadas) si dos filas ocupan la misma franja."""
    duplicadas = xp.franjas_duplicadas(filas, campos, columnas)
    if not duplicadas:
        return False
    st.warning(f"{len(duplicadas)} {tipo} comparten franja (mismo instante) en esta selección. No se "
               "grafican ni se explican, y no se elige ninguna; revise las filas afectadas:")
    st.dataframe(pd.DataFrame(duplicadas, columns=list(columnas)))
    return True


def _por_franja(df: pd.DataFrame) -> pd.DataFrame | None:
    """df ordenado e indexado por franja_inicio, o None (con aviso) si las fechas no son homogéneas.

    Todas con zona: se expresan en hora de Bogotá (mismo instante, aunque lleguen con desplazamientos
    distintos). Ninguna con zona: se dejan como llegaron. Mezcla o ilegibles: no se grafican."""
    instantes = [xp.instante(v) for v in df.franja_inicio]
    if any(t is None for t in instantes) or len({xp.con_zona(t) for t in instantes}) > 1:
        st.warning("Las franjas mezclan fechas con y sin zona horaria o no son legibles; no se grafican.")
        return None
    if instantes and xp.con_zona(instantes[0]):
        franjas = pd.DatetimeIndex([t.tz_convert(xp.ZONA_DIA) for t in instantes])
    else:
        franjas = pd.DatetimeIndex(instantes)
    return df.assign(franja_inicio=franjas).sort_values("franja_inicio").set_index("franja_inicio")


def mostrar_predicciones(filtros: dict) -> None:
    """RF-006: predicción puntual, límites, nivel y versión del modelo de una sola ejecución y modelo."""
    filas = _leer("predictions", filtros, "prediction_id")
    if filas is None:
        return
    filas = _sin_otros_filtros(filas)
    if not filas:
        st.info(f"Sin predicciones para la ruta {NOMBRES[idruta]}, horizonte {horizonte} min, "
                f"día {dia_txt}.")
        return
    grupo = xp.separar_ejecuciones(filas)
    eid = _elegir_ejecucion(grupo, "Ejecución de predicción", "ejecucion_prediccion")
    if eid is None:
        return
    de_eid = xp.de_ejecucion(filas, eid)
    otras_ejecuciones, filas = len(filas) - len(de_eid) - grupo.excluidas, de_eid

    ocultas: list[dict] = []
    modelos = xp.modelos(filas)
    if len(modelos) > 1:
        st.warning("Esta ejecución tiene filas de más de un modelo (model_id y versión); se muestra "
                   "un modelo a la vez.")
        # La clave es la pareja (model_id, model_version): un mismo id puede tener varias versiones
        etiquetas = {(mid, ver): f"{ver} (model_id {mid}, {n} filas)" for (mid, ver), n in modelos}
        modelo = st.selectbox("Modelo", list(etiquetas), format_func=etiquetas.get,
                              key="modelo_prediccion")
        ocultas = xp.ocultas_por_modelo(filas, modelo)
        filas = xp.de_modelo(filas, modelo)
    st.caption(f"Ejecución de predicciones: {eid} · {len(filas)} franjas mostradas · descartadas: "
               f"{otras_ejecuciones} filas de otras ejecuciones y {len(ocultas)} de otros modelos.")
    if ocultas:
        st.warning(f"{len(ocultas)} franjas de esta ejecución quedan fuera por ser de otro modelo:")
        st.dataframe(pd.DataFrame(ocultas))
    if _hay_duplicadas(filas, xp.CLAVE_FRANJA_PREDICCION, xp.COLUMNAS_DUPLICADO_PREDICCION,
                       "predicciones"):
        return                    # sin gráfico ni más consultas a la API

    ejecucion = _ejecucion(eid)
    respaldo = xp.respaldo_registrado(ejecucion)
    if respaldo is not None:
        st.write("Modelo y respaldo registrados en el resumen de la ejecución:")
        st.json(respaldo)
    elif ejecucion is not None:
        st.caption("El resumen de la ejecución no registra el modelo activo ni el respaldo.")

    p = _por_franja(pd.DataFrame(filas))
    if p is not None:
        st.line_chart(p[["limite_inferior", "demanda_pred", "limite_superior"]].astype(float))

    unicos = {c: xp.valores_unicos(filas, c) for c in
              ("model_version", "model_id", "feature_version", "data_cutoff", "nivel_intervalo")}
    st.caption(" · ".join(f"{c}: {', '.join(str(v) for v in vs)}" for c, vs in unicos.items()))
    for c in ("data_cutoff", "nivel_intervalo", "feature_version"):
        if len(unicos[c]) > 1:
            st.warning(f"{c} no es único en esta ejecución: {', '.join(str(v) for v in unicos[c])}.")

    conteo = xp.resumen_intervalos(filas)
    st.dataframe(pd.DataFrame([{"clasificación": xp.ETIQUETA_INTERVALO[c], "franjas": n,
                                "significado": xp.DESCRIPCION_INTERVALO[c]}
                               for c, n in conteo.items()]))
    if conteo.get(xp.COLAPSADO):
        st.warning(f"{conteo[xp.COLAPSADO]} franjas con intervalo colapsado: no informan "
                   "incertidumbre.")
    for limitacion in xp.LIMITACIONES_PREDICCION:
        st.caption(limitacion)
    columnas = ["franja_inicio", "demanda_pred", "limite_inferior", "limite_superior",
                "nivel_intervalo", "model_version"]
    st.dataframe(pd.DataFrame(filas)[columnas])


def _predicciones_asociadas(eid: str, filtros: dict) -> tuple[str | None, list[dict], str | None]:
    """(execution_id de predicción, sus filas, motivo si no hay filas utilizables). Nunca usa filas de
    otra ejecución."""
    ejecucion = _ejecucion(eid)
    if ejecucion is None:
        return None, [], "no se pudo consultar la ejecución de recomendaciones"
    eid_pred = xp.ejecucion_prediccion_asociada(ejecucion)
    if eid_pred is None:
        st.warning(f"La ejecución {eid} no indica de qué ejecución de predicciones salió (o el "
                   "identificador no es válido).")
        return None, [], "la ejecución de recomendaciones no indica su ejecución de predicciones"
    leidas = _leer("predictions", {**filtros, "execution_id": eid_pred}, "prediction_id")
    if leidas is None:
        return eid_pred, [], "no se pudieron leer las predicciones de la ejecución asociada"
    propias = xp.de_ejecucion(_sin_otros_filtros(leidas), eid_pred)
    if len(propias) != len(leidas):
        st.warning(f"Se descartaron {len(leidas) - len(propias)} predicciones que no son de la "
                   f"ejecución {eid_pred}.")
    if not propias:
        st.warning(f"La ejecución de predicciones asociada ({eid_pred}) no tiene predicciones "
                   "guardadas para esta ruta, horizonte y día. No se completan con datos de otra "
                   "ejecución.")
        return eid_pred, [], xp.SIN_PREDICCIONES
    return eid_pred, propias, None


def mostrar_recomendaciones(filtros: dict) -> None:
    """RF-008 (actual vs recomendada) y RF-015 (explicación) de una sola ejecución."""
    filas = _leer("recommendations", filtros, "recommendation_id")
    if filas is None:
        return
    filas = _sin_otros_filtros(filas)
    if not filas:
        st.info(f"Sin recomendaciones para la ruta {NOMBRES[idruta]}, horizonte {horizonte} min, "
                f"día {dia_txt}.")
        return
    grupo = xp.separar_ejecuciones(filas)
    eid = _elegir_ejecucion(grupo, "Ejecución de recomendación", "ejecucion_recomendacion")
    if eid is None:
        return
    de_eid = xp.de_ejecucion(filas, eid)
    otras_ejecuciones, filas = len(filas) - len(de_eid) - grupo.excluidas, de_eid
    if _hay_duplicadas(filas, xp.CLAVE_FRANJA_RECOMENDACION, xp.COLUMNAS_DUPLICADO_RECOMENDACION,
                       "recomendaciones"):
        st.caption(f"Ejecución de recomendaciones: {eid} · descartadas: {otras_ejecuciones} filas de "
                   "otras ejecuciones · ejecución de predicciones asociada: no consultada (datos "
                   "ambiguos).")
        return                    # sin gráfico, sin explicación y sin consultar la ejecución
    eid_pred, predicciones, motivo = _predicciones_asociadas(eid, filtros)
    st.caption(f"Ejecución de recomendaciones: {eid} · {len(filas)} franjas mostradas · descartadas: "
               f"{otras_ejecuciones} filas de otras ejecuciones · ejecución de predicciones asociada: "
               f"{eid_pred or 'no disponible'}.")

    r = _por_franja(pd.DataFrame(filas))
    if r is not None:
        st.bar_chart(r[["buses_hora_actual", "buses_hora_recomendado"]].astype(float))
    no_viables = [f for f in filas if f.get("es_viable") is False]
    if no_viables:
        st.warning(f"{len(no_viables)} franjas no viables")
        st.dataframe(pd.DataFrame(no_viables)[["franja_inicio", "vehiculos_requeridos", "flota_tope",
                                               "motivo_no_viable"]])

    st.subheader("Explicación de la recomendación")
    # Franjas por instante normalizado (nunca por texto); los duplicados ya se descartaron arriba
    claves = [(xp.clave_instante(f.get("franja_inicio")), f) for f in filas]
    if any(k is None for k, _ in claves):
        st.warning("Hay franjas que no son fechas legibles; no se explica para no elegir una "
                   "arbitrariamente.")
        return
    por_franja = {k: f for k, f in sorted(claves, key=lambda kf: kf[0])}
    if len(por_franja) != len(filas):
        st.warning("La ejecución tiene más de una recomendación para la misma franja; no se explica "
                   "para no elegir una arbitrariamente.")
        return
    etiquetas = {k: str(f.get("franja_inicio")) for k, f in por_franja.items()}
    franja = st.selectbox("Franja a explicar", list(por_franja), key="franja_explicada",
                          format_func=etiquetas.get)
    rec = por_franja[franja]
    asociacion = xp.prediccion_para(rec, predicciones) if motivo is None else xp.Asociacion(None, motivo)
    if asociacion.prediccion is not None:
        st.caption(f"Predicción de entrada: prediction_id {asociacion.prediccion.get('prediction_id')} "
                   f"de la ejecución {eid_pred}.")
    elif motivo is None:          # los motivos de la ejecución ya se avisaron arriba
        st.warning("No se asoció una predicción a esta franja: "
                   f"{xp.MOTIVO_NO_ASOCIADA.get(asociacion.motivo, asociacion.motivo)}.")
    exp = xp.explicar_recomendacion(rec, asociacion.prediccion, asociacion.motivo)
    for linea in exp["regla"]:
        st.write(linea)
    if exp["motivo"]:
        st.warning(f"Motivo de no viabilidad: {exp['motivo']}")
    st.dataframe(pd.DataFrame(exp["pasos"]))
    st.caption("Valores tal como los devolvió la API; el tablero no los recalcula.")
    st.caption("No disponibles en la API: " + " ".join(exp["no_disponibles"]))
    st.caption("Las variables son las de la regla de recomendación. La API no ofrece explicación "
               "local de las variables del modelo de demanda.")
    st.caption(xp.ALCANCE_RF015)


with st.sidebar:
    idruta = st.selectbox("Ruta", list(NOMBRES), format_func=NOMBRES.get, index=1)
    horizonte = st.radio("Horizonte (min)", [30, 60, 120], index=1, horizontal=True)
    dia = st.date_input("Día", pd.Timestamp("2025-10-01"))
    if st.button("Calcular frecuencias para este día"):
        r = requests.post(f"{API}/frequency/executions",
                          json={"dia": str(dia), "idrutas": [idruta]}, timeout=120)
        st.write(r.json())

pestanas = st.tabs(["Demanda prevista", "Frecuencia recomendada", "Alertas"])
desde, hasta = xp.rango_dia(dia)
dia_txt = desde[:10]
filtros_dia = {"idruta": idruta, "horizonte_min": horizonte, "desde": desde, "hasta": hasta}

with pestanas[0]:
    mostrar_predicciones(filtros_dia)

with pestanas[1]:
    mostrar_recomendaciones(filtros_dia)

with pestanas[2]:
    estado = st.selectbox("Estado", ["abierta", "reconocida", "asignada", "cerrada", "descartada"])
    a = traer("alerts", idruta=idruta, estado=estado, tamano=200)
    if a.empty:
        st.success("Sin alertas en este estado.")
    else:
        st.dataframe(a[["alert_id", "codigo", "severidad", "franja_inicio", "valor_observado",
                        "valor_esperado", "explicacion", "recomendacion", "usuario_asignado"]])
        with st.form("gestion"):
            aid = st.selectbox("Alerta", a.alert_id)
            nuevo = st.selectbox("Nuevo estado", ["reconocida", "asignada", "cerrada", "descartada"])
            usuario = st.text_input("Usuario")
            asignado = st.text_input("Asignar a (si aplica)")
            comentario = st.text_area("Comentario")
            if st.form_submit_button("Guardar"):
                r = requests.patch(f"{API}/alerts/{aid}/status", json={
                    "estado": nuevo, "usuario": usuario, "comentario": comentario or None,
                    "usuario_asignado": asignado or None}, timeout=10)
                st.write(r.json())
