"""M1 contra PostgreSQL REAL: las consultas de solo lectura y el emparejamiento de punta a punta.

Inserta datos sintéticos DENTRO de una transacción que siempre se revierte (nada queda guardado), pero
igual solo corre en una base desechable:
  - variable SOBUSA_BD_DESECHABLE=1, y
  - current_database() empieza por "sobusa_desechable".
"""
import importlib.util
import os
import uuid
from datetime import datetime, timedelta

import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)
if os.environ.get("SOBUSA_BD_DESECHABLE") != "1":
    pytest.skip("requiere una base desechable (SOBUSA_BD_DESECHABLE=1)", allow_module_level=True)

from sqlalchemy import text  # noqa: E402

from app import evaluacion_real as er  # noqa: E402
from app.db.conexion import motor_escritura  # noqa: E402

DIA = datetime(2025, 10, 8)


@pytest.fixture()
def c():
    with motor_escritura().connect() as conexion:
        tx = conexion.begin()
        if not conexion.execute(text("SELECT current_database()")).scalar().startswith("sobusa_desechable"):
            tx.rollback()
            pytest.skip("la base no es desechable")
        conexion.execute(text("SET LOCAL timezone = 'America/Bogota'"))
        try:
            yield conexion
        finally:
            tx.rollback()


def _sembrar(c, marca: datetime | None):
    mid = c.execute(text("""INSERT INTO ai_frecuencias.model_registry
        (nombre, version, tipo, estado, version_datos, feature_version)
        VALUES ('m1_prueba', 'v1', 'candidato', 'candidato', 'sint', 'fe31') RETURNING model_id""")).scalar()
    eid, eid2 = str(uuid.uuid4()), str(uuid.uuid4())
    for e, resumen in ((eid, "{}"), (eid2, f'{{"reutiliza": "{eid}"}}')):
        c.execute(text("""INSERT INTO ai_frecuencias.execution_log (execution_id, tipo, estado, resumen)
                          VALUES (CAST(:e AS uuid), 'prediccion', 'exitosa', CAST(:r AS jsonb))"""),
                  {"e": e, "r": resumen})
    franjas = []
    for d in (DIA - timedelta(days=7), DIA):
        for k in range(96):
            t = d + timedelta(minutes=15 * k)
            pas = None if t == DIA + timedelta(hours=12) else 10       # franja en cuarentena
            franjas.append({"t": t, "p": pas})
    c.execute(text("""INSERT INTO ai_frecuencias.demanda_franja
                      (idruta, franja_inicio, pasajeros, programaciones, programaciones_validas, lote)
                      VALUES (16, :t, :p, 1, 1, 'm1_prueba')"""), franjas)
    if marca is not None:
        c.execute(text("""INSERT INTO ai_frecuencias.watermark (tabla_origen, ultima_actualizacion, lote)
                          VALUES ('progvehiculos', :m, 'm1_prueba')
                          ON CONFLICT (tabla_origen)
                          DO UPDATE SET ultima_actualizacion = EXCLUDED.ultima_actualizacion"""),
                  {"m": marca})
    preds = [(8, 60, 30.0), (8, 30, 25.0), (12, 60, 40.0)]
    for h, H, v in preds:
        c.execute(text("""INSERT INTO ai_frecuencias.prediction (execution_id, idruta, franja_inicio,
            horizonte_min, demanda_pred, limite_inferior, limite_superior, nivel_intervalo, model_id,
            model_version, feature_version, data_cutoff, latency_ms)
            VALUES (CAST(:e AS uuid), 16, :t, :H, :v, 0, 100, 0.8, :m, 'v1', 'fe31', :dc, 5)"""),
                  {"e": eid, "t": DIA + timedelta(hours=h), "H": H, "v": v, "m": mid, "dc": DIA})
    return mid, eid, eid2


def test_m1_de_punta_a_punta(c):
    mid, eid, eid2 = _sembrar(c, DIA + timedelta(days=10))
    r = er.evaluar(c, model_id=mid)
    p = r["pares"].set_index(["horizonte_min", "franja_inicio"])
    assert p.loc[(60, DIA + timedelta(hours=8)), "demanda_real"] == 40.0
    assert p.loc[(30, DIA + timedelta(hours=8)), "demanda_real"] == 20.0
    assert p.loc[(60, DIA + timedelta(hours=12)), "estado"] == "incompleto_cuarentena"
    res = er.resumen_metricas(r)
    assert res["conteo_estados"]["valido"] == 2
    assert {(g["horizonte_min"], g["mae"]) for g in res["datos"]} == {(30, 5.0), (60, 10.0)}
    assert r["parametros"]["corte_consolidacion"] == (DIA + timedelta(days=7)).isoformat()
    # la ejecución que reutiliza devuelve las mismas filas que la dueña, con la misma huella
    h_reutiliza = er.evaluar(c, execution_id=eid2)["huella"]
    assert h_reutiliza == r["huella"] == er.evaluar(c, execution_id=eid)["huella"]
    # filtros por ruta, horizonte y periodo
    assert len(er.evaluar(c, model_id=mid, horizonte_min=30)["pares"]) == 1
    tarde = er.evaluar(c, model_id=mid, desde=DIA + timedelta(hours=9), hasta=DIA + timedelta(days=1))
    assert len(tarde["pares"]) == 1
    assert er.evaluar(c, model_id=mid, idruta=2)["pares"].empty


