"""Ciclo diario, GET /alerts por HTTP y concurrencia real: pruebas que CONFIRMAN datos.

Solo corren en una instancia EXCLUSIVA para esta suite, preparada con scripts/preparar_bd_suite_alertas.sh:
además de SOBUSA_BD_DESECHABLE=1 y del nombre `sobusa_desechable_suite_*`, la base debe tener el comentario
`sobusa_suite_alertas` (lo pone únicamente ese script) y el servidor debe ser local (socket, sin dirección
de red). Antes de escribir se verifican la revisión Alembic, los permisos y que la instancia no tenga
alertas de retraso ni intentos de ingesta de `progvehiculos` previos (el verificador los leería).
Nunca se ejecutan contra la base del equipo (`sobusa_desechable_db`): el nombre la excluye.

No hay TRUNCATE ni DROP. Cada prueba crea sus ejecuciones con correlation_id EXACTOS de una corrida única
(`suite-alertas-<uuid>-...`), que el ciclo propaga a todas las ejecuciones que crea. Al terminar, incluso si
falla una aserción, se borran solo esas ejecuciones y sus alertas, historial y notificaciones.
`test_limpieza_conserva_una_ejecucion_ajena_de_la_misma_ventana` demuestra que lo ajeno sobrevive.
"""
# ruff: noqa: DTZ007  -- hora local de Bogotá a propósito
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time as reloj
import uuid
from datetime import datetime, timedelta

import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)
if os.environ.get("SOBUSA_BD_DESECHABLE") != "1":
    pytest.skip("requiere SOBUSA_BD_DESECHABLE=1", allow_module_level=True)
fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import ciclo_alertas as ciclo  # noqa: E402
from app.alertas import retraso_ingesta as rt  # noqa: E402
from app.api import main  # noqa: E402
from app.db.conexion import motor_escritura  # noqa: E402

MARCA_INSTANCIA = "sobusa_suite_alertas"


def q(sql, **p):
    with motor_escritura().connect() as c, c.begin():
        return c.execute(text(sql), p).all()


@pytest.fixture(scope="module")
def instancia_dedicada():
    (base, ip, comentario, crea_public, escribe_public, crea_ai), = q("""
        SELECT current_database(), inet_server_addr()::text,
               coalesce(shobj_description((SELECT oid FROM pg_database WHERE datname = current_database()),
                                          'pg_database'), ''),
               has_schema_privilege(current_user, 'public', 'CREATE'),
               (SELECT count(*) FROM information_schema.role_table_grants
                WHERE grantee = current_user AND table_schema = 'public'
                  AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')),
               has_schema_privilege(current_user, 'ai_frecuencias', 'CREATE')""")
    if (not base.startswith("sobusa_desechable_suite_") or MARCA_INSTANCIA not in comentario
            or ip is not None):
        pytest.skip(f"'{base}' no es la instancia exclusiva de esta suite "
                    f"(sobusa_desechable_suite_*, comentario '{MARCA_INSTANCIA}', socket local)")
    (rev,), = q("SELECT version_num FROM ai_frecuencias.alembic_version")
    assert re.fullmatch(r"[0-9]{4}", rev) and int(rev) >= 8, rev
    assert not crea_public and escribe_public == 0 and crea_ai, "permisos inesperados"
    return base


def limpiar(correlation_ids: list[str]) -> None:
    """Borra SOLO las ejecuciones con esos correlation_id exactos y sus alertas, historial y notificaciones."""
    if not correlation_ids:
        return
    with motor_escritura().connect() as c, c.begin():
        p = {"cids": list(correlation_ids)}
        ejec = "SELECT execution_id FROM ai_frecuencias.execution_log WHERE correlation_id = ANY(:cids)"
        alertas = f"SELECT alert_id FROM ai_frecuencias.alert WHERE execution_id IN ({ejec})"
        c.execute(text(f"DELETE FROM ai_frecuencias.notification_log WHERE alert_id IN ({alertas})"), p)
        c.execute(text(f"DELETE FROM ai_frecuencias.alert_history WHERE alert_id IN ({alertas})"), p)
        c.execute(text(f"DELETE FROM ai_frecuencias.alert WHERE alert_id IN ({alertas})"), p)
        c.execute(text("DELETE FROM ai_frecuencias.execution_log WHERE correlation_id = ANY(:cids)"), p)


class Corrida:
    def __init__(self):
        self.id = f"suite-alertas-{uuid.uuid4().hex}"
        self.cids: list[str] = []

    def cid(self, nombre="ejecucion") -> str:
        c = f"{self.id}-{nombre}-{len(self.cids)}"
        self.cids.append(c)
        return c


