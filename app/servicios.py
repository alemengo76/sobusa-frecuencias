"""Casos de uso que orquestan modelo, optimización y alertas sobre la base de datos.

La lógica de negocio vive en app/ml, app/optimizacion y app/alertas (funciones puras con
pruebas). Aquí solo se lee, se llama a esas funciones y se guarda el resultado.
"""
import logging
import time
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import text

from app.alertas import motor
from app.core.config import obtener_config
from app.core.errores import Conflicto, NoEncontrado, SolicitudInvalida
from app.db import repositorio as repo
from app.db.conexion import escribir
from app.fe31 import FEATURE_VERSION as FEATURE_VERSION_GBM
from app.fe31.viajes import a_formato_fe
from app.ml import baseline, contrato, modelo_gbm
from app.ml.features import FEATURE_VERSION
from app.optimizacion.frecuencias import recomendar
from app.optimizacion.parametros import TablaParametros, desde_filas

log = logging.getLogger("sobusa.servicios")
HISTORIA_DIAS = 63           # 9 semanas: alcanza para el promedio de 8 semanas
BANDA_POR_DEFECTO = (-0.25, 0.30)   # PROVISIONAL hasta guardar los cuantiles de validación


# ------------------------------------------------------------------ predicción

def _intervalos(pred: pd.DataFrame, modelo: dict) -> pd.DataFrame:
    errores = (modelo.get("metricas") or {}).get("intervalos")
    pred = pred.assign(hora=pd.DatetimeIndex(pred.franja_inicio).hour)
    if errores:
        out = baseline.aplicar_intervalos(pred, pd.DataFrame(errores))
    else:
        bi, bs = BANDA_POR_DEFECTO
        out = pred.assign(limite_inferior=(pred.demanda_pred * (1 + bi)).clip(lower=0),
                          limite_superior=pred.demanda_pred * (1 + bs))
    return out.drop(columns="hora")


def predecir_dia(c, idruta: int, dia: datetime, modelo: dict, horizontes) -> pd.DataFrame:
    """Línea base (dia_hora_4sem, dia_hora_8sem o ultima_semana) sobre demanda_franja."""
    inicio = time.perf_counter()
    metodo = (modelo.get("hiperparametros") or {}).get("metodo", "dia_hora_4sem")
    s = repo.demanda_franja(c, idruta, dia - timedelta(days=HISTORIA_DIAS), dia + timedelta(days=1))
    if s.empty:
        return pd.DataFrame()
    # Extender la malla hasta el final del día pedido: para un día futuro no hay filas
    fin = pd.Timestamp(dia) + timedelta(days=1) - timedelta(minutes=15)
    s = s.reindex(pd.date_range(s.index.min(), max(fin, s.index.max()), freq="15min"))
    pred = baseline.predecir(s, metodo, horizontes)
    pred = pred[(pred.franja_inicio >= dia) & (pred.franja_inicio < dia + timedelta(days=1))]
    pred = pred.dropna(subset=["demanda_pred"])
    pred = _intervalos(pred, modelo)
    return _completar_contrato(pred.assign(idruta=idruta), modelo, dia, inicio,
                               modelo.get("feature_version") or FEATURE_VERSION)


def _completar_contrato(pred: pd.DataFrame, modelo: dict, dia: datetime, inicio: float,
                        feature_version: str) -> pd.DataFrame:
    latencia = int((time.perf_counter() - inicio) * 1000)
    return pred.assign(
        sentido=contrato.SENTIDO, segmento_id=contrato.SEGMENTO,
        franja_minutos=contrato.FRANJA_MIN, nivel_intervalo=0.80,
        definicion_tiempo="salida_programada", model_id=modelo["model_id"],
        model_version=modelo["version"], feature_version=feature_version,
        data_cutoff=pd.Timestamp(dia).normalize(), latency_ms=latencia)


