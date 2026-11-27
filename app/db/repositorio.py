"""Consultas a ai_frecuencias (escritura) y a las tablas fuente (solo lectura).

Todas las consultas usan parámetros enlazados (nunca f-strings con datos del usuario).
"""
import json
import uuid
from datetime import datetime
from decimal import Decimal

import pandas as pd
from sqlalchemy import text

from app.core.errores import Conflicto

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


def activar_modelo(c, model_id: int, usuario: str, motivo: str) -> dict | None:
    """Activa un modelo y retira el anterior, dejando bitácora (permite reversión)."""
    # Espacio SOBUSA 5438, operación 1: serializa activaciones, incluso sin activo previo.
    # PostgreSQL libera este bloqueo al confirmar o revertir la transacción.
    c.execute(text("SELECT pg_advisory_xact_lock(5438, 1)"))
    objetivo = c.execute(text("SELECT * FROM model_registry WHERE model_id = :m FOR UPDATE"),
                         {"m": model_id}).mappings().first()
    if objetivo is None:
        return None
    if objetivo["estado"] == "rechazado":
        raise Conflicto(f"No se puede activar el modelo {model_id}: está rechazado")
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

class PrediccionesNoGuardadas(RuntimeError):
    """La base no guardó exactamente las predicciones enviadas: la transacción debe revertirse."""


_COLUMNAS_PREDICCION_BD = (
    ("idruta", "integer"), ("sentido", "text"), ("segmento_id", "text"), ("franja_inicio", "timestamp"),
    ("franja_minutos", "integer"), ("horizonte_min", "integer"), ("demanda_pred", "numeric"),
    ("limite_inferior", "numeric"), ("limite_superior", "numeric"), ("nivel_intervalo", "numeric"),
    ("definicion_tiempo", "text"), ("model_id", "integer"), ("model_version", "text"),
    ("feature_version", "text"), ("data_cutoff", "timestamp"), ("latency_ms", "integer"),
)


def _valor_json(v):
    if isinstance(v, Decimal):
        return format(v, "f")               # exacto: ya viene redondeado a 2 decimales
    if isinstance(v, (pd.Timestamp, datetime)):
        return pd.Timestamp(v).strftime("%Y-%m-%dT%H:%M:%S")
    if hasattr(v, "item"):                  # escalares de numpy
        return v.item()
    return v


def insertar_predicciones(c, eid: str, df: pd.DataFrame) -> int:
    """Inserta TODAS las filas de df (de contrato.normalizar_predicciones) con execution_id = eid, en una
    sola sentencia con RETURNING. Sin ON CONFLICT: un choque es un error y revierte la transacción.
    Si la base no devuelve exactamente len(df) filas, lanza PrediccionesNoGuardadas."""
    if df.empty:
        raise PrediccionesNoGuardadas("no hay predicciones para guardar")
    if "model_id" not in df.columns:
        raise PrediccionesNoGuardadas("cada predicción debe traer el model_id que la produjo")
    columnas = [n for n, _ in _COLUMNAS_PREDICCION_BD]
    filas = [{k: _valor_json(v) for k, v in zip(columnas, fila, strict=True)}
             for fila in df[columnas].itertuples(index=False, name=None)]
    tipos = ", ".join(f"{n} {t}" for n, t in _COLUMNAS_PREDICCION_BD)
    devueltas = c.execute(text(f"""
        INSERT INTO prediction (execution_id, {", ".join(columnas)})
        SELECT CAST(:eid AS uuid), {", ".join(columnas)}
        FROM jsonb_to_recordset(CAST(:filas AS jsonb)) AS f({tipos})
        RETURNING prediction_id"""), {"eid": eid, "filas": json.dumps(filas)}).all()
    if len(devueltas) != len(df):
        raise PrediccionesNoGuardadas(f"se enviaron {len(df)} predicciones y la base devolvió {len(devueltas)}")
    return len(devueltas)


def reservar_conjunto(c, huella: str, eid: str, filas: int, formato: str) -> bool:
    """True si esta ejecución queda como dueña de la huella; False si otra ya lo es.

    Si otra transacción reservó la misma huella y no ha terminado, PostgreSQL hace esperar a esta
    sentencia (clave primaria) hasta que la otra confirme (-> False) o se revierta (-> True)."""
    return c.execute(text("""
        INSERT INTO prediction_set (huella, execution_id, formato, filas)
        VALUES (:h, CAST(:e AS uuid), :f, :n)
        ON CONFLICT (huella) DO NOTHING
        RETURNING execution_id"""), {"h": huella, "e": eid, "f": formato, "n": filas}).scalar() is not None


