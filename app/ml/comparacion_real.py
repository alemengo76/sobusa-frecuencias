"""M1: emparejamiento de predicciones guardadas con la demanda real observada después.

Funciones puras (sin base de datos). La capa de servicio lee de PostgreSQL y llama aquí.

Qué es una predicción (contrato de ai_frecuencias.prediction):
    demanda_pred = pasajeros de los viajes con salida PROGRAMADA en [franja_inicio, franja_inicio + H),
    H = horizonte_min (30, 60 o 120), emitida por model_id / model_version / feature_version con datos
    hasta data_cutoff.

Qué es lo real (misma definición que la etiqueta del FE, reglas comun-1.5, en demanda_franja):
    real(t, H) = suma de demanda_franja.pasajeros en las H/15 franjas t, t+15, ..., t+H-15.
    Una franja sin fila no tuvo salidas (aporta 0). Una franja con pasajeros NULL tuvo algún viaje
    elegible sin conteo usable o en cuarentena: la ventana entera queda sin medición (no se imputa).

Estados de cada predicción (excluyentes, se asignan en este orden):
    inconsistente          la predicción en sí no es comparable (ver MOTIVOS)
    duplicado              otra predicción del MISMO modelo para el mismo objetivo la reemplaza
    no_disponible          lo real aún no existe o no está consolidado
    incompleto_cuarentena  lo real existe pero falta medición (día sin datos o franja NULL)
    sin_correspondencia    día cargado, sin salidas en la ventana y origen fuera del horario de operación
                           (o sin horario para decidirlo)
    valido                 entra en las métricas. Incluye la ventana OPERATIVA sin salidas: su demanda
                           real es 0, como en la etiqueta del FE (comun-1.5). Faltantes y cuarentena
                           nunca se vuelven 0: quedan en incompleto_cuarentena.

Las métricas siempre se agrupan por model_id (nunca se mezclan modelos ni versiones) y solo usan
filas en estado 'valido'.
"""
import hashlib
import json
import math

import numpy as np
import pandas as pd

from app.ml.evaluacion import metricas

VERSION_REGLAS = "m1-1.1"
FRANJA_MIN = 15
HORIZONTES = (30, 60, 120)
DIAS_INGENUO = 7

ESTADOS = ("valido", "no_disponible", "incompleto_cuarentena", "sin_correspondencia",
           "duplicado", "inconsistente")

MOTIVOS = {
    # inconsistente
    "horizonte_invalido": "horizonte_min fuera de (30, 60, 120)",
    "franja_no_alineada": "franja_inicio no cae en múltiplos de 15 minutos",
    "valor_invalido": "demanda_pred nula o negativa",
    "limites_invalidos": "no se cumple limite_inferior <= demanda_pred <= limite_superior",
    "informacion_futura": "data_cutoff posterior a franja_inicio (usaría datos no disponibles)",
    "version_distinta_al_registro": "model_version o feature_version distintas a las de model_registry",
    "duplicado_con_valores_distintos": "mismo modelo, objetivo y data_cutoff con valores distintos",
    # duplicado
    "repeticion": "repetición idéntica de otra predicción (se conserva la primera emitida)",
    "pronostico_anterior": "existe un pronóstico del mismo modelo con data_cutoff más reciente",
    # no_disponible
    "sin_marca_agua": "no hay marca de agua de progvehiculos: no se sabe qué está consolidado",
    "sin_consolidar": "la ventana termina después del corte de consolidación",
    # incompleto_cuarentena
    "dia_sin_datos": "la ruta no tiene ninguna franja cargada en ese día",
    "franja_sin_medicion": "alguna franja de la ventana tiene pasajeros NULL (sin conteo o cuarentena)",
    # sin_correspondencia
    "fuera_de_horario": "sin salidas en una ventana fuera del horario de operación",
    "sin_servicio": "sin salidas y sin horario de operación para decidir si la demanda es 0",
    # valido
    "cero_operativo": "ventana operativa completa y consolidada sin salidas: demanda real 0 (FE comun-1.5)",
    # valido
    "ok": "",
}

