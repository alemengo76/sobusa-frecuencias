"""Consultas a ai_frecuencias (escritura) y a las tablas fuente (solo lectura).

Todas las consultas usan parámetros enlazados (nunca f-strings con datos del usuario).
"""
import json
import uuid
from datetime import datetime

import pandas as pd
from sqlalchemy import text

# ------------------------------------------------------------------ ejecuciones

def crear_ejecucion(c, tipo: str, parametros: dict, correlation_id: str) -> str:
    eid = str(uuid.uuid4())
    c.execute(text("""INSERT INTO execution_log (execution_id, tipo, parametros, correlation_id)
                      VALUES (:id, :tipo, CAST(:p AS jsonb), :cid)"""),
              {"id": eid, "tipo": tipo, "p": json.dumps(parametros, default=str), "cid": correlation_id})
    return eid


def cerrar_ejecucion(c, eid: str, estado: str, resumen: dict, error: str | None = None):
    c.execute(text("""UPDATE execution_log SET estado = :e, resumen = CAST(:r AS jsonb),
                      error = :err, finalizada_en = now() WHERE execution_id = :id"""),
              {"e": estado, "r": json.dumps(resumen, default=str), "err": error, "id": eid})


def obtener_ejecucion(c, eid: str) -> dict | None:
    fila = c.execute(text("SELECT * FROM ai_frecuencias.execution_log WHERE execution_id = :id"),
                     {"id": eid}).mappings().first()
    return dict(fila) if fila else None


# ------------------------------------------------------------------ modelos

def listar_modelos(c) -> list[dict]:
    return [dict(f) for f in c.execute(text(
        "SELECT * FROM ai_frecuencias.model_registry ORDER BY creado_en DESC")).mappings()]


def modelo_activo(c) -> dict | None:
    f = c.execute(text("SELECT * FROM ai_frecuencias.model_registry WHERE estado = 'activo'")
                  ).mappings().first()
    return dict(f) if f else None


def activar_modelo(c, model_id: int, usuario: str, motivo: str) -> dict:
    """Activa un modelo y retira el anterior, dejando bitácora (permite reversión)."""
    objetivo = c.execute(text("SELECT * FROM model_registry WHERE model_id = :m FOR UPDATE"),
                         {"m": model_id}).mappings().first()
    if objetivo is None:
        return None
    anterior = c.execute(text("SELECT model_id FROM model_registry WHERE estado = 'activo' FOR UPDATE")
                         ).scalar()
    if anterior is not None and anterior != model_id:
        c.execute(text("UPDATE model_registry SET estado = 'retirado' WHERE model_id = :m"),
                  {"m": anterior})
        c.execute(text("""INSERT INTO model_history (model_id, estado_antes, estado_nuevo, motivo, usuario)
                          VALUES (:m, 'activo', 'retirado', :mot, :u)"""),
                  {"m": anterior, "mot": f"reemplazado por {model_id}: {motivo}", "u": usuario})
    c.execute(text("""UPDATE model_registry SET estado = 'activo', activado_en = now(),
                      activado_por = :u WHERE model_id = :m"""), {"m": model_id, "u": usuario})
    c.execute(text("""INSERT INTO model_history (model_id, estado_antes, estado_nuevo, motivo, usuario)
                      VALUES (:m, :antes, 'activo', :mot, :u)"""),
              {"m": model_id, "antes": objetivo["estado"], "mot": motivo, "u": usuario})
    return {"model_id": model_id, "modelo_anterior": anterior}


# ------------------------------------------------------------------ demanda y parámetros

def demanda_franja(c, idruta: int, desde: datetime, hasta: datetime) -> pd.Series:
    filas = c.execute(text("""SELECT franja_inicio, pasajeros FROM ai_frecuencias.demanda_franja
                              WHERE idruta = :r AND franja_inicio >= :d AND franja_inicio < :h
                              ORDER BY franja_inicio"""),
                      {"r": idruta, "d": desde, "h": hasta}).all()
    if not filas:
        return pd.Series(dtype=float)
    s = pd.Series([f[1] for f in filas], index=pd.DatetimeIndex([f[0] for f in filas]), dtype=float)
    malla = pd.date_range(s.index.min(), s.index.max(), freq="15min")
    return s.reindex(malla)


