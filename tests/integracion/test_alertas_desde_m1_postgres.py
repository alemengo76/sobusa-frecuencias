"""ALT-005/ALT-007 desde M1 contra PostgreSQL real: simula tras la ingesta y NO inserta alertas.
Transacción revertida, solo en base desechable."""
import importlib.util
import json
import os
import uuid
from datetime import timedelta

import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)
if os.environ.get("SOBUSA_BD_DESECHABLE") != "1":
    pytest.skip("requiere una base desechable (SOBUSA_BD_DESECHABLE=1)", allow_module_level=True)

from sqlalchemy import text  # noqa: E402

from app.alertas import desde_m1  # noqa: E402
from app.alertas.motor import clave_dedup  # noqa: E402
from tests.integracion.test_m1_postgres import DIA, c  # noqa: E402,F401


def _contar(c, tabla):  # noqa: F811
    return c.execute(text(f"SELECT count(*) FROM ai_frecuencias.{tabla}")).scalar()


def _escenario(c):  # noqa: F811
    """Modelo activo 'gbm' que cayó al respaldo en la ruta 21. Real: 10 pasajeros por franja."""
    c.execute(text("SET LOCAL search_path TO ai_frecuencias"))
    c.execute(text("UPDATE ai_frecuencias.model_registry SET estado = 'retirado' WHERE estado = 'activo'"))
    gbm = c.execute(text("""INSERT INTO ai_frecuencias.model_registry
        (nombre, version, tipo, estado, version_datos, feature_version, hiperparametros)
        VALUES ('gbm_prueba', 'g1', 'candidato', 'activo', 'sint', 'fe31', '{"metodo": "gbm_fe31"}')
        RETURNING model_id""")).scalar()
    base = c.execute(text("""INSERT INTO ai_frecuencias.model_registry
        (nombre, version, tipo, estado, version_datos, feature_version, hiperparametros)
        VALUES ('base_prueba', 'b1', 'baseline', 'retirado', 'sint', 'fe21', '{"metodo": "dia_hora_4sem"}')
        RETURNING model_id""")).scalar()
    otro = c.execute(text("""INSERT INTO ai_frecuencias.model_registry
        (nombre, version, tipo, estado, version_datos, feature_version)
        VALUES ('viejo', 'v0', 'candidato', 'retirado', 'sint', 'fe31') RETURNING model_id""")).scalar()
    eid, eid_otro = str(uuid.uuid4()), str(uuid.uuid4())
    for e, res in ((eid, {"modelo_activo": gbm, "respaldo": True}), (eid_otro, {"model_id": otro})):
        c.execute(text("""INSERT INTO ai_frecuencias.execution_log (execution_id, tipo, estado, resumen)
                          VALUES (CAST(:e AS uuid), 'prediccion', 'exitosa', CAST(:r AS jsonb))"""),
                  {"e": e, "r": json.dumps(res)})
    filas = [{"r": r, "t": DIA + timedelta(minutes=15 * k), "p": 10} for r in (16, 21) for k in range(96)]
    c.execute(text("""INSERT INTO ai_frecuencias.demanda_franja
                      (idruta, franja_inicio, pasajeros, programaciones, programaciones_validas, lote)
                      VALUES (:r, :t, :p, 1, 1, 'm1_alt')"""), filas)
    preds = [(eid, gbm, "g1", 16, 8, 60, 80.0), (eid, gbm, "g1", 16, 10, 60, 25.0),
             (eid, gbm, "g1", 16, 12, 60, 41.0), (eid, gbm, "g1", 16, 8, 30, 60.0),
             (eid, base, "b1", 21, 8, 60, 80.0), (eid_otro, otro, "v0", 16, 14, 60, 90.0)]
    for e, m, v, ruta, h, hz, val in preds:
        c.execute(text("""INSERT INTO ai_frecuencias.prediction (execution_id, idruta, franja_inicio,
            horizonte_min, demanda_pred, limite_inferior, limite_superior, nivel_intervalo, model_id,
            model_version, feature_version, data_cutoff, latency_ms)
            VALUES (CAST(:e AS uuid), :r, :t, :hz, :v, 0, 200, 0.8, :m, :ver, :fv, :dc, 5)"""),
                  {"e": e, "r": ruta, "t": DIA + timedelta(hours=h), "hz": hz, "v": val, "m": m, "ver": v,
                   "fv": "fe21" if m == base else "fe31", "dc": DIA})
    marca = DIA + timedelta(days=10)
    c.execute(text("""INSERT INTO ai_frecuencias.watermark (tabla_origen, ultima_actualizacion, lote)
                      VALUES ('progvehiculos', :m, 'm1_alt') ON CONFLICT (tabla_origen)
                      DO UPDATE SET ultima_actualizacion = EXCLUDED.ultima_actualizacion"""), {"m": marca})
    return gbm, base, eid, marca


def test_tras_ingesta_simula_con_respaldo_sin_escribir(c):  # noqa: F811
    gbm, base, eid, marca = _escenario(c)
    antes = {t: _contar(c, t) for t in ("alert", "alert_history", "notification_log", "execution_log")}
    r = desde_m1.evaluar_tras_ingesta(c, None, marca)
    assert r["modelo_activo"] == gbm and r["ejecuciones_monitoreadas"] == 1
    assert r["por_codigo"] == {"ALT-005": 2, "ALT-007": 1}
    assert r["por_origen"] == {"modelo_activo": 2, "respaldo": 1}
    assert {a["trazabilidad"]["model_id"] for a in r["alertas"]} == {gbm, base}
    assert all(a["trazabilidad"]["execution_id_prediccion"] == eid for a in r["alertas"])
    assert all(a["horizonte_min"] == 60 for a in r["alertas"])     # la de 30' no entra
    assert {t: _contar(c, t) for t in antes} == antes                # nada escrito
    # idempotente
    assert desde_m1.evaluar_tras_ingesta(c, None, marca) == r


def test_deduplica_contra_alertas_abiertas(c):  # noqa: F811
    gbm, _, _, marca = _escenario(c)
    clave = clave_dedup("ALT-005", 16, DIA + timedelta(hours=8), 60)
    c.execute(text("""INSERT INTO ai_frecuencias.alert (codigo, severidad, idruta, franja_inicio,
                      horizonte_min, umbral, explicacion, recomendacion, clave_dedup)
                      VALUES ('ALT-005', 'media', 16, :t, 60, 0.4, 'previa', 'x', :k)"""),
              {"t": DIA + timedelta(hours=8), "k": clave})
    r = desde_m1.evaluar_tras_ingesta(c, None, marca)
    assert r["candidatas"] == 3 and r["nuevas_tras_deduplicar"] == 2
    assert clave not in {a["clave_dedup"] for a in r["alertas"]}
