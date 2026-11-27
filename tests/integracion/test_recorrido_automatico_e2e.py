"""Recorrido AUTOMÁTICO completo en un entorno aislado: el programador (proceso real) dispara el ciclo
diario y el verificador de retraso a horas de PRUEBA explícitas; luego se comprueba por HTTP lo guardado.

Requisitos (si falta alguno, la prueba se omite):
- Base EXCLUSIVA y RECIÉN CREADA con scripts/preparar_bd_suite_alertas.sh, nombre
  `sobusa_desechable_suite_e2e*`, comentario `sobusa_suite_alertas`, socket local y sin ejecuciones previas.
- SOBUSA_E2E_ADMIN_URL: superusuario de ESA instancia. Solo se usa para crear y ampliar las tablas
  SINTÉTICAS `public.progvehiculos` y `public.timbradas_anio` (simulan a Extreme; comentario
  `sobusa_sintetico`). La aplicación las lee con el usuario de solo lectura.
- En esa base aislada se aplica la PROPUESTA 0010 (completitud_ruta_dia) para probar el recorrido. No se
  aplica en ninguna otra base.

La prueba deja los datos en la base para inspección (la base es desechable; para repetir, cree otra).
Con SOBUSA_E2E_REPORTE=<archivo> escribe un resumen JSON de lo comprobado.
"""
# ruff: noqa: DTZ001, DTZ005  -- hora local de Bogotá a propósito
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)
if os.environ.get("SOBUSA_BD_DESECHABLE") != "1" or not os.environ.get("SOBUSA_E2E_ADMIN_URL"):
    pytest.skip("requiere SOBUSA_BD_DESECHABLE=1 y SOBUSA_E2E_ADMIN_URL", allow_module_level=True)
fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.alertas.retraso_ingesta import ZONA  # noqa: E402
from app.db.conexion import motor_escritura  # noqa: E402

RAIZ = Path(__file__).resolve().parents[2]
D = datetime(2025, 12, 10)                       # día que se predice y luego se compara con lo real
RUTA = 16


def q(sql, **p):
    with motor_escritura().connect() as c, c.begin():
        return c.execute(text(sql), p).all()


@pytest.fixture(scope="module")
def admin():
    motor = create_engine(os.environ["SOBUSA_E2E_ADMIN_URL"])
    with motor.connect() as c:
        base, ip, comentario = c.execute(text("""
            SELECT current_database(), inet_server_addr()::text,
                   coalesce(shobj_description((SELECT oid FROM pg_database WHERE datname = current_database()),
                                              'pg_database'), '')""")).one()
    (base_app,), = q("SELECT current_database()")
    if (base != base_app or not base.startswith("sobusa_desechable_suite_e2e") or ip is not None
            or "sobusa_suite_alertas" not in comentario):
        pytest.skip("no es una base e2e exclusiva (sobusa_desechable_suite_e2e*, marcada y local)")
    if q("SELECT count(*) FROM ai_frecuencias.execution_log")[0][0]:
        pytest.skip("la base e2e ya se usó: cree una nueva con scripts/preparar_bd_suite_alertas.sh")
    yield motor
    motor.dispose()


def _viajes(dias, pax_por_franja=None, tardias=(), sin_inicio=(), sin_conteo=()):
    filas, k = [], 0
    for d in dias:
        for m in range(24, 36):                                  # 06:00 a 08:45, cada 15 min
            s = d + timedelta(minutes=15 * m)
            pax = pax_por_franja(s) if pax_por_franja else 20
            inicio = None if s in sin_inicio else s + timedelta(minutes=25 if s in tardias else 3)
            filas.append({"id": int(s.strftime("%y%m%d%H%M")), "s": s, "i": inicio, "f": s + timedelta(hours=1),
                          "p": None if s in sin_conteo else pax})
            k += 1
    return filas


def _cargar_fuente(admin, filas):
    with admin.begin() as c:
        c.execute(text("""INSERT INTO public.progvehiculos (idprogramacion, idruta, idvehiculo, fechasalida, fechainicio,
                          fechafin, finalizado, cancelada, numeropasajeros, numerobajadas, fecha_fin_liquidacion,
                          apto_estadisticas_historicas)
                          VALUES (:id, 16, 'SINT', :s, :i, :f, 'S', 'N', :p, 0, NULL, true)"""), filas)