def predecir_gbm(c, idrutas, dia: datetime, modelo: dict, horizontes) -> pd.DataFrame:
    """Gradient boosting FE 3.1: carga el artefacto registrado (una vez, verificado por hash) y
    calcula las variables del día con los viajes ingeridos antes de ese día. No entrena."""
    inicio = time.perf_counter()
    cfg = obtener_config()
    if not modelo.get("artefacto_ruta"):
        raise modelo_gbm.ModeloNoDisponible("El modelo no tiene artefacto registrado")
    art = modelo_gbm.cargar(modelo_gbm.ruta_artefacto(modelo["artefacto_ruta"], cfg.modelos_dir),
                            modelo.get("artefacto_sha256") or "")
    D = pd.Timestamp(dia).normalize()
    viajes = repo.viajes_fuente(c, idrutas, (D - timedelta(days=modelo_gbm.HISTORIA_DIAS)).to_pydatetime(),
                                D.to_pydatetime())
    pred = modelo_gbm.predecir_dia(art, a_formato_fe(viajes), D, idrutas, horizontes)
    return _completar_contrato(pred, modelo, dia, inicio, FEATURE_VERSION_GBM)


def predecir(c, idrutas, dia: datetime, modelo: dict, horizontes) -> tuple[pd.DataFrame, dict]:
    """Predicción con el modelo activo y respaldo con línea base.

    Si el activo es el GBM y no puede predecir una ruta (artefacto ausente, alterado o de otra
    versión de scikit-learn, sin viajes ingeridos, historia insuficiente...), esa ruta se predice
    con la línea base registrada (METODO_RESPALDO). Cada fila guarda el model_id que la produjo y
    la ejecución deja el motivo: el respaldo nunca es silencioso."""
    metodo = (modelo.get("hiperparametros") or {}).get("metodo", "dia_hora_4sem")
    info = {"modelo_activo": modelo["model_id"], "metodo_activo": metodo, "respaldo": False}
    partes, pendientes = [], list(idrutas)
    if metodo == modelo_gbm.METODO:
        try:
            g = predecir_gbm(c, idrutas, dia, modelo, horizontes)
            partes.append(g)
            pendientes = [r for r in idrutas if r not in set(g.idruta)]
            if pendientes:
                info["motivo_respaldo"] = f"el GBM no produjo filas para las rutas {pendientes}"
        except Exception as e:   # noqa: BLE001 - cualquier falla del GBM debe pasar al respaldo
            info["motivo_respaldo"] = f"{type(e).__name__}: {e}"
        if pendientes:
            respaldo = repo.modelo_respaldo(c, obtener_config().metodo_respaldo)
            if respaldo is None:
                raise Conflicto("El GBM no pudo predecir y no hay una línea base registrada como respaldo")
            log.warning("GBM no disponible; se usa la línea base",
                        extra={"datos": {"rutas": pendientes, "motivo": info["motivo_respaldo"],
                                         "model_id_respaldo": respaldo["model_id"]}})
            info.update(respaldo=True, rutas_respaldo=pendientes, modelo_respaldo=respaldo["model_id"])
            modelo_base = respaldo
        else:
            modelo_base = None
    else:
        modelo_base = modelo
    if modelo_base is not None:
        partes += [predecir_dia(c, r, dia, modelo_base, horizontes) for r in pendientes]
    partes = [p for p in partes if not p.empty]
    pred = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()
    if not pred.empty:
        info["filas_por_modelo"] = {int(k): int(v) for k, v in pred.model_id.value_counts().items()}
    return pred, info


def verificar_activable(c, model_id: int) -> None:
    """Un GBM solo se activa si su artefacto existe, su hash coincide y lo puede abrir esta
    versión de scikit-learn. Así la activación no deja la API funcionando solo con el respaldo."""
    m = next((x for x in repo.listar_modelos(c) if x["model_id"] == model_id), None)
    if m is None or (m.get("hiperparametros") or {}).get("metodo") != modelo_gbm.METODO:
        return
    try:
        modelo_gbm.cargar(modelo_gbm.ruta_artefacto(m.get("artefacto_ruta") or "", obtener_config().modelos_dir),
                          m.get("artefacto_sha256") or "")
    except modelo_gbm.ModeloNoDisponible as e:
        raise Conflicto(f"No se puede activar el modelo {model_id}: {e}") from e