COLUMNAS_PREDICCION = [
    "prediction_id", "execution_id", "idruta", "sentido", "segmento_id", "franja_inicio",
    "horizonte_min", "demanda_pred", "limite_inferior", "limite_superior", "nivel_intervalo",
    "model_id", "model_version", "feature_version", "data_cutoff", "generated_at",
]
OBJETIVO = ["model_id", "idruta", "sentido", "segmento_id", "franja_inicio", "horizonte_min"]
AGRUPACIONES = ("idruta", "horizonte_min", "execution_id", "dia", "hora", "tipo_dia")


# ----------------------------------------------------------------- real por ventana

def real_por_ventana(franjas: pd.DataFrame, claves: pd.DataFrame, completitud=None) -> pd.DataFrame:
    """Demanda real de cada (idruta, franja_inicio, horizonte_min) de `claves`.

    franjas: idruta, franja_inicio (naive, hora de Bogotá), pasajeros (NaN = sin medición),
             programaciones.
    Devuelve las claves con: demanda_real (NaN si no se puede medir), programaciones_ventana,
    franjas_sin_medicion, dia_cargado (todas las franjas de la ventana caen en días con datos).
    """
    cols = ["idruta", "franja_inicio", "horizonte_min"]
    base = claves[cols].drop_duplicates().reset_index(drop=True)
    if base.empty:
        return base.assign(demanda_real=pd.Series(dtype=float), programaciones_ventana=0,
                           franjas_sin_medicion=0, dia_cargado=False, parcial=False, desconocido=False)
    f = franjas[["idruta", "franja_inicio", "pasajeros", "programaciones"]].copy()
    f["franja_inicio"] = pd.to_datetime(f.franja_inicio)
    f["pasajeros"] = pd.to_numeric(f.pasajeros, errors="coerce").astype(float)
    f["programaciones"] = pd.to_numeric(f.programaciones, errors="coerce").fillna(0).astype(int)
    dias = set(zip(f.idruta.astype(int), f.franja_inicio.dt.normalize(), strict=True))

    pasos = base.horizonte_min.astype(int) // FRANJA_MIN
    exp = base.loc[base.index.repeat(pasos)].copy()
    exp["k"] = exp.groupby(level=0).cumcount()
    exp["franja"] = exp.franja_inicio + pd.to_timedelta(exp.k * FRANJA_MIN, unit="min")
    exp = exp.reset_index(names="i").merge(
        f.rename(columns={"franja_inicio": "franja"}), on=["idruta", "franja"], how="left")
    exp["sin_medicion"] = exp.programaciones.notna() & exp.pasajeros.isna()
    exp["dia_ok"] = [(int(r), d) in dias for r, d in zip(exp.idruta, exp.franja.dt.normalize(), strict=True)]
    # None conserva las llamadas puras legadas. La API pasa siempre un mapa explícito.
    exp["parcial"] = exp["desconocido"] = False
    if completitud is not None:
        estado = pd.Series([
            completitud.get((int(r), d.date())) for r, d in zip(exp.idruta, exp.franja, strict=True)
        ], index=exp.index)
        exp["dia_ok"] = estado.eq("completo")
        exp["parcial"] = estado.eq("parcial")
        exp["desconocido"] = ~estado.isin(("completo", "parcial"))
    g = exp.groupby("i")
    out = base.copy()
    out["programaciones_ventana"] = g.programaciones.sum().reindex(out.index).fillna(0).astype(int)
    out["franjas_sin_medicion"] = g.sin_medicion.sum().reindex(out.index).astype(int)
    out["dia_cargado"] = g.dia_ok.all().reindex(out.index).astype(bool)
    out["parcial"] = g.parcial.any().reindex(out.index).astype(bool)
    out["desconocido"] = g.desconocido.any().reindex(out.index).astype(bool)
    suma = g.pasajeros.sum(min_count=0).reindex(out.index)
    medible = out.dia_cargado & out.franjas_sin_medicion.eq(0)
    out["demanda_real"] = suma.where(medible).astype(float)
    return out


