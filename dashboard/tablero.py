"""Tablero mínimo (Streamlit) que consume la API: predicción, recomendación y alertas.

Uso:  streamlit run dashboard/tablero.py   (con la API corriendo en http://localhost:8000)
"""
import os

import pandas as pd
import requests
import streamlit as st

API = os.environ.get("API_URL", "http://localhost:8000/api/v1")
NOMBRES = {2: "2 · SILENCIO", 16: "16 · CRA 54", 21: "21 · CALLE 72"}

st.set_page_config(page_title="Sobusa · Frecuencias", layout="wide")
st.title("Frecuencias de despacho · Sobusa")


def traer(ruta: str, **params) -> pd.DataFrame:
    r = requests.get(f"{API}/{ruta}", params={k: v for k, v in params.items() if v is not None},
                     timeout=10)
    r.raise_for_status()
    return pd.DataFrame(r.json()["datos"])


with st.sidebar:
    idruta = st.selectbox("Ruta", list(NOMBRES), format_func=NOMBRES.get, index=1)
    horizonte = st.radio("Horizonte (min)", [30, 60, 120], index=1, horizontal=True)
    dia = st.date_input("Día", pd.Timestamp("2025-10-01"))
    if st.button("Calcular frecuencias para este día"):
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