@pytest.fixture
def corrida(instancia_dedicada):
    previas = q("""SELECT (SELECT count(*) FROM ai_frecuencias.alert
                           WHERE starts_with(clave_dedup, 'ALT-009|retraso|progvehiculos|')),
                          (SELECT count(*) FROM ai_frecuencias.execution_log
                           WHERE tipo = 'ingesta' AND parametros->>'fuente' = 'progvehiculos')""")[0]
    assert tuple(previas) == (0, 0), (
        f"la instancia ya tiene alertas de retraso o intentos de progvehiculos {tuple(previas)}: no es exclusiva")
    r = Corrida()
    try:
        yield r
    finally:
        limpiar(r.cids)


def hoy():
    return datetime.now(rt.ZONA).date()


@pytest.fixture
def politica(monkeypatch):
    """SOLO PRUEBAS: valores controlados. La política operativa real está pendiente de validación."""
    def fijar(vencimiento="05:00", inicio=None):
        for k, v in {"apertura": "00:00", "vencimiento": vencimiento, "tolerancia_min": "0",
                     "version": "prueba-controlada",
                     "inicio_monitoreo": (inicio or hoy() - timedelta(days=2)).isoformat()}.items():
            monkeypatch.setenv(rt.ENV[k], v)
    return fijar


def alertas_suite(corrida):
    return [r._asdict() for r in q("""
        SELECT a.* FROM ai_frecuencias.alert a JOIN ai_frecuencias.execution_log e USING (execution_id)
        WHERE e.correlation_id = ANY(:cids) ORDER BY a.alert_id""", cids=corrida.cids)]


def ejecuciones(corrida, tipo):
    return q("""SELECT execution_id, estado, error, parametros, resumen FROM ai_frecuencias.execution_log
                WHERE tipo = :t AND correlation_id = ANY(:cids) ORDER BY iniciada_en""", t=tipo, cids=corrida.cids)


@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setenv("SOBUSA_ADMIN_TOKEN", "token-pruebas-admin")
    return TestClient(main.app, headers={"Authorization": "Bearer token-pruebas-admin"})


def test_verificador_sin_ingesta_get_alerts_por_http_y_cierre(corrida, politica, cliente):
    politica()
    r = ciclo.verificar_retraso(correlation_id=corrida.cid("verificar"))["alt_009_retraso"]
    assert r["estado"] == "evaluada" and r["insertadas"] == 3                    # rutas 2, 16 y 21
    filas = alertas_suite(corrida)
    assert {a["idruta"] for a in filas} == {2, 16, 21}
    inicio = (hoy() - timedelta(days=2)).isoformat()
    assert all(a["clave_dedup"] == f"ALT-009|retraso|progvehiculos|ruta:{a['idruta']}|{inicio}" for a in filas)
    assert q("SELECT count(*) FROM ai_frecuencias.notification_log n JOIN ai_frecuencias.alert a USING (alert_id) "
             "WHERE a.codigo = 'ALT-009'")[0][0] == 0
    # GET /alerts por HTTP
    resp = cliente.get("/api/v1/alerts", params={"codigo": "ALT-009", "idruta": 16, "estado": "abierta"})
    assert resp.status_code == 200, resp.text
    cuerpo = resp.json()
    alerta_16 = next(a for a in filas if a["idruta"] == 16)
    assert alerta_16["alert_id"] in [d["alert_id"] for d in cuerpo["datos"]]
    item = next(d for d in cuerpo["datos"] if d["alert_id"] == alerta_16["alert_id"])
    assert item["severidad"] == "alta" and "no se atribuye" in item["explicacion"] and resp.headers.get("x-correlation-id")
    # cierre por HTTP (PATCH) y verificación repetida: el episodio cerrado no se recrea
    resp = cliente.patch(f"/api/v1/alerts/{alerta_16['alert_id']}/status",
                         json={"estado": "cerrada", "usuario": "operador", "comentario": "carga restablecida"})
    assert resp.status_code == 200, resp.text
    assert [h["accion"] for h in resp.json()["historial"]] == ["creada", "cambio_estado"]
    r2 = ciclo.verificar_retraso(correlation_id=corrida.cid("verificar"))["alt_009_retraso"]
    assert r2["insertadas"] == 0 and len(alertas_suite(corrida)) == 3
    resp = cliente.get("/api/v1/alerts", params={"codigo": "ALT-009", "idruta": 16, "estado": "abierta"})
    assert alerta_16["alert_id"] not in [d["alert_id"] for d in resp.json()["datos"]]


