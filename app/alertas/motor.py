"""Motor de alertas explicables ALT-001 a ALT-010.

Cada regla recibe un DataFrame y los umbrales vigentes ({codigo: valor}) y devuelve
una lista de alertas con: codigo, severidad, idruta, franja_inicio, horizonte_min,
valor_observado, valor_esperado, umbral, puntuacion_anomalia, explicacion,
recomendacion y clave_dedup. El estado inicial siempre es 'abierta'.
"""
import hashlib

import pandas as pd

from app.alertas.catalogo import CATALOGO, HORIZONTE_ALERTAS_FRECUENCIA, TRANSICIONES
from app.calidad import cuarentena


class TransicionInvalida(ValueError):
    pass


def umbrales_por_defecto() -> dict:
    return {c: v["valor"] for c, v in CATALOGO.items()}


def clave_dedup(codigo, idruta, franja_inicio=None, horizonte_min=None) -> str:
    texto = f"{codigo}|{idruta}|{franja_inicio}|{horizonte_min}"
    return hashlib.sha1(texto.encode()).hexdigest()[:16]


def _alerta(codigo, idruta, observado, esperado, umbral, puntuacion, explicacion,
            franja_inicio=None, horizonte_min=None, severidad=None):
    c = CATALOGO[codigo]
    if severidad is None:
        severidad = c["severidad"]
        if severidad != "alta" and puntuacion is not None and abs(puntuacion) >= 1.0:
            severidad = "alta"
    return {
        "codigo": codigo, "nombre": c["nombre"], "severidad": severidad, "estado": "abierta",
        "idruta": int(idruta),
        "franja_inicio": None if franja_inicio is None else pd.Timestamp(franja_inicio),
        "horizonte_min": None if horizonte_min is None else int(horizonte_min),
        "valor_observado": None if observado is None else round(float(observado), 3),
        "valor_esperado": None if esperado is None else round(float(esperado), 3),
        "umbral": float(umbral),
        "puntuacion_anomalia": None if puntuacion is None else round(float(puntuacion), 3),
        "explicacion": explicacion, "recomendacion": c["recomendacion"],
        "clave_dedup": clave_dedup(codigo, idruta, franja_inicio, horizonte_min),
    }


def _ocupacion(r) -> float | None:
    if r.buses_hora_actual is None or pd.isna(r.buses_hora_actual) or r.buses_hora_actual <= 0:
        return None
    return r.carga_max_estimada_hora / (r.buses_hora_actual * r.capacidad_nominal)


# ----------------------------------------------------------------- reglas de pronóstico

def alt_001(pred: pd.DataFrame, u: dict) -> list:
    """pred: contrato de prediction + demanda_referencia (media día-hora histórica)."""
    out = []
    for r in pred.dropna(subset=["demanda_referencia"]).itertuples(index=False):
        if r.demanda_referencia <= 0:
            continue
        limite = r.demanda_referencia * u["ALT-001"]
        if r.demanda_pred > limite:
            exceso = r.demanda_pred / r.demanda_referencia - 1
            out.append(_alerta(
                "ALT-001", r.idruta, r.demanda_pred, r.demanda_referencia, u["ALT-001"],
                exceso / (u["ALT-001"] - 1) - 1 if u["ALT-001"] > 1 else exceso,
                f"Se esperan {r.demanda_pred:.0f} subidas en {r.horizonte_min} min desde "
                f"{pd.Timestamp(r.franja_inicio):%H:%M}, {exceso:.0%} sobre lo habitual "
                f"({r.demanda_referencia:.0f}).", r.franja_inicio, r.horizonte_min))
    return out