def parametros_vigentes(c) -> list[dict]:
    """Todas las filas vigentes: 'todos' y, desde param_v2, laboral / sabado / domingo_festivo."""
    return [dict(f) for f in c.execute(text(
        "SELECT * FROM ai_frecuencias.parametro_operativo WHERE vigente ORDER BY idruta, tipo_dia")
    ).mappings()]


def viajes_fuente(c, idrutas, desde: datetime, hasta: datetime) -> pd.DataFrame:
    """Viajes ingeridos (copia local de progvehiculos, solo las columnas que usa el FE)."""
    return pd.read_sql(text(
        """SELECT idprogramacion, idruta, fechasalida, fechainicio, fechafin, finalizado, cancelada,
                  numeropasajeros, numerobajadas
           FROM ai_frecuencias.viaje_fuente
           WHERE idruta = ANY(:r) AND fechasalida >= :d AND fechasalida < :h"""),
        c, params={"r": [int(r) for r in idrutas], "d": desde, "h": hasta})


def modelo_respaldo(c, metodo: str) -> dict | None:
    """Línea base registrada que se usa si el modelo activo no puede predecir."""
    filas = [dict(f) for f in c.execute(text(
        "SELECT * FROM ai_frecuencias.model_registry WHERE tipo = 'baseline' "
        "ORDER BY (estado = 'activo') DESC, creado_en DESC")).mappings()]
    for f in filas:
        if (f.get("hiperparametros") or {}).get("metodo", "dia_hora_4sem") == metodo:
            return f
    return filas[0] if filas else None


def umbrales_vigentes(c) -> dict:
    filas = c.execute(text("SELECT codigo, valor, version FROM ai_frecuencias.umbral WHERE vigente")).all()
    return {f[0]: float(f[1]) for f in filas}, {f[0]: f[2] for f in filas}


# ------------------------------------------------------------------ escritura de resultados

def insertar_predicciones(c, eid: str, model_id: int, df: pd.DataFrame) -> int:
    """model_id: el del modelo activo; si df trae su propia columna model_id (filas del respaldo),
    se respeta fila por fila."""
    if "model_id" not in df.columns:
        df = df.assign(model_id=model_id)
    # astype(object): psycopg no sabe adaptar numpy.int64
    filas = df.assign(execution_id=eid).astype(object).to_dict("records")
    if not filas:
        return 0
    c.execute(text("""
        INSERT INTO prediction (execution_id, idruta, sentido, segmento_id, franja_inicio,
            franja_minutos, horizonte_min, demanda_pred, limite_inferior, limite_superior,
            nivel_intervalo, definicion_tiempo, model_id, model_version, feature_version,
            data_cutoff, latency_ms)
        VALUES (:execution_id, :idruta, :sentido, :segmento_id, :franja_inicio, :franja_minutos,
            :horizonte_min, :demanda_pred, :limite_inferior, :limite_superior, :nivel_intervalo,
            :definicion_tiempo, :model_id, :model_version, :feature_version, :data_cutoff, :latency_ms)
        ON CONFLICT (idruta, sentido, segmento_id, franja_inicio, horizonte_min, model_id, data_cutoff)
        DO NOTHING"""), filas)
    return len(filas)


def insertar_recomendaciones(c, eid: str, df: pd.DataFrame) -> int:
    filas = df.assign(execution_id=eid).astype(object)
    filas = filas.where(filas.notna(), None).to_dict("records")
    if not filas:
        return 0
    columnas = list(filas[0])
    c.execute(text(f"INSERT INTO recommendation ({', '.join(columnas)}) "
                   f"VALUES ({', '.join(':' + k for k in columnas)})"), filas)
    return len(filas)


def claves_alertas_abiertas(c) -> set:
    return {f[0] for f in c.execute(text(
        "SELECT clave_dedup FROM alert WHERE estado IN ('abierta', 'reconocida', 'asignada')"))}


