"""Comparación JUSTA candidato vs modelo activo (RF-019): mismas observaciones, validación temporal.

Reglas:
1. El periodo de comparación debe ser POSTERIOR al entrenamiento de TODOS los modelos comparados
   (si no, alguno se evalúa con datos que ya vio).
2. El periodo debe estar en `PERIODOS_APROBADOS`. Hoy está vacío: B1 (jul-sep 2025 como monitoreo)
   no está confirmado, así que `comparar` se detiene antes de leer o predecir nada.
3. Cada modelo predice cada día D con el código de producción (`modelo_gbm.predecir_dia`), usando
   solo viajes anteriores a D. No hay respaldo: si un modelo no puede predecir, la fila queda fuera
   para todos y se informa.
4. Se comparan solo las ventanas que TODOS predijeron y que tienen etiqueta completa
   (comun-1.5; los días anómalos o de baja calidad se evalúan, igual que en M1; faltantes y
   cuarentena nunca se vuelven 0).
5. Métricas por ruta x horizonte y total (MAE, RMSE, WAPE, sMAPE, cobertura del intervalo) y la
   diferencia diaria de MAE (candidato - activo) con IC 95 % por bootstrap de bloques de 7 días.
   No hay veredicto de activación: los umbrales de RF-019 no están aprobados.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from app.fe31 import rc
from app.ml import modelo_gbm as G
from app.ml.evaluacion import metricas

PERIODOS_APROBADOS: tuple[tuple[str, str], ...] = ()     # [desde, hasta): ninguno aprobado todavía
HORIZONTES = (30, 60, 120)
RUTAS = (2, 16, 21)
BLOQUE_DIAS, REMUESTREOS, SEMILLA_BOOTSTRAP = 7, 2000, 0


class PeriodoNoAutorizado(PermissionError):
    """El periodo de comparación no está aprobado: no se abre."""


class ComparacionInvalida(ValueError):
    """El periodo se solapa con el entrenamiento de algún modelo u otra condición de justicia."""


def comprobar_periodo(desde, hasta, entrenado_hasta: dict, aprobados=PERIODOS_APROBADOS) -> None:
    d, h = pd.Timestamp(desde), pd.Timestamp(hasta)
    if not d < h:
        raise ComparacionInvalida("desde debe ser anterior a hasta")
    for nombre, fin in entrenado_hasta.items():
        if fin is None or pd.isna(pd.Timestamp(fin)):
            raise ComparacionInvalida(f"{nombre}: no declara hasta cuándo se entrenó")
        if d <= pd.Timestamp(fin):
            raise ComparacionInvalida(f"{nombre} se entrenó hasta {fin}: el periodo debe empezar después")
    if not any(pd.Timestamp(a) <= d and h <= pd.Timestamp(b) for a, b in aprobados):
        raise PeriodoNoAutorizado(
            f"El periodo [{d:%Y-%m-%d}, {h:%Y-%m-%d}) no está aprobado para comparar modelos "
            f"(aprobados: {list(aprobados) or 'ninguno'}). Requiere autorización (B1).")


def etiquetas(P: pd.DataFrame, desde, hasta) -> pd.DataFrame:
    """Etiqueta comun-1.5 de cada ventana del periodo que termina antes de `hasta`. P: formato FE,
    solo viajes del periodo. Se evalúan todos los días con datos (anómalos y de baja calidad
    incluidos, como M1); una ventana con un viaje sin conteo o en cuarentena queda sin etiqueta."""
    d, h = pd.Timestamp(desde), pd.Timestamp(hasta)
    P = P[(P.salida_programada >= d) & (P.salida_programada < h)]
    cal = rc.calendario(d, h - pd.Timedelta(days=1))
    Pq, _ = rc.aplicar_cuarentena(P)
    dias = rc.clasificar_dias(Pq, cal)
    dias = dias.assign(clase_dia=dias.clase_dia.replace({"anomalo": "completo", "baja_calidad": "completo"}))
    malla = pd.MultiIndex.from_product(
        [sorted(P.id_ruta.unique()), ["laboral", "sabado", "dom_festivo"],
         pd.date_range("2000-01-01", periods=96, freq="15min").strftime("%H:%M")],
        names=["id_ruta", "tipo_dia", "hhmm"]).to_frame(index=False).assign(operativa=True)
    Y = rc.etiqueta_operativa(Pq, cal, dias, malla)
    Y = Y[(Y.origen + pd.to_timedelta(Y.horizonte_min, unit="min")) <= h]
    return pd.DataFrame({"idruta": Y.id_ruta.astype(int).to_numpy(), "franja_inicio": Y.origen.to_numpy(),
                         "horizonte_min": Y.horizonte_min.astype(int).to_numpy(),
                         "demanda_real": Y.y_pasajeros.to_numpy(dtype="float64"),
                         "estado_etiqueta": Y.estado_etiqueta.to_numpy()})


def predicciones(art: G.Artefacto, P: pd.DataFrame, dias, rutas=RUTAS, horizontes=HORIZONTES):
    """Predicción de cada día con el código de producción y solo viajes anteriores a ese día."""
    partes, fallas = [], []
    for D in dias:
        try:
            partes.append(G.predecir_dia(art, P[P.salida_programada < D], D, rutas, horizontes))
        except G.ModeloNoDisponible as e:
            fallas.append({"dia": pd.Timestamp(D).strftime("%Y-%m-%d"), "motivo": str(e)})
    cols = ["idruta", "franja_inicio", "horizonte_min", "demanda_pred", "limite_inferior", "limite_superior"]
    return (pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=cols)), fallas


def _bootstrap_bloques(d: np.ndarray, bloque=BLOQUE_DIAS, B=REMUESTREOS, semilla=SEMILLA_BOOTSTRAP):
    n = len(d)
    if n < 2:
        return None, None
    rng = np.random.default_rng(semilla)
    k = int(np.ceil(n / bloque))
    medias = np.empty(B)
    for b in range(B):
        inicios = rng.integers(0, max(n - bloque + 1, 1), size=k)
        idx = np.concatenate([np.arange(i, min(i + bloque, n)) for i in inicios])[:n]
        medias[b] = d[idx].mean()
    return float(np.quantile(medias, 0.025)), float(np.quantile(medias, 0.975))


def tabla(comunes: pd.DataFrame, nombres) -> list[dict]:
    filas = []
    grupos = [((int(r), int(h)), g) for (r, h), g in comunes.groupby(["idruta", "horizonte_min"])]
    for (r, h), g in grupos + [((0, 0), comunes)]:
        for n in nombres:
            dentro = (g.demanda_real >= g[f"{n}_inf"]) & (g.demanda_real <= g[f"{n}_sup"])
            filas.append({"idruta": r, "horizonte_min": h, "modelo": n,
                          **metricas(g.demanda_real, g[f"{n}_pred"]),
                          "cobertura_intervalo": float(dentro.mean()) if len(g) else None})
    return filas


def comparar_predicciones(Y: pd.DataFrame, preds: dict[str, pd.DataFrame], referencia: str) -> dict:
    """Une las predicciones de todos los modelos con la etiqueta, en las mismas ventanas."""
    k = ["idruta", "franja_inicio", "horizonte_min"]
    base = Y[Y.estado_etiqueta.eq("completa")][k + ["demanda_real"]]
    comunes = base
    for n, p in preds.items():
        comunes = comunes.merge(p[k + ["demanda_pred", "limite_inferior", "limite_superior"]].rename(
            columns={"demanda_pred": f"{n}_pred", "limite_inferior": f"{n}_inf",
                     "limite_superior": f"{n}_sup"}),
            on=k, how="inner")
    comunes = comunes.sort_values(k).reset_index(drop=True)
    nombres = list(preds)
    resultado = {"filas_comunes": int(len(comunes)),
                 "filas_por_modelo": {n: int(len(p)) for n, p in preds.items()},
                 "etiquetas_completas": int(len(base)), "metricas": tabla(comunes, nombres),
                 "diferencias": {}}
    if comunes.empty:
        resultado["advertencia"] = "sin ventanas comunes con etiqueta completa: no hay comparación"
        return resultado
    dia = comunes.franja_inicio.dt.normalize()
    for n in nombres:
        if n == referencia:
            continue
        err = (comunes[f"{n}_pred"] - comunes.demanda_real).abs() - \
              (comunes[f"{referencia}_pred"] - comunes.demanda_real).abs()
        d = err.groupby(dia).mean().sort_index().to_numpy()
        li, ls = _bootstrap_bloques(d)
        por_grupo = pd.DataFrame(tabla(comunes, [n, referencia])).pivot_table(
            index=["idruta", "horizonte_min"], columns="modelo", values="mae")
        por_grupo = por_grupo.drop(index=(0, 0), errors="ignore")
        resultado["diferencias"][n] = {
            "contra": referencia, "dias": int(len(d)), "dif_mae_diaria_media": float(d.mean()),
            "ic95_bootstrap_bloques_7d": [li, ls],
            "grupos_ruta_horizonte_mejor": int((por_grupo[n] < por_grupo[referencia]).sum()),
            "grupos_ruta_horizonte": int(len(por_grupo))}
    texto = comunes.round(6).to_csv(index=False)
    resultado["huella"] = hashlib.sha256(texto.encode()).hexdigest()
    return resultado


def comparar(artefactos: dict[str, G.Artefacto], crudo: pd.DataFrame, desde, hasta, referencia: str,
             aprobados=PERIODOS_APROBADOS) -> dict:
    """Comparación completa. Se detiene (PeriodoNoAutorizado) ANTES de usar el periodo si no está
    aprobado, y (ComparacionInvalida) si algún modelo vio datos del periodo."""
    from app.fe31.viajes import a_formato_fe
    fines = {n: a.meta.get("entrenado_hasta") for n, a in artefactos.items()}
    comprobar_periodo(desde, hasta, fines, aprobados)
    d, h = pd.Timestamp(desde), pd.Timestamp(hasta)
    crudo = crudo[pd.to_datetime(crudo.fechasalida) < h]        # nada posterior al periodo
    P = a_formato_fe(crudo)
    dias = pd.date_range(d, h - pd.Timedelta(days=1), freq="D")
    Y = etiquetas(P, d, h)
    preds, fallas = {}, {}
    for n, a in artefactos.items():
        preds[n], fallas[n] = predicciones(a, P, dias)
    r = comparar_predicciones(Y, preds, referencia)
    r.update({"periodo": [str(d), str(h)], "referencia": referencia, "fallas_prediccion": fallas,
              "modelos": {n: {"version": a.meta.get("version"), "entrenado_hasta": fines[n]}
                          for n, a in artefactos.items()},
              "decision": "pendiente: RF-019 exige umbrales de activación aprobados"})
    return json.loads(json.dumps(r, default=str))
