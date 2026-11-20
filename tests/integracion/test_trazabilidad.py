"""P1 (A1 + A2): trazabilidad entre predicciones y recomendaciones, contra PostgreSQL REAL.

Estas pruebas CONFIRMAN transacciones (ejecutan los servicios completos y prueban concurrencia entre
dos conexiones), así que solo corren en una base DESECHABLE:

  - variable SOBUSA_BD_DESECHABLE=1, y
  - el nombre de la base (current_database()) empieza por "sobusa_desechable".

Con cualquier otra base se saltan. La base desechable se crea restaurando un respaldo (ver
DECISIONES.md §15) y se borra al terminar; nunca se usa la base de trabajo ni la de la demo.

Las pruebas marcadas "reproduce A1/A2" fallan con el código anterior a la corrección.
"""
import importlib.util
import os
import threading
import time
import uuid
from datetime import datetime

import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)
if os.environ.get("SOBUSA_BD_DESECHABLE") != "1":
    pytest.skip("requiere una base desechable (SOBUSA_BD_DESECHABLE=1)", allow_module_level=True)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError, OperationalError  # noqa: E402

from app import servicios  # noqa: E402
from app.db.conexion import motor_escritura  # noqa: E402

RUTAS, HORIZONTES = [16], [60]
HUELLA_PRUEBA = "f" * 63 + "0"


@pytest.fixture(scope="module", autouse=True)
def base_desechable():
    with motor_escritura().connect() as c:
        nombre = c.execute(text("SELECT current_database()")).scalar()
        version = c.execute(text("SELECT version_num FROM ai_frecuencias.alembic_version")).scalar()
    if not nombre.startswith("sobusa_desechable"):
        pytest.skip(f"la base '{nombre}' no es desechable: no se ejecutan pruebas que confirman datos")
    if version < "0008":
        pytest.skip("la base desechable no está en la versión 0008")


def q(sql, **p):
    with motor_escritura().connect() as c:
        return c.execute(text(sql), p).all()


def ejecucion(eid):
    (fila,) = q("SELECT tipo, estado, resumen, parametros FROM ai_frecuencias.execution_log "
                "WHERE execution_id = :e", e=eid)
    return fila


def filas_guardadas(eid) -> int:
    return q("SELECT count(*) FROM ai_frecuencias.prediction WHERE execution_id = :e", e=eid)[0][0]


def duena(eid) -> str:
    """Ejecución que tiene las filas de `eid`: ella misma o la que reutiliza."""
    return (ejecucion(eid).resumen or {}).get("reutiliza") or eid


def predecir(dia):
    return servicios.ejecutar_predicciones(RUTAS, dia, HORIZONTES, f"prueba-{uuid.uuid4()}")


def frecuencias(dia):
    return servicios.ejecutar_frecuencias(RUTAS, dia, HORIZONTES, "puntual", f"prueba-{uuid.uuid4()}")


# ------------------------------------------------------------------ A1

def test_reproduce_a1_lo_declarado_coincide_con_lo_guardado_al_repetir():
    dia = datetime(2025, 12, 3)
    r1, r2 = predecir(dia), predecir(dia)
    for r in (r1, r2):
        assert r["estado"] == "exitosa"
        declaradas = ejecucion(r["execution_id"]).resumen["predicciones"]
        assert filas_guardadas(duena(r["execution_id"])) == declaradas > 0
    # la segunda no guarda copias: reutiliza las de la primera
    assert filas_guardadas(r2["execution_id"]) == 0 and duena(r2["execution_id"]) == r1["execution_id"]


def test_historico_cambiado_guarda_predicciones_nuevas():
    dia = datetime(2025, 12, 4)
    r1 = predecir(dia)
    with motor_escritura().begin() as c:     # base desechable: se altera un viaje anterior al día
        c.execute(text("""UPDATE ai_frecuencias.viaje_fuente SET numeropasajeros = numeropasajeros + 40
                          WHERE idprogramacion = (SELECT idprogramacion FROM ai_frecuencias.viaje_fuente
                                                  WHERE idruta = 16 AND fechasalida >= :d - interval '7 days'
                                                    AND fechasalida < :d AND numeropasajeros IS NOT NULL
                                                  ORDER BY fechasalida LIMIT 1)"""), {"d": dia})
    r2 = predecir(dia)
    assert duena(r2["execution_id"]) == r2["execution_id"], "no debe reutilizar si el histórico cambió"
    assert filas_guardadas(r2["execution_id"]) > 0
    distintas = q("""SELECT count(*) FROM ai_frecuencias.prediction a JOIN ai_frecuencias.prediction b
                     USING (idruta, sentido, segmento_id, franja_inicio, horizonte_min)
                     WHERE a.execution_id = :a AND b.execution_id = :b AND a.demanda_pred <> b.demanda_pred""",
                  a=r1["execution_id"], b=r2["execution_id"])[0][0]
    assert distintas > 0


