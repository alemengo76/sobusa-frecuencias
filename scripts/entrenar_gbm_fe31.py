"""Entrena el gradient boosting de producción sobre FE 3.1 / comun-1.5 y lo compara con las
líneas base (E01, E05). Se corre FUERA de la API; la API solo carga el artefacto.

Uso (desde la raíz del proyecto; dentro de Docker para que la versión de scikit-learn sea la
misma con la que la API cargará el modelo):

  # viajes desde la base local (los copia la ingesta en ai_frecuencias.viaje_fuente)
  docker compose run --rm app python -m scripts.entrenar_gbm_fe31 --fuente local --registrar

  # o desde el CSV oficial de fe_v4_comun15 (regenerar_viajes_etl.py)
  python -m scripts.entrenar_gbm_fe31 --csv ../fe_v4_comun15/progvehiculos_limpio.csv

Particiones (cronológicas, las del FE 3.1, sin fuga):
  entrenamiento < 2025-01-01 · ajuste 2025-01..03 (parada temprana e intervalos) ·
  evaluación 2025-04..06 (métricas que se reportan) · prueba >= 2025-07-01 NO SE USA (la reserva Extreme).
Modelo de producción: se reentrena con entrenamiento + ajuste + evaluación (todo < 2025-07-01) con el
número de árboles elegido en el ajuste. Se registra como 'candidato': se activa con
POST /api/v1/models/{id}/activate, lo que deja bitácora y permite reversión.

Salidas en --salida: metricas.csv (por ruta, horizonte y pico/valle), comparacion.csv,
cobertura_intervalos.csv, importancia_global.csv, explicacion_local.csv, resumen.json y en
--modelos el artefacto .pkl + .json con su hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.inspection import permutation_importance

from app.fe31 import FEATURE_VERSION, REGLAS_VERSION, ce, rc
from app.fe31.viajes import RUTAS, a_formato_fe
from app.ml import modelo_gbm as G
from app.ml.evaluacion import metricas

AJUSTE_HASTA = pd.Timestamp("2025-04-01")      # dentro de la partición 'validacion' del FE
PRUEBA_DESDE = pd.Timestamp(ce.CORTES["prueba_desde"])


# ------------------------------------------------------------------ datos

def viajes_csv(ruta: Path) -> tuple[pd.DataFrame, str]:
    df = pd.read_csv(ruta, usecols=["idprogramacion", "idruta", "fechasalida", "fechainicio", "fechafin",
                                    "finalizado", "cancelada", "numeropasajeros", "numerobajadas"],
                     low_memory=False)
    sha = hashlib.sha256(ruta.read_bytes()).hexdigest()
    return df, f"csv:{ruta.name}:{sha[:16]}"


def viajes_local() -> tuple[pd.DataFrame, str]:
    from sqlalchemy import text

    from app.db.conexion import consultar_resultados
    with consultar_resultados() as c:
        df = pd.read_sql(text("""SELECT idprogramacion, idruta, fechasalida, fechainicio, fechafin, finalizado,
                                        cancelada, numeropasajeros, numerobajadas
                                 FROM ai_frecuencias.viaje_fuente WHERE idruta = ANY(:r)"""),
                         c, params={"r": list(RUTAS)})
        marca = c.execute(text("SELECT ultima_actualizacion FROM ai_frecuencias.watermark "
                               "WHERE tabla_origen = 'progvehiculos'")).scalar()
    if df.empty:
        raise SystemExit("ai_frecuencias.viaje_fuente está vacía: corra la ingesta (python -m app.ingesta --reiniciar)")
    return df, f"viaje_fuente:marca_{marca:%Y%m%dT%H%M%S}" if marca else "viaje_fuente"


# ------------------------------------------------------------------ líneas base sobre las mismas filas

def lineas_base(X: pd.DataFrame, P: pd.DataFrame, cal, dias, hor) -> pd.DataFrame:
    """Las tres líneas base de la API, calculadas sobre la etiqueta del FE (mismas filas)."""
    dias_hist = dias.assign(clase_dia=dias.clase_dia.replace({"anomalo": "completo"}))
    Yh = rc.etiqueta_operativa(P, cal, dias_hist, hor.assign(operativa=True))
    yv = Yh.set_index(["id_ruta", "horizonte_min", "origen"]).y_pasajeros
    sem8 = np.column_stack([ce._rezago_seguro(yv, X, 7 * k) for k in range(1, 9)])
    n8 = np.isfinite(sem8).sum(axis=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        dh8 = np.where(n8 >= 2, np.nanmean(sem8, axis=1), np.nan)
    return pd.DataFrame({"dia_hora_4sem": X.y_media_4_semanas.to_numpy(),
                         "dia_hora_8sem": dh8,
                         "ultima_semana": X.y_rezago_7d.to_numpy()}, index=X.index)


def metodo_extreme(E: pd.DataFrame, X: pd.DataFrame) -> np.ndarray:
    """Perfil promedio por ruta, tipo de día, franja y horizonte, ajustado solo con entrenamiento."""
    clave = ["id_ruta", "tipo_dia", "hhmm", "horizonte_min"]
    perfil = E.assign(hhmm=E.origen.dt.strftime("%H:%M")).groupby(clave, observed=True).y_pasajeros.mean()
    k = X.assign(hhmm=X.origen.dt.strftime("%H:%M"))[clave]
    return perfil.reindex(pd.MultiIndex.from_frame(k)).to_numpy()


def marcar_pico(E: pd.DataFrame, X: pd.DataFrame, proporcion=0.25) -> np.ndarray:
    """Pico = el 25 % de franjas con más pasajeros promedio por ruta y tipo de día (H = 60),
    calculado con entrenamiento (definición del equipo, app/ml/evaluacion.marcar_pico)."""
    e = E[E.horizonte_min.eq(60)].assign(hhmm=E.origen.dt.strftime("%H:%M"))
    perfil = e.groupby(["id_ruta", "tipo_dia", "hhmm"], observed=True).y_pasajeros.mean().reset_index()
    umbral = perfil.groupby(["id_ruta", "tipo_dia"], observed=True).y_pasajeros.transform(
        lambda s: s.quantile(1 - proporcion))
    perfil["periodo"] = np.where(perfil.y_pasajeros >= umbral, "pico", "valle")
    k = X.assign(hhmm=X.origen.dt.strftime("%H:%M"))[["id_ruta", "tipo_dia", "hhmm"]]
    return k.merge(perfil[["id_ruta", "tipo_dia", "hhmm", "periodo"]], how="left").periodo.fillna("valle").to_numpy()


def tabla_metricas(ev: pd.DataFrame, modelos: list[str]) -> pd.DataFrame:
    """Métricas en las filas donde TODOS los métodos tienen predicción (comparación justa)."""
    comunes = ev.dropna(subset=modelos)
    filas = []
    for (r, H), g in comunes.groupby(["id_ruta", "horizonte_min"], observed=True):
        for periodo, gp in [("total", g), *g.groupby("periodo")]:
            for m in modelos:
                filas.append({"idruta": int(r), "horizonte_min": int(H), "periodo": periodo, "modelo": m,
                              **metricas(gp[G.OBJETIVO], gp[m])})
    for periodo, g in [("total", comunes), *comunes.groupby("periodo")]:     # idruta 0 = las tres rutas
        for m in modelos:
            filas.append({"idruta": 0, "horizonte_min": 0, "periodo": periodo, "modelo": m,
                          **metricas(g[G.OBJETIVO], g[m])})
    return pd.DataFrame(filas)


# ------------------------------------------------------------------ explicabilidad

def importancia(m, V: pd.DataFrame, n=8000) -> pd.DataFrame:
    v = V.sample(min(n, len(V)), random_state=G.SEMILLA)
    r = permutation_importance(m, G.matriz(v), v[G.OBJETIVO], n_repeats=3, random_state=G.SEMILLA,
                               scoring="neg_mean_absolute_error")
    return (pd.DataFrame({"variable": G.VARIABLES, "importancia_mae": r.importances_mean,
                          "desviacion": r.importances_std})
              .sort_values("importancia_mae", ascending=False))


def explicacion_local(m, fila: pd.DataFrame, referencia: pd.DataFrame) -> pd.DataFrame:
    """Cuánto cambia UNA predicción si cada variable toma su valor típico de entrenamiento."""
    base = G.predecir_matriz(m, fila)[0]
    filas = []
    for v in G.VARIABLES:
        x = fila.copy()
        x[v] = referencia[v].mode().iloc[0] if v in G.CATEGORIAS else referencia[v].median()
        filas.append({"variable": v, "valor": fila[v].iloc[0], "valor_tipico": x[v].iloc[0],
                      "aporte": base - G.predecir_matriz(m, x)[0]})
    return pd.DataFrame(filas).sort_values("aporte", key=abs, ascending=False).assign(prediccion=base)


# ------------------------------------------------------------------ principal

def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    fuente = a.add_mutually_exclusive_group(required=True)
    fuente.add_argument("--csv", type=Path, help="progvehiculos_limpio.csv de fe_v4_comun15")
    fuente.add_argument("--fuente", choices=["local"], help="ai_frecuencias.viaje_fuente")
    fuente.add_argument("--registrar-artefacto", type=Path, metavar="JSON",
                        help="No entrena: registra un artefacto ya entrenado (su .json) como candidato")
    a.add_argument("--salida", type=Path, default=Path("salidas_modelo_fe31"))
    a.add_argument("--modelos", type=Path, default=Path("modelos"))
    a.add_argument("--registrar", action="store_true", help="Registra el modelo en model_registry como candidato")
    args = a.parse_args()
    if args.registrar_artefacto:
        meta = json.loads(args.registrar_artefacto.read_text(encoding="utf-8"))
        pkl = args.registrar_artefacto.with_name(meta["artefacto"])
        if hashlib.sha256(pkl.read_bytes()).hexdigest() != meta["sha256"]:
            raise SystemExit("El .pkl no coincide con el hash de su .json")
        registrar(meta["version"], meta["sha256"], meta["artefacto"], meta["version_datos"],
                  meta["hiperparametros"]["max_iter"], meta.get("metricas", {}))
        return
    args.salida.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    crudo, version_datos = viajes_csv(args.csv) if args.csv else viajes_local()
    crudo = crudo[crudo.idruta.isin(RUTAS)]
    P = a_formato_fe(crudo)
    print(f"{len(P):,} viajes ({version_datos}) · {P.salida_programada.min()} -> {P.salida_programada.max()}")

    cal = rc.calendario(P.salida_programada.min(), P.salida_programada.max())
    Pq, _ = rc.aplicar_cuarentena(P)
    dias = rc.clasificar_dias(Pq, cal)
    hor = rc.horario_operacion(Pq, cal, dias)
    X = ce.construir_conjunto(Pq, cal, dias, hor)
    base = lineas_base(X, Pq, cal, dias, hor)
    X = pd.concat([X, base], axis=1)
    print(f"Conjunto FE 3.1: {len(X):,} filas, {int(X.habilitada_supervisada.sum()):,} habilitadas "
          f"({time.perf_counter() - t0:.0f} s)")

    H = X[X.habilitada_supervisada & X.origen.lt(PRUEBA_DESDE)].copy()
    E = H[H.particion.eq("entrenamiento")]
    A = H[H.particion.eq("validacion") & H.origen.lt(AJUSTE_HASTA)]
    V = H[H.particion.eq("validacion") & H.origen.ge(AJUSTE_HASTA)]
    print(f"entrenamiento {len(E):,} · ajuste {len(A):,} · evaluación {len(V):,} · prueba sin usar")

    m = G.entrenar(E)
    n_arboles, curva = G.mejor_iteracion(m, A)
    m = G.entrenar(E, max_iter=n_arboles)
    print(f"árboles elegidos en el ajuste: {n_arboles} (WAPE ajuste {curva[n_arboles - 1]:.3f})")

    # Intervalos: cuantiles del error en ajuste, por ruta, horizonte y hora
    pa = A.assign(demanda_pred=G.predecir_matriz(m, A))
    intervalos = G.ajustar_intervalos(pd.DataFrame({
        "idruta": pa.id_ruta.astype(int), "horizonte_min": pa.horizonte_min.astype(int),
        "franja_inicio": pa.origen, "demanda_real": pa[G.OBJETIVO], "demanda_pred": pa.demanda_pred}))

    # Evaluación (abr-jun 2025) contra las líneas base en las mismas filas
    ev = V.copy()
    ev["gbm_fe31"] = G.predecir_matriz(m, V)
    ev["metodo_extreme"] = metodo_extreme(E, V)
    ev["periodo"] = marcar_pico(E, V)
    modelos = ["gbm_fe31", "dia_hora_4sem", "dia_hora_8sem", "ultima_semana", "metodo_extreme"]
    tm = tabla_metricas(ev, modelos)
    tm.to_csv(args.salida / "metricas.csv", index=False)
    cobertura_pred = {mm: float(ev[mm].notna().mean()) for mm in modelos}
    comp = (tm[tm.periodo.eq("total")].pivot_table(index=["idruta", "horizonte_min"], columns="modelo",
                                                    values="wape").round(4))
    comp.to_csv(args.salida / "comparacion.csv")

    ie = G.aplicar_intervalos(pd.DataFrame({"idruta": V.id_ruta.astype(int).to_numpy(),
                                            "franja_inicio": V.origen.to_numpy(),
                                            "horizonte_min": V.horizonte_min.astype(int).to_numpy(),
                                            "demanda_pred": ev.gbm_fe31.to_numpy()}), intervalos)
    real = V[G.OBJETIVO].to_numpy()
    dentro = (real >= ie.limite_inferior.to_numpy()) & (real <= ie.limite_superior.to_numpy())
    cob = (pd.DataFrame({"idruta": ie.idruta, "horizonte_min": ie.horizonte_min, "dentro": dentro})
             .groupby(["idruta", "horizonte_min"]).dentro.mean().rename("cobertura_80").reset_index())
    cob.to_csv(args.salida / "cobertura_intervalos.csv", index=False)

    imp = importancia(m, V)
    imp.to_csv(args.salida / "importancia_global.csv", index=False)
    ejemplo = V[V.horizonte_min.eq(60)].sort_values(G.OBJETIVO).iloc[[-1]]
    explicacion_local(m, ejemplo, E).to_csv(args.salida / "explicacion_local.csv", index=False)

    # Modelo de producción: todo lo anterior a la prueba, mismo número de árboles
    m_prod = G.entrenar(H, max_iter=n_arboles)
    version = f"gbm_fe31_{datetime.now():%Y%m%dT%H%M%S}"
    total = tm[(tm.idruta == 0) & tm.periodo.eq("total")].set_index("modelo")
    resumen_metricas = {
        "evaluacion": "2025-04-01..2025-06-30 (filas comunes a todos los métodos)",
        "wape_total": total.wape.round(4).to_dict(), "mae_total": total.mae.round(2).to_dict(),
        "wape_pico_valle": tm[tm.idruta.eq(0) & tm.periodo.ne("total")].pivot_table(
            index="modelo", columns="periodo", values="wape").round(4).to_dict("index"),
        "cobertura_prediccion": cobertura_pred,
        "cobertura_intervalo_80": round(float(dentro.mean()), 3),
        "por_ruta_horizonte": tm[tm.modelo.eq("gbm_fe31") & tm.idruta.ne(0)]
            [["idruta", "horizonte_min", "periodo", "mae", "rmse", "wape", "smape", "n"]].round(4).to_dict("records"),
        "importancia_top10": imp.head(10)[["variable", "importancia_mae"]].round(3).to_dict("records"),
        "intervalos": intervalos.round(3).to_dict("records"),
    }
    art = G.Artefacto(m_prod, hor, intervalos, {
        "version": version, "version_datos": version_datos, "semilla": G.SEMILLA,
        "hiperparametros": {**G.HIPERPARAMETROS, "max_iter": n_arboles},
        "entrenado_hasta": str(H.origen.max()), "filas_entrenamiento": int(len(H)),
        "particiones": {"entrenamiento": "< 2025-01-01", "ajuste": "2025-01..03", "evaluacion": "2025-04..06",
                        "prueba": ">= 2025-07-01 (no usada)"},
        "metricas": {k: v for k, v in resumen_metricas.items() if k != "intervalos"}})
    ruta, sha = G.guardar(art, args.modelos, version)
    resumen = {"version": version, "artefacto": str(ruta), "sha256": sha, "version_datos": version_datos,
               "feature_version": FEATURE_VERSION, "reglas_version": REGLAS_VERSION,
               "arboles": n_arboles, "segundos": round(time.perf_counter() - t0, 1),
               "metricas": {k: v for k, v in resumen_metricas.items() if k != "intervalos"}}
    (args.salida / "resumen.json").write_text(json.dumps(resumen, indent=2, default=str), encoding="utf-8")

    print("\nWAPE en evaluación (abr-jun 2025), menor es mejor:\n", comp.to_string())
    print("\nTotal:", json.dumps(resumen_metricas["wape_total"]))
    print(f"\nArtefacto: {ruta} (sha256 {sha[:16]}…)")

    if args.registrar:
        registrar(version, sha, ruta.name, version_datos, n_arboles, resumen_metricas)


def registrar(version, sha, archivo, version_datos, n_arboles, met):
    from sqlalchemy import text

    from app.db.conexion import escribir
    with escribir() as c:
        mid = c.execute(text("""
            INSERT INTO model_registry (nombre, version, tipo, estado, semilla, hiperparametros, version_datos,
                                        feature_version, metricas, artefacto_ruta, artefacto_sha256)
            VALUES ('gbm_fe31', :v, 'candidato', 'candidato', :s, CAST(:h AS jsonb), :vd, :fv,
                    CAST(:m AS jsonb), :a, :sha) RETURNING model_id"""),
            {"v": version, "s": G.SEMILLA,
             "h": json.dumps({**G.HIPERPARAMETROS, "max_iter": n_arboles, "metodo": G.METODO,
                              "objetivo": G.OBJETIVO, "reglas_version": REGLAS_VERSION}),
             "vd": version_datos, "fv": FEATURE_VERSION, "m": json.dumps(met, default=float),
             "a": archivo, "sha": sha}).scalar()
    print(f"Registrado model_id={mid} como candidato. Actívelo con POST /api/v1/models/{mid}/activate")


if __name__ == "__main__":
    main()