# ----------------------------------------------------------------- clasificación

def _inconsistencias(p: pd.DataFrame) -> pd.Series:
    motivo = pd.Series(None, index=p.index, dtype=object)

    def marcar(mask, codigo):
        motivo.loc[mask & motivo.isna()] = codigo

    marcar(~p.horizonte_min.isin(HORIZONTES), "horizonte_invalido")
    marcar(p.franja_inicio.dt.floor(f"{FRANJA_MIN}min") != p.franja_inicio, "franja_no_alineada")
    marcar(p.demanda_pred.isna() | (p.demanda_pred < 0), "valor_invalido")
    marcar(~((p.limite_inferior <= p.demanda_pred) & (p.demanda_pred <= p.limite_superior)),
           "limites_invalidos")
    marcar(p.data_cutoff > p.franja_inicio, "informacion_futura")
    if "registro_version" in p.columns:
        distinta = ((p.registro_version.notna() & (p.model_version != p.registro_version))
                    | (p.registro_feature_version.notna()
                       & (p.feature_version != p.registro_feature_version)))
        marcar(distinta, "version_distinta_al_registro")
    return motivo


def _duplicados(p: pd.DataFrame, motivo: pd.Series) -> pd.Series:
    """Una predicción por objetivo y modelo. Entre las del data_cutoff más reciente, si todas valen lo
    mismo se conserva la primera emitida; si difieren, ninguna es confiable (inconsistente)."""
    motivo = motivo.copy()
    vivas = p[motivo.isna()]
    if vivas.empty:
        return motivo
    ult = vivas.groupby(OBJETIVO, dropna=False).data_cutoff.transform("max")
    motivo.loc[vivas.index[(vivas.data_cutoff < ult).to_numpy()]] = "pronostico_anterior"
    cand = vivas[(vivas.data_cutoff == ult).to_numpy()]
    clave = OBJETIVO + ["data_cutoff"]
    valores = cand.assign(_v=cand.demanda_pred.round(2), _i=cand.limite_inferior.round(2),
                          _s=cand.limite_superior.round(2))
    distintos = valores.groupby(clave, dropna=False)[["_v", "_i", "_s"]].transform("nunique").max(axis=1) > 1
    motivo.loc[cand.index[distintos.to_numpy()]] = "duplicado_con_valores_distintos"
    resto = cand[~distintos.to_numpy()].sort_values(["generated_at", "prediction_id"])
    repetidas = resto.duplicated(clave, keep="first")
    motivo.loc[resto.index[repetidas.to_numpy()]] = "repeticion"
    return motivo


ESTADO_DE_MOTIVO = {
    "horizonte_invalido": "inconsistente", "franja_no_alineada": "inconsistente",
    "valor_invalido": "inconsistente", "limites_invalidos": "inconsistente",
    "informacion_futura": "inconsistente", "version_distinta_al_registro": "inconsistente",
    "duplicado_con_valores_distintos": "inconsistente",
    "repeticion": "duplicado", "pronostico_anterior": "duplicado",
    "sin_marca_agua": "no_disponible", "sin_consolidar": "no_disponible",
    "carga_parcial": "no_disponible", "completitud_desconocida": "no_disponible",
    "dia_sin_datos": "incompleto_cuarentena", "franja_sin_medicion": "incompleto_cuarentena",
    "sin_servicio": "sin_correspondencia", "fuera_de_horario": "sin_correspondencia",
    "cero_operativo": "valido", "ok": "valido",
}


def corte_consolidacion(marca_agua, margen_dias: int):
    """Último instante cuyo real se considera definitivo. La ingesta relee los últimos
    `dias_revision` días antes de la marca de agua porque pueden corregirse: esos días son
    provisionales y no se evalúan."""
    if marca_agua is None or pd.isna(marca_agua):
        return None
    m = pd.Timestamp(marca_agua)
    if m.tzinfo is not None:
        m = m.tz_convert("America/Bogota").tz_localize(None)
    return m - pd.Timedelta(days=margen_dias)


