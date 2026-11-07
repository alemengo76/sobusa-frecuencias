"""Variables históricas con el contrato de disponibilidad D+1.

Contrato D+1: al predecir en el instante t solo se usan resultados de viajes con
salida programada ANTES de las 00:00 del día de t (carga nocturna diaria).

Reemplaza a `historial_retro` del cuaderno de feature engineering como fuente de
predictores: su ventana [t - W, t) incluye viajes del mismo día de t (fuga).
"""
import numpy as np
import pandas as pd

RESOLUCION = pd.Timedelta("15min")
FEATURE_VERSION = "sobusa-fe-2.1-d1"


def validar_malla(s: pd.Series) -> pd.Series:
    s = s.sort_index()
    pasos = s.index.to_series().diff().dropna()
    if not (pasos == RESOLUCION).all():
        raise ValueError("La malla debe ser regular de 15 minutos")
    return s


def features_d1(s: pd.Series, semanas=(4, 8), dias_nivel=28, minimo=2) -> pd.DataFrame:
    """Variables históricas sin fuga bajo D+1, para UNA ruta.

    s: proxy por franja de 15 min. Índice = origen (malla regular, hora local de
       Bogotá). NA = sin medición; no se imputa.
    """
    s = validar_malla(s)
    out = pd.DataFrame(index=s.index)

    # Rezagos exactos: misma franja ayer y hace una semana (días anteriores a t).
    out["lag_1d"] = s.shift(freq="1D").reindex(s.index)
    out["lag_7d"] = s.shift(freq="7D").reindex(s.index)

    # Promedio día-hora: misma franja y día de la semana en las últimas k semanas.
    for k in semanas:
        rezagos = pd.concat(
            [s.shift(freq=f"{7 * i}D").reindex(s.index) for i in range(1, k + 1)], axis=1)
        n = rezagos.notna().sum(axis=1)
        out[f"media_dh_{k}sem"] = rezagos.mean(axis=1).where(n >= minimo)
        out[f"n_dh_{k}sem"] = n.astype("int16")

    # Nivel reciente: media por franja de los últimos días, cerrando el día anterior.
    dia = s.index.normalize()
    diario = s.groupby(dia).mean()
    diario.index = pd.DatetimeIndex(diario.index)
    nivel = diario.rolling(f"{dias_nivel}D", closed="left", min_periods=7).mean()
    out[f"nivel_{dias_nivel}d"] = nivel.reindex(dia).to_numpy()

    # Equivalente D+1 de la antigua ventana de 24 h: el día anterior completo.
    out["media_dia_anterior"] = diario.shift(1, freq="1D").reindex(dia).to_numpy()
    return out


def calendario(idx: pd.DatetimeIndex, festivos=None) -> pd.DataFrame:
    """Variables de calendario conocidas de antemano (autorizadas sin restricción)."""
    x = pd.DataFrame(index=idx)
    minuto_dia = idx.hour * 60 + idx.minute
    x["hora"] = idx.hour
    x["dia_semana_iso"] = idx.dayofweek + 1
    x["mes"] = idx.month
    x["hora_sin"] = np.sin(2 * np.pi * minuto_dia / 1440)
    x["hora_cos"] = np.cos(2 * np.pi * minuto_dia / 1440)
    x["es_fin_semana"] = (idx.dayofweek >= 5).astype("int8")
    if festivos is None:
        try:
            import holidays
            festivos = holidays.Colombia(years=range(idx.year.min(), idx.year.max() + 1))
        except ImportError:
            festivos = {}
    x["es_festivo"] = pd.Index(idx.date).isin(list(festivos)).astype("int8")
    return x


def acumulado_adelante(s: pd.Series, horizonte_min: int) -> pd.Series:
    """Etiqueta: suma de las franjas en [t, t+H). NA si alguna franja falta (no se imputa)."""
    pasos = horizonte_min // 15
    s = validar_malla(s)
    ventana = pd.concat([s.shift(-k) for k in range(pasos)], axis=1)
    return ventana.sum(axis=1, min_count=pasos)