def alt_002_003_004(rec: pd.DataFrame, u: dict,
                    horizonte: int | None = HORIZONTE_ALERTAS_FRECUENCIA) -> list:
    """rec: contrato de recommendation (necesita buses_hora_actual).

    Solo evalúa las recomendaciones de `horizonte` (60 min, ver catalogo.py). No modifica `rec`:
    las recomendaciones de los demás horizontes siguen existiendo. horizonte=None evalúa todas."""
    out = []
    if horizonte is not None:
        rec = rec[rec.horizonte_min == horizonte]
    for r in rec.itertuples(index=False):
        occ = _ocupacion(r)
        if occ is not None and occ >= u["ALT-002"]:
            out.append(_alerta(
                "ALT-002", r.idruta, occ, r.ocupacion_objetivo, u["ALT-002"],
                (occ - u["ALT-002"]) / (1 - u["ALT-002"]),
                f"Con {r.buses_hora_actual:.0f} buses/h la ocupación prevista es {occ:.0%} "
                f"(máximo {u['ALT-002']:.0%}).", r.franja_inicio, r.horizonte_min))
        if (r.es_viable and r.buses_hora_actual is not None and not pd.isna(r.buses_hora_actual)
                and r.buses_hora_recomendado - r.buses_hora_actual >= u["ALT-003"]):
            deficit = r.buses_hora_recomendado - r.buses_hora_actual
            out.append(_alerta(
                "ALT-003", r.idruta, r.buses_hora_actual, r.buses_hora_recomendado, u["ALT-003"],
                deficit / max(r.buses_hora_recomendado, 1),
                f"Hoy salen {r.buses_hora_actual:.0f} buses/h y se recomiendan "
                f"{r.buses_hora_recomendado} (faltan {deficit:.0f}).",
                r.franja_inicio, r.horizonte_min))
        if occ is not None and occ < u["ALT-004"]:
            out.append(_alerta(
                "ALT-004", r.idruta, occ, r.ocupacion_objetivo, u["ALT-004"],
                (u["ALT-004"] - occ) / u["ALT-004"],
                f"Con {r.buses_hora_actual:.0f} buses/h la ocupación prevista es solo {occ:.0%}.",
                r.franja_inicio, r.horizonte_min))
    return out


# ----------------------------------------------------------------- reglas sobre lo observado

def alt_005_007(obs: pd.DataFrame, u: dict) -> list:
    """obs: idruta, franja_inicio, horizonte_min, demanda_pred, demanda_real."""
    out = []
    for r in obs.dropna(subset=["demanda_real", "demanda_pred"]).itertuples(index=False):
        base = max(r.demanda_pred, 1.0)
        rel = (r.demanda_real - r.demanda_pred) / base
        if rel <= -u["ALT-005"]:
            out.append(_alerta(
                "ALT-005", r.idruta, r.demanda_real, r.demanda_pred, u["ALT-005"],
                -rel / u["ALT-005"] - 1,
                f"Se registraron {r.demanda_real:.0f} subidas frente a {r.demanda_pred:.0f} "
                f"previstas ({rel:.0%}).", r.franja_inicio, r.horizonte_min))
        elif abs(rel) > u["ALT-007"]:
            out.append(_alerta(
                "ALT-007", r.idruta, r.demanda_real, r.demanda_pred, u["ALT-007"],
                abs(rel) / u["ALT-007"] - 1,
                f"El pronóstico se desvió {rel:+.0%} ({r.demanda_real:.0f} reales frente a "
                f"{r.demanda_pred:.0f}).", r.franja_inicio, r.horizonte_min))
    return out


def alt_006(salidas: pd.DataFrame, u: dict) -> list:
    """salidas: idruta, idprogramacion, fechasalida (programada), fechainicio (real),
    cancelada ('S'/'N')."""
    out = []
    for r in salidas.itertuples(index=False):
        if r.cancelada == "S" or pd.isna(r.fechainicio):
            out.append(_alerta(
                "ALT-006", r.idruta, None, None, u["ALT-006"], None,
                f"La salida programada {pd.Timestamp(r.fechasalida):%Y-%m-%d %H:%M} "
                f"(programación {r.idprogramacion}) no se realizó.",
                pd.Timestamp(r.fechasalida).floor("15min")))
            continue
        retraso = (pd.Timestamp(r.fechainicio) - pd.Timestamp(r.fechasalida)).total_seconds() / 60
        if retraso > u["ALT-006"]:
            out.append(_alerta(
                "ALT-006", r.idruta, retraso, 0, u["ALT-006"], retraso / u["ALT-006"] - 1,
                f"La salida de las {pd.Timestamp(r.fechasalida):%H:%M} (programación "
                f"{r.idprogramacion}) salió {retraso:.0f} min tarde.",
                pd.Timestamp(r.fechasalida).floor("15min")))
    return out