def ejecutar_predicciones(idrutas, dia: datetime, horizontes, correlation_id: str) -> dict:
    with escribir() as c:
        modelo = repo.modelo_activo(c)
        if modelo is None:
            raise Conflicto("No hay un modelo activo; active uno con POST /models/{id}/activate")
        eid = repo.crear_ejecucion(c, "prediccion", {"idrutas": idrutas, "dia": dia,
                                                     "horizontes": horizontes}, correlation_id)
        pred, info = predecir(c, idrutas, dia, modelo, horizontes)
        if pred.empty:
            repo.cerrar_ejecucion(c, eid, "fallida", info, "sin datos de demanda para el día pedido")
            return {"execution_id": eid, "estado": "fallida", "predicciones": 0, **info}
        contrato.validar_predicciones(pred.assign(franja_inicio=pd.to_datetime(pred.franja_inicio)))
        n = repo.insertar_predicciones(c, eid, modelo["model_id"], pred)
        repo.cerrar_ejecucion(c, eid, "exitosa", {"predicciones": n, "model_id": modelo["model_id"], **info})
    log.info("predicciones guardadas", extra={"datos": {"execution_id": eid, "filas": n, **info}})
    return {"execution_id": eid, "estado": "exitosa", "predicciones": n, **info, "_pred": pred}


# ------------------------------------------------------------------ recomendación

def _parametros(c) -> TablaParametros:
    """Parámetros vigentes por (ruta, tipo de día), con respaldo 'todos' (E03)."""
    return desde_filas(repo.parametros_vigentes(c))


def _frecuencia_actual(c, pred: pd.DataFrame) -> pd.DataFrame:
    """Salidas programadas en la hora que empieza en cada franja (RF-008)."""
    partes = []
    for idruta, g in pred.groupby("idruta"):
        desde, hasta = g.franja_inicio.min(), g.franja_inicio.max() + timedelta(hours=1)
        filas = c.execute(text("""SELECT franja_inicio, programaciones FROM demanda_franja
                                       WHERE idruta = :r AND franja_inicio >= :d AND franja_inicio < :h"""),
                          {"r": int(idruta), "d": desde, "h": hasta}).all()
        if not filas:
            continue
        s = pd.Series([f[1] for f in filas], index=pd.DatetimeIndex([f[0] for f in filas]))
        s = s.reindex(pd.date_range(desde, hasta, freq="15min"), fill_value=0)
        hora = sum(s.shift(-k, fill_value=0) for k in range(4))
        partes.append(pd.DataFrame({"idruta": idruta, "franja_inicio": hora.index,
                                    "buses_hora_actual": hora.to_numpy()}))
    return pd.concat(partes, ignore_index=True) if partes else None


def calcular_recomendaciones(c, eid: str, pred: pd.DataFrame, base_demanda: str) -> pd.DataFrame:
    parametros = _parametros(c)
    faltan = set(pred.idruta) - parametros.rutas()
    if faltan:
        raise SolicitudInvalida(f"No hay parámetros operativos vigentes para las rutas {sorted(faltan)}")
    rec = recomendar(pred, parametros, _frecuencia_actual(c, pred), base_demanda)
    repo.insertar_recomendaciones(c, eid, rec)
    return rec


def predicciones_de_ejecucion(c, eid_prediccion: str) -> pd.DataFrame:
    filas, _ = repo.consultar(c, "prediction", {"execution_id": eid_prediccion}, tamano=100_000)
    if not filas:
        raise NoEncontrado("Ejecución de predicción", eid_prediccion)
    df = pd.DataFrame(filas)
    df["franja_inicio"] = pd.to_datetime(df.franja_inicio).dt.tz_localize(None)
    for col in ("demanda_pred", "limite_inferior", "limite_superior"):
        df[col] = df[col].astype(float)
    return df


# ------------------------------------------------------------------ alertas

def evaluar_alertas(c, eid: str, pred: pd.DataFrame, rec: pd.DataFrame) -> int:
    umbrales, versiones = repo.umbrales_vigentes(c)
    u = {**motor.umbrales_por_defecto(), **umbrales}
    referencia = []
    for idruta, g in pred.groupby("idruta"):
        dia = g.franja_inicio.min().normalize()
        s = repo.demanda_franja(c, int(idruta), dia - timedelta(days=HISTORIA_DIAS), dia + timedelta(days=1))
        if not s.empty:
            ref = baseline.predecir(s, "dia_hora_8sem", tuple(sorted(set(g.horizonte_min))))
            referencia.append(ref.assign(idruta=idruta))
    p = pred.copy()
    if referencia:
        ref = pd.concat(referencia).rename(columns={"demanda_pred": "demanda_referencia"})
        p = p.merge(ref, on=["idruta", "franja_inicio", "horizonte_min"], how="left")
    else:
        p["demanda_referencia"] = None
    nuevas = motor.alt_001(p, u) + motor.alt_002_003_004(rec, u)
    nuevas = motor.deduplicar(nuevas, repo.claves_alertas_abiertas(c))
    repo.insertar_alertas(c, eid, nuevas, versiones)
    notificar(c, [a for a in nuevas if a["severidad"] == "alta"])
    return len(nuevas)


