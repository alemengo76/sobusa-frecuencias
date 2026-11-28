"""Bandeja de consulta derivada de la sección Alertas del tablero local."""
import base64
import os
import re
from time import time as now
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlsplit

import pandas as pd
import streamlit as st

import cliente as cl
import estilo as es

st.set_page_config(page_title="SOBUSA · Alertas", layout="wide")
st.title("Bandeja de alertas de SOBUSA")
allowed = {item.strip().casefold() for item in os.environ.get("SOBUSA_ALLOWED_EMAILS", "").split(",")
           if item.strip()}
credential = os.environ.get("SOBUSA_API_TOKEN", "")
if (not allowed or any("@" not in item or "*" in item for item in allowed)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", credential)):
    st.error("El acceso a la bandeja aún no está configurado.")
    st.stop()

if not st.user.is_logged_in:
    if st.button("Iniciar sesión con Google"):
        st.login()
    st.stop()

email = st.user.get("email", "")
expires = st.user.get("exp")
if (not isinstance(email, str) or email.casefold() not in allowed
        or st.user.get("email_verified") is not True
        or not isinstance(expires, (int, float)) or not expires > now()):
    st.error("Este correo no está autorizado o la sesión expiró.")
    if st.button("Cerrar sesión"):
        st.logout()
    st.stop()

if st.sidebar.button("Cerrar sesión"):
    st.logout()
    st.stop()

url = cl.url_api()
parsed = urlsplit(url)
if parsed.path != "/api/v1" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or (
    parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"))
):
    st.error("Configura API_URL con la URL HTTPS de la API y el sufijo /api/v1.")
    st.stop()

rutas = {2: "2 · SILENCIO", 16: "16 · CRA 54", 21: "21 · CALLE 72"}
bogota = timezone(timedelta(hours=-5))
with st.container(border=True):
    st.markdown(es.encabezado("Filtros", None), unsafe_allow_html=True)
    a, b, c = st.columns(3)
    with a:
        ruta = st.selectbox("Ruta", [None, *rutas], index=2,
                            format_func=lambda x: rutas[x] if x else "Todas")
        desde = st.date_input("Desde (franja)", None)
    with b:
        estado = st.selectbox("Estado", [None, *cl.ESTADOS_ALERTA], index=1,
                              format_func=lambda x: x.capitalize() if x else "Todos")
        hasta = st.date_input("Hasta (inclusive)", None)
    with c:
        severidad = st.selectbox("Severidad", [None, *cl.SEVERIDADES],
                                 format_func=lambda x: x.capitalize() if x else "Todas")
        codigo = st.selectbox("Código", [None, *(f"ALT-{i:03d}" for i in range(1, 11))],
                              format_func=lambda x: x if x else "Todos")

if desde and hasta and desde > hasta:
    st.warning("Desde debe ser anterior o igual a Hasta.")
    st.stop()

filters = {
    "idruta": ruta, "estado": estado, "severidad": severidad, "codigo": codigo,
    "desde": datetime.combine(desde, time.min, bogota).isoformat() if desde else None,
    "hasta": datetime.combine(hasta + timedelta(days=1), time.min, bogota).isoformat() if hasta else None,
}
page = st.number_input("Página", min_value=1, value=1, step=1, key=f"pagina:{filters}")
try:
    alerts = cl.obtener_pagina("alerts", {**filters, "pagina": int(page), "tamano": 50}, credencial=credential)
except cl.ErrorApi as exc:
    st.error(exc.texto())
    st.stop()

st.metric("Total de alertas", alerts.total)
if alerts.datos:
    fields = {
        "alert_id": "ID", "codigo": "Código", "severidad": "Severidad",
        "estado": "Estado", "idruta": "Ruta", "franja_inicio": "Franja",
        "horizonte_min": "Horizonte", "valor_observado": "Observado",
        "valor_esperado": "Esperado", "umbral": "Umbral",
        "puntuacion_anomalia": "Anomalía", "explicacion": "Explicación",
        "recomendacion": "Recomendación",
    }
    st.dataframe(pd.DataFrame(alerts.datos).reindex(columns=list(fields)).rename(columns=fields),
                 hide_index=True, width="stretch")
else:
    st.info("No hay alertas en esta página para los filtros seleccionados.")
st.caption(f"{len(alerts.datos)} filas en esta página. Página {alerts.pagina} de {alerts.paginas}.")

if st.button("Preparar CSV de alertas autorizadas"):
    try:
        content = base64.b64encode(cl.exportar_csv(credential)).decode("ascii")
    except cl.ErrorApi as exc:
        st.error(exc.texto())
    else:
        # El CSV viaja en esta sesión y no se registra en el servidor de archivos de Streamlit.
        st.iframe(f'<a href="data:text/csv;base64,{content}" download="sobusa_alertas_autorizadas.csv">'
                  'Descargar CSV</a>', height=42, alt="Descarga de alertas")