def test_m1_sin_consolidar(c):
    mid, _, _ = _sembrar(c, DIA + timedelta(days=3, hours=8, minutes=30))
    est = er.evaluar(c, model_id=mid)["pares"].set_index("horizonte_min").estado
    assert est.loc[30] == "valido"          # termina 08:30, justo en el corte
    assert (est.loc[60] == "no_disponible").all()


def test_m1_consultas_validas_bajo_read_only():
    """Las cuatro consultas de M1 corren en una transacción READ ONLY, como en la API."""
    from app.db import lectura_real as lr
    from app.db.conexion import consultar_resultados
    with consultar_resultados() as ro:
        assert ro.execute(text("SHOW transaction_read_only")).scalar() == "on"
        lr.predicciones(ro, model_id=1, execution_id=str(uuid.uuid4()), idruta=16, horizonte_min=60,
                        desde=DIA, hasta=DIA + timedelta(days=1))
        lr.contar_predicciones(ro, idruta=16)
        lr.franjas_reales(ro, [16], DIA, DIA + timedelta(days=1))
        lr.marca_agua(ro)


SHA_ACTIVO = "37b5a28a1c807e0821f10c285ad1a3c61fd6906e49b26163bc7bf9788f4eadf1"


def test_m1_cero_operativo_con_el_horario_del_artefacto_activo(c):
    """Usa el horario guardado en el artefacto GBM activo (solo lectura, hash verificado): una ventana
    operativa sin salidas vale 0; a las 02:00 (fuera de horario) no se evalúa."""
    from pathlib import Path
    if not (Path("modelos") / "gbm_fe31_20261107T065452.pkl").exists():
        pytest.skip("no está el artefacto activo en ./modelos")
    mid = c.execute(text("""INSERT INTO ai_frecuencias.model_registry
        (nombre, version, tipo, estado, version_datos, feature_version, hiperparametros,
         artefacto_ruta, artefacto_sha256)
        VALUES ('gbm_prueba', 'v1', 'candidato', 'candidato', 'sint', 'fe31',
                '{"metodo": "gbm_fe31"}', 'gbm_fe31_20261107T065452.pkl', :sha) RETURNING model_id"""),
        {"sha": SHA_ACTIVO}).scalar()
    eid = str(uuid.uuid4())
    c.execute(text("""INSERT INTO ai_frecuencias.execution_log (execution_id, tipo, estado, resumen)
                      VALUES (CAST(:e AS uuid), 'prediccion', 'exitosa', '{}')"""), {"e": eid})
    # día cargado con una salida a las 06:00; 08:00-09:00 y 02:00-03:00 sin salidas
    c.execute(text("""INSERT INTO ai_frecuencias.demanda_franja
                      (idruta, franja_inicio, pasajeros, programaciones, programaciones_validas, lote)
                      VALUES (16, :t, 10, 1, 1, 'm1_prueba')"""), {"t": DIA + timedelta(hours=6)})
    c.execute(text("""INSERT INTO ai_frecuencias.watermark (tabla_origen, ultima_actualizacion, lote)
                      VALUES ('progvehiculos', :m, 'm1_prueba') ON CONFLICT (tabla_origen)
                      DO UPDATE SET ultima_actualizacion = EXCLUDED.ultima_actualizacion"""),
              {"m": DIA + timedelta(days=10)})
    for i, h in enumerate((8, 2)):
        c.execute(text("""INSERT INTO ai_frecuencias.prediction (execution_id, idruta, franja_inicio,
            horizonte_min, demanda_pred, limite_inferior, limite_superior, nivel_intervalo, model_id,
            model_version, feature_version, data_cutoff, latency_ms)
            VALUES (CAST(:e AS uuid), 16, :t, 60, :v, 0, 100, 0.8, :m, 'v1', 'fe31', :dc, 5)"""),
                  {"e": eid, "t": DIA + timedelta(hours=h), "v": 30.0 + i, "m": mid, "dc": DIA})
    r = er.evaluar(c, model_id=mid)
    m = r["pares"].set_index(r["pares"].franja_inicio.dt.hour).motivo
    assert m[8] == "cero_operativo" and m[2] == "fuera_de_horario"
    assert r["pares"].set_index(r["pares"].franja_inicio.dt.hour).demanda_real[8] == 0.0
    assert r["parametros"]["horario_operacion"][mid] == {
        "origen": "propio", "version": "v1", "artefacto_sha256": SHA_ACTIVO}