# ------------------------------------------------------------------ A2

CLAVE = ("idruta", "sentido", "segmento_id", "franja_inicio", "horizonte_min")


def verificar_enlace(eid_recomendaciones, eid_prediccion):
    """Comprueba en PostgreSQL, fila por fila, que las recomendaciones de eid_recomendaciones apuntan a
    las predicciones guardadas de la dueña de eid_prediccion: una por predicción, ninguna sin
    prediction_id, ninguna de otra ejecución y con la misma ruta, sentido, segmento, franja y horizonte."""
    eid_pred = duena(eid_prediccion)
    distintas = " OR ".join(f"r.{k} IS DISTINCT FROM p.{k}" for k in CLAVE)
    malas = q(f"""SELECT r.recommendation_id, r.prediction_id, p.execution_id,
                         {", ".join(f"r.{k} AS r_{k}, p.{k} AS p_{k}" for k in CLAVE)}
                  FROM ai_frecuencias.recommendation r
                  LEFT JOIN ai_frecuencias.prediction p ON p.prediction_id = r.prediction_id
                  WHERE r.execution_id = :e
                    AND (r.prediction_id IS NULL OR p.execution_id IS DISTINCT FROM :p OR {distintas})
                  ORDER BY r.recommendation_id LIMIT 5""", e=eid_recomendaciones, p=eid_pred)
    assert not malas, f"recomendaciones mal enlazadas (primeras 5): {malas}"
    (total, distintas_pid), = q("""SELECT count(*), count(DISTINCT prediction_id)
                                   FROM ai_frecuencias.recommendation WHERE execution_id = :e""",
                                e=eid_recomendaciones)
    sin_rec = q("""SELECT count(*) FROM ai_frecuencias.prediction p WHERE p.execution_id = :p
                   AND NOT EXISTS (SELECT 1 FROM ai_frecuencias.recommendation r
                                   WHERE r.execution_id = :e AND r.prediction_id = p.prediction_id)""",
                e=eid_recomendaciones, p=eid_pred)[0][0]
    assert total > 0 and total == distintas_pid == filas_guardadas(eid_pred) and sin_rec == 0


def test_reproduce_a2_frecuencias_enlaza_cada_recomendacion():
    r = frecuencias(datetime(2025, 12, 5))
    verificar_enlace(r["execution_id"], ejecucion(r["execution_id"]).parametros["prediccion"])


def test_frecuencias_repetida_reutiliza_y_enlaza():
    dia = datetime(2025, 12, 9)
    r1, r2 = frecuencias(dia), frecuencias(dia)
    p1, p2 = (ejecucion(r["execution_id"]).parametros["prediccion"] for r in (r1, r2))
    for r, p in ((r1, p1), (r2, p2)):
        verificar_enlace(r["execution_id"], p)
    assert duena(p2) == duena(p1)


def test_recommendations_calculate_con_ejecucion_que_reutiliza():
    """El camino de POST /recommendations/calculate (antes: 404 con una ejecución que reutiliza)."""
    dia = datetime(2025, 12, 10)
    predecir(dia)
    r2 = predecir(dia)
    with motor_escritura().begin() as c:
        c.execute(text("SET LOCAL search_path TO ai_frecuencias"))
        pred = servicios.predicciones_de_ejecucion(c, r2["execution_id"])
        assert len(pred) > 0 and pred.prediction_id.notna().all()
        eid = servicios.repo.crear_ejecucion(c, "recomendacion", {"execution_id_prediccion": r2["execution_id"]},
                                             f"prueba-{uuid.uuid4()}")
        servicios.calcular_recomendaciones(c, eid, pred, "puntual")
        servicios.repo.cerrar_ejecucion(c, eid, "exitosa", {})
    verificar_enlace(eid, r2["execution_id"])


# ------------------------------------------------------------------ fallos y reserva de la huella

def test_falla_al_insertar_no_deja_reserva_ni_ejecucion_exitosa(monkeypatch):
    dia = datetime(2025, 12, 11)
    antes = q("SELECT count(*) FROM ai_frecuencias.prediction_set")[0][0]

    def falla(*a, **k):
        raise RuntimeError("falla simulada al insertar")

    monkeypatch.setattr(servicios.repo, "insertar_predicciones", falla)
    with pytest.raises(RuntimeError, match="simulada"):
        predecir(dia)
    assert q("SELECT count(*) FROM ai_frecuencias.prediction_set")[0][0] == antes
    ultimas = q("""SELECT estado, error FROM ai_frecuencias.execution_log WHERE tipo = 'prediccion'
                   AND parametros->>'dia' LIKE '2025-12-11%' ORDER BY iniciada_en DESC LIMIT 1""")
    assert ultimas and ultimas[0].estado == "fallida" and "simulada" in ultimas[0].error