def normalizar_horario(horario: pd.DataFrame | None) -> pd.DataFrame | None:
    """horario: model_id, id_ruta, tipo_dia, hhmm, operativa (el del artefacto GBM, comun-1.5)."""
    if horario is None or len(horario) == 0:
        return None
    h = horario[["model_id", "id_ruta", "tipo_dia", "hhmm", "operativa"]].copy()
    h["model_id"] = pd.to_numeric(h.model_id).astype(int)
    h["id_ruta"] = pd.to_numeric(h.id_ruta).astype(int)
    h["operativa"] = h.operativa.astype(bool)
    return h.drop_duplicates(["model_id", "id_ruta", "tipo_dia", "hhmm"])


def origen_operativo(claves: pd.DataFrame, horario: pd.DataFrame | None) -> pd.Series:
    """True/False si el origen (model_id, idruta, franja_inicio) cae en una franja operativa del
    horario del modelo; NA si no hay horario para ese modelo. El tipo de día sale del mismo
    calendario del FE (festivos incluidos)."""
    out = pd.Series(pd.NA, index=claves.index, dtype="boolean")
    if horario is None or claves.empty:
        return out
    from app.fe31 import rc
    t = pd.to_datetime(claves.franja_inicio)
    cal = rc.calendario(t.min(), t.max())[["fecha", "tipo_dia"]]
    k = pd.DataFrame({"model_id": claves.model_id.astype(int).to_numpy(),
                      "id_ruta": claves.idruta.astype(int).to_numpy(),
                      "fecha": t.dt.normalize().to_numpy(), "hhmm": t.dt.strftime("%H:%M").to_numpy(),
                      "_i": claves.index.to_numpy()})
    k = k.merge(cal, on="fecha", how="left")
    con = k.model_id.isin(set(horario.model_id))
    k = k.merge(horario, on=["model_id", "id_ruta", "tipo_dia", "hhmm"], how="left")
    # franja fuera de la malla del horario: no operativa; modelo sin horario: no se sabe (NA)
    operativa = k.operativa.astype("boolean").fillna(False)
    operativa[~con.to_numpy()] = pd.NA
    out.loc[k._i.to_numpy()] = operativa.to_numpy()
    return out


def _normalizar(pred: pd.DataFrame) -> pd.DataFrame:
    p = pred.copy()
    for col in ("franja_inicio", "data_cutoff", "generated_at"):
        s = pd.to_datetime(p[col])
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_convert("America/Bogota").dt.tz_localize(None)
        p[col] = s
    for col in ("demanda_pred", "limite_inferior", "limite_superior", "nivel_intervalo"):
        p[col] = pd.to_numeric(p[col], errors="coerce").astype(float)
    for col in ("idruta", "horizonte_min", "model_id", "prediction_id"):
        p[col] = pd.to_numeric(p[col], errors="coerce").astype("Int64")
    p["execution_id"] = p.execution_id.astype(str)
    return p