def notificar(c, alertas: list[dict]) -> None:
    """Adaptadores de correo y WhatsApp. El envío real se simula (lo permite la especificación)."""
    cfg = obtener_config()
    canales = [("correo", cfg.correo_destino), ("whatsapp", cfg.whatsapp_destino)]
    for a in alertas:
        aid = c.execute(text("SELECT alert_id FROM alert WHERE clave_dedup = :k "
                                  "AND estado = 'abierta'"), {"k": a["clave_dedup"]}).scalar()
        for canal, destino in canales:
            if not destino or aid is None:
                continue
            log.info(f"notificación {canal} {'simulada' if cfg.notificaciones_simuladas else ''}",
                     extra={"datos": {"alert_id": aid, "canal": canal, "codigo": a["codigo"]}})
            c.execute(text("""INSERT INTO notification_log (alert_id, canal, destino, simulada, estado)
                                   VALUES (:a, :c, :d, :s, 'enviada')"""),
                      {"a": aid, "c": canal, "d": destino, "s": cfg.notificaciones_simuladas})


def cambiar_estado_alerta(alert_id: int, estado: str, usuario: str, comentario: str | None,
                          usuario_asignado: str | None) -> dict:
    with escribir() as c:
        alerta = repo.obtener_alerta(c, alert_id, bloquear=True)
        if alerta is None:
            raise NoEncontrado("Alerta", alert_id)
        if estado != alerta["estado"]:
            try:
                motor.validar_transicion(alerta["estado"], estado)
            except motor.TransicionInvalida as e:
                raise Conflicto(str(e)) from e
        elif not comentario:
            raise SolicitudInvalida("Sin cambio de estado, el comentario es obligatorio")
        if estado == "asignada" and not usuario_asignado:
            raise SolicitudInvalida("Para asignar se requiere usuario_asignado")
        repo.actualizar_alerta(c, alert_id, alerta["estado"], estado, usuario, comentario,
                               usuario_asignado)
        alerta = repo.obtener_alerta(c, alert_id)
        alerta["historial"] = repo.historial_alerta(c, alert_id)
        return alerta


# ------------------------------------------------------------------ ejecución completa

def ejecutar_frecuencias(idrutas, dia: datetime, horizontes, base_demanda: str,
                         correlation_id: str) -> dict:
    """Predicción + recomendación + alertas en una sola ejecución trazable."""
    resultado = ejecutar_predicciones(idrutas, dia, horizontes, correlation_id)
    if resultado["estado"] != "exitosa":
        return {k: v for k, v in resultado.items() if not k.startswith("_")}
    parametros = {"idrutas": idrutas, "dia": dia, "base_demanda": base_demanda,
                  "prediccion": resultado["execution_id"]}
    try:
        with escribir() as c:
            eid = repo.crear_ejecucion(c, "frecuencias", parametros, correlation_id)
            pred = resultado["_pred"]
            rec = calcular_recomendaciones(c, eid, pred, base_demanda)
            n_alertas = evaluar_alertas(c, eid, pred, rec)
            resumen = {"execution_id_prediccion": resultado["execution_id"],
                       "predicciones": resultado["predicciones"], "recomendaciones": len(rec),
                       "no_viables": int((~rec.es_viable).sum()), "alertas_nuevas": n_alertas}
            repo.cerrar_ejecucion(c, eid, "exitosa", resumen)
    except Exception as e:
        # La transacción anterior se revirtió; la falla se registra en una nueva.
        with escribir() as c:
            eid = repo.crear_ejecucion(c, "frecuencias", parametros, correlation_id)
            repo.cerrar_ejecucion(c, eid, "fallida", {}, str(e))
        raise
    return {"execution_id": eid, "estado": "exitosa", **resumen}
