"""M1: consultas de solo lectura sobre ai_frecuencias para comparar predicción contra real.

Todas corren dentro de consultar_resultados() (transacción READ ONLY). No leen tablas de Extreme:
lo real sale de demanda_franja, que la ingesta construye con las reglas comun-1.5.
"""
from datetime import datetime

import pandas as pd
from sqlalchemy import text

LIMITE_PREDICCIONES = 300_000

_SELECT_PRED = """
SELECT p.prediction_id, p.execution_id::text AS execution_id, p.idruta, p.sentido, p.segmento_id,
       p.franja_inicio, p.horizonte_min, p.demanda_pred, p.limite_inferior, p.limite_superior,
       p.nivel_intervalo, p.model_id, p.model_version, p.feature_version, p.data_cutoff,
       p.generated_at, m.version AS registro_version, m.feature_version AS registro_feature_version,
       m.nombre AS modelo_nombre, m.estado AS modelo_estado
FROM ai_frecuencias.prediction p
JOIN ai_frecuencias.model_registry m ON m.model_id = p.model_id
"""


def _filtros(model_id, execution_id, idruta, horizonte_min, desde, hasta, execution_ids=None):
    cond, params = [], {}
    if execution_ids is not None:
        cond.append("p.execution_id = ANY(CAST(:execution_ids AS uuid[]))")
        params["execution_ids"] = [str(e) for e in execution_ids]
    if model_id is not None:
        cond.append("p.model_id = :model_id")
        params["model_id"] = int(model_id)
    if execution_id is not None:
        cond.append("p.execution_id = CAST(:execution_id AS uuid)")
        params["execution_id"] = str(execution_id)
    if idruta is not None:
        cond.append("p.idruta = :idruta")
        params["idruta"] = int(idruta)
    if horizonte_min is not None:
        cond.append("p.horizonte_min = :horizonte_min")
        params["horizonte_min"] = int(horizonte_min)
    if desde is not None:
        cond.append("p.franja_inicio >= :desde")
        params["desde"] = desde
    if hasta is not None:
        cond.append("p.franja_inicio < :hasta")
        params["hasta"] = hasta
    return (" WHERE " + " AND ".join(cond)) if cond else "", params


def contar_predicciones(c, model_id=None, execution_id=None, idruta=None, horizonte_min=None,
                        desde: datetime | None = None, hasta: datetime | None = None,
                        execution_ids=None) -> int:
    where, params = _filtros(model_id, execution_id, idruta, horizonte_min, desde, hasta, execution_ids)
    return int(c.execute(text("SELECT count(*) FROM ai_frecuencias.prediction p" + where),
                         params).scalar())


def predicciones(c, model_id=None, execution_id=None, idruta=None, horizonte_min=None,
                 desde: datetime | None = None, hasta: datetime | None = None,
                 execution_ids=None) -> pd.DataFrame:
    where, params = _filtros(model_id, execution_id, idruta, horizonte_min, desde, hasta, execution_ids)
    filas = c.execute(text(_SELECT_PRED + where + " ORDER BY p.prediction_id"), params).mappings().all()
    return pd.DataFrame([dict(f) for f in filas])


def franjas_reales(c, idrutas: list[int], desde: datetime, hasta: datetime) -> pd.DataFrame:
    filas = c.execute(text("""SELECT idruta, franja_inicio, pasajeros, programaciones,
                                     programaciones_validas
                              FROM ai_frecuencias.demanda_franja
                              WHERE idruta = ANY(:r) AND franja_inicio >= :d AND franja_inicio < :h
                              ORDER BY idruta, franja_inicio"""),
                      {"r": [int(r) for r in idrutas], "d": desde, "h": hasta}).mappings().all()
    cols = ["idruta", "franja_inicio", "pasajeros", "programaciones", "programaciones_validas"]
    return pd.DataFrame([dict(f) for f in filas], columns=cols)


def marca_agua(c) -> dict | None:
    f = c.execute(text("SELECT ultima_actualizacion, lote, actualizada_en FROM ai_frecuencias.watermark "
                       "WHERE tabla_origen = 'progvehiculos'")).mappings().first()
    return dict(f) if f else None


def ejecuciones_prediccion_de_modelo(c, model_id: int) -> list[dict]:
    """Ejecuciones de predicción exitosas en que `model_id` era el modelo ACTIVO. Incluye las que
    cayeron al respaldo (sus filas llevan el model_id de la línea base). Las ejecuciones anteriores al
    GBM no tienen `modelo_activo` en el resumen: ahí el activo es `model_id`."""
    filas = c.execute(text("""
        SELECT execution_id::text AS execution_id, resumen
        FROM ai_frecuencias.execution_log
        WHERE tipo = 'prediccion' AND estado = 'exitosa'
          AND COALESCE(resumen->>'modelo_activo', resumen->>'model_id') = :m
        ORDER BY iniciada_en, execution_id"""), {"m": str(int(model_id))}).mappings().all()
    return [dict(f) for f in filas]
