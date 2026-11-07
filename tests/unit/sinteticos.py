"""Viajes sintéticos con la forma de progvehiculos, para probar el GBM sin la base real."""
import numpy as np
import pandas as pd

from app.fe31 import ce, rc
from app.fe31.viajes import a_formato_fe
from app.ml import modelo_gbm as G

INICIO = pd.Timestamp("2025-01-06")


def viajes(dias=84, rutas=(2, 16, 21), semilla=0, inicio=INICIO) -> pd.DataFrame:
    """Salidas cada 12 min de 05:00 a 21:48; pasajeros con perfil de horas pico y fin de semana."""
    rng = np.random.default_rng(semilla)
    filas = []
    ident = 1
    for r in rutas:
        nivel = {2: 40, 16: 70, 21: 30}.get(r, 40)
        for d in range(dias):
            dia = inicio + pd.Timedelta(days=d)
            semana = 0.6 if dia.dayofweek >= 5 else 1.0
            salidas = dia + pd.to_timedelta(np.arange(5 * 60, 22 * 60, 12), unit="min")
            h = salidas.hour + salidas.minute / 60
            perfil = 1 + 1.2 * np.exp(-((h - 7) ** 2) / 2) + 0.8 * np.exp(-((h - 17.5) ** 2) / 3)
            pax = rng.poisson(nivel * perfil * semana)
            n = len(salidas)
            filas.append(pd.DataFrame({
                "idprogramacion": np.arange(ident, ident + n), "idruta": r, "idvehiculo": "V",
                "fechasalida": salidas, "fechainicio": salidas + pd.Timedelta(minutes=2),
                "fechafin": salidas + pd.Timedelta(hours=4), "finalizado": "S", "cancelada": "N",
                "numeropasajeros": pax.astype(float), "numerobajadas": pax.astype(float)}))
            ident += n
    return pd.concat(filas, ignore_index=True)


def artefacto(crudo: pd.DataFrame, max_iter=40) -> tuple[G.Artefacto, pd.DataFrame]:
    """Entrena un GBM pequeño con el mismo pipeline del script de entrenamiento."""
    P = a_formato_fe(crudo)
    X, hor, _ = G.preparar(P, P.salida_programada.min(), P.salida_programada.max())
    E = X[X.habilitada_supervisada & X.y_media_4_semanas.notna()]
    m = G.entrenar(E, max_iter=max_iter)
    pe = E.assign(demanda_pred=G.predecir_matriz(m, E))
    intervalos = G.ajustar_intervalos(pd.DataFrame({
        "idruta": pe.id_ruta.astype(int), "horizonte_min": pe.horizonte_min.astype(int),
        "franja_inicio": pe.origen, "demanda_real": pe.y_pasajeros, "demanda_pred": pe.demanda_pred}))
    return G.Artefacto(m, hor, intervalos, {"version": "gbm_fe31_prueba"}), X


__all__ = ["viajes", "artefacto", "ce", "rc"]