def emparejar(pred: pd.DataFrame, franjas: pd.DataFrame, marca_agua,
              margen_dias: int, horario: pd.DataFrame | None = None, *, completitud=None) -> pd.DataFrame:
    """Una fila por predicción con su estado, motivo, demanda_real y el real del ingenuo de 7 días.

    pred: columnas COLUMNAS_PREDICCION (+ registro_version, registro_feature_version opcionales).
    franjas: demanda_franja que cubra [min franja - 7 días, max franja + 120 min).
    marca_agua: watermark.ultima_actualizacion de progvehiculos (None si no hay).
    horario: horario de operación por model_id (normalizar_horario). Sin él, una ventana sin salidas
             queda sin_correspondencia (no se puede decidir si es un 0 operativo).
    """
    if pred.empty:
        return pd.DataFrame(columns=COLUMNAS_PREDICCION + [
            "estado", "motivo", "demanda_real", "error", "dentro_intervalo",
            "demanda_ingenuo_7d", "programaciones_ventana", "fin_ventana"])
    p = _normalizar(pred).reset_index(drop=True)
    motivo = _inconsistencias(p)
    motivo = _duplicados(p, motivo)

    p["fin_ventana"] = p.franja_inicio + pd.to_timedelta(p.horizonte_min.astype(float), unit="min")
    vivas = motivo.isna()
    corte = corte_consolidacion(marca_agua, margen_dias)
    if corte is None:
        motivo.loc[vivas] = "sin_marca_agua"
    else:
        motivo.loc[vivas & (p.fin_ventana > corte)] = "sin_consolidar"

    validas_h = p.horizonte_min.isin(HORIZONTES) & (p.franja_inicio.dt.floor("15min") == p.franja_inicio)
    claves = p.loc[validas_h, ["idruta", "franja_inicio", "horizonte_min"]].astype(
        {"idruta": int, "horizonte_min": int})
    real = real_por_ventana(franjas, claves, completitud)
    p = p.merge(real.astype({"idruta": "Int64", "horizonte_min": "Int64"}),
                on=["idruta", "franja_inicio", "horizonte_min"], how="left")

    vivas = motivo.isna()
    motivo.loc[vivas & p.parcial.fillna(False).astype(bool)] = "carga_parcial"
    vivas = motivo.isna()
    motivo.loc[vivas & p.desconocido.fillna(False).astype(bool)] = "completitud_desconocida"
    vivas = motivo.isna()
    motivo.loc[vivas & ~p.dia_cargado.fillna(False).astype(bool)] = "dia_sin_datos"
    vivas = motivo.isna()
    motivo.loc[vivas & p.franjas_sin_medicion.fillna(0).gt(0)] = "franja_sin_medicion"
    horario = normalizar_horario(horario)
    op = origen_operativo(p.assign(model_id=p.model_id.fillna(-1)), horario)
    vivas = motivo.isna()
    sin_salidas = vivas & p.programaciones_ventana.fillna(0).eq(0)
    motivo.loc[sin_salidas & op.isna().to_numpy()] = "sin_servicio"
    motivo.loc[sin_salidas & op.eq(False).fillna(False).to_numpy()] = "fuera_de_horario"
    motivo.loc[sin_salidas & op.eq(True).fillna(False).to_numpy()] = "cero_operativo"
    motivo = motivo.fillna("ok")

    p["motivo"] = motivo.to_numpy()
    p["estado"] = p.motivo.map(ESTADO_DE_MOTIVO)
    p["demanda_real"] = p.demanda_real.where(p.estado.eq("valido"))
    p["error"] = p.demanda_pred - p.demanda_real
    p["dentro_intervalo"] = ((p.demanda_real >= p.limite_inferior)
                             & (p.demanda_real <= p.limite_superior)).where(p.estado.eq("valido"))

    # Ingenuo de 7 días: el real (consolidado y medible) de la misma ventana una semana antes.
    # Misma regla: sin salidas cuenta como 0 solo si el origen de hace 7 días era operativo.
    ing = claves.assign(franja_inicio=claves.franja_inicio - pd.Timedelta(days=DIAS_INGENUO))
    ri = real_por_ventana(franjas, ing, completitud)
    ri = ri.assign(franja_inicio=ri.franja_inicio + pd.Timedelta(days=DIAS_INGENUO))[
        ["idruta", "franja_inicio", "horizonte_min", "demanda_real", "programaciones_ventana"]].rename(
        columns={"demanda_real": "demanda_ingenuo_7d", "programaciones_ventana": "_prog_ing"})
    p = p.merge(ri.astype({"idruta": "Int64", "horizonte_min": "Int64"}),
                on=["idruta", "franja_inicio", "horizonte_min"], how="left")
    hace_7d = p.franja_inicio - pd.Timedelta(days=DIAS_INGENUO)
    op_ing = origen_operativo(p.assign(model_id=p.model_id.fillna(-1), franja_inicio=hace_7d), horario)
    usable = p._prog_ing.fillna(0).gt(0).to_numpy() | op_ing.eq(True).fillna(False).to_numpy()
    p["demanda_ingenuo_7d"] = p.demanda_ingenuo_7d.where(p.estado.eq("valido") & usable)
    p = p.drop(columns="_prog_ing")
    p["programaciones_ventana"] = p.programaciones_ventana.astype("Int64")
    return p.drop(columns=["franjas_sin_medicion", "dia_cargado", "parcial", "desconocido"]).sort_values(
        ["model_id", "idruta", "horizonte_min", "franja_inicio", "prediction_id"]).reset_index(drop=True)