def test_ciclo_con_ingesta_fallida_registra_el_intento_y_verifica_el_retraso(corrida, politica, monkeypatch):
    politica()

    def falla():
        raise RuntimeError("fuente caída (simulada)")
    monkeypatch.setattr(ciclo.ingesta, "correr", falla)
    with pytest.raises(RuntimeError, match="fuente caída"):
        ciclo.correr(correlation_id=corrida.cid("ciclo"))
    (ing,) = ejecuciones(corrida, "ingesta")
    assert ing.estado == "fallida" and "fuente caída" in ing.error
    assert ing.parametros == {"fuente": "progvehiculos", "alcance": {"rutas": [2, 16, 21]}}
    assert ing.resumen == {"consulta_valida": False, "completa": False}
    (al,) = ejecuciones(corrida, "alertas")
    assert al.estado == "exitosa" and al.resumen["alt_009_retraso"]["estado"] == "evaluada"
    assert al.resumen["alt_005_007"]["motivo"] == "sin_marca_confirmada"
    assert al.resumen["alt_008"]["motivo"] == "fuente_no_autorizada_o_no_de_solo_lectura"   # ALT008_REFERENCIA sin habilitar
    assert al.resumen["completitud"]["motivo"] == "sin_marca_confirmada"
    assert al.resumen["alt_006"]["motivo"] == "sin_marca_confirmada"
    assert len(alertas_suite(corrida)) == 3


def test_ciclo_dos_veces_sin_novedades_cumple_y_no_duplica(corrida, politica, monkeypatch):
    politica(vencimiento="23:59", inicio=hoy())
    if datetime.now(rt.ZONA).time() >= datetime.strptime("23:58", "%H:%M").time():
        pytest.skip("demasiado cerca del vencimiento de prueba")
    monkeypatch.setattr(ciclo.ingesta, "correr", lambda: {"lote": "x", "ventanas": 1, "viajes": 0,
                                                          "marca_agua": None, "pendiente": False})
    r1 = ciclo.correr(correlation_id=corrida.cid("ciclo"))
    r2 = ciclo.correr(correlation_id=corrida.cid("ciclo"))
    ings = ejecuciones(corrida, "ingesta")
    assert [i.estado for i in ings] == ["exitosa", "exitosa"]
    assert all(i.resumen["consulta_valida"] and i.resumen["sin_novedades"] for i in ings)
    for r in (r1, r2):
        ciclo_hoy = r["alertas"]["alt_009_retraso"]["rutas"][16]["ciclos"][-1]
        assert ciclo_hoy["estado"] == "cumplido" and ciclo_hoy["sin_novedades"] is True
    assert len(ciclo_hoy["intentos"]) == 2                                       # los dos intentos, un ciclo
    assert alertas_suite(corrida) == []


