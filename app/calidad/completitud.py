"""Evidencia de completitud por ruta y día (regla completitud-v1). Alimenta
ai_frecuencias.completitud_ruta_dia, que leen M1, ALT-005, ALT-007 y ALT-010.

Qué significa «completo» aquí: la COPIA que tenemos del día (viaje_fuente) coincide con lo que Extreme
tiene programado para esa ruta y día, la ingesta que la trajo terminó completa y la cobertura de conteos
de los viajes elegibles alcanza el umbral. NO certifica que la fuente de Extreme haya registrado todo lo
que ocurrió (DECISIONES §16.1 y §16.6).

Para marcar un día `completo` deben cumplirse las cuatro condiciones:
1. El día está consolidado: termina antes de marca de agua − dias_revision.
2. La última ingesta registrada de la fuente terminó `exitosa`, completa y con consulta válida.
3. La referencia externa: el número de programaciones de Extreme para la ruta y el día (progvehiculos,
   leído con el usuario de solo lectura) es mayor que cero e IGUAL al número de viajes copiados.
4. La cobertura de conteos de los viajes elegibles (finalizado S, no cancelado; conteo no nulo ni
   negativo) es mayor o igual al umbral vigente de ALT-009 (cobertura_minima).

Si se cumplen 1 a 3 pero no 4, o si faltan o sobran viajes, el día queda `parcial`. Si falla 1 o 2, o si
Extreme no tiene programaciones ese día, queda `desconocido`. Cada fila guarda su evidencia.

La tabla NO existe en el esquema aprobado (revisión 0008). Su DDL está en
propuestas/migraciones/0010_completitud_ruta_dia.sql, pendiente de aprobación. Sin la tabla, `registrar`
calcula los estados, NO escribe nada y responde `tabla_pendiente_de_aprobacion` con lo que escribiría.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

REGLA = "completitud-v1"
DIAS_REVISAR = 35                 # cubre la ventana de ALT-010 (7 + 28 días)
COLUMNAS = {"idruta", "fecha", "estado", "regla_version", "evidencia", "execution_id", "calculado_en"}


def tabla_lista(c) -> tuple[bool, str | None]:
    existe = c.execute(text("SELECT to_regclass('ai_frecuencias.completitud_ruta_dia') IS NOT NULL")).scalar()
    if not existe:
        return False, "tabla_pendiente_de_aprobacion"
    cols = {r[0] for r in c.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'ai_frecuencias' AND table_name = 'completitud_ruta_dia'"""))}
    if not COLUMNAS <= cols:
        return False, f"tabla_con_otra_estructura (faltan {sorted(COLUMNAS - cols)})"
    hist = c.execute(text(
        "SELECT to_regclass('ai_frecuencias.completitud_ruta_dia_historial') IS NOT NULL")).scalar()
    return (True, None) if hist else (False, "falta_completitud_ruta_dia_historial")


def _ultima_ingesta(c, fuente: str) -> dict | None:
    f = c.execute(text("""
        SELECT execution_id::text AS execution_id, estado, resumen, finalizada_en FROM execution_log
        WHERE tipo = 'ingesta' AND parametros->>'fuente' = :f AND estado <> 'en_curso'
        ORDER BY iniciada_en DESC LIMIT 1"""), {"f": fuente}).mappings().first()
    return dict(f) if f else None


