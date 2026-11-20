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


# ------------------------------------------------------------------ explicación de una predicción (RF-015, A1)

def explicar_prediccion(prediction_id: int) -> dict:
    """RF-015 (A1): explicación TreeSHAP de una predicción del GBM, RECONSTRUIDA Y VERIFICADA.

    Nunca devuelve una explicación parcial. Rechaza con:
    - 404: la predicción no existe.
    - 409: no es del GBM FE 3.1 (p. ej., respaldo de línea base); el modelo o el artefacto no
      coinciden (hash SHA-256 completo, model_id, model_version, feature_version, formato o versión
      exacta de scikit-learn) o su estructura interna no es la esperada; algún viaje del período se
      cargó después del inicio de la ejecución; o las predicciones de la ejecución y la ruta
      (valores, límites y franjas) no se reproducen a la precisión de la base.
    - 500 (error interno): la identidad referencia + suma(aportes) = predicción raw no se cumple.
    """
    import sklearn

    from app.db.conexion import consultar_resultados
    from app.ml import explicacion_gbm as XG

    # 1. Lecturas (solo lectura); el cálculo se hace después, fuera de la conexión
    with consultar_resultados() as c:
        p = repo.obtener_prediccion(c, prediction_id)
        if p is None:
            raise NoEncontrado("Predicción", prediction_id)
        modelo = repo.obtener_modelo(c, int(p["model_id"]))
        if modelo is None:
            raise Conflicto(f"El modelo {p['model_id']} de la predicción {prediction_id} no está registrado")
        metodo = (modelo.get("hiperparametros") or {}).get("metodo")
        if metodo != modelo_gbm.METODO:
            raise Conflicto(f"La predicción {prediction_id} es del modelo {modelo.get('version')} "
                            f"({metodo or modelo.get('tipo')}): TreeSHAP solo explica el GBM FE 3.1 y las "
                            "predicciones de línea base no se explican con este método")
        eid, idruta = str(p["execution_id"]), int(p["idruta"])
        ejecucion = repo.obtener_ejecucion(c, eid)
        filas = repo.predicciones_ejecucion_ruta(c, eid, idruta)
        dia = XG.instante_local(p["data_cutoff"])
        desde, hasta = XG.ventana_viajes(dia)
        ultima_carga = repo.ultima_carga_viajes(c, idruta, desde.to_pydatetime(), hasta.to_pydatetime())
        viajes = repo.viajes_fuente(c, [idruta], desde.to_pydatetime(), hasta.to_pydatetime())

    # 2. Identidad del modelo y del artefacto
    if p["model_version"] != modelo.get("version"):
        raise Conflicto(f"model_version de la predicción ({p['model_version']}) distinta de la del registro "
                        f"({modelo.get('version')})")
    sha = (modelo.get("artefacto_sha256") or "").strip()
    if len(sha) != 64 or not modelo.get("artefacto_ruta"):
        raise Conflicto(f"El modelo {p['model_id']} no tiene artefacto o hash SHA-256 registrado")
    try:
        art = modelo_gbm.cargar(modelo_gbm.ruta_artefacto(modelo["artefacto_ruta"], obtener_config().modelos_dir),
                                sha)       # hash completo, formato, scikit-learn exacto y FE 3.1 / comun-1.5
    except modelo_gbm.ModeloNoDisponible as e:
        raise Conflicto(f"No se puede usar el artefacto del modelo {p['model_id']}: {e}") from e
    meta = art.meta
    for campo, esperado, obtenido in (
            ("version del artefacto", modelo.get("version"), meta.get("version")),
            ("formato", modelo_gbm.FORMATO, meta.get("formato")),
            ("scikit-learn", sklearn.__version__, meta.get("sklearn")),
            ("feature_version del artefacto", FEATURE_VERSION_GBM, meta.get("feature_version")),
            ("feature_version de la predicción", FEATURE_VERSION_GBM, p["feature_version"])):
        if esperado != obtenido:
            raise Conflicto(f"{campo}: se esperaba {esperado!r} y se obtuvo {obtenido!r}")
    try:
        XG.validar_estructura(art.modelo)
    except XG.ExplicacionNoValida as e:
        raise Conflicto(f"Estructura interna del artefacto no compatible con TreeSHAP: {e}") from e

    # 3. Coherencia de la ejecución y procedencia de los viajes
    if dia != dia.normalize():
        raise Conflicto(f"data_cutoff {p['data_cutoff']} no es el inicio de un día")
    if ejecucion is None:
        raise Conflicto(f"La ejecución {eid} de la predicción no está registrada")
    if not filas or any(int(f["model_id"]) != int(p["model_id"]) or f["model_version"] != p["model_version"]
                        or XG.instante_local(f["data_cutoff"]) != dia for f in filas):
        raise Conflicto(f"La ejecución {eid} no tiene un único modelo y corte de datos para la ruta {idruta}")
    if ultima_carga is None:
        raise Conflicto(f"No hay viajes de la ruta {idruta} entre {desde:%Y-%m-%d} y {hasta:%Y-%m-%d}")
    if pd.Timestamp(ultima_carga) > pd.Timestamp(ejecucion["iniciada_en"]):
        raise Conflicto(f"Viajes de la ruta {idruta} del período se cargaron de nuevo ({ultima_carga}) "
                        f"después del inicio de la ejecución ({ejecucion['iniciada_en']}): no se reconstruye")

    # 4. Reproducción histórica de TODAS las predicciones de la ejecución y la ruta
    P = a_formato_fe(viajes)
    horizontes = sorted({int(f["horizonte_min"]) for f in filas})
    try:
        recalculadas = modelo_gbm.predecir_dia(art, P, dia, [idruta], horizontes)
        X = modelo_gbm.variables_dia(P, dia, art.horario)
    except modelo_gbm.ModeloNoDisponible as e:
        raise Conflicto(f"No se pudieron recalcular las predicciones: {e}") from e
    try:
        comparadas = XG.verificar_reproduccion(filas, recalculadas)
    except XG.ReproduccionDistinta as e:
        raise Conflicto(f"Reproducción rechazada: {e}") from e

    # 5. La fila pedida: sus variables, su predicción y su explicación
    franja, horizonte = XG.instante_local(p["franja_inicio"]), int(p["horizonte_min"])
    fila = X[X.id_ruta.astype(str).eq(str(idruta)) & X.horizonte_min.astype(int).eq(horizonte)
             & pd.to_datetime(X.origen).eq(franja)]
    propia = recalculadas[pd.to_datetime(recalculadas.franja_inicio).eq(franja)
                          & recalculadas.horizonte_min.astype(int).eq(horizonte)]
    if len(fila) != 1 or len(propia) != 1:
        raise Conflicto(f"No se encontró una única fila de variables para la predicción {prediction_id}")
    if XG.a_precision_bd(propia.demanda_pred.iloc[0]) != XG.a_precision_bd(p["demanda_pred"]):
        raise Conflicto(f"La predicción {prediction_id} recalculada no coincide con la guardada")
    explicacion = XG.explicar(art, fila)[0]                 # ExplicacionNoValida -> error interno (500)
    if XG.a_precision_bd(max(explicacion["prediccion_pasajeros"], 0.0)) != XG.a_precision_bd(p["demanda_pred"]):
        raise XG.ExplicacionNoValida("exp(raw) no reproduce la predicción guardada")

    importancia = XG.importancia_ganancia(art.modelo)
    return {
        "prediction_id": int(p["prediction_id"]), "execution_id": eid, "model_id": int(p["model_id"]),
        "model_version": p["model_version"], "feature_version": p["feature_version"],
        "artefacto_sha256": sha, "scikit_learn": sklearn.__version__,
        "idruta": idruta, "franja_inicio": p["franja_inicio"], "horizonte_min": horizonte,
        "data_cutoff": p["data_cutoff"], "demanda_pred": float(p["demanda_pred"]),
        "estado": XG.ESTADO, "advertencia": XG.ADVERTENCIA,
        "metodo": XG.METODO, "escala": "log: prediccion = exp(referencia_log + suma de aporte_log); "
                                       "factor = exp(aporte_log) multiplica la predicción",
        **{k: explicacion[k] for k in ("referencia_log", "referencia_pasajeros", "prediccion_log",
                                        "prediccion_pasajeros", "aportes")},
        "verificaciones": {
            "procedencia": {"ultima_carga_viajes": ultima_carga, "inicio_ejecucion": ejecucion["iniciada_en"],
                            "ventana": [desde.to_pydatetime(), hasta.to_pydatetime()]},
            "reproduccion": {"predicciones_comparadas": comparadas, "horizontes": horizontes},
            "identidad_treeshap": {"error_absoluto": explicacion["error_identidad"],
                                   "tolerancia": XG.TOLERANCIA},
        },
        "importancia_global_artefacto": {
            "metodo": XG.METODO_IMPORTANCIA, "model_version": meta.get("version"),
            "nota": "Global y del artefacto de producción: proporción de la ganancia de entrenamiento en las "
                    "divisiones de cada variable. No es la importancia por permutación de "
                    "metricas.importancia_top10 (modelo de evaluación) ni una explicación individual.",
            "variables": importancia.to_dict("records")},
    }
