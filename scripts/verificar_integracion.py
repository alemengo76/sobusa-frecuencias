"""Verifica la integración contra la base real (local o Docker). SOLO LEE.

  python -m scripts.verificar_integracion --guardar huella_antes.json     # antes de migrar
  docker compose up --build                                               # aplica 0003 y 0004
  python -m scripts.verificar_integracion --comparar huella_antes.json    # después

Compara, tabla por tabla, el número de filas y un hash del contenido de lo que ya existía
(demanda_franja, prediction, recommendation, alert, data_quality_error, model_registry,
param_v1...). Lo nuevo (param_v2, viaje_fuente, modelos registrados después) no cuenta como cambio.
Con --prediccion AAAA-MM-DD además corre el modelo activo para ese día SIN guardar nada.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

HUELLA = {
    "demanda_franja": "SELECT count(*), md5(coalesce(string_agg(idruta || '|' || franja_inicio || '|' || "
                      "coalesce(pasajeros::text, '') || '|' || programaciones, ',' ORDER BY idruta, franja_inicio), '')) "
                      "FROM ai_frecuencias.demanda_franja",
    "prediction": "SELECT count(*), md5(coalesce(string_agg(prediction_id || '|' || demanda_pred, ',' "
                  "ORDER BY prediction_id), '')) FROM ai_frecuencias.prediction",
    "recommendation": "SELECT count(*), md5(coalesce(string_agg(recommendation_id || '|' || parametros_version, ',' "
                      "ORDER BY recommendation_id), '')) FROM ai_frecuencias.recommendation",
    "alert": "SELECT count(*), md5(coalesce(string_agg(alert_id || '|' || estado, ',' ORDER BY alert_id), '')) "
             "FROM ai_frecuencias.alert",
    "model_registry_previos": "SELECT count(*), md5(coalesce(string_agg(model_id || '|' || version, ',' "
                              "ORDER BY model_id), '')) FROM ai_frecuencias.model_registry WHERE nombre <> 'gbm_fe31'",
    "parametro_v1": "SELECT count(*), md5(coalesce(string_agg(id || '|' || factor_carga_max || '|' || capacidad_nominal, ',' "
                    "ORDER BY id), '')) FROM ai_frecuencias.parametro_operativo WHERE version = 'param_v1'",
    "execution_log": "SELECT count(*), '' FROM ai_frecuencias.execution_log",
}


# Historial de calidad: las 10 columnas de cada fila de data_quality_error tal como era antes de la
# 0007 (distinguiendo NULL, 'NaN' y 'nan'). Desde la 0007 se calcula con evento + detección de las
# filas migradas, así que la huella NO cambia al migrar: si cambia, se perdió o alteró algo.
_FILA_DQ = ("concat_ws('|', {id}, quote_nullable({lote}), quote_nullable(e.tabla_origen), "
            "quote_nullable(e.idprogramacion), quote_nullable({idruta}), quote_nullable(e.regla), "
            "quote_nullable({causa}), quote_nullable({valor}), quote_nullable(e.reglas_version), "
            "quote_nullable({creado_en} AT TIME ZONE 'UTC'))")
DQ_ANTES = ("SELECT count(*), coalesce(md5(string_agg(" + _FILA_DQ.format(
    id="e.id", lote="e.lote", idruta="e.idruta", causa="e.causa", valor="e.valor", creado_en="e.creado_en")
    + ", E'\\n' ORDER BY e.id)), '') FROM ai_frecuencias.data_quality_error e")
DQ_DESDE_0007 = ("SELECT count(*), coalesce(md5(string_agg(" + _FILA_DQ.format(
    id="d.id_original", lote="d.lote", idruta="d.idruta", causa="d.causa", valor="d.valor_original",
    creado_en="d.detectado_en") + ", E'\\n' ORDER BY d.id_original)), '') "
    "FROM ai_frecuencias.data_quality_deteccion d JOIN ai_frecuencias.data_quality_error e ON e.id = d.evento_id "
    "WHERE d.id_original IS NOT NULL")


def huella(c) -> dict:
    h = {k: list(c.execute(text(q)).one()) for k, q in HUELLA.items()}
    con_detecciones = c.execute(text("SELECT to_regclass('ai_frecuencias.data_quality_deteccion') IS NOT NULL")).scalar()
    h["data_quality_historial"] = list(c.execute(text(DQ_DESDE_0007 if con_detecciones else DQ_ANTES)).one())
    return h


def estado(c) -> dict:
    q = lambda s: c.execute(text(s)).scalar()  # noqa: E731
    existe = q("SELECT to_regclass('ai_frecuencias.viaje_fuente') IS NOT NULL")
    return {
        "alembic": q("SELECT version_num FROM ai_frecuencias.alembic_version"),
        "param_v2_vigentes": q("SELECT count(*) FROM ai_frecuencias.parametro_operativo "
                               "WHERE vigente AND version = 'param_v2'"),
        "viaje_fuente": q("SELECT count(*) FROM ai_frecuencias.viaje_fuente") if existe else None,
        "demanda_franja": q("SELECT count(*) FROM ai_frecuencias.demanda_franja"),
        "reglas_cuarentena": [list(f) for f in c.execute(text(
            "SELECT reglas_version, count(*) FROM ai_frecuencias.data_quality_error GROUP BY 1 ORDER BY 1"))],
        "calidad_eventos_repetidos": q(
            "SELECT count(*) FROM (SELECT 1 FROM ai_frecuencias.data_quality_error GROUP BY tabla_origen, "
            "idprogramacion, regla, reglas_version, (valor IS NULL), coalesce(valor, '') HAVING count(*) > 1) x"),
        "calidad_detecciones": q("SELECT count(*) FROM ai_frecuencias.data_quality_deteccion")
            if q("SELECT to_regclass('ai_frecuencias.data_quality_deteccion') IS NOT NULL") else None,
        "modelo_activo": q("SELECT nombre || ' ' || version FROM ai_frecuencias.model_registry WHERE estado = 'activo'"),
    }


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--guardar", type=Path)
    a.add_argument("--comparar", type=Path)
    a.add_argument("--prediccion", help="Día AAAA-MM-DD para probar el modelo activo (no guarda nada)")
    args = a.parse_args()

    from app.db.conexion import consultar_resultados
    with consultar_resultados() as c:
        h, e = huella(c), estado(c)
        print(json.dumps(e, indent=2, default=str))
        if args.guardar:
            args.guardar.write_text(json.dumps(h, indent=2), encoding="utf-8")
            print(f"huella guardada en {args.guardar}")
        fallas = 0
        if args.comparar:
            antes = json.loads(args.comparar.read_text(encoding="utf-8"))
            for k, v in antes.items():
                if k not in h:
                    print(f"--     {k}: huella de una versión anterior del script; vuelva a guardarla")
                    continue
                ok = h.get(k) == v or (k == "execution_log" and h[k][0] >= v[0])
                fallas += not ok
                print(f"{'OK   ' if ok else 'CAMBIO'} {k}: antes {v[0]} filas, ahora {h[k][0]}")
        if args.prediccion:
            from app import servicios
            from app.db import repositorio as repo
            modelo = repo.modelo_activo(c)
            pred, info = servicios.predecir(c, [2, 16, 21], datetime.fromisoformat(args.prediccion), modelo,
                                            (30, 60, 120))
            print(json.dumps(info, indent=2, default=str))
            print(pred.groupby(["idruta", "horizonte_min"]).demanda_pred.agg(["count", "sum"]).round(0).to_string())
    sys.exit(1 if fallas else 0)


if __name__ == "__main__":
    main()