def _crear_fuentes(admin):
    with admin.begin() as c:
        c.execute(text("""
            CREATE TABLE public.progvehiculos (
                idprogramacion bigint PRIMARY KEY, idruta bigint NOT NULL, idvehiculo varchar NOT NULL,
                fechasalida timestamp NOT NULL, fechainicio timestamp, fechafin timestamp,
                finalizado varchar(1) NOT NULL, cancelada varchar(1) NOT NULL, numeropasajeros bigint,
                numerobajadas bigint, fecha_fin_liquidacion timestamp, apto_estadisticas_historicas boolean NOT NULL);
            COMMENT ON TABLE public.progvehiculos IS 'sobusa_sintetico: simula progvehiculos de Extreme';
            CREATE TABLE public.timbradas_anio (
                id_timbrada_anio bigint PRIMARY KEY, proceso varchar(50) NOT NULL, ano integer NOT NULL,
                id_ruta bigint NOT NULL, id_dia_semana integer NOT NULL, hora_inicio time NOT NULL,
                hora_fin time NOT NULL, subidas integer NOT NULL, bajadas integer NOT NULL, nro_viajes integer NOT NULL);
            COMMENT ON TABLE public.timbradas_anio IS 'sobusa_sintetico: simula timbradas_anio de Extreme';
            GRANT SELECT ON public.progvehiculos, public.timbradas_anio TO sobusa_lector, sobusa_escritor;"""))
        # consolidado 2024 de Extreme con nro_viajes = 1 en todas las franjas (síntoma del hallazgo §9.1)
        c.execute(text("""INSERT INTO public.timbradas_anio VALUES
            (1, 'sobusa', 2024, 16, 3, '06:00', '06:15', 30, 0, 1), (2, 'sobusa', 2024, 16, 3, '06:15', '06:30', 30, 0, 1),
            (3, 'sobusa', 2024, 16, 3, '06:30', '06:45', 30, 0, 1), (4, 'sobusa', 2024, 16, 3, '06:45', '07:00', 30, 0, 1)"""))
    # 2024: 10 viajes de 100 subidas (1.000) frente a 120 del consolidado → −88 %
    filas_2024 = [{"id": 2400000 + k, "s": datetime(2024, 6, 5, 6) + timedelta(minutes=15 * k),
                   "i": datetime(2024, 6, 5, 6, 3) + timedelta(minutes=15 * k),
                   "f": datetime(2024, 6, 5, 7) + timedelta(minutes=15 * k), "p": 100} for k in range(10)]
    _cargar_fuente(admin, filas_2024)
    # Fase 1: 9 semanas de historia hasta D+1. En D−3: una salida tardía (+25 min), una sin inicio y
    # 3 viajes sin conteo (cobertura 75 %). En D: de 06:00 a 06:45 solo 2 pasajeros por viaje.
    d3 = D - timedelta(days=3)
    dias = [D - timedelta(days=k) for k in range(63, -2, -1)]
    _cargar_fuente(admin, _viajes(
        dias, pax_por_franja=lambda s: 2 if (s.date() == D.date() and s.hour == 6) else 20,
        tardias={d3 + timedelta(hours=6)}, sin_inicio={d3 + timedelta(hours=7)},
        sin_conteo={d3 + timedelta(hours=8), d3 + timedelta(hours=8, minutes=15), d3 + timedelta(hours=8, minutes=30)}))


def _aplicar_propuesta_0010():
    sql = (RAIZ / "propuestas/migraciones/0010_completitud_ruta_dia.sql").read_text("utf-8")
    with motor_escritura().connect() as c, c.begin():
        c.exec_driver_sql(sql)


def _programador(prefijo, extra_env=None, inicio_monitoreo=None, ciclo_en_s=3, disparos=2):
    ahora = datetime.now(ZONA).replace(tzinfo=None)
    env = {**os.environ, "PYTHONPATH": str(RAIZ), "PROGRAMADOR_PREFIJO": prefijo,
           "PROGRAMADOR_CICLO_HORAS": (ahora + timedelta(seconds=ciclo_en_s)).strftime("%H:%M:%S"),
           "PROGRAMADOR_VERIFICADOR_HORAS": (ahora + timedelta(seconds=5)).strftime("%H:%M:%S"),
           # Política de ALT-009 SOLO DE PRUEBA (la real está pendiente de validación)
           "ALT009_RETRASO_APERTURA": "00:00", "ALT009_RETRASO_VENCIMIENTO": "23:59",
           "ALT009_RETRASO_TOLERANCIA_MIN": "0", "ALT009_RETRASO_POLITICA_VERSION": "e2e-prueba",
           "ALT009_RETRASO_INICIO_MONITOREO": (inicio_monitoreo or ahora.date()).isoformat(),
           "ALT008_REFERENCIA": "timbradas_anio", **(extra_env or {})}
    r = subprocess.run([sys.executable, "-m", "app.programador", "--max-disparos", str(disparos)], cwd=RAIZ, env=env,
                       capture_output=True, text=True, timeout=300, check=False)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    final = json.loads(r.stdout.strip().splitlines()[-1])
    return final["registro"]