def duena_conjunto(c, huella: str) -> str:
    eid = c.execute(text("SELECT execution_id::text FROM prediction_set WHERE huella = :h"),
                    {"h": huella}).scalar()
    if eid is None:
        raise PrediccionesNoGuardadas(f"la huella {huella[:12]}… no tiene ejecución dueña")
    return eid


def ejecucion_duena(c, eid: str) -> str | None:
    """Ejecución que tiene las filas de la ejecución de predicción eid: ella misma o la que reutiliza.
    None si eid no existe."""
    fila = c.execute(text("SELECT resumen->>'reutiliza' FROM ai_frecuencias.execution_log "
                          "WHERE execution_id = CAST(:e AS uuid)"), {"e": eid}).first()
    if fila is None:
        return None
    return fila[0] or eid


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


def insertar_alerta(c, eid: str, a: dict, umbral_version: int | None) -> int | None:
    """Inserta UNA alerta con su historial «creada». Devuelve su alert_id, o None si ya había una
    abierta o en gestión con la misma clave. (insertar_alertas conserva su contrato: cuenta candidatas.)"""
    fila = {k: a.get(k) for k in ("codigo", "severidad", "idruta", "franja_inicio", "horizonte_min",
                                  "valor_observado", "valor_esperado", "umbral",
                                  "puntuacion_anomalia", "explicacion", "recomendacion", "clave_dedup")}
    fila.update(execution_id=eid, umbral_version=umbral_version)
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
    return aid


def alertas_con_prefijo(c, codigo: str, prefijo: str) -> list[dict]:
    """Alertas de `codigo` cuya clave empieza por `prefijo`, en cualquier estado."""
    return [dict(f) for f in c.execute(text("""
        SELECT alert_id, estado, idruta, franja_inicio, clave_dedup, valor_observado
        FROM alert WHERE codigo = :codigo AND starts_with(clave_dedup, :p) ORDER BY alert_id"""),
        {"codigo": codigo, "p": prefijo}).mappings()]


def notas_de_alertas(c, alert_ids, marca: str) -> dict[int, list[str]]:
    """Comentarios del sistema que empiezan por `marca`, por alerta."""
    ids = [int(x) for x in alert_ids]
    if not ids:
        return {}
    out: dict[int, list[str]] = {i: [] for i in ids}
    for aid, comentario in c.execute(text("""
            SELECT alert_id, comentario FROM alert_history
            WHERE alert_id = ANY(:ids) AND accion = 'comentario' AND usuario = 'sistema'
              AND starts_with(comentario, :m) ORDER BY id"""), {"ids": ids, "m": marca}):
        out[aid].append(comentario)
    return out


def anotar_alerta(c, alert_id: int, comentario: str) -> None:
    """Deja constancia en el historial SIN cambiar el estado de la alerta."""
    c.execute(text("""INSERT INTO alert_history (alert_id, accion, comentario, usuario)
                      VALUES (:a, 'comentario', :c, 'sistema')"""), {"a": int(alert_id), "c": comentario})


def ingestas_registradas(c, fuente: str, desde: datetime) -> list[dict]:
    """Intentos de ingesta de `fuente` iniciados desde `desde` (execution_log, tipo 'ingesta')."""
    return [dict(f) for f in c.execute(text("""
        SELECT execution_id::text AS execution_id, estado, parametros, resumen, iniciada_en, finalizada_en
        FROM execution_log
        WHERE tipo = 'ingesta' AND parametros->>'fuente' = :f AND iniciada_en >= :d
        ORDER BY iniciada_en, execution_id"""), {"f": fuente, "d": desde}).mappings()]


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


# ------------------------------------------------------------------ explicación de predicciones (RF-015, solo lectura)

def obtener_prediccion(c, prediction_id: int) -> dict | None:
    fila = c.execute(text("SELECT * FROM ai_frecuencias.prediction WHERE prediction_id = :p"),
                     {"p": prediction_id}).mappings().first()
    return dict(fila) if fila else None


def obtener_modelo(c, model_id: int) -> dict | None:
    fila = c.execute(text("SELECT * FROM ai_frecuencias.model_registry WHERE model_id = :m"),
                     {"m": model_id}).mappings().first()
    return dict(fila) if fila else None


def predicciones_ejecucion_ruta(c, execution_id: str, idruta: int) -> list[dict]:
    """Todas las predicciones que guardó una ejecución para una ruta (todos los horizontes)."""
    return [dict(f) for f in c.execute(text(
        """SELECT * FROM ai_frecuencias.prediction WHERE execution_id = :e AND idruta = :r
           ORDER BY horizonte_min, franja_inicio"""), {"e": execution_id, "r": idruta}).mappings()]