# ----------------------------------------------------------------- métricas

def _columnas_grupo(pares: pd.DataFrame, agrupar_por) -> tuple[pd.DataFrame, list]:
    d = pares.copy()
    extra = [g for g in agrupar_por if g not in AGRUPACIONES]
    if extra:
        raise ValueError(f"agrupación no soportada: {extra}; use {AGRUPACIONES}")
    if "dia" in agrupar_por:
        d["dia"] = d.franja_inicio.dt.strftime("%Y-%m-%d")
    if "hora" in agrupar_por:
        d["hora"] = d.franja_inicio.dt.hour
    if "tipo_dia" in agrupar_por:
        from app.ml.baseline import tipo_dia
        d["tipo_dia"] = tipo_dia(pd.DatetimeIndex(d.franja_inicio))
    grupos = ["model_id", "model_version"] + [g for g in AGRUPACIONES if g in agrupar_por]
    return d, grupos


def _num(x):
    if x is None or x is pd.NA or x is pd.NaT:
        return None
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    return None if math.isnan(f) or math.isinf(f) else f


def metricas_grupo(g: pd.DataFrame) -> dict:
    """Métricas de un grupo de pares (ya de un solo modelo). Solo usa estado == 'valido'."""
    conteo = {e: int((g.estado == e).sum()) for e in ESTADOS}
    v = g[g.estado.eq("valido")]
    m = metricas(v.demanda_real, v.demanda_pred)
    out = {"n_predicciones": int(len(g)), "n_validas": int(m["n"]), "conteo_estados": conteo,
           "mae": m["mae"], "rmse": m["rmse"], "wape": m["wape"], "smape": m["smape"],
           "sesgo": float(v.error.mean()) if len(v) else np.nan,
           "suma_real": float(v.demanda_real.sum()) if len(v) else np.nan,
           "suma_pred": float(v.demanda_pred.sum()) if len(v) else np.nan,
           "cobertura_intervalo": float(v.dentro_intervalo.astype(float).mean()) if len(v) else np.nan,
           "nivel_intervalo_nominal": (float(v.nivel_intervalo.iloc[0])
                                       if len(v) and v.nivel_intervalo.nunique() == 1 else np.nan)}
    con_ing = v[v.demanda_ingenuo_7d.notna()]
    mae_m = float((con_ing.demanda_real - con_ing.demanda_pred).abs().mean()) if len(con_ing) else np.nan
    mae_i = (float((con_ing.demanda_real - con_ing.demanda_ingenuo_7d).abs().mean())
             if len(con_ing) else np.nan)
    out.update({"n_ingenuo_7d": int(len(con_ing)), "mae_mismas_filas_ingenuo": mae_m,
                "mae_ingenuo_7d": mae_i,
                "mae_relativo_ingenuo_7d": mae_m / mae_i if len(con_ing) and mae_i > 0 else np.nan})
    if m["n"] == 0:
        out["advertencia"] = "sin observaciones válidas: no se calculan métricas"
    elif m["wape"] is None or (isinstance(m["wape"], float) and math.isnan(m["wape"])):
        out["advertencia"] = "la demanda real suma 0: WAPE no definido"
    return {k: (_num(x) if not isinstance(x, dict) else x) for k, x in out.items()}