def calcular(c, referencia, rutas, dias: list[date], marca, dias_revision: int, umbral_cobertura: float,
             fuente: str = "progvehiculos") -> list[dict]:
    """Estado y evidencia de cada (ruta, día). `referencia.leer(desde, hasta)` da las programaciones de
    Extreme por ruta y día (solo lectura)."""
    if not dias:
        return []
    desde, hasta = min(dias), max(dias) + timedelta(days=1)
    corte = None if marca is None else pd.Timestamp(marca) - pd.Timedelta(days=int(dias_revision))
    ing = _ultima_ingesta(c, fuente)
    res = (ing or {}).get("resumen") or {}
    ingesta_ok = bool(ing and ing["estado"] == "exitosa" and res.get("completa") is True
                      and res.get("consulta_valida") is True)
    ext = referencia.leer(desde, hasta)
    esperadas = {(int(r.idruta), pd.Timestamp(r.fecha).date()): int(r.programaciones) for r in ext.itertuples()}
    loc = pd.read_sql(text("""
        SELECT idruta, fechasalida::date AS fecha, COUNT(*) AS recibidas,
               COUNT(*) FILTER (WHERE finalizado = 'S' AND cancelada = 'N') AS elegibles,
               COUNT(*) FILTER (WHERE finalizado = 'S' AND cancelada = 'N'
                                AND numeropasajeros IS NOT NULL AND numeropasajeros >= 0) AS con_conteo
        FROM ai_frecuencias.viaje_fuente
        WHERE idruta = ANY(:r) AND fechasalida >= :d AND fechasalida < :h GROUP BY 1, 2"""),
        c, params={"r": [int(r) for r in rutas], "d": desde, "h": hasta})
    locales = {(int(r.idruta), pd.Timestamp(r.fecha).date()): r for r in loc.itertuples()}
    out = []
    for d in dias:
        for ruta in rutas:
            k = (int(ruta), d)
            l = locales.get(k)
            recibidas = int(l.recibidas) if l is not None else 0
            elegibles = int(l.elegibles) if l is not None else 0
            con_conteo = int(l.con_conteo) if l is not None else 0
            cobertura = None if elegibles == 0 else round(con_conteo / elegibles, 4)
            esp = esperadas.get(k, 0)
            consolidado = corte is not None and pd.Timestamp(d) + pd.Timedelta(days=1) <= corte
            motivos = []
            if not consolidado:
                motivos.append("dia_sin_consolidar")
            if not ingesta_ok:
                motivos.append("ultima_ingesta_no_completa")
            if esp == 0:
                motivos.append("sin_programaciones_en_la_referencia")
            if motivos:
                estado = "desconocido"
            else:
                if recibidas != esp:
                    motivos.append(f"copia_{'incompleta' if recibidas < esp else 'con_sobrantes'}")
                if cobertura is None or cobertura < umbral_cobertura:
                    motivos.append("cobertura_de_conteos_bajo_umbral")
                estado = "parcial" if motivos else "completo"
            out.append({"idruta": int(ruta), "fecha": d, "estado": estado, "evidencia": {
                "regla": REGLA, "referencia": referencia.origen, "programaciones_referencia": esp,
                "viajes_copiados": recibidas, "elegibles": elegibles, "con_conteo": con_conteo,
                "cobertura_conteos": cobertura, "umbral_cobertura": umbral_cobertura,
                "marca_agua": None if marca is None else str(pd.Timestamp(marca)),
                "corte_consolidacion": None if corte is None else str(corte),
                "ingesta": None if ing is None else {"execution_id": ing["execution_id"], "estado": ing["estado"],
                                                      "completa": res.get("completa"),
                                                      "consulta_valida": res.get("consulta_valida")},
                "motivos": motivos}})
    return out


def dias_a_revisar(marca, dias_revision: int, n: int = DIAS_REVISAR) -> list[date]:
    if marca is None:
        return []
    ultimo = (pd.Timestamp(marca) - pd.Timedelta(days=int(dias_revision))).normalize() - pd.Timedelta(days=1)
    return [(ultimo - pd.Timedelta(days=k)).date() for k in range(n - 1, -1, -1)]


def registrar(c, eid: str, filas: list[dict]) -> dict:
    """Escribe los estados si la tabla aprobada existe; antes copia al historial la fila que reemplaza.
    Una fila que no cambia de estado ni de evidencia no se reescribe."""
    lista, motivo = tabla_lista(c)
    conteo = {e: sum(f["estado"] == e for f in filas) for e in ("completo", "parcial", "desconocido")}
    if not lista:
        return {"estado": "no_evaluable", "motivo": motivo, "escritas": 0, "calculadas": conteo,
                "detalle": "Se calcularon los estados, pero no se escribió nada: la tabla es una propuesta "
                           "(propuestas/migraciones/0010_completitud_ruta_dia.sql)."}
    escritas = sin_cambio = 0
    for f in filas:
        evidencia = json.dumps(f["evidencia"], default=str, sort_keys=True)
        prev = c.execute(text("""SELECT estado, evidencia::text AS ev FROM completitud_ruta_dia
                                 WHERE idruta = :r AND fecha = :f FOR UPDATE"""),
                         {"r": f["idruta"], "f": f["fecha"]}).mappings().first()
        if prev and prev["estado"] == f["estado"] and json.loads(prev["ev"]) == json.loads(evidencia):
            sin_cambio += 1
            continue
        if prev:
            c.execute(text("""INSERT INTO completitud_ruta_dia_historial
                              SELECT * FROM completitud_ruta_dia WHERE idruta = :r AND fecha = :f"""),
                      {"r": f["idruta"], "f": f["fecha"]})
        c.execute(text("""
            INSERT INTO completitud_ruta_dia (idruta, fecha, estado, regla_version, evidencia, execution_id)
            VALUES (:r, :f, :e, :v, CAST(:ev AS jsonb), CAST(:x AS uuid))
            ON CONFLICT (idruta, fecha) DO UPDATE SET estado = EXCLUDED.estado, regla_version = EXCLUDED.regla_version,
                evidencia = EXCLUDED.evidencia, execution_id = EXCLUDED.execution_id, calculado_en = now()"""),
                  {"r": f["idruta"], "f": f["fecha"], "e": f["estado"], "v": REGLA, "ev": evidencia, "x": eid})
        escritas += 1
    return {"estado": "evaluada", "escritas": escritas, "sin_cambio": sin_cambio, "calculadas": conteo}
