"""Motor de alertas explicables ALT-001 a ALT-010.

Cada regla recibe un DataFrame y los umbrales vigentes ({codigo: valor}) y devuelve
una lista de alertas con: codigo, severidad, idruta, franja_inicio, horizonte_min,
valor_observado, valor_esperado, umbral, puntuacion_anomalia, explicacion,
recomendacion y clave_dedup. El estado inicial siempre es 'abierta'.
"""
import hashlib

import pandas as pd

from app.alertas.catalogo import CATALOGO, TRANSICIONES


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


def alt_002_003_004(rec: pd.DataFrame, u: dict) -> list:
    """rec: contrato de recommendation (necesita buses_hora_actual)."""
    out = []
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


def alt_009(cobertura: pd.DataFrame, u: dict) -> list:
    """cobertura: idruta, fecha, cobertura (viajes con conteo válido / elegibles)."""
    out = []
    for r in cobertura.itertuples(index=False):
        if r.cobertura < u["ALT-009"]:
            out.append(_alerta(
                "ALT-009", r.idruta, r.cobertura, 1.0, u["ALT-009"],
                (u["ALT-009"] - r.cobertura) / u["ALT-009"],
                f"El {pd.Timestamp(r.fecha):%Y-%m-%d} solo el {r.cobertura:.0%} de los viajes "
                f"tiene conteo válido.", pd.Timestamp(r.fecha)))
    return out


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