def _ejecuciones(prefijo, tipo):
    return q("""SELECT execution_id::text AS id, estado, resumen, correlation_id, iniciada_en
                FROM ai_frecuencias.execution_log WHERE starts_with(correlation_id, :p) AND tipo = :t
                ORDER BY iniciada_en""", p=prefijo, t=tipo)


def _por_codigo():
    return {r.codigo: r.n for r in q("SELECT codigo, count(*) AS n FROM ai_frecuencias.alert GROUP BY codigo")}


def test_recorrido_automatico_completo(admin, monkeypatch):
    reporte = {}
    _crear_fuentes(admin)
    _aplicar_propuesta_0010()
    corrida = f"e2e-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("SOBUSA_ADMIN_TOKEN", "token-e2e")
    from app.api import main
    http = TestClient(main.app, headers={"Authorization": "Bearer token-e2e"})

    # 1) Disparo automático: ciclo (ingesta + alertas) y verificador
    reg1 = _programador(f"{corrida}-1")
    assert sorted(e["trabajo"] for e in reg1 if e["resultado"] == "ok") == ["ciclo", "verificador"], reg1
    for e in reg1:
        if e["resultado"] == "ok":
            assert e["inicio"] >= e["programado"]                    # disparó a su hora, no antes
    (ing1,) = _ejecuciones(f"{corrida}-1-ciclo", "ingesta")
    assert ing1.estado == "exitosa" and ing1.resumen["consulta_valida"] and ing1.resumen["completa"]
    (al1,) = _ejecuciones(f"{corrida}-1-ciclo", "alertas")
    assert al1.resumen["completitud"]["estado"] == "evaluada" and al1.resumen["completitud"]["escritas"] > 0
    assert al1.resumen["alt_006"]["insertadas"] == 1 and al1.resumen["alt_008"]["insertadas"] == 1, al1.resumen
    assert al1.resumen["alt_008"]["referencia"] == "public.timbradas_anio"
    assert al1.resumen["alt_008"]["evaluado"] == "public.progvehiculos"
    (ver1,) = _ejecuciones(f"{corrida}-1-verificador", "alertas")
    assert ver1.resumen["alt_009_retraso"]["estado"] == "evaluada" and ver1.resumen["alt_009_retraso"]["insertadas"] == 0
    reporte["disparo_1"] = {"registro": reg1, "alertas_ciclo": {k: v.get("insertadas") if isinstance(v, dict) else v
                                                                for k, v in al1.resumen.items()}}

    # 2) Predicción del día D por la API (entrada real): ALT-001 a 004 e incompletitud de ALT-009
    resp = http.post("/api/v1/frequency/executions", json={"idrutas": [RUTA], "dia": D.date().isoformat(),
                                                             "horizontes": [60], "base_demanda": "puntual"})
    assert resp.status_code == 201, resp.text
    reporte["frecuencias"] = resp.json()
    inc = q("SELECT * FROM ai_frecuencias.alert WHERE codigo = 'ALT-009' AND explicacion LIKE '%alt009-v1%'")
    assert len(inc) == 1 and inc[0].idruta == RUTA                   # D−3: 9 de 12 con conteo (75 %)

    # 3) Llegan datos nuevos a la fuente (D+2 a D+4) y el programador vuelve a disparar
    _cargar_fuente(admin, _viajes([D + timedelta(days=k) for k in (2, 3, 4)]))
    reg2 = _programador(f"{corrida}-2")
    (al2,) = _ejecuciones(f"{corrida}-2-ciclo", "alertas")
    a57 = al2.resumen["alt_005_007"]
    assert a57["estado"] == "evaluada" and a57["insertadas"] >= 1, a57
    comp = q("SELECT estado, evidencia FROM ai_frecuencias.completitud_ruta_dia WHERE idruta = 16 AND fecha = :f",
             f=D.date())
    assert comp[0].estado == "completo" and comp[0].evidencia["programaciones_referencia"] == 12
    d3 = q("SELECT estado, evidencia FROM ai_frecuencias.completitud_ruta_dia WHERE idruta = 16 AND fecha = :f",
           f=(D - timedelta(days=3)).date())
    assert d3[0].estado == "parcial" and "cobertura_de_conteos_bajo_umbral" in d3[0].evidencia["motivos"]
    reporte["disparo_2"] = {"registro": reg2, "alt_005_007": {k: a57.get(k) for k in ("estado", "insertadas",
                                                                                       "conteo_estados_m1")}}

    # 4a) La ingesta NO arranca (su hora llega dentro de 2 horas): el verificador corre solo, a su hora
    hoy = datetime.now(ZONA).date()
    reg3 = _programador(f"{corrida}-3", inicio_monitoreo=hoy - timedelta(days=2), ciclo_en_s=7200, disparos=1)
    assert [(e["trabajo"], e["resultado"]) for e in reg3] == [("verificador", "disparado"), ("verificador", "ok")]
    assert _ejecuciones(f"{corrida}-3-ciclo", "ingesta") == []
    (ver3,) = _ejecuciones(f"{corrida}-3-verificador", "alertas")
    r009 = ver3.resumen["alt_009_retraso"]
    assert r009["insertadas"] == 3, r009                            # rutas 2, 16 y 21: dos ciclos vencidos
    ciclos16 = r009["rutas"]["16"]["ciclos"]
    assert [(x["estado"], x["causa"]) for x in ciclos16[:2]] == [("vencido", "no_iniciada"), ("vencido", "no_iniciada")]
    reporte["disparo_3_verificador_sin_ingesta"] = {"registro": reg3}

    # 4b) La ingesta arranca pero la fuente es inalcanzable: queda «fallida» y el retraso se verifica igual
    roto = os.environ["DATABASE_URL_READ"].replace("/sobusa_desechable", "/no_existe_sobusa_desechable")
    reg3b = _programador(f"{corrida}-3b", {"DATABASE_URL_READ": roto}, inicio_monitoreo=hoy - timedelta(days=2))
    (ing3,) = _ejecuciones(f"{corrida}-3b-ciclo", "ingesta")
    assert ing3.estado == "fallida"
    assert any(e["trabajo"] == "ciclo" and e["resultado"] == "error" for e in reg3b)
    (ver3b,) = _ejecuciones(f"{corrida}-3b-verificador", "alertas")
    assert ver3b.resumen["alt_009_retraso"]["estado"] == "evaluada" and ver3b.resumen["alt_009_retraso"]["insertadas"] == 0
    reporte["disparo_3b_fuente_inalcanzable"] = {"registro": reg3b}

    # 5) Repetir sin datos nuevos no duplica alertas
    antes = _por_codigo()
    _programador(f"{corrida}-4", inicio_monitoreo=hoy - timedelta(days=2))
    assert _por_codigo() == antes, (antes, _por_codigo())
    reporte["alertas_por_codigo"] = antes

    # 6) Consulta y gestión por HTTP, historial
    vistos = {}
    for codigo in ("ALT-005", "ALT-006", "ALT-008", "ALT-009"):
        r = http.get("/api/v1/alerts", params={"codigo": codigo, "tamano": 50})
        assert r.status_code == 200 and r.json()["datos"], (codigo, r.text)
        vistos[codigo] = len(r.json()["datos"])
    a006 = http.get("/api/v1/alerts", params={"codigo": "ALT-006"}).json()["datos"][0]
    assert "+25 min" in a006["explicacion"]
    r = http.patch(f"/api/v1/alerts/{a006['alert_id']}/status",
                   json={"estado": "reconocida", "usuario": "operador", "comentario": "despacho revisado"})
    assert r.status_code == 200 and [h["accion"] for h in r.json()["historial"]] == ["creada", "cambio_estado"]
    notif_009 = q("SELECT count(*) FROM ai_frecuencias.notification_log n JOIN ai_frecuencias.alert a USING (alert_id) "
                  "WHERE a.codigo = 'ALT-009'")[0][0]
    assert notif_009 == 0
    reporte["http_get_alerts"] = vistos
    if os.environ.get("SOBUSA_E2E_REPORTE"):
        Path(os.environ["SOBUSA_E2E_REPORTE"]).write_text(json.dumps(reporte, ensure_ascii=False, indent=1, default=str),
                                                          encoding="utf-8")