def ultima_carga_viajes(c, idruta: int, desde: datetime, hasta: datetime):
    """Momento de la carga más reciente de los viajes de la ruta con salida en [desde, hasta)."""
    return c.execute(text(
        """SELECT max(cargado_en) FROM ai_frecuencias.viaje_fuente
           WHERE idruta = :r AND fechasalida >= :d AND fechasalida < :h"""),
        {"r": idruta, "d": desde, "h": hasta}).scalar()


# ------------------------------------------------------------------ G3: errores de calidad por lote

# Condición de lote según el alcance. Cada una va COMPLETA entre paréntesis: los filtros opcionales
# se agregan con AND y, sin el paréntesis exterior de "auto", AND (que precede a OR) se aplicaría
# solo a la rama por ejecución. Sufijo de ventana: "_" + tres o más dígitos ASCII (app/ingesta.py,
# f"{lote}_{i:03d}"). Cadenas normales, no f-strings: las llaves de {3,} deben llegar tal cual.
COND_LOTE = {
    "ventana": "(d.lote = CAST(:lote AS text))",
    "ejecucion": ("(starts_with(d.lote, CAST(:lote AS text))"
                  " AND substr(d.lote, char_length(CAST(:lote AS text)) + 1) ~ '^_[0123456789]{3,}$')"),
}
COND_LOTE["auto"] = "(" + COND_LOTE["ventana"] + " OR " + COND_LOTE["ejecucion"] + ")"

# Filtro público -> columna (constantes; el valor del usuario solo viaja como parámetro enlazado).
FILTROS_CALIDAD = {
    "idruta": "d.idruta",
    "regla": "e.regla",
    "tabla_origen": "e.tabla_origen",
    "reglas_version": "e.reglas_version",
    "causa": "d.causa",
}

_FROM_CALIDAD = ("FROM ai_frecuencias.data_quality_deteccion d "
                 "JOIN ai_frecuencias.data_quality_error e ON e.id = d.evento_id")

_COLUMNAS_CALIDAD = (
    "d.id AS deteccion_id, d.evento_id AS evento_id, d.lote AS lote, d.detectado_en AS detectado_en, "
    "d.idruta AS idruta, d.causa AS causa, d.valor_original AS valor_original, "
    "e.tabla_origen AS tabla_origen, e.idprogramacion AS idprogramacion, e.regla AS regla, "
    "e.reglas_version AS reglas_version, e.valor AS valor_normalizado, e.lote AS primer_lote, "
    "e.creado_en AS primera_deteccion, (e.lote = d.lote) AS nuevo_en_lote"
)


def errores_calidad_por_lote(c, lote: str, alcance: str, filtros: dict | None,
                             pagina: int = 1, tamano: int = 50) -> tuple[list[dict], int]:
    """Detecciones de calidad de un lote (G3), paginadas, con su evento.

    alcance: 'ventana' (lote exacto), 'ejecucion' (ventanas del lote: prefijo literal + '_' y 3 o
    más dígitos ASCII) o 'auto' (ambas). El lote se usa tal cual: sin strip ni escape.
    Devuelve (filas, total); el conteo usa exactamente el mismo FROM, JOIN y WHERE."""
    if alcance not in COND_LOTE:
        raise ValueError(f"alcance no válido: {alcance!r}")
    filtros = filtros or {}
    desconocidos = set(filtros) - set(FILTROS_CALIDAD)
    if desconocidos:
        raise ValueError(f"filtros no permitidos: {sorted(desconocidos)}")
    condiciones, params = [COND_LOTE[alcance]], {"lote": lote}
    for clave, columna in FILTROS_CALIDAD.items():
        valor = filtros.get(clave)
        if valor is not None:
            condiciones.append(f"{columna} = :{clave}")
            params[clave] = valor
    where = "WHERE " + " AND ".join(condiciones)          # se arma UNA vez
    total = c.execute(text(f"SELECT count(*) {_FROM_CALIDAD} {where}"), params).scalar()
    filas = c.execute(text(f"SELECT {_COLUMNAS_CALIDAD} {_FROM_CALIDAD} {where} "
                           "ORDER BY d.id DESC LIMIT :lim OFFSET :off"),
                      {**params, "lim": tamano, "off": (pagina - 1) * tamano}).mappings()
    return [dict(f) for f in filas], int(total)