def agregar(pares: pd.DataFrame, agrupar_por=("idruta", "horizonte_min")) -> list[dict]:
    """Métricas por model_id + model_version + agrupar_por. Nunca mezcla modelos."""
    if pares.empty:
        return []
    d, grupos = _columnas_grupo(pares, agrupar_por)
    filas = []
    for clave, g in d.groupby(grupos, sort=True, dropna=False):
        clave = clave if isinstance(clave, tuple) else (clave,)
        texto = ("model_version", "execution_id", "dia", "tipo_dia")
        enteros = ("model_id", "idruta", "horizonte_min", "hora")
        filas.append({**{k: (v if k in texto else int(v) if k in enteros and pd.notna(v) else _num(v))
                         for k, v in zip(grupos, clave, strict=True)},
                      **metricas_grupo(g)})
    return filas


def conteo_estados(pares: pd.DataFrame) -> dict:
    return {e: int((pares.estado == e).sum()) for e in ESTADOS} if not pares.empty else \
        {e: 0 for e in ESTADOS}


def conteo_motivos(pares: pd.DataFrame) -> dict:
    return {k: int(v) for k, v in pares.motivo.value_counts().sort_index().items()} if not pares.empty else {}


def huella(pares: pd.DataFrame, parametros: dict) -> str:
    """SHA-256 de las entradas que determinan el resultado: reglas, parámetros y cada par
    (prediction_id, estado, motivo, pred, límites, real, ingenuo). Misma huella = mismo resultado."""
    cols = ["prediction_id", "estado", "motivo", "demanda_pred", "limite_inferior",
            "limite_superior", "demanda_real", "demanda_ingenuo_7d"]
    filas = [] if pares.empty else [
        [_num(x) if not isinstance(x, str) else x for x in r]
        for r in pares.sort_values("prediction_id")[cols].astype(object).itertuples(index=False)]
    texto = json.dumps({"reglas": VERSION_REGLAS, "parametros": parametros, "pares": filas},
                       sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(texto.encode()).hexdigest()


# ----------------------------------------------------------------- salida para alertas

def para_alt_005_007(pares: pd.DataFrame) -> pd.DataFrame:
    """Entrada de motor.alt_005_007: solo pares válidos de UN modelo (el que se monitorea).
    Columnas: idruta, franja_inicio, horizonte_min, demanda_pred, demanda_real (+ límites, para que
    la regla pueda pasar a 'real fuera del intervalo' como dice la especificación §5.2)."""
    v = pares[pares.estado.eq("valido")]
    if v.model_id.nunique() > 1:
        raise ValueError("para_alt_005_007 recibe pares de un solo model_id")
    cols = ["idruta", "franja_inicio", "horizonte_min", "demanda_pred", "demanda_real",
            "limite_inferior", "limite_superior", "model_id", "prediction_id"]
    return v[cols].astype({"idruta": int, "horizonte_min": int}).reset_index(drop=True)


def para_alt_010(reciente: list[dict], referencia: list[dict]) -> pd.DataFrame:
    """Entrada de motor.alt_010 a partir de DOS resultados de agregar(..., ('idruta','horizonte_min'))
    del mismo model_id: un periodo reciente y uno de referencia, ambos predicción-real (no métricas
    de entrenamiento). Grupos sin WAPE en alguno de los dos periodos se omiten."""
    r, b = pd.DataFrame(reciente), pd.DataFrame(referencia)
    if r.empty or b.empty:
        return pd.DataFrame(columns=["idruta", "horizonte_min", "wape_reciente", "wape_referencia"])
    if set(r.model_id) != set(b.model_id) or r.model_id.nunique() != 1:
        raise ValueError("para_alt_010 compara un mismo y único model_id")
    m = r.merge(b, on=["model_id", "idruta", "horizonte_min"], suffixes=("_rec", "_ref"))
    m = m.rename(columns={"wape_rec": "wape_reciente", "wape_ref": "wape_referencia"})
    m = m.dropna(subset=["wape_reciente", "wape_referencia"])
    return m[["idruta", "horizonte_min", "wape_reciente", "wape_referencia"]].astype(
        {"idruta": int, "horizonte_min": int}).reset_index(drop=True)