def test_recomendacion_nueva_sin_prediction_id_se_rechaza():
    with pytest.raises(DBAPIError, match="ck_recommendation_con_prediccion"):
        with motor_escritura().begin() as c:
            c.execute(text("""INSERT INTO ai_frecuencias.recommendation
                              SELECT * FROM ai_frecuencias.recommendation WHERE prediction_id IS NULL LIMIT 1"""))


# ------------------------------------------------------------------ concurrencia sobre prediction_set

def _reservar(c, eid, huella=HUELLA_PRUEBA, lock_timeout=None):
    if lock_timeout:
        c.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
    c.execute(text("INSERT INTO ai_frecuencias.execution_log (execution_id, tipo, estado) "
                   "VALUES (:e, 'prediccion', 'en_curso')"), {"e": eid})
    return c.execute(text("""INSERT INTO ai_frecuencias.prediction_set (huella, execution_id, formato, filas)
                             VALUES (:h, :e, 'huella-pred/1', 1) ON CONFLICT (huella) DO NOTHING
                             RETURNING execution_id"""), {"h": huella, "e": eid}).scalar()


def _limpiar_reserva():
    with motor_escritura().begin() as c:
        c.execute(text("DELETE FROM ai_frecuencias.prediction_set WHERE huella = :h"), {"h": HUELLA_PRUEBA})


def _segunda(resultado, **k):
    try:
        with motor_escritura().begin() as c:
            t0 = time.perf_counter()
            resultado["duena"] = _reservar(c, str(uuid.uuid4()), **k)
            resultado["espera"] = time.perf_counter() - t0
            resultado["vista"] = c.execute(text("SELECT execution_id FROM ai_frecuencias.prediction_set "
                                                "WHERE huella = :h"), {"h": HUELLA_PRUEBA}).scalar()
    except Exception as e:  # noqa: BLE001
        resultado["error"] = e


@pytest.mark.parametrize("fin", ["rollback", "commit"])
def test_dos_solicitudes_con_la_misma_huella(fin):
    _limpiar_reserva()
    primera = str(uuid.uuid4())
    c1 = motor_escritura().connect()
    t1 = c1.begin()
    assert c1.execute(text("SELECT current_setting('transaction_isolation')")).scalar() == "read committed"
    assert str(_reservar(c1, primera)) == primera
    r = {}
    hilo = threading.Thread(target=_segunda, args=(r,))
    hilo.start()
    time.sleep(1.5)
    assert hilo.is_alive(), "la segunda solicitud debe esperar a que la primera termine"
    (t1.rollback if fin == "rollback" else t1.commit)()
    c1.close()
    hilo.join(10)
    assert "error" not in r and r["espera"] >= 1.0
    if fin == "rollback":
        assert r["duena"] is not None and str(r["vista"]) == str(r["duena"])     # la segunda pasa a ser dueña
    else:
        assert r["duena"] is None and str(r["vista"]) == primera                 # reutiliza a la primera
    _limpiar_reserva()


def test_servicio_responde_conflicto_con_el_error_real_del_controlador(monkeypatch):
    """lock_timeout de verdad (psycopg 3 + SQLAlchemy): el servicio lo reconoce, responde Conflicto (409)
    y registra la ejecución fallida aparte."""
    _limpiar_reserva()
    monkeypatch.setattr(servicios, "LOCK_TIMEOUT", "1s")
    monkeypatch.setattr(servicios.contrato, "huella_predicciones", lambda *a, **k: HUELLA_PRUEBA)
    c1 = motor_escritura().connect()
    t1 = c1.begin()
    _reservar(c1, str(uuid.uuid4()))                     # otra "ejecución" tiene la huella sin confirmar
    try:
        with pytest.raises(servicios.Conflicto) as info:
            predecir(datetime(2025, 12, 12))
    finally:
        t1.rollback()
        c1.close()
    assert servicios._es_lock_timeout(info.value.__cause__)
    assert type(info.value.__cause__.orig).__name__ == "LockNotAvailable"
    (fila,) = q("""SELECT estado, error FROM ai_frecuencias.execution_log WHERE tipo = 'prediccion'
                   AND parametros->>'dia' LIKE '2025-12-12%' ORDER BY iniciada_en DESC LIMIT 1""")
    assert fila.estado == "fallida" and "LockNotAvailable" in fila.error
    _limpiar_reserva()


def test_lock_timeout_corta_la_espera():
    _limpiar_reserva()
    c1 = motor_escritura().connect()
    t1 = c1.begin()
    _reservar(c1, str(uuid.uuid4()))
    r = {}
    hilo = threading.Thread(target=_segunda, args=(r,), kwargs={"lock_timeout": "1s"})
    hilo.start()
    hilo.join(10)
    t1.rollback()
    c1.close()
    assert isinstance(r.get("error"), OperationalError) and "lock timeout" in str(r["error"])
    _limpiar_reserva()