def test_dos_verificadores_concurrentes_crean_una_alerta_por_episodio(corrida, politica, monkeypatch):
    """Dos conexiones y dos transacciones reales. V1 toma el bloqueo por fuente y se detiene; V2 queda
    esperando el bloqueo (se observa en pg_stat_activity); V1 confirma; V2 continúa y no duplica."""
    politica()
    v1_dentro, soltar_v1 = threading.Event(), threading.Event()
    original = rt.ciclos_de_ruta
    primero = threading.Lock()

    def lento(*a, **k):
        if primero.acquire(blocking=False):
            v1_dentro.set()
            soltar_v1.wait(10)
        return original(*a, **k)
    monkeypatch.setattr(rt, "ciclos_de_ruta", lento)
    resultados, eventos = {}, []

    cids = {"v1": corrida.cid("v1"), "v2": corrida.cid("v2")}

    def correr(nombre):
        resultados[nombre] = ciclo.verificar_retraso(correlation_id=cids[nombre])["alt_009_retraso"]
        eventos.append(nombre)

    h1 = threading.Thread(target=correr, args=("v1",))
    h1.start()
    assert v1_dentro.wait(10)
    h2 = threading.Thread(target=correr, args=("v2",))
    h2.start()
    esperando = False
    for _ in range(50):
        esperando = bool(q("SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                           "AND wait_event = 'advisory' AND datname = current_database()"))
        if esperando:
            break
        reloj.sleep(0.1)
    assert esperando, "V2 no quedó esperando el bloqueo de V1"
    soltar_v1.set()
    h1.join(20)
    h2.join(20)
    assert eventos == ["v1", "v2"]
    assert resultados["v1"]["insertadas"] == 3 and resultados["v2"]["insertadas"] == 0
    filas = alertas_suite(corrida)
    assert len(filas) == 3
    for a in filas:
        assert [h.accion for h in q("SELECT accion FROM ai_frecuencias.alert_history WHERE alert_id = :a",
                                    a=a["alert_id"])] == ["creada"]


def test_verificador_durante_una_ingesta_en_curso(corrida, politica, monkeypatch):
    politica(vencimiento="23:59", inicio=hoy())
    if datetime.now(rt.ZONA).time() >= datetime.strptime("23:58", "%H:%M").time():
        pytest.skip("demasiado cerca del vencimiento de prueba")
    en_ingesta, terminar = threading.Event(), threading.Event()

    def ingesta_lenta():
        en_ingesta.set()
        terminar.wait(10)
        return {"lote": "x", "ventanas": 1, "viajes": 4, "marca_agua": None, "pendiente": False}
    monkeypatch.setattr(ciclo.ingesta, "correr", ingesta_lenta)
    salida = {}
    cid_ciclo = corrida.cid("ciclo")
    h = threading.Thread(target=lambda: salida.update(ciclo.correr(correlation_id=cid_ciclo)))
    h.start()
    assert en_ingesta.wait(10)
    r = ciclo.verificar_retraso(correlation_id=corrida.cid("verificar"))["alt_009_retraso"]   # ingesta en curso
    (c16,) = r["rutas"][16]["ciclos"]
    assert c16["estado"] == "pendiente" and [i["estado"] for i in c16["intentos"]] == ["en_curso"]
    terminar.set()
    h.join(20)
    (c16,) = salida["alertas"]["alt_009_retraso"]["rutas"][16]["ciclos"]
    assert c16["estado"] == "cumplido" and c16["satisfecho_por"] == salida["execution_id_ingesta"]
    assert alertas_suite(corrida) == []


def test_limpieza_conserva_una_ejecucion_ajena_de_la_misma_ventana(instancia_dedicada, politica):
    """Una ejecución ajena creada DURANTE la corrida (mismo tipo y misma ventana de tiempo, que el criterio
    anterior por hora y tipo habría borrado) sobrevive; las de la corrida desaparecen."""
    politica()
    r = Corrida()
    ajenas = []
    try:
        with motor_escritura().connect() as c, c.begin():
            for tipo in ("ingesta", "alertas"):
                ajenas.append(c.execute(text("""
                    INSERT INTO ai_frecuencias.execution_log (execution_id, tipo, estado, parametros, correlation_id)
                    VALUES (gen_random_uuid(), :t, 'exitosa', '{"fuente": "otra_fuente_ajena"}'::jsonb, :cid)
                    RETURNING execution_id::text"""), {"t": tipo, "cid": f"ajena-{uuid.uuid4().hex}"}).scalar_one())
        salida = ciclo.verificar_retraso(correlation_id=r.cid("verificar"))
        assert salida["alt_009_retraso"]["insertadas"] == 3
        assert len(alertas_suite(r)) == 3
        limpiar(r.cids)
        assert alertas_suite(r) == []
        assert q("SELECT count(*) FROM ai_frecuencias.execution_log WHERE correlation_id = ANY(:c)", c=r.cids)[0][0] == 0
        assert sorted(x[0] for x in q("SELECT execution_id::text FROM ai_frecuencias.execution_log "
                                      "WHERE execution_id::text = ANY(:a)", a=ajenas)) == sorted(ajenas)
    finally:
        limpiar(r.cids)
        with motor_escritura().connect() as c, c.begin():
            c.execute(text("DELETE FROM ai_frecuencias.execution_log WHERE execution_id::text = ANY(:a)"), {"a": ajenas})


def test_la_limpieza_corre_aunque_falle_una_asercion(instancia_dedicada, tmp_path):
    """Corre en otro proceso una prueba que escribe con la fixture `corrida` y luego FALLA: al terminar,
    las filas de esa corrida ya no están."""
    marca = tmp_path / "corrida.txt"
    prueba = tmp_path / "test_falla_a_proposito.py"
    prueba.write_text(
        "from tests.integracion.test_alertas_ciclo_postgres import ciclo, corrida, instancia_dedicada, politica  # noqa\n"
        "def test_falla(corrida, politica):\n"
        "    politica()\n"
        "    ciclo.verificar_retraso(correlation_id=corrida.cid('falla'))\n"
        f"    open({str(marca)!r}, 'w').write(corrida.id)\n"
        "    assert False, 'falla a propósito'\n", encoding="utf-8")
    raiz = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(prueba)],
                       cwd=raiz, env={**os.environ, "PYTHONPATH": raiz}, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 1 and "falla a propósito" in r.stdout, r.stdout[-2000:]
    corrida_id = marca.read_text()
    assert corrida_id.startswith("suite-alertas-")
    assert q("SELECT count(*) FROM ai_frecuencias.execution_log WHERE starts_with(correlation_id, :r)",
             r=corrida_id)[0][0] == 0
    assert q("SELECT count(*) FROM ai_frecuencias.alert WHERE starts_with(clave_dedup, 'ALT-009|retraso|progvehiculos|')"
             )[0][0] == 0
