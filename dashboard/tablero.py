"""Tablero mínimo (Streamlit) que consume la API: predicción, recomendación y alertas.

Uso:  streamlit run dashboard/tablero.py   (con la API corriendo en http://localhost:8000)
"""
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

try:                                   # pytest y python -m importan el paquete dashboard
    from dashboard import exportacion as ex
except ModuleNotFoundError:            # streamlit run agrega dashboard/ a sys.path, no la raíz
    import exportacion as ex

API = os.environ.get("API_URL", "http://localhost:8000/api/v1")
NOMBRES = {2: "2 · SILENCIO", 16: "16 · CRA 54", 21: "21 · CALLE 72"}

# Estado de la exportación de alertas en st.session_state
CLAVE_EXPORTACION = "exportacion_alertas"
CLAVE_AVISO_EXPORTACION = "exportacion_alertas_aviso"
BOGOTA = timezone(timedelta(hours=-5))   # solo para mostrar la hora de preparación

st.set_page_config(page_title="Sobusa · Frecuencias", layout="wide")
st.title("Frecuencias de despacho · Sobusa")


def traer(ruta: str, **params) -> pd.DataFrame:
    r = requests.get(f"{API}/{ruta}", params={k: v for k, v in params.items() if v is not None},
                     timeout=10)
    r.raise_for_status()
    return pd.DataFrame(r.json()["datos"])


