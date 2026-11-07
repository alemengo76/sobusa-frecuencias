"""Predicciones falsas que cumplen el contrato, para trabajar en paralelo mientras A
termina el modelo. Se reemplazan por la línea base sin cambiar el resto del código."""
import numpy as np
import pandas as pd

from app.ml import contrato


def generar_predicciones_falsas(seed: int = 0, inicio="2025-10-01 05:00", horas: int = 18,
                                rutas=contrato.RUTAS) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    franjas = pd.date_range(inicio, periods=horas * 4, freq="15min")
    filas = []
    for idruta in rutas:
        nivel = {2: 120, 16: 260, 21: 90}.get(idruta, 100)
        for t in franjas:
            perfil = 1 + 0.8 * np.exp(-((t.hour + t.minute / 60 - 7) ** 2) / 2) \
                       + 0.6 * np.exp(-((t.hour + t.minute / 60 - 17.5) ** 2) / 3)
            for h in contrato.HORIZONTES:
                media = nivel * perfil * h / 60
                pred = float(rng.gamma(20, media / 20))
                filas.append({
                    "idruta": idruta, "sentido": contrato.SENTIDO,
                    "segmento_id": contrato.SEGMENTO, "franja_inicio": t,
                    "franja_minutos": contrato.FRANJA_MIN, "horizonte_min": h,
                    "demanda_pred": round(pred, 2),
                    "limite_inferior": round(pred * 0.8, 2),
                    "limite_superior": round(pred * 1.25, 2),
                    "nivel_intervalo": 0.80, "definicion_tiempo": "salida_programada",
                    "model_version": "falso_v0", "feature_version": "falso",
                    "data_cutoff": t.normalize(), "latency_ms": 1,
                })
    return pd.DataFrame(filas)