def insertar_alertas(c, eid: str, alertas: list[dict], versiones: dict) -> int:
    for a in alertas:
        fila = {k: a[k] for k in ("codigo", "severidad", "idruta", "franja_inicio", "horizonte_min",
                                  "valor_observado", "valor_esperado", "umbral",
                                  "puntuacion_anomalia", "explicacion", "recomendacion", "clave_dedup")}
        fila.update(execution_id=eid, umbral_version=versiones.get(a["codigo"]))
        aid = c.execute(text("""
            INSERT INTO alert (execution_id, codigo, severidad, idruta, franja_inicio, horizonte_min,
                valor_observado, valor_esperado, umbral, umbral_version, puntuacion_anomalia,
                explicacion, recomendacion, clave_dedup)
            VALUES (:execution_id, :codigo, :severidad, :idruta, :franja_inicio, :horizonte_min,
                :valor_observado, :valor_esperado, :umbral, :umbral_version, :puntuacion_anomalia,
                :explicacion, :recomendacion, :clave_dedup)
            ON CONFLICT (clave_dedup) WHERE estado IN ('abierta', 'reconocida', 'asignada') DO NOTHING
            RETURNING alert_id"""), fila).scalar()
        if aid:
            c.execute(text("""INSERT INTO alert_history (alert_id, accion, estado_nuevo, usuario)
                              VALUES (:a, 'creada', 'abierta', 'sistema')"""), {"a": aid})
    return len(alertas)


# ------------------------------------------------------------------ gestión de alertas

def obtener_alerta(c, alert_id: int, bloquear=False) -> dict | None:
    sql = "SELECT * FROM ai_frecuencias.alert WHERE alert_id = :a" + (" FOR UPDATE" if bloquear else "")
    f = c.execute(text(sql), {"a": alert_id}).mappings().first()
    return dict(f) if f else None


def actualizar_alerta(c, alert_id: int, estado_antes: str, estado_nuevo: str, usuario: str,
                      comentario: str | None, usuario_asignado: str | None):
    c.execute(text("""UPDATE alert SET estado = :e, actualizada_en = now(),
                      usuario_asignado = COALESCE(:asig, usuario_asignado) WHERE alert_id = :a"""),
              {"e": estado_nuevo, "asig": usuario_asignado, "a": alert_id})
    accion = "asignada" if estado_nuevo == "asignada" else "cambio_estado"
    if estado_nuevo == estado_antes:
        accion = "comentario"
    c.execute(text("""INSERT INTO alert_history (alert_id, accion, estado_antes, estado_nuevo,
                      comentario, usuario) VALUES (:a, :acc, :antes, :nuevo, :com, :u)"""),
              {"a": alert_id, "acc": accion, "antes": estado_antes, "nuevo": estado_nuevo,
               "com": comentario, "u": usuario})


def historial_alerta(c, alert_id: int) -> list[dict]:
    return [dict(f) for f in c.execute(text(
        "SELECT * FROM ai_frecuencias.alert_history WHERE alert_id = :a ORDER BY creado_en"),
        {"a": alert_id}).mappings()]


# ------------------------------------------------------------------ consultas paginadas

TABLAS_CONSULTABLES = {
    "prediction": ("prediction_id", {"idruta", "horizonte_min", "model_id", "execution_id"}),
    "recommendation": ("recommendation_id", {"idruta", "horizonte_min", "es_viable", "execution_id"}),
    "alert": ("alert_id", {"idruta", "estado", "severidad", "codigo"}),
}


def consultar(c, tabla: str, filtros: dict, desde=None, hasta=None, pagina=1, tamano=50):
    """Consulta paginada con filtros de igualdad sobre columnas permitidas."""
    orden, permitidas = TABLAS_CONSULTABLES[tabla]
    condiciones, params = [], {}
    for k, v in filtros.items():
        if v is not None and k in permitidas:
            condiciones.append(f"{k} = :{k}")
            params[k] = v
    if desde is not None:
        condiciones.append("franja_inicio >= :desde")
        params["desde"] = desde
    if hasta is not None:
        condiciones.append("franja_inicio < :hasta")
        params["hasta"] = hasta
    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""
    total = c.execute(text(f"SELECT count(*) FROM ai_frecuencias.{tabla} {where}"), params).scalar()
    params.update(lim=tamano, off=(pagina - 1) * tamano)
    filas = c.execute(text(f"SELECT * FROM ai_frecuencias.{tabla} {where} "
                           f"ORDER BY {orden} DESC LIMIT :lim OFFSET :off"), params).mappings()
    return [dict(f) for f in filas], int(total)