def alt_008(consolidado: pd.DataFrame, u: dict) -> list:
    """consolidado: idruta, periodo, subidas_consolidado (Extreme), subidas_viajes."""
    out = []
    for r in consolidado.itertuples(index=False):
        if r.subidas_viajes <= 0:
            continue
        dif = (r.subidas_consolidado - r.subidas_viajes) / r.subidas_viajes
        if abs(dif) > u["ALT-008"]:
            out.append(_alerta(
                "ALT-008", r.idruta, r.subidas_consolidado, r.subidas_viajes, u["ALT-008"],
                abs(dif) / u["ALT-008"] - 1,
                f"El consolidado de {r.periodo} suma {r.subidas_consolidado:,.0f} subidas y los "
                f"viajes {r.subidas_viajes:,.0f} ({dif:+.0%})."))
    return out


# ALT-009, causa «información incompleta» (regla alt009-v1): cobertura = 1 - viajes elegibles sin
# conteo usable (pasajeros_sin_conteo, comun-1.5) / viajes elegibles, por ruta y día de salida, solo en
# días consolidados (el día completo termina antes de marca - DIAS_REVISION, como ALT-006); alerta si
# cobertura < umbral vigente (u["ALT-009"]). La causa «información retrasada» no se implementa: requiere
# un registro confiable de cada ejecución de la ingesta (G4).
REGLA_ALT009 = "alt009-v1"
# Especificación v1.5 §5.2: ALT-009 es «Crítica»; la base solo admite baja/media/alta y no lee
# alert_catalog.severidad_base, así que la regla emite «alta» (como ALT-006) sin tocar el catálogo.
SEVERIDAD_ALT009 = "alta"
REGLA_SIN_CONTEO = "pasajeros_sin_conteo"          # cuarentena.py y DECISIONES.md §8
TABLA_ORIGEN_ALT009 = "progvehiculos"              # ingesta.filas_calidad
COLUMNAS_DETECCIONES = ("idprogramacion", "lote", "tabla_origen", "regla", "reglas_version")
# Lectura que alimenta `detecciones` (solo lectura; no se ejecuta aquí). Cada detección se une a su
# evento por evento_id y se filtra por tabla de origen, regla y versión de reglas.
CONSULTA_DETECCIONES_ALT009 = """
SELECT e.idprogramacion, d.lote, d.detectado_en, e.tabla_origen, e.regla, e.reglas_version
FROM ai_frecuencias.data_quality_deteccion AS d
JOIN ai_frecuencias.data_quality_error AS e ON e.id = d.evento_id
WHERE e.tabla_origen = :tabla_origen AND e.regla = :regla AND e.reglas_version = :reglas_version
"""


def _sin_conteo_por_viaje(viajes: pd.DataFrame) -> pd.DataFrame:
    """Marca cada viaje con la misma lógica de comun-1.5 que el FE (reglas_comunes.clasificar_dias):
    elegible = finalizado S y cancelada N; sin_conteo = elegible sin conteo usable (nulo, negativo o no
    finito) que NO está en cuarentena por otra regla de etiqueta (pax_bloquea & ~cuarentena_etiqueta)."""
    P, _ = cuarentena.marcar(viajes)
    return pd.DataFrame({
        "idprogramacion": P.id_programacion.astype("int64").to_numpy(),
        "idruta": P.id_ruta.astype(int).to_numpy(),
        "salida": pd.to_datetime(P.salida_programada).to_numpy(),
        "elegible": P.elegible.astype(bool).to_numpy(),
        "sin_conteo": (P.pax_bloquea & ~P.cuarentena_etiqueta).astype(bool).to_numpy(),
        "valor": P.pasajeros_original.to_numpy(dtype="float64"),
    })


def _dia_consolidado(fecha: pd.Series, corte: pd.Timestamp) -> pd.Series:
    """True si el día completo [fecha, fecha + 1 día) es anterior al corte de consolidación: un día
    con alguna salida igual o posterior al corte no se evalúa (ni siquiera en parte)."""
    return fecha + pd.Timedelta(days=1) <= corte


def _lotes_sin_conteo(detecciones: pd.DataFrame | None) -> dict:
    """{idprogramacion: lotes (sin repetir, ordenados)} de las detecciones de pasajeros_sin_conteo.

    detecciones: resultado de CONSULTA_DETECCIONES_ALT009 (data_quality_deteccion unida a
    data_quality_error por evento_id). Exige tabla_origen, regla y reglas_version y solo usa las de
    progvehiculos, pasajeros_sin_conteo y comun-1.5: una detección de otra tabla, regla o versión no
    cuenta."""
    if detecciones is None:
        return {}
    faltan = [c for c in COLUMNAS_DETECCIONES if c not in detecciones.columns]
    if faltan:
        raise ValueError(f"ALT-009: a las detecciones les faltan columnas: {', '.join(faltan)}")
    d = detecciones[detecciones.tabla_origen.eq(TABLA_ORIGEN_ALT009) & detecciones.regla.eq(REGLA_SIN_CONTEO)
                    & detecciones.reglas_version.eq(cuarentena.REGLAS_VERSION)]
    return {int(i): sorted(set(g.lote.astype(str))) for i, g in d.groupby("idprogramacion")}


