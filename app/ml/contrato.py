"""Contrato de datos entre componentes (prediction y recommendation).

Congelado el 2026-11-02. Cualquier cambio necesita el acuerdo de las tres personas.
Las pruebas validan este contrato para que una ruptura falle en CI y no en la demo.
"""
import pandas as pd

HORIZONTES = (30, 60, 120)
RUTAS = (2, 16, 21)
FRANJA_MIN = 15
SENTIDO = "CICLO"      # no hay sentido en los datos de Sobusa (ver DECISIONES.md)
SEGMENTO = "TOTAL"     # los segmentos de Extreme son de prueba (ver DECISIONES.md)

COLUMNAS_PREDICCION = {
    "idruta": "int64",
    "sentido": "object",
    "segmento_id": "object",
    "franja_inicio": "datetime64[ns]",
    "franja_minutos": "int64",
    "horizonte_min": "int64",
    "demanda_pred": "float64",
    "limite_inferior": "float64",
    "limite_superior": "float64",
    "nivel_intervalo": "float64",
    "definicion_tiempo": "object",
    "model_version": "object",
    "feature_version": "object",
    "data_cutoff": "datetime64[ns]",
    "latency_ms": "int64",
}

COLUMNAS_RECOMENDACION = [
    "prediction_id", "idruta", "sentido", "segmento_id", "franja_inicio", "horizonte_min",
    "demanda_hora", "base_demanda", "factor_carga_max", "carga_max_estimada_hora",
    "capacidad_nominal", "capacidad_fuente", "ocupacion_objetivo", "capacidad_efectiva",
    "buses_hora_recomendado", "headway_recomendado_min", "ciclo_min",
    "vehiculos_requeridos", "flota_tope", "buses_hora_actual", "headway_actual_min",
    "delta_buses_hora", "es_viable", "motivo_no_viable", "parametros_version",
]


class ContratoInvalido(ValueError):
    pass


def validar_predicciones(df: pd.DataFrame) -> pd.DataFrame:
    faltan = set(COLUMNAS_PREDICCION) - set(df.columns)
    if faltan:
        raise ContratoInvalido(f"Faltan columnas en prediction: {sorted(faltan)}")
    errores = []
    if not df["idruta"].isin(RUTAS).all():
        errores.append("idruta fuera de (2, 16, 21)")
    if not df["horizonte_min"].isin(HORIZONTES).all():
        errores.append("horizonte_min fuera de (30, 60, 120)")
    if not df["sentido"].eq(SENTIDO).all():
        errores.append(f"sentido debe ser '{SENTIDO}'")
    if (df["demanda_pred"] < 0).any():
        errores.append("demanda_pred negativa")
    if not ((df["limite_inferior"] <= df["demanda_pred"])
            & (df["demanda_pred"] <= df["limite_superior"])).all():
        errores.append("se requiere limite_inferior <= demanda_pred <= limite_superior")
    if df[list(COLUMNAS_PREDICCION)].isna().any().any():
        errores.append("hay nulos en columnas obligatorias")
    clave = ["idruta", "sentido", "segmento_id", "franja_inicio", "horizonte_min",
             "model_version", "data_cutoff"]
    if df.duplicated(clave).any():
        errores.append("filas duplicadas para la clave única")
    if errores:
        raise ContratoInvalido("; ".join(errores))
    return df


def validar_recomendaciones(df: pd.DataFrame) -> pd.DataFrame:
    faltan = set(COLUMNAS_RECOMENDACION) - set(df.columns)
    if faltan:
        raise ContratoInvalido(f"Faltan columnas en recommendation: {sorted(faltan)}")
    sin_motivo = (~df["es_viable"]) & df["motivo_no_viable"].isna()
    if sin_motivo.any():
        raise ContratoInvalido("toda recomendación no viable necesita motivo_no_viable")
    return df
