"""Motor de alertas explicables ALT-001 a ALT-010.

Cada regla recibe un DataFrame y los umbrales vigentes ({codigo: valor}) y devuelve
una lista de alertas con: codigo, severidad, idruta, franja_inicio, horizonte_min,
valor_observado, valor_esperado, umbral, puntuacion_anomalia, explicacion,
recomendacion y clave_dedup. El estado inicial siempre es 'abierta'.
"""
import hashlib

import numpy as np
import pandas as pd

from app.alertas.catalogo import CATALOGO, HORIZONTE_ALERTAS_FRECUENCIA, TRANSICIONES


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


# ALT-006 (contrato `alt006-v2`, docs del proyecto: ALT006_contrato_2026-11-20). Constantes de la versión
# de la regla; el umbral de tolerancia viene de ai_frecuencias.umbral (u["ALT-006"]).
REGLA_ALT006 = "alt006-v2"
LIMITE_DESFASE_MIN = 120.0
RUTAS_ALT006 = (2, 16, 21)
CLASES_EVALUADAS = ("tardia", "a_tiempo", "ausente")


def clasificar_salidas(salidas: pd.DataFrame, tolerancia_min: float, marca, dias_revision: int) -> pd.DataFrame:
    """Una clase por programación, en este orden:
    duplicada (todas las apariciones del id) · estado_invalido (cancelada fuera de S/N o sin salida) · futura ·
    cancelada · no_evaluable (salida + tolerancia > marca) · desfase_extremo (|d| > 120 min) ·
    tardia (d > tolerancia) · a_tiempo · no_evaluable (sin inicio y salida >= marca - dias_revision) ·
    ausente (sin inicio y salida < marca - dias_revision).
    La referencia temporal es la marca de agua, nunca el reloj del sistema. `finalizado` no se usa."""
    s = salidas[salidas.idruta.isin(RUTAS_ALT006)].copy()
    s["fechasalida"] = pd.to_datetime(s.fechasalida)
    s["fechainicio"] = pd.to_datetime(s.fechainicio)
    marca = pd.Timestamp(marca)
    tol = pd.Timedelta(minutes=float(tolerancia_min))
    consolidado = marca - pd.Timedelta(days=int(dias_revision))
    s["desfase_min"] = (s.fechainicio - s.fechasalida).dt.total_seconds() / 60
    s["franja_inicio"] = s.fechasalida.dt.floor("15min")
    dup = s.idprogramacion.duplicated(keep=False)
    sin_inicio = s.fechainicio.isna()
    s["clase"] = np.select(
        [dup,
         s.fechasalida.isna() | ~s.cancelada.isin(["S", "N"]),
         s.fechasalida > marca,
         s.cancelada.eq("S"),
         s.fechasalida + tol > marca,
         ~sin_inicio & (s.desfase_min.abs() > LIMITE_DESFASE_MIN),
         ~sin_inicio & (s.desfase_min > float(tolerancia_min)),
         ~sin_inicio,
         s.fechasalida >= consolidado],
        ["duplicada", "estado_invalido", "futura", "cancelada", "no_evaluable", "desfase_extremo",
         "tardia", "a_tiempo", "no_evaluable"],
        default="ausente")
    return s


def alt_006(salidas: pd.DataFrame, u: dict, marca, dias_revision: int) -> list:
    """salidas: idruta, idprogramacion, fechasalida (programada), fechainicio (real), cancelada (S/N).
    Una alerta por ruta y franja de 15 min con al menos una programación tardía o sin inicio registrado."""
    tol = u["ALT-006"]
    c = clasificar_salidas(salidas, tol, marca, dias_revision)
    corte = pd.Timestamp(marca) - pd.Timedelta(days=int(dias_revision))
    out = []
    for (ruta, franja), g in c.groupby(["idruta", "franja_inicio"], sort=True):
        aus = g[g.clase.eq("ausente")].sort_values("idprogramacion")
        tar = g[g.clase.eq("tardia")].sort_values(["desfase_min", "idprogramacion"], ascending=[False, True])
        if aus.empty and tar.empty:
            continue
        n_eval = int(g.clase.isin(CLASES_EVALUADAS).sum())
        n_can = int(g.clase.eq("cancelada").sum())
        maximo = None if tar.empty else float(tar.desfase_min.max())
        partes = [f"Ruta {ruta}, franja {franja:%Y-%m-%d %H:%M}-{franja + pd.Timedelta(minutes=15):%H:%M}: "
                  f"{n_eval} programaciones evaluadas"]
        if len(aus):
            partes.append(f"{len(aus)} sin inicio registrado ({', '.join(str(int(x)) for x in aus.idprogramacion)})")
        if len(tar):
            partes.append(f"{len(tar)} con inicio más de {tol:g} min tarde ("
                          + ", ".join(f"{int(r.idprogramacion)} +{r.desfase_min:.0f} min" for r in tar.itertuples())
                          + f"); retraso máximo {maximo:.0f} min")
        partes.append(f"{n_can} canceladas no evaluadas")
        explicacion = ("; ".join(partes) + f". Reloj: marca de agua {pd.Timestamp(marca)}; las ausencias solo "
                       f"se evalúan con salida anterior a {corte}. «Sin inicio registrado» no prueba que el bus "
                       f"no saliera. Regla {REGLA_ALT006}, límite de desfase {LIMITE_DESFASE_MIN:g} min.")
        out.append(_alerta("ALT-006", ruta, maximo, 0, tol, (len(aus) + len(tar)) / n_eval,
                           explicacion, franja, severidad="alta"))
    return out


def diagnostico_alt_006(salidas: pd.DataFrame, u: dict, marca, dias_revision: int) -> dict:
    """Conteo por clase y casos para diagnóstico. No escribe en ninguna tabla."""
    c = clasificar_salidas(salidas, u["ALT-006"], marca, dias_revision)
    cols = ["idruta", "idprogramacion", "fechasalida", "fechainicio", "desfase_min"]

    def filas(clase, extra=()):
        d = c.loc[c.clase.eq(clase), cols + list(extra)]
        return [{k: (None if pd.isna(v) else (str(v) if isinstance(v, pd.Timestamp) else v))
                 for k, v in r.items()} for r in d.to_dict("records")]

    return {"regla": REGLA_ALT006, "marca_agua": str(pd.Timestamp(marca)), "dias_revision": int(dias_revision),
            "tolerancia_min": float(u["ALT-006"]), "limite_desfase_min": LIMITE_DESFASE_MIN,
            "conteo_por_clase": {k: int(v) for k, v in c.clase.value_counts().sort_index().items()},
            "desfase_extremo": filas("desfase_extremo"),
            "duplicada": filas("duplicada"),
            "estado_invalido": filas("estado_invalido", ("cancelada",)),
            "finalizado_por_clase": {f"{k[0]}|{k[1]}": int(v) for k, v in
                                     c.groupby(["clase", c.finalizado.astype("string").fillna("<nulo>")]).size().items()}
            if "finalizado" in c else {}}


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