def cobertura_alt_009(viajes: pd.DataFrame, detecciones: pd.DataFrame | None, marca,
                      dias_revision: int) -> pd.DataFrame:
    """Cobertura de conteo por ruta y día de salida, solo en días consolidados.

    viajes: idprogramacion, idruta, fechasalida, fechainicio, fechafin, finalizado, cancelada,
    numeropasajeros[, numerobajadas] (columnas de viaje_fuente).
    detecciones: None (sin fuente de detecciones: todos los viajes sin conteo quedan «sin detección
    registrada») o el resultado de CONSULTA_DETECCIONES_ALT009 con las cinco columnas obligatorias
    idprogramacion, lote, tabla_origen, regla y reglas_version; se validan siempre, aunque no haya viajes.

    cobertura = 1 - n_sin_conteo / n_elegibles. Solo se evalúan días consolidados: el día completo
    termina antes de marca - dias_revision (como ALT-006), así que ninguna salida igual o posterior al
    corte entra. Los días sin consolidar o sin elegibles no aparecen aquí (ver diagnostico_alt_009). Un
    viaje detectado en varios lotes cuenta una vez y conserva sus lotes."""
    columnas = ["idruta", "fecha", "elegibles", "sin_conteo", "cobertura", "ids_sin_conteo",
                "valores_sin_conteo", "lotes_sin_conteo", "ids_sin_deteccion", "corte_consolidacion"]
    corte = pd.Timestamp(marca) - pd.Timedelta(days=int(dias_revision))
    lotes = _lotes_sin_conteo(detecciones)            # valida las detecciones aunque no haya viajes
    if viajes.empty:
        return pd.DataFrame(columns=columnas)
    v = _sin_conteo_por_viaje(viajes).assign(fecha=lambda d: d.salida.dt.normalize())
    v = v[v.elegible & _dia_consolidado(v.fecha, corte)]
    filas = []
    for (ruta, fecha), g in v.groupby(["idruta", "fecha"], sort=True):
        s = g[g.sin_conteo].sort_values("idprogramacion")
        ids = [int(i) for i in s.idprogramacion]
        filas.append({
            "idruta": int(ruta), "fecha": pd.Timestamp(fecha), "elegibles": len(g), "sin_conteo": len(s),
            "cobertura": 1 - len(s) / len(g), "ids_sin_conteo": ids,
            "valores_sin_conteo": {int(i): (None if pd.isna(x) else float(x))
                                   for i, x in zip(s.idprogramacion, s.valor)},
            "lotes_sin_conteo": {i: lotes.get(i, []) for i in ids},
            "ids_sin_deteccion": [i for i in ids if not lotes.get(i)],
            "corte_consolidacion": corte})
    return pd.DataFrame(filas, columns=columnas)


def _evidencia_alt_009(r) -> str:
    """Ids sin conteo con su valor y sus lotes (si la cobertura trae esas columnas)."""
    if not hasattr(r, "ids_sin_conteo"):
        return ""
    partes = []
    for i in r.ids_sin_conteo:
        valor = r.valores_sin_conteo.get(i)
        lotes = r.lotes_sin_conteo.get(i) or []
        partes.append(f"{i} ({'nulo' if valor is None else f'{valor:g}'}; "
                      + (f"lotes {', '.join(lotes)}" if lotes else "sin detección registrada") + ")")
    return (f" {r.sin_conteo} de {r.elegibles} viajes elegibles sin conteo usable "
            f"({REGLA_SIN_CONTEO}): {', '.join(partes)}. Solo días consolidados (días completos antes de "
            f"{pd.Timestamp(r.corte_consolidacion)}). Regla {REGLA_ALT009}.")


