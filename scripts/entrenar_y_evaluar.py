"""REEMPLAZADO PARA PRODUCCIÓN por scripts/entrenar_gbm_fe31.py. Este script usa las variables
antiguas (sobusa-fe-2.1-d1, auditoria_franjas_conjunta.parquet), un modelo por horizonte, parada
temprana con una muestra aleatoria del entrenamiento y artefactos que la API no carga.

Entrena y evalúa líneas base, método Extreme y gradient boosting (E05).

Uso (desde la raíz del proyecto):
  python -m scripts.entrenar_y_evaluar \
      --franjas "../sobusa-book-main/salidas_feature_engineering/auditoria_franjas_conjunta.parquet" \
      --salida salidas_modelo

Particiones (cronológicas, sin fuga):
  entrenamiento 2023-2024 · calibración de intervalos ene-mar 2025 ·
  evaluación abr-jun 2025 · prueba jul-dic 2025 NO SE TOCA (la reserva el equipo/Extreme).
Salidas: metricas.csv, cobertura_intervalos.csv, intervalos_<modelo>.json,
importancia_global.csv, explicacion_local.csv, artefactos/ y registro_modelos.sql.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from app.ml import baseline, evaluacion, gbm
from app.ml.datos import cargar_series

HORIZONTES = (30, 60, 120)
CAL_DESDE, EVAL_DESDE, PRUEBA_DESDE = "2025-01-01", "2025-04-01", "2025-07-01"


def festivos_colombia():
    try:
        import holidays
        return holidays.Colombia(years=range(2023, 2027))
    except ImportError:
        print("AVISO: instale 'holidays' para marcar festivos.")
        return {}


def predicciones(series, festivos) -> pd.DataFrame:
    partes, modelos = [], {}
    for h in HORIZONTES:
        tabla = gbm.tabla_modelo(series, h, festivos)
        entren = tabla[tabla.franja_inicio < CAL_DESDE]
        m = gbm.entrenar(entren)
        modelos[h] = (m, tabla)
        fuera = tabla[(tabla.franja_inicio >= CAL_DESDE) & (tabla.franja_inicio < PRUEBA_DESDE)]
        partes.append(fuera[["idruta", "franja_inicio", "horizonte_min", "y"]].assign(
            modelo="gbm_v1", demanda_pred=gbm.predecir(m, fuera)))
        for idruta, s in series.items():
            real = fuera[fuera.idruta == idruta][["franja_inicio", "y"]]
            for metodo in baseline.METODOS:
                p = baseline.predecir(s, metodo, (h,))
                partes.append(real.merge(p, on="franja_inicio").assign(idruta=idruta, modelo=metodo))
            ext = baseline.MetodoExtreme(festivos).ajustar(s[s.index < CAL_DESDE])
            p = ext.predecir(pd.DatetimeIndex(real.franja_inicio), (h,))
            partes.append(real.merge(p, on="franja_inicio").assign(idruta=idruta, modelo="metodo_extreme"))
    df = pd.concat(partes, ignore_index=True).rename(columns={"y": "demanda_real"})
    return df, modelos


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--franjas", required=True, type=Path)
    a.add_argument("--salida", default=Path("salidas_modelo"), type=Path)
    args = a.parse_args()
    args.salida.mkdir(parents=True, exist_ok=True)

    festivos = festivos_colombia()
    series = cargar_series(args.franjas)
    df, modelos = predicciones(series, festivos)
    df["hora"] = pd.DatetimeIndex(df.franja_inicio).hour

    # Pico/valle con la definición del equipo, calculada sobre entrenamiento
    entren = pd.concat([pd.DataFrame({"idruta": r, "franja_inicio": s.index, "demanda_real": s.to_numpy()})
                        for r, s in series.items()])
    pico = evaluacion.marcar_pico(entren[entren.franja_inicio < CAL_DESDE], festivos)

    cal = df[df.franja_inicio < EVAL_DESDE]
    ev = df[df.franja_inicio >= EVAL_DESDE].copy()
    metricas = evaluacion.evaluar(ev, pico=pico, festivos=festivos)
    metricas.to_csv(args.salida / "metricas.csv", index=False)

    cobertura = []
    for modelo, g in cal.groupby("modelo"):
        errores = baseline.ajustar_intervalos(g)
        (args.salida / f"intervalos_{modelo}.json").write_text(errores.to_json(orient="records"))
        e = baseline.aplicar_intervalos(ev[ev.modelo == modelo].dropna(subset=["demanda_pred"]), errores)
        dentro = (e.demanda_real >= e.limite_inferior) & (e.demanda_real <= e.limite_superior)
        cobertura.append({"modelo": modelo, "cobertura_80": dentro[e.demanda_real.notna()].mean()})
    pd.DataFrame(cobertura).to_csv(args.salida / "cobertura_intervalos.csv", index=False)

    # Explicabilidad (horizonte de 60 min)
    m60, tabla60 = modelos[60]
    entren60 = tabla60[tabla60.franja_inicio < CAL_DESDE]
    evaluacion60 = tabla60[(tabla60.franja_inicio >= EVAL_DESDE) & (tabla60.franja_inicio < PRUEBA_DESDE)]
    gbm.importancia_global(m60, evaluacion60).to_csv(args.salida / "importancia_global.csv", index=False)
    ejemplo = evaluacion60.dropna(subset=["y"]).sort_values("y").iloc[[-1]]
    gbm.explicacion_local(m60, ejemplo, entren60).to_csv(args.salida / "explicacion_local.csv", index=False)

    # Artefactos con hash y SQL para registrarlos
    sql = []
    resumen = metricas[metricas.periodo == "total"]
    for h, (m, _) in modelos.items():
        met = resumen[(resumen.modelo == "gbm_v1") & (resumen.horizonte_min == h)]
        meta = gbm.guardar_artefacto(m, args.salida / "artefactos", f"gbm_v1_h{h}", {
            "horizonte_min": h, "version_datos": args.franjas.name,
            "metricas": met[["idruta", "mae", "rmse", "wape"]].to_dict("records")})
        sql.append("INSERT INTO ai_frecuencias.model_registry (nombre, version, tipo, estado, semilla, "
                   "hiperparametros, version_datos, feature_version, metricas, artefacto_ruta, artefacto_sha256) "
                   f"VALUES ('gbm_h{h}', 'gbm_v1_h{h}', 'candidato', 'candidato', {gbm.SEMILLA}, "
                   f"'{json.dumps(gbm.HIPERPARAMETROS)}', '{args.franjas.name}', '{meta['feature_version']}', "
                   f"'{json.dumps(meta['metricas'], default=float)}', '{meta['artefacto_ruta']}', "
                   f"'{meta['artefacto_sha256']}');")
    (args.salida / "registro_modelos.sql").write_text("\n".join(sql) + "\n", encoding="utf-8")

    tabla = (resumen.pivot_table(index=["idruta", "horizonte_min"], columns="modelo", values="wape")
                    .round(3))
    print("WAPE en evaluación (abr-jun 2025), menor es mejor:\n", tabla.to_string())
    print(pd.DataFrame(cobertura).to_string(index=False))


if __name__ == "__main__":
    main()