def _get(ruta: str, params) -> dict:
    """Adaptador HTTP para exportacion.traer_todo: lanza ante errores HTTP y devuelve el JSON."""
    r = requests.get(f"{API}/{ruta}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _invalidar_exportacion(motivo: str) -> None:
    """Descarta la exportación preparada (si había una) y deja el motivo para mostrarlo."""
    if st.session_state.pop(CLAVE_EXPORTACION, None) is not None:
        st.session_state[CLAVE_AVISO_EXPORTACION] = motivo


def _mensaje_error(e: Exception) -> str:
    """Texto para el usuario, sin URL ni traza."""
    causa = e.__cause__
    if isinstance(causa, requests.Timeout):
        return f"{e} — la API no respondió a tiempo."
    if isinstance(causa, requests.ConnectionError):
        return f"{e} — no se pudo conectar con la API."
    if isinstance(causa, requests.HTTPError):
        codigo = getattr(causa.response, "status_code", "desconocido")
        return f"{e} — la API respondió con el código {codigo}."
    if isinstance(e, (ex.ErrorExportacion, ValueError)):
        return str(e)
    return f"Error inesperado ({type(e).__name__})."


def _exportar_alertas(idruta: int, estado: str) -> None:
    """Prepara (solo al pulsar el botón) y ofrece la exportación completa de las alertas filtradas."""
    filtros = {"idruta": idruta, "estado": estado}
    clave = (idruta, estado)
    guardada = st.session_state.get(CLAVE_EXPORTACION)
    if guardada is not None and guardada["clave"] != clave:
        _invalidar_exportacion("Los filtros cambiaron: se descartó la exportación preparada.")
        guardada = None

    st.subheader("Exportar alertas")
    st.caption(f"Incluye todas las alertas de la ruta {NOMBRES[idruta]} en estado «{estado}», no solo "
               "las que muestra la tabla.")
    if st.button("Preparar exportación", key="preparar_exportacion_alertas"):
        st.session_state.pop(CLAVE_EXPORTACION, None)          # nunca queda un archivo anterior
        st.session_state.pop(CLAVE_AVISO_EXPORTACION, None)
        guardada = None
        try:
            with st.spinner("Preparando la exportación: leyendo todas las páginas…"):
                res = ex.traer_todo(_get, ex.ALERTAS, filtros)
                csv = ex.a_csv(res.datos, columna_id=ex.ALERTAS.columna_id)
                xlsx = ex.a_xlsx(res.datos, ex.metadatos(res), columna_id=ex.ALERTAS.columna_id)
        except Exception as e:  # noqa: BLE001 - cualquier fallo: ningún archivo y mensaje claro
            st.error(f"No se generó ningún archivo. {_mensaje_error(e)}")
        else:
            guardada = {"clave": clave, "total": res.total, "advertencias": res.advertencias,
                        "csv": csv, "xlsx": xlsx, "preparada_en": datetime.now(BOGOTA)}
            st.session_state[CLAVE_EXPORTACION] = guardada

    aviso = st.session_state.get(CLAVE_AVISO_EXPORTACION)
    if guardada is None:
        if aviso:
            st.info(aviso)
        return
    st.success(f"Exportación lista: {guardada['total']} alertas "
               f"(preparada a las {guardada['preparada_en']:%H:%M:%S}).")
    for advertencia in guardada["advertencias"]:
        st.warning(advertencia)
    st.caption(ex.AVISO_INSTANTANEA)
    nombre = f"alertas_ruta{idruta}_{estado}"
    st.download_button("Descargar CSV", guardada["csv"], file_name=f"{nombre}.csv",
                       mime="text/csv", key="descargar_alertas_csv")
    st.download_button("Descargar Excel", guardada["xlsx"], file_name=f"{nombre}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       key="descargar_alertas_xlsx")


with st.sidebar:
    idruta = st.selectbox("Ruta", list(NOMBRES), format_func=NOMBRES.get, index=1)
    horizonte = st.radio("Horizonte (min)", [30, 60, 120], index=1, horizontal=True)
    dia = st.date_input("Día", pd.Timestamp("2025-10-01"))
    if st.button("Calcular frecuencias para este día"):
        _invalidar_exportacion("Se calcularon frecuencias: se descartó la exportación preparada.")
        r = requests.post(f"{API}/frequency/executions",
                          json={"dia": str(dia), "idrutas": [idruta]}, timeout=120)
        st.write(r.json())

pestanas = st.tabs(["Demanda prevista", "Frecuencia recomendada", "Alertas"])

with pestanas[0]:
    p = traer("predictions", idruta=idruta, horizonte_min=horizonte, tamano=500)
    if p.empty:
        st.info("Sin predicciones todavía.")
    else:
        p["franja_inicio"] = pd.to_datetime(p.franja_inicio)
        p = p.sort_values("franja_inicio").set_index("franja_inicio")
        st.line_chart(p[["limite_inferior", "demanda_pred", "limite_superior"]].astype(float))
        st.caption(f"Modelo {p.model_version.iloc[-1]} · datos hasta {p.data_cutoff.iloc[-1]}")

with pestanas[1]:
    r = traer("recommendations", idruta=idruta, horizonte_min=horizonte, tamano=500)
    if r.empty:
        st.info("Sin recomendaciones todavía.")
    else:
        r["franja_inicio"] = pd.to_datetime(r.franja_inicio)
        r = r.sort_values("franja_inicio")
        st.bar_chart(r.set_index("franja_inicio")[["buses_hora_actual", "buses_hora_recomendado"]]
                     .astype(float))
        no_viables = r[~r.es_viable]
        if len(no_viables):
            st.warning(f"{len(no_viables)} franjas no viables")
            st.dataframe(no_viables[["franja_inicio", "vehiculos_requeridos", "flota_tope",
                                     "motivo_no_viable"]])

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
                _invalidar_exportacion(
                    "Se intentó actualizar una alerta: se descartó la exportación preparada."
                )

                try:
                    r = requests.patch(
                        f"{API}/alerts/{aid}/status",
                        json={
                            "estado": nuevo,
                            "usuario": usuario,
                            "comentario": comentario or None,
                            "usuario_asignado": asignado or None,
                        },
                        timeout=10,
                    )
                    r.raise_for_status()
                    resultado = r.json()

                except requests.Timeout:
                    st.error(
                        "No se pudo confirmar la actualización. "
                        "Revise el estado de la alerta antes de reintentar."
                    )

                except requests.ConnectionError:
                    st.error(
                        "No se pudo confirmar la actualización. "
                        "Revise el estado de la alerta antes de reintentar."
                    )

                except requests.HTTPError as e:
                    respuesta = e.response
                    codigo = respuesta.status_code if respuesta is not None else "desconocido"

                    mensajes = {
                        404: "La alerta solicitada no existe.",
                        409: "La operación no está permitida por el estado actual de la alerta.",
                        422: "Los datos enviados no cumplen las validaciones.",
                        500: "La API encontró un error interno.",
                    }

                    texto_error = (
                        f"No se pudo actualizar la alerta (HTTP {codigo}). "
                        f"{mensajes.get(codigo, 'La API rechazó la solicitud.')}"
                    )

                    cuerpo_error = {}
                    if respuesta is not None:
                        try:
                            cuerpo_error = respuesta.json()
                        except requests.exceptions.JSONDecodeError:
                            pass

                    error_api = (
                        cuerpo_error.get("error", {})
                        if isinstance(cuerpo_error, dict)
                        else {}
                    )
                    if isinstance(error_api, dict):
                        mensaje_api = error_api.get("mensaje")
                        correlation_id = error_api.get("correlation_id")

                        if mensaje_api:
                            texto_error += f" Mensaje de la API: {mensaje_api}"
                        if correlation_id:
                            texto_error += f" Identificador de seguimiento: {correlation_id}"

                    st.error(texto_error)

                except requests.exceptions.JSONDecodeError:
                    st.error(
                        f"La API respondió con HTTP {r.status_code}, pero no se pudo leer su respuesta. "
                        "La actualización pudo haberse aplicado. "
                        "Revise el estado de la alerta antes de reintentar."
                    )

                except requests.RequestException:
                    st.error("Ocurrió un problema de comunicación con la API.")

                except ValueError:
                    st.error("Ocurrió un error al procesar la respuesta de la API.")

                else:
                    st.success("La alerta se actualizó correctamente.")
                    st.json(resultado)
    # Después del formulario: si se acaba de guardar una alerta, la exportación ya se descartó
    _exportar_alertas(idruta, estado)