def alt_009(cobertura: pd.DataFrame, u: dict) -> list:
    """cobertura: idruta, fecha, cobertura (1 - viajes sin conteo / elegibles) y, si viene de
    cobertura_alt_009, la evidencia por viaje. Alerta si cobertura < u["ALT-009"]."""
    out = []
    for r in cobertura.itertuples(index=False):
        if r.cobertura < u["ALT-009"]:
            out.append(_alerta(
                "ALT-009", r.idruta, r.cobertura, 1.0, u["ALT-009"],
                (u["ALT-009"] - r.cobertura) / u["ALT-009"],
                f"El {pd.Timestamp(r.fecha):%Y-%m-%d} solo el {r.cobertura:.0%} de los viajes "
                f"tiene conteo válido." + _evidencia_alt_009(r), pd.Timestamp(r.fecha),
                severidad=SEVERIDAD_ALT009))
    return out


def diagnostico_alt_009(viajes: pd.DataFrame, detecciones: pd.DataFrame | None, marca,
                        dias_revision: int) -> dict:
    """Días que no se evalúan (sin consolidar o sin elegibles) y diferencias entre viajes y
    detecciones. Valida las detecciones igual que cobertura_alt_009. No escribe en ninguna tabla."""
    corte = pd.Timestamp(marca) - pd.Timedelta(days=int(dias_revision))
    base = {"regla": REGLA_ALT009, "marca_agua": str(pd.Timestamp(marca)), "dias_revision": int(dias_revision),
            "corte_consolidacion": str(corte), "dias_no_consolidados": [], "dias_sin_elegibles": [],
            "ids_sin_deteccion": [], "detecciones_sin_viaje_sin_conteo": []}
    lotes = _lotes_sin_conteo(detecciones)
    if viajes.empty:
        base["detecciones_sin_viaje_sin_conteo"] = sorted(lotes)
        return base
    v = _sin_conteo_por_viaje(viajes).assign(fecha=lambda d: d.salida.dt.normalize())
    consolidado = _dia_consolidado(v.fecha, corte)
    pendientes = v[~consolidado]
    base["dias_no_consolidados"] = [{"idruta": int(r), "fecha": str(f.date()), "viajes": int(len(g))}
                                    for (r, f), g in pendientes.groupby(["idruta", "fecha"], sort=True)]
    consolidados = v[consolidado]
    sin_elegibles = consolidados.groupby(["idruta", "fecha"], sort=True).elegible.sum()
    base["dias_sin_elegibles"] = [{"idruta": int(r), "fecha": str(f.date())}
                                  for (r, f), n in sin_elegibles.items() if n == 0]
    sin_conteo = set(consolidados.loc[consolidados.elegible & consolidados.sin_conteo, "idprogramacion"].astype(int))
    base["ids_sin_deteccion"] = sorted(i for i in sin_conteo if not lotes.get(i))
    base["detecciones_sin_viaje_sin_conteo"] = sorted(
        i for i in lotes if i not in set(v.loc[v.elegible & v.sin_conteo, "idprogramacion"].astype(int)))
    return base


def alt_010(desempeno: pd.DataFrame, u: dict) -> list:
    """desempeno: idruta, horizonte_min, wape_reciente, wape_referencia."""
    out = []
    for r in desempeno.itertuples(index=False):
        if r.wape_referencia > 0 and r.wape_reciente > r.wape_referencia * (1 + u["ALT-010"]):
            deg = r.wape_reciente / r.wape_referencia - 1
            out.append(_alerta(
                "ALT-010", r.idruta, r.wape_reciente, r.wape_referencia, u["ALT-010"],
                deg / u["ALT-010"] - 1,
                f"El WAPE reciente ({r.wape_reciente:.1%}) es {deg:.0%} peor que el de "
                f"validación ({r.wape_referencia:.1%}).", horizonte_min=r.horizonte_min))
    return out


# ----------------------------------------------------------------- gestión

def deduplicar(nuevas: list, claves_abiertas: set) -> list:
    """Descarta alertas cuya clave ya tiene una alerta abierta, y duplicadas en el lote."""
    vistas, out = set(claves_abiertas), []
    for a in nuevas:
        if a["clave_dedup"] not in vistas:
            vistas.add(a["clave_dedup"])
            out.append(a)
    return out


def validar_transicion(estado_actual: str, estado_nuevo: str) -> None:
    if estado_actual not in TRANSICIONES:
        raise TransicionInvalida(f"Estado desconocido: {estado_actual}")
    if estado_nuevo not in TRANSICIONES[estado_actual]:
        raise TransicionInvalida(
            f"No se puede pasar de '{estado_actual}' a '{estado_nuevo}'")
