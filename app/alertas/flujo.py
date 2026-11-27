"""Persistencia de las alertas que no nacen del cálculo de frecuencias.

ALT-001 a ALT-004 y la incompletitud de ALT-009 se guardan en `servicios.evaluar_alertas` (sin cambios).
Este módulo conecta, desde el ciclo automático (app/ciclo_alertas.py) o una conexión dada:

- ALT-009 por retraso de ingesta (`verificar_retraso`): un episodio por fuente y ruta, sin notificar.
- ALT-005 / ALT-007 (`evaluar_alt_005_007`) y ALT-010 (`evaluar_alt_010`): guardan las candidatas que
  calculan los módulos de solo lectura `desde_m1` y `alt010_desde_m1`, con la verificación de
  completitud por ruta y día (sin evidencia, el caso no es evaluable).
- Completitud por ruta y día (`evaluar_completitud`, app/calidad/completitud.py), antes de 005/007/010.
- ALT-006 (`evaluar_alt_006`): salidas tardías con hora real observada; sin inicio → indeterminada.
- ALT-008 (`evaluar_alt_008`): consolidado de Extreme (timbradas_anio) frente a la suma de los viajes
  (progvehiculos), ambos de solo lectura; se habilita con ALT008_REFERENCIA=timbradas_anio.

Cada evaluación corre en su SAVEPOINT (`en_savepoint`): si falla, se revierten solo sus filas.
Todas las escrituras van a ai_frecuencias; las tablas de Extreme no se tocan.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import text

from app.alertas import motor
from app.alertas import retraso_ingesta as rt
from app.db import lectura_real as lr
from app.db import repositorio as repo

log = logging.getLogger(__name__)

FUENTE_INGESTA = "progvehiculos"
RUTAS = (2, 16, 21)                        # = app.ingesta.RUTAS
SIN_NOTIFICACION = frozenset({"ALT-009"})  # DECISIONES §16.5: ambas condiciones sin notificaciones
DIAS_REPORTE_COMPLETITUD = 31


def persistir(c, eid: str, candidatas: list[dict], versiones: dict, notificar: bool = True) -> dict:
    """Deduplica contra las abiertas, guarda con historial y notifica SOLO lo guardado con severidad
    alta (nunca ALT-009)."""
    nuevas = motor.deduplicar(candidatas, repo.claves_alertas_abiertas(c))
    guardadas = []
    for a in nuevas:
        aid = repo.insertar_alerta(c, eid, a, a.get("umbral_version", versiones.get(a["codigo"])))
        if aid:
            guardadas.append({**a, "alert_id": int(aid)})
    avisar = [a for a in guardadas if a["severidad"] == "alta" and a["codigo"] not in SIN_NOTIFICACION]
    if notificar and avisar:
        # import tardío: servicios no depende de flujo
        from app.servicios import notificar as enviar
        enviar(c, avisar)
    return {"candidatas": len(candidatas), "tras_deduplicar": len(nuevas), "insertadas": len(guardadas),
            "alert_ids": [a["alert_id"] for a in guardadas], "notificadas": len(avisar) if notificar else 0}


def en_savepoint(c, nombre: str, fn) -> dict:
    """Ejecuta `fn` en un SAVEPOINT. Un error de la evaluación revierte solo sus filas y se informa;
    un fallo al abrir o cerrar el savepoint pertenece a la transacción principal y se propaga."""
    error_propio = None
    try:
        with c.begin_nested():
            try:
                return fn()
            except Exception as e:
                error_propio = e
                raise
    except Exception as e:
        if e is not error_propio or c.invalidated or not c.in_transaction():
            raise
        log.exception(f"falló la evaluación {nombre}")
        sqlstate = getattr(getattr(e, "orig", None), "sqlstate", None)
        return {"estado": "error", "motivo": f"{type(e).__name__}: {e}"[:300], "sqlstate": sqlstate}


# ------------------------------------------------------------------ ALT-009 por retraso de ingesta

def _bogota(d: datetime) -> datetime:
    return d.replace(tzinfo=rt.ZONA) if d.tzinfo is None else d


def verificar_retraso(c, eid: str, politica: rt.Politica | None, ahora: datetime | None = None,
                      rutas=RUTAS, no_evaluable: dict | None = None, verificar_autorizacion=None) -> dict:
    """Clasifica los ciclos de cada ruta, guarda una alerta por episodio de 2 o más ciclos vencidos y
    deja constancia en el historial del crecimiento y de la recuperación. Idempotente.

    `verificar_autorizacion(intento) -> bool` confirma reintentos manuales. No hay un mecanismo aprobado:
    el ciclo diario pasa None y ningún reintento cuenta (se conservan en `intentos_no_usados`)."""
    if politica is None:
        return {**(no_evaluable or {"estado": "no_evaluable", "motivo": "politica_operativa_no_configurada"}),
                "insertadas": 0}
    ahora = rt.local(ahora) if ahora is not None else datetime.now(rt.ZONA).replace(tzinfo=None)
    # Un verificador a la vez por fuente: dos ejecuciones simultáneas no duplican notas ni alertas.
    c.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
              {"k": rt.PREFIJO_CLAVE + politica.fuente})
    desde = _bogota(datetime.combine(politica.inicio_monitoreo - timedelta(days=1), politica.apertura))
    intentos = [rt.intento_desde_fila(f) for f in repo.ingestas_registradas(c, politica.fuente, desde)]
    rt.marcar_autorizaciones(intentos, verificar_autorizacion)
    resumen_rutas, insertadas, alert_ids, notas = {}, 0, [], 0
    for ruta in rutas:
        ciclos, no_usados = rt.ciclos_de_ruta(politica, ruta, intentos, ahora)
        eps = rt.episodios(ruta, ciclos, intentos, politica.fuente)
        existentes = repo.alertas_con_prefijo(c, "ALT-009", rt.prefijo_ruta(politica.fuente, ruta))
        notas_previas = repo.notas_de_alertas(c, [e["alert_id"] for e in existentes], rt.MARCA_NOTA)

        def recuperada(e, notas_previas=notas_previas):
            return any(" recuperado en " in n for n in notas_previas.get(e["alert_id"], []))

        detalle = []
        for ep in (e for e in eps if e.incumplimiento):
            k = rt.clave(politica.fuente, ruta, ep.primero.fecha)
            if ep.truncado:
                # La racha empieza en el primer ciclo observado: puede venir de antes del rango consultado.
                previas = [e for e in existentes if not recuperada(e)
                           and rt.primer_ciclo_de_clave(e["clave_dedup"]) < ep.primero.fecha]
                if previas:
                    k = max(previas, key=lambda e: rt.primer_ciclo_de_clave(e["clave_dedup"]))["clave_dedup"]
            mismas = [e for e in existentes if e["clave_dedup"] == k]
            if mismas:
                fila = mismas[-1]
                aid, accion = int(fila["alert_id"]), f"existente_{fila['estado']}"
                registrados = [int(float(fila["valor_observado"] or 0))] + [
                    int(n.split("ciclos_vencidos=")[1].split()[0])
                    for n in notas_previas.get(aid, []) if "ciclos_vencidos=" in n]
                if len(ep.ciclos) > max(registrados):
                    repo.anotar_alerta(c, aid, rt.nota_crecimiento(ep))
                    notas += 1
            else:
                aid = repo.insertar_alerta(c, eid, rt.alerta(ep, politica, k), None)
                accion = "creada" if aid else "ya_abierta_concurrente"
                if aid:
                    insertadas += 1
                    alert_ids.append(int(aid))
                    existentes.append({"alert_id": aid, "estado": "abierta", "clave_dedup": k,
                                       "valor_observado": len(ep.ciclos)})
            if aid and ep.recuperado_en is not None and not any(
                    " recuperado en " in n for n in notas_previas.get(aid, [])):
                repo.anotar_alerta(c, aid, rt.nota_recuperacion(ep))
                notas += 1
            detalle.append({"clave": k, "alert_id": aid, "accion": accion,
                            "ciclos": [x.ciclo_id for x in ep.ciclos],
                            "recuperado_en": None if ep.recuperado_en is None else ep.recuperado_en.isoformat(sep=" "),
                            "recuperado_por": ep.recuperado_por})
        resumen_rutas[int(ruta)] = {
            "ciclos": [x.resumen() for x in ciclos], "episodios": detalle,
            "rachas_sin_incumplimiento": sum(not e.incumplimiento for e in eps),
            "intentos_no_usados": no_usados}
    return {"estado": "evaluada", "regla": rt.REGLA, "politica": politica.describir(),
            "ahora": ahora.isoformat(sep=" "), "insertadas": insertadas, "alert_ids": alert_ids,
            "notas_historial": notas, "notificadas": 0,
            "verificacion_autorizacion": "aprobada" if verificar_autorizacion is not None else "no_disponible",
            "severidad": {"funcional": rt.SEVERIDAD_FUNCIONAL, "persistida": rt.SEVERIDAD_PERSISTIDA,
                          "equivalencia": "pendiente de aprobación"},
            "rutas": resumen_rutas}


# ------------------------------------------------------------------ completitud (ALT-005, 007 y 010)

def completitud_de_ventana(c, rutas, desde: date, hasta: date) -> dict:
    """Qué días de la ventana tienen evidencia de completitud en ai_frecuencias.completitud_ruta_dia.
    La tabla vacía NO es evidencia: sin fila 'completo' el día no es evaluable."""
    existe = c.execute(text("SELECT to_regclass('ai_frecuencias.completitud_ruta_dia') IS NOT NULL")).scalar()
    out = {"tabla_existe": bool(existe), "desde": desde.isoformat(), "hasta": hasta.isoformat(),
           "dias_completos": 0, "dias_parciales": 0, "dias_sin_evidencia": []}
    estados = lr.completitud_por_dia(c, list(rutas), pd.Timestamp(desde), pd.Timestamp(hasta)) if existe else {}
    d = desde
    while d <= hasta:
        for r in rutas:
            e = estados.get((int(r), d))
            if e == "completo":
                out["dias_completos"] += 1
            elif e == "parcial":
                out["dias_parciales"] += 1
            else:
                out["dias_sin_evidencia"].append(f"{int(r)}:{d.isoformat()}")
        d += timedelta(days=1)
    return out


def _no_evaluable_por_completitud(comp: dict) -> dict | None:
    if not comp["tabla_existe"]:
        return {"estado": "no_evaluable", "motivo": "falta_tabla_completitud_ruta_dia", "completitud": comp,
                "insertadas": 0,
                "detalle": "Sin ai_frecuencias.completitud_ruta_dia no hay evidencia de completitud por ruta y "
                           "día; ningún par predicción-real es evaluable (docs/PROPUESTAS_ESQUEMA_ALERTAS.md)."}
    if comp["dias_completos"] == 0:
        return {"estado": "no_evaluable", "motivo": "completitud_desconocida", "completitud": comp,
                "insertadas": 0,
                "detalle": "Ningún día de la ventana tiene evidencia 'completo' para estas rutas."}
    return None


def evaluar_alt_005_007(c, eid: str, marca_anterior, marca_nueva, rutas=RUTAS) -> dict:
    from app.alertas import desde_m1
    from app.core.config import obtener_config
    from app.core.errores import Conflicto
    if marca_nueva is None:
        return {"estado": "no_evaluable", "motivo": "sin_marca_confirmada", "insertadas": 0}
    corte_ant, corte = desde_m1.ventana_tras_ingesta(marca_anterior, marca_nueva, obtener_config().dias_revision)
    hasta = pd.Timestamp(corte).date()
    desde = hasta - timedelta(days=DIAS_REPORTE_COMPLETITUD)     # sin marca anterior: últimos 31 días
    if corte_ant is not None:
        desde = max(desde, pd.Timestamp(corte_ant).date())
    comp = completitud_de_ventana(c, rutas, desde, hasta)
    if (bloqueo := _no_evaluable_por_completitud(comp)) is not None:
        return bloqueo
    try:
        r = desde_m1.evaluar_tras_ingesta(c, marca_anterior, marca_nueva)
    except Conflicto as e:
        return {"estado": "no_evaluable", "motivo": str(e), "insertadas": 0, "completitud": comp}
    p = persistir(c, eid, r["alertas"], r.get("umbral_version") or {})
    return {"estado": "evaluada", "completitud": comp, "conteo_estados_m1": r["conteo_estados_m1"],
            "observaciones_validas": r["observaciones_validas"], "candidatas_m1": r["candidatas"],
            "huella_m1": r.get("huella_m1"), **p}


def evaluar_alt_010(c, eid: str, rutas=RUTAS) -> dict:
    from app.alertas import alt010_desde_m1
    from app.core.config import obtener_config
    from app.core.errores import Conflicto
    wm = lr.marca_agua(c)
    v = alt010_desde_m1.ventanas(None if wm is None else wm["ultima_actualizacion"], obtener_config().dias_revision)
    if v is None:
        return {"estado": "no_evaluable", "motivo": "sin_marca_agua", "insertadas": 0}
    comp = completitud_de_ventana(c, rutas, v["referencia"][0].date(),
                                  (v["reciente"][1] - pd.Timedelta(days=1)).date())
    if (bloqueo := _no_evaluable_por_completitud(comp)) is not None:
        return bloqueo
    try:
        r = alt010_desde_m1.simular(c)
    except Conflicto as e:
        return {"estado": "no_evaluable", "motivo": str(e), "insertadas": 0, "completitud": comp}
    p = persistir(c, eid, r["alertas"], {})
    return {"estado": "evaluada", "completitud": comp, "candidatas_regla": r["candidatas"],
            "rutas": r.get("rutas"), **p}


# ------------------------------------------------------------------ ALT-006 (salida tardía)

def evaluar_alt_006(c, eid: str, marca_anterior, marca_nueva, rutas=RUTAS, evidencia_cobertura=None) -> dict:
    """Salidas programadas que se volvieron evaluables con esta ingesta (salida en (corte anterior, corte
    nuevo]; la primera vez, solo el último día consolidado). Fuente: ai_frecuencias.viaje_fuente, copia de
    progvehiculos con la salida PROGRAMADA (fechasalida) y el inicio REAL observado (fechainicio).
    Alerta por salidas tardías (inicio − salida > tolerancia vigente). Un inicio nulo, sin evidencia de
    cobertura de la fuente, queda «indeterminada» y no alerta (hoy no hay esa evidencia)."""
    from app.core.config import obtener_config
    if marca_nueva is None:
        return {"estado": "no_evaluable", "motivo": "sin_marca_confirmada", "insertadas": 0}
    dias = obtener_config().dias_revision
    corte = pd.Timestamp(marca_nueva) - pd.Timedelta(days=dias)
    inicio = (pd.Timestamp(marca_anterior) - pd.Timedelta(days=dias) if marca_anterior is not None
              else corte.normalize() - pd.Timedelta(days=1))
    if inicio >= corte:
        return {"estado": "evaluada", "motivo": "sin_salidas_nuevas_consolidadas", "insertadas": 0}
    salidas = repo.viajes_fuente(c, list(rutas), inicio.to_pydatetime(), corte.to_pydatetime())
    umbrales, versiones = repo.umbrales_vigentes(c)
    u = {**motor.umbrales_por_defecto(), **umbrales}
    if salidas.empty:
        return {"estado": "evaluada", "ventana": [str(inicio), str(corte)], "salidas": 0, "insertadas": 0}
    diag = motor.diagnostico_alt_006(salidas, u, marca_nueva, dias, evidencia_cobertura)
    p = persistir(c, eid, motor.alt_006(salidas, u, marca_nueva, dias, evidencia_cobertura), versiones)
    return {"estado": "evaluada", "regla": motor.REGLA_ALT006, "ventana": [str(inicio), str(corte)],
            "salidas": len(salidas), "conteo_por_clase": diag["conteo_por_clase"],
            "evidencia_cobertura": "configurada" if evidencia_cobertura else "no_disponible", **p}


# ------------------------------------------------------------------ ALT-008 (consolidado de Extreme)

def _independientes(referencia, evaluado) -> str | None:
    """Motivo por el que NO se pueden comparar, o None. Ambos deben ser de solo lectura, venir de tablas
    distintas, ninguna de ai_frecuencias, y con la misma granularidad."""
    if referencia is None or evaluado is None:
        return "sin_fuente_consolidado_autorizada"
    if not (getattr(referencia, "autorizada", False) is True and getattr(referencia, "solo_lectura", False) is True
            and getattr(evaluado, "solo_lectura", False) is True):
        return "fuente_no_autorizada_o_no_de_solo_lectura"
    o_ref, o_eval = str(getattr(referencia, "origen", "") or ""), str(getattr(evaluado, "origen", "") or "")
    if not o_ref or not o_eval or any(o.lower().startswith("ai_frecuencias") for o in (o_ref, o_eval)):
        return "fuente_es_el_agregado_local"
    if o_ref == o_eval:
        return "referencia_y_evaluado_son_la_misma_tabla"
    if getattr(referencia, "granularidad", None) != getattr(evaluado, "granularidad", None):
        return "granularidad_distinta"
    return None


def periodos_cerrados_alt008(marca, dias_revision: int) -> list[int]:
    """Años completos ya consolidados: el 31-dic + 1 día está antes de marca − dias_revision."""
    if marca is None:
        return []
    corte = pd.Timestamp(marca) - pd.Timedelta(days=int(dias_revision))
    return [a for a in range(2023, corte.year + 1) if pd.Timestamp(a + 1, 1, 1) <= corte]


def evaluar_alt_008(c, eid: str, referencia=None, evaluado=None, periodos=None) -> dict:
    """Compara el consolidado de REFERENCIA de Extreme (hoy: timbradas_anio, por ruta y año) con lo
    EVALUADO (suma de subidas de progvehiculos con el mismo criterio de fecha). Ambos se leen con el
    usuario de solo lectura; solo la alerta se escribe en ai_frecuencias."""
    motivo = _independientes(referencia, evaluado)
    if motivo is not None:
        return {"estado": "no_evaluable", "motivo": motivo, "insertadas": 0}
    if not periodos:
        return {"estado": "no_evaluable", "motivo": "sin_periodos_cerrados", "insertadas": 0}
    ref = referencia.leer(periodos)
    ev = evaluado.leer(periodos)
    m = ref.merge(ev, on=["idruta", "periodo"], how="inner")
    if m.empty:
        return {"estado": "evaluada", "periodos_comparados": 0, "insertadas": 0,
                "periodos_sin_par": int(len(ref) + len(ev))}
    m["inicio_periodo"] = [pd.Timestamp(int(a), 1, 1) for a in m.periodo]
    if "prop_franjas_un_viaje" in m:
        m["evidencia"] = [
            f"Referencia {referencia.origen} ({int(r.franjas)} franjas; {r.prop_franjas_un_viaje:.0%} con nro_viajes = 1"
            + ("; posible falla de acumulación de Extreme, DECISIONES §9.1" if r.prop_franjas_un_viaje >= 0.9 else "")
            + f") frente a {evaluado.origen} ({int(r.viajes)} viajes aptos)." for r in m.itertuples()]
    umbrales, versiones = repo.umbrales_vigentes(c)
    u = {**motor.umbrales_por_defecto(), **umbrales}
    p = persistir(c, eid, motor.alt_008(m, u), versiones)
    return {"estado": "evaluada", "referencia": referencia.origen, "evaluado": evaluado.origen,
            "granularidad": referencia.granularidad, "periodos": list(periodos),
            "periodos_comparados": len(m), "periodos_sin_par": len(ref) + len(ev) - 2 * len(m), **p}


# ------------------------------------------------------------------ completitud (antes de 005/007/010)

def evaluar_completitud(c, eid: str, marca, referencia=None, rutas=RUTAS) -> dict:
    from app.calidad import completitud
    from app.core.config import obtener_config
    if marca is None:
        return {"estado": "no_evaluable", "motivo": "sin_marca_confirmada", "escritas": 0}
    if referencia is None:
        from app.alertas.fuentes_extreme import ProgramacionesPorDia
        referencia = ProgramacionesPorDia(tuple(rutas))
    dias_rev = obtener_config().dias_revision
    umbrales, _ = repo.umbrales_vigentes(c)
    umbral = float({**motor.umbrales_por_defecto(), **umbrales}["ALT-009"])
    filas = completitud.calcular(c, referencia, rutas, completitud.dias_a_revisar(marca, dias_rev), marca,
                                 dias_rev, umbral, FUENTE_INGESTA)
    return {"regla": completitud.REGLA, **completitud.registrar(c, eid, filas)}


# ------------------------------------------------------------------ después de la ingesta

def alertas_tras_ingesta(c, marca_anterior, marca_nueva, correlation_id: str,
                         politica: rt.Politica | None = None, no_evaluable_retraso: dict | None = None,
                         ahora: datetime | None = None, rutas=RUTAS, fuentes: dict | None = None) -> dict:
    """Ejecución 'alertas' con sus evaluaciones, cada una en su SAVEPOINT. Primero la completitud (la usan
    005, 007 y 010). `fuentes` permite sustituir los adaptadores de Extreme (pruebas)."""
    from app.alertas import fuentes_extreme as fx
    from app.core.config import obtener_config
    f = {"programaciones": fx.ProgramacionesPorDia(tuple(rutas)),
         "referencia_008": fx.ConsolidadoTimbradasAnio(tuple(rutas)), "evaluado_008": fx.SubidasViajesAnio(tuple(rutas)),
         **(fuentes or {})}
    eid = repo.crear_ejecucion(c, "alertas", {"origen": "ciclo_diario", "marca_anterior": marca_anterior,
                                              "marca_nueva": marca_nueva}, correlation_id)
    r = {"execution_id": eid}
    r["completitud"] = en_savepoint(c, "completitud", lambda: evaluar_completitud(
        c, eid, marca_nueva, f["programaciones"], rutas))
    r["alt_009_retraso"] = en_savepoint(c, "ALT-009 retraso", lambda: verificar_retraso(
        c, eid, politica, ahora, rutas, no_evaluable_retraso))
    r["alt_005_007"] = en_savepoint(c, "ALT-005/007",
                                    lambda: evaluar_alt_005_007(c, eid, marca_anterior, marca_nueva, rutas))
    r["alt_010"] = (en_savepoint(c, "ALT-010", lambda: evaluar_alt_010(c, eid, rutas)) if marca_nueva is not None
                    else {"estado": "no_evaluable", "motivo": "sin_marca_confirmada", "insertadas": 0})
    r["alt_006"] = en_savepoint(c, "ALT-006", lambda: evaluar_alt_006(c, eid, marca_anterior, marca_nueva, rutas))
    r["alt_008"] = en_savepoint(c, "ALT-008", lambda: evaluar_alt_008(
        c, eid, f["referencia_008"], f["evaluado_008"],
        periodos_cerrados_alt008(marca_nueva, obtener_config().dias_revision)))
    r["insertadas"] = sum(int(v.get("insertadas") or 0) for v in r.values() if isinstance(v, dict))
    repo.cerrar_ejecucion(c, eid, "exitosa", r)
    return r
