"""Alertas conectadas, contra PostgreSQL DESECHABLE, con ROLLBACK EXTERIOR al final de cada prueba.

Nada de esta suite se confirma: cada prueba trabaja dentro de una transacción que se revierte, con
identificadores sintéticos únicos (fuente `prueba_<hex>`, rutas 1000+). No ejecuta TRUNCATE ni DROP y no
crea objetos que sobrevivan: si `ai_frecuencias.completitud_ruta_dia` no existe, se crea DENTRO de la
transacción y se revierte con ella; si existe, solo se insertan filas que también se revierten.

Recorridos (cada uno desde la función que usa su punto de entrada, sobre la misma conexión):
- flujo.verificar_retraso      → ALT-009 por retraso (lo llaman ciclo_alertas.correr y ciclo_alertas.verificar_retraso).
- servicios.evaluar_alertas    → incompletitud de ALT-009 (para la coexistencia).
- flujo.evaluar_alt_005_007 / evaluar_alt_010 / evaluar_alt_008 → lo que corre después de la ingesta.
La consulta se verifica con repositorio.consultar, la misma función de GET /alerts. GET /alerts por
HTTP, ciclo_alertas.correr y la concurrencia real están en test_alertas_ciclo_postgres.py (instancia dedicada).

  SOBUSA_BD_DESECHABLE=1 DATABASE_URL_WRITE=... pytest -q tests/integracion/test_alertas_conectadas_postgres.py
"""
# ruff: noqa: DTZ001  -- las fechas sin zona son hora local de Bogotá a propósito
import importlib.util
import json
import os
import re
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pandas as pd
import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)
if os.environ.get("SOBUSA_BD_DESECHABLE") != "1":
    pytest.skip("requiere SOBUSA_BD_DESECHABLE=1", allow_module_level=True)

from sqlalchemy import text  # noqa: E402

from app import servicios  # noqa: E402
from app.alertas import flujo  # noqa: E402
from app.alertas import retraso_ingesta as rt  # noqa: E402
from app.calidad.cuarentena import REGLAS_VERSION  # noqa: E402
from app.core.config import Config  # noqa: E402
from app.db import repositorio as repo  # noqa: E402
from app.db.conexion import motor_escritura  # noqa: E402
from app.ingesta import SQL_EVENTO_CALIDAD  # noqa: E402


@pytest.fixture
def c():
    """Conexión con transacción exterior que SIEMPRE se revierte. Verifica destino, permisos y revisión
    antes de escribir nada."""
    with motor_escritura().connect() as conexion, conexion.begin() as tx:
        base, ip, crea_public, escribe_public, crea_ai = conexion.execute(text("""
            SELECT current_database(), inet_server_addr()::text,
                   has_schema_privilege(current_user, 'public', 'CREATE'),
                   (SELECT count(*) FROM information_schema.role_table_grants
                    WHERE grantee = current_user AND table_schema = 'public'
                      AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')),
                   has_schema_privilege(current_user, 'ai_frecuencias', 'CREATE')""")).one()
        if not base.startswith("sobusa_desechable"):
            pytest.skip(f"la base '{base}' no es desechable")
        revisiones = conexion.execute(text("SELECT version_num FROM ai_frecuencias.alembic_version")).scalars().all()
        if len(revisiones) != 1 or re.fullmatch(r"[0-9]{4}", str(revisiones[0])) is None or int(revisiones[0]) < 8:
            pytest.skip(f"se requiere una sola revisión Alembic >= 0008 (hay {revisiones})")
        assert not crea_public and escribe_public == 0, "el usuario puede escribir en public: destino no seguro"
        assert crea_ai, "el usuario no es dueño de ai_frecuencias"
        conexion.execute(text("SET LOCAL search_path TO ai_frecuencias"))
        conexion.info["servidor_local"] = ip is None
        try:
            yield conexion
        finally:
            tx.rollback()


@pytest.fixture
def ids():
    h = uuid.uuid4()
    return {"fuente": f"prueba_{h.hex[:12]}", "ruta": 1000 + h.int % 1_000_000, "tag": h.hex}


def alertas(c, codigo=None, ruta=None, prefijo=None, eid=None):
    filas = c.execute(text("""
        SELECT * FROM ai_frecuencias.alert
        WHERE (CAST(:c AS text) IS NULL OR codigo = :c) AND (CAST(:r AS int) IS NULL OR idruta = :r)
          AND (CAST(:p AS text) IS NULL OR starts_with(clave_dedup, :p))
          AND (CAST(:e AS uuid) IS NULL OR execution_id = CAST(:e AS uuid)) ORDER BY alert_id"""),
                      {"c": codigo, "r": ruta, "p": prefijo, "e": eid}).mappings()
    return [dict(f) for f in filas]


def historial(c, aid):
    return [(f.accion, f.estado_antes, f.estado_nuevo, f.comentario) for f in c.execute(text(
        "SELECT * FROM ai_frecuencias.alert_history WHERE alert_id = :a ORDER BY id"), {"a": aid})]


def consulta_api(c, **filtros):
    """La misma consulta que GET /alerts (app/api/main.py: listar_alertas)."""
    base = {"idruta": None, "estado": None, "severidad": None, "codigo": None}
    return repo.consultar(c, "alert", {**base, **filtros}, None, None, 1, 50)


# ------------------------------------------------------------------ ALT-009 por retraso

INICIO = date(2026, 11, 1)


def politica(ids, inicio=INICIO, tolerancia=0):
    # SOLO PRUEBAS: apertura 00:00 y vencimiento 05:00 son valores controlados, no operativos.
    return rt.Politica(ids["fuente"], time(0, 0), time(5, 0), tolerancia, "prueba-controlada", inicio)


def intento(c, ids, inicio, fin, estado="exitosa", completa=True, valida=True, viajes=10, objetivo=None,
            declarado=None, evidencia=None):
    par = {"fuente": ids["fuente"], "alcance": {"rutas": [ids["ruta"]]}}
    if objetivo:
        par.update(ciclo_objetivo=objetivo.isoformat(), autorizacion_declarada_por=declarado)
    if evidencia:
        par["evidencia_autorizacion"] = evidencia
    eid = str(uuid.uuid4())
    c.execute(text("""INSERT INTO ai_frecuencias.execution_log
                      (execution_id, tipo, estado, parametros, resumen, correlation_id, iniciada_en, finalizada_en)
                      VALUES (CAST(:e AS uuid), 'ingesta', :s, CAST(:p AS jsonb), CAST(:r AS jsonb), :t, :i, :f)"""),
              {"e": eid, "s": estado, "p": json.dumps(par), "t": ids["tag"],
               "r": json.dumps({"consulta_valida": valida, "completa": completa, "viajes": viajes}),
               "i": _bog(inicio), "f": None if fin is None else _bog(fin)})
    return eid


def _bog(d):
    return d.replace(tzinfo=rt.ZONA)


# Verificador SINTÉTICO de autorización (solo pruebas). En operación no hay uno aprobado: se pasa None.
EVIDENCIA_SINTETICA = "aut-sintetica-001"


def verificador_sintetico(i):
    return i.evidencia_autorizacion == EVIDENCIA_SINTETICA


def verificar(c, ids, ahora, pol=None, verificar_autorizacion=None):
    eid = repo.crear_ejecucion(c, "alertas", {"origen": "prueba"}, ids["tag"])
    r = flujo.en_savepoint(c, "ALT-009 retraso", lambda: flujo.verificar_retraso(
        c, eid, pol or politica(ids), ahora, (ids["ruta"],), verificar_autorizacion=verificar_autorizacion))
    repo.cerrar_ejecucion(c, eid, "exitosa", {"alt_009_retraso": r})
    return r, eid


def estados(r, ids):
    return {x["fecha"]: (x["estado"], x["causa"]) for x in r["rutas"][ids["ruta"]]["ciclos"]}


def test_retraso_dos_ciclos_vencidos_persiste_con_evidencia_historial_y_consulta(c, ids):
    intento(c, ids, datetime(2026, 11, 1, 1), datetime(2026, 11, 1, 2))                      # cumplido
    fallida = intento(c, ids, datetime(2026, 11, 2, 1), datetime(2026, 11, 2, 1, 30), "fallida")
    #                                                                      día 3: no arrancó nada
    intento(c, ids, datetime(2026, 11, 4, 1), datetime(2026, 11, 4, 2), viajes=0)            # sin novedades
    r, eid = verificar(c, ids, datetime(2026, 11, 4, 12))
    assert estados(r, ids) == {"2026-11-01": ("cumplido", None), "2026-11-02": ("vencido", "fallida"),
                               "2026-11-03": ("vencido", "no_iniciada"), "2026-11-04": ("cumplido", None)}
    assert r["rutas"][ids["ruta"]]["ciclos"][3]["sin_novedades"] is True
    assert r["insertadas"] == 1
    (a,) = alertas(c, "ALT-009", ids["ruta"])
    assert a["clave_dedup"] == rt.clave(ids["fuente"], ids["ruta"], date(2026, 11, 2))
    assert (a["severidad"], a["estado"], a["umbral_version"]) == ("alta", "abierta", None)
    assert a["valor_observado"] == Decimal(2) and a["umbral"] == Decimal(2)
    assert str(a["execution_id"]) == eid
    assert a["franja_inicio"] == _bog(datetime(2026, 11, 2, 5))         # vencimiento del primer ciclo
    for texto in (f"{ids['fuente']}|ruta:{ids['ruta']}|2026-11-02", "fallida", fallida[:8], "no_iniciada",
                  "prueba-controlada", "no se atribuye", "no certifica", "pendiente de aprobación"):
        assert texto in a["explicacion"]
    # el día 4 se cumplió: la racha terminó y se registró la recuperación sin cambiar el estado
    assert [h[:3] for h in historial(c, a["alert_id"])] == [("creada", None, "abierta"), ("comentario", None, None)]
    assert "recuperado en 2026-11-04 02:00" in historial(c, a["alert_id"])[1][3]
    assert c.execute(text("SELECT count(*) FROM ai_frecuencias.notification_log WHERE alert_id = :a"),
                     {"a": a["alert_id"]}).scalar() == 0
    datos, total = consulta_api(c, codigo="ALT-009", idruta=ids["ruta"])
    assert total == 1 and datos[0]["alert_id"] == a["alert_id"]
    # la ejecución 'alertas' guarda la trazabilidad de los ciclos y de los intentos
    resumen = repo.obtener_ejecucion(c, eid)["resumen"]["alt_009_retraso"]
    ciclo_2 = resumen["rutas"][str(ids["ruta"])]["ciclos"][1]
    assert ciclo_2["ciclo_id"].endswith("2026-11-02") and ciclo_2["apertura"] == "2026-11-02 00:00:00"
    assert [i["resultado"] for i in ciclo_2["intentos"]] == ["fallida"]
    assert resumen["politica"]["version"] == "prueba-controlada"


def test_retraso_un_solo_vencido_o_parcial_aislado_no_alerta(c, ids):
    intento(c, ids, datetime(2026, 11, 1, 1), datetime(2026, 11, 1, 2))
    intento(c, ids, datetime(2026, 11, 2, 1), datetime(2026, 11, 2, 2), completa=False)       # parcial
    intento(c, ids, datetime(2026, 11, 3, 1), datetime(2026, 11, 3, 2))
    intento(c, ids, datetime(2026, 11, 4, 1), datetime(2026, 11, 4, 2), valida=False, viajes=0)  # sin consulta válida
    intento(c, ids, datetime(2026, 11, 5, 1), datetime(2026, 11, 5, 2), viajes=0)            # sin novedades válido
    r, _ = verificar(c, ids, datetime(2026, 11, 5, 12))
    e = estados(r, ids)
    assert e["2026-11-02"] == ("vencido", "parcial") and e["2026-11-04"] == ("vencido", "sin_consulta_valida")
    assert e["2026-11-05"] == ("cumplido", None)                       # sin viajes nuevos, consulta válida
    assert r["insertadas"] == 0 and alertas(c, "ALT-009", ids["ruta"]) == []
    assert r["rutas"][ids["ruta"]]["episodios"] == [] and r["rutas"][ids["ruta"]]["rachas_sin_incumplimiento"] == 2


def test_retraso_parcial_y_sin_consulta_valida_consecutivos_si_alertan(c, ids):
    intento(c, ids, datetime(2026, 11, 1, 1), datetime(2026, 11, 1, 2), completa=False)
    intento(c, ids, datetime(2026, 11, 2, 1), datetime(2026, 11, 2, 2), valida=False, viajes=0)
    r, _ = verificar(c, ids, datetime(2026, 11, 2, 12))
    assert r["insertadas"] == 1
    (a,) = alertas(c, "ALT-009", ids["ruta"])
    assert "parcial" in a["explicacion"] and "sin_consulta_valida" in a["explicacion"]


def test_retraso_verificador_sin_ninguna_ingesta(c, ids):
    r, _ = verificar(c, ids, datetime(2026, 11, 2, 6))
    assert estados(r, ids) == {"2026-11-01": ("vencido", "no_iniciada"), "2026-11-02": ("vencido", "no_iniciada")}
    assert r["insertadas"] == 1


def test_ingesta_tardia_no_satisface_el_ciclo_siguiente(c, ids):
    """La ingesta del ciclo 2 empieza 04:50 y termina 05:30 (después del vencimiento): el ciclo 2 queda
    «tardia» y recuperado; el 3 sigue vencido («no_iniciada») aunque esa ingesta terminó antes de su plazo.
    Como la ingesta tardía recuperó el 2, el 3 empieza una racha nueva: solo, no alerta; con el 4, sí."""
    intento(c, ids, datetime(2026, 11, 1, 1), datetime(2026, 11, 1, 2))
    tarde = intento(c, ids, datetime(2026, 11, 2, 4, 50), datetime(2026, 11, 2, 5, 30))
    r, _ = verificar(c, ids, datetime(2026, 11, 3, 12))
    e = estados(r, ids)
    assert e["2026-11-02"] == ("vencido", "tardia") and e["2026-11-03"] == ("vencido", "no_iniciada")
    ciclos = r["rutas"][ids["ruta"]]["ciclos"]
    assert ciclos[1]["recuperado_por"] == tarde and [i["execution_id"] for i in ciclos[2]["intentos"]] == []
    assert r["insertadas"] == 0 and alertas(c, "ALT-009", ids["ruta"]) == []
    assert r["rutas"][ids["ruta"]]["rachas_sin_incumplimiento"] == 2
    r2, _ = verificar(c, ids, datetime(2026, 11, 4, 12))
    assert r2["insertadas"] == 1
    (a,) = alertas(c, "ALT-009", ids["ruta"])
    assert a["clave_dedup"] == rt.clave(ids["fuente"], ids["ruta"], date(2026, 11, 3))
    assert tarde[:8] not in a["explicacion"]                         # el episodio nuevo no incluye el ciclo 2


def test_reintento_declarado_queda_para_auditoria_y_solo_cuenta_con_autorizacion_verificada(c, ids):
    sin_aut = intento(c, ids, datetime(2026, 11, 3, 8), datetime(2026, 11, 3, 8, 30), objetivo=date(2026, 11, 2))
    declarado = intento(c, ids, datetime(2026, 11, 3, 9), datetime(2026, 11, 3, 9, 30), objetivo=date(2026, 11, 2),
                        declarado="coordinación", evidencia=EVIDENCIA_SINTETICA)
    # Operación actual: sin verificador aprobado, el nombre declarado no basta (ni con evidencia adjunta).
    r, _ = verificar(c, ids, datetime(2026, 11, 3, 12))
    (a,) = alertas(c, "ALT-009", ids["ruta"])
    no_usados = {n["execution_id"]: n for n in r["rutas"][ids["ruta"]]["intentos_no_usados"]}
    assert no_usados[sin_aut]["motivo"] == "reintento_sin_autorizacion"
    assert no_usados[declarado]["motivo"] == "autorizacion_declarada_no_verificada"
    assert no_usados[declarado]["autorizacion_declarada_por"] == "coordinación"
    assert r["verificacion_autorizacion"] == "no_disponible"
    assert len(historial(c, a["alert_id"])) == 1                       # no satisfizo ni recuperó
    # la declaración se conserva en el registro del intento
    par = c.execute(text("SELECT parametros FROM ai_frecuencias.execution_log WHERE execution_id = CAST(:e AS uuid)"),
                    {"e": declarado}).scalar_one()
    assert par["autorizacion_declarada_por"] == "coordinación"
    # Comportamiento esperado SI existiera un mecanismo aprobado (verificador sintético):
    r2, _ = verificar(c, ids, datetime(2026, 11, 3, 12), verificar_autorizacion=verificador_sintetico)
    ciclo_2 = r2["rutas"][ids["ruta"]]["ciclos"][1]
    assert [i["execution_id"] for i in ciclo_2["intentos"]] == [declarado]
    assert [n["execution_id"] for n in r2["rutas"][ids["ruta"]]["intentos_no_usados"]] == [sin_aut]
    assert f"por la ingesta {declarado}" in historial(c, a["alert_id"])[1][3]


def test_episodio_cerrado_no_se_recrea_crece_sin_reabrir_y_racha_nueva_abre_otro(c, ids):
    verificar(c, ids, datetime(2026, 11, 2, 12))
    (a,) = alertas(c, "ALT-009", ids["ruta"])
    servicios.repo.actualizar_alerta(c, a["alert_id"], "abierta", "cerrada", "operador", "visto", None)
    r2, _ = verificar(c, ids, datetime(2026, 11, 3, 12))                # la racha crece a 3 ciclos
    assert r2["insertadas"] == 0
    filas = alertas(c, "ALT-009", ids["ruta"])
    assert len(filas) == 1 and filas[0]["estado"] == "cerrada"
    h = historial(c, a["alert_id"])
    assert [x[0] for x in h] == ["creada", "cambio_estado", "comentario"] and "ciclos_vencidos=3" in h[2][3]
    verificar(c, ids, datetime(2026, 11, 3, 13))                         # misma racha: nada nuevo
    assert len(historial(c, a["alert_id"])) == 3
    # recuperación (día 4) registrada en la alerta cerrada SIN reabrirla
    intento(c, ids, datetime(2026, 11, 4, 1), datetime(2026, 11, 4, 2))
    verificar(c, ids, datetime(2026, 11, 4, 12))
    h = historial(c, a["alert_id"])
    assert "recuperado en 2026-11-04 02:00" in h[-1][3] and alertas(c, "ALT-009", ids["ruta"])[0]["estado"] == "cerrada"
    # racha nueva después de la recuperación (días 5 y 6): otro episodio, otra alerta
    r3, _ = verificar(c, ids, datetime(2026, 11, 6, 12))
    assert r3["insertadas"] == 1
    nuevas = [x for x in alertas(c, "ALT-009", ids["ruta"]) if x["alert_id"] != a["alert_id"]]
    assert len(nuevas) == 1 and nuevas[0]["clave_dedup"] == rt.clave(ids["fuente"], ids["ruta"], date(2026, 11, 5))


def test_la_identidad_se_conserva_si_cambia_el_rango_del_verificador(c, ids):
    verificar(c, ids, datetime(2026, 11, 3, 12))                         # episodio desde el 1
    (a,) = alertas(c, "ALT-009", ids["ruta"])
    r, _ = verificar(c, ids, datetime(2026, 11, 5, 12), politica(ids, inicio=date(2026, 11, 3)))
    assert r["insertadas"] == 0
    (b,) = alertas(c, "ALT-009", ids["ruta"])
    assert b["alert_id"] == a["alert_id"]
    assert r["rutas"][ids["ruta"]]["episodios"][0]["clave"] == a["clave_dedup"]


def test_sin_politica_es_no_evaluable_y_no_escribe(c, ids):
    pol, motivo = rt.politica_desde_entorno({}, ids["fuente"])
    eid = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    r = flujo.verificar_retraso(c, eid, pol, datetime(2026, 11, 5), (ids["ruta"],), motivo)
    assert r["estado"] == "no_evaluable" and r["motivo"] == "politica_operativa_no_configurada"
    assert r["insertadas"] == 0 and alertas(c, "ALT-009", ids["ruta"]) == []


# ------------------------------------------------------------------ coexistencia con la incompletitud

def _incompletitud(c, ids, monkeypatch):
    """Escenario de tests/integracion/test_alt009_postgres.py: 1 de 2 viajes sin conteo el 16-dic."""
    ruta, lote, dia = ids["ruta"], f"alt009_{ids['tag']}", datetime(2025, 12, 16)
    viaje = uuid.UUID(ids["tag"]).int % (2**62)
    c.execute(text("""INSERT INTO ai_frecuencias.watermark (tabla_origen, ultima_actualizacion, lote)
                      VALUES ('progvehiculos', :m, :l) ON CONFLICT (tabla_origen)
                      DO UPDATE SET ultima_actualizacion = EXCLUDED.ultima_actualizacion, lote = EXCLUDED.lote"""),
              {"m": dia + timedelta(days=4, hours=12), "l": lote})
    c.execute(text("""INSERT INTO ai_frecuencias.viaje_fuente (idprogramacion, idruta, fechasalida, fechainicio, fechafin,
                      finalizado, cancelada, numeropasajeros, lote) VALUES (:v, :r, :s, :s, :f, 'S', 'N', :p, :l)"""),
              [{"v": viaje + i, "r": ruta, "s": dia + timedelta(hours=6 + i), "f": dia + timedelta(hours=8 + i),
                "p": p, "l": lote} for i, p in enumerate((None, 40))])
    c.execute(text(SQL_EVENTO_CALIDAD), {"lote": lote, "tabla_origen": "progvehiculos", "idprogramacion": viaje,
                                         "idruta": ruta, "regla": "pasajeros_sin_conteo", "causa": "Conteo nulo",
                                         "valor": None, "reglas_version": REGLAS_VERSION, "valor_original": None})
    monkeypatch.setattr(servicios, "obtener_config", lambda: Config(dias_revision=3, notificaciones_simuladas=True,
                                                                    correo_destino=None, whatsapp_destino=None))
    monkeypatch.setattr(servicios.repo, "demanda_franja", lambda *_: pd.Series(dtype=float))
    franja = pd.Timestamp(dia + timedelta(days=7, hours=8))
    pred = pd.DataFrame({"idruta": [ruta], "franja_inicio": [franja], "horizonte_min": [60], "demanda_pred": [10]})
    rec = pd.DataFrame([{"idruta": ruta, "franja_inicio": franja, "horizonte_min": 60, "buses_hora_actual": 1,
                         "buses_hora_recomendado": 1, "es_viable": True, "carga_max_estimada_hora": 50,
                         "capacidad_nominal": 100, "ocupacion_objetivo": 0.75}])

    def evaluar():
        eid = repo.crear_ejecucion(c, "frecuencias", {}, ids["tag"])
        return servicios.evaluar_alertas(c, eid, pred, rec)
    return evaluar


def test_retraso_e_incompletitud_coexisten_y_no_se_cierran_entre_si(c, ids, monkeypatch):
    evaluar = _incompletitud(c, ids, monkeypatch)
    evaluar()
    (inc,) = [a for a in alertas(c, "ALT-009", ids["ruta"]) if not a["clave_dedup"].startswith(rt.PREFIJO_CLAVE)]
    assert "alt009-v1" in inc["explicacion"]
    verificar(c, ids, datetime(2026, 11, 3, 12))
    (ret,) = alertas(c, "ALT-009", ids["ruta"], prefijo=rt.PREFIJO_CLAVE)
    assert ret["clave_dedup"] != inc["clave_dedup"]
    # cerrar el retraso no toca la incompletitud
    servicios.repo.actualizar_alerta(c, ret["alert_id"], "abierta", "cerrada", "operador", "ingesta restablecida", None)
    # la recuperación del retraso tampoco
    intento(c, ids, datetime(2026, 11, 4, 1), datetime(2026, 11, 4, 2))
    verificar(c, ids, datetime(2026, 11, 4, 12))
    assert [h[0] for h in historial(c, inc["alert_id"])] == ["creada"]
    assert {a["alert_id"]: a["estado"] for a in alertas(c, "ALT-009", ids["ruta"])} == \
        {inc["alert_id"]: "abierta", ret["alert_id"]: "cerrada"}
    # la incompletitud conserva su comportamiento: cerrada, reaparece si la condición persiste
    servicios.repo.actualizar_alerta(c, inc["alert_id"], "abierta", "cerrada", "operador", "revisado", None)
    evaluar()
    reabiertas = [a for a in alertas(c, "ALT-009", ids["ruta"])
                  if a["clave_dedup"] == inc["clave_dedup"] and a["estado"] == "abierta"]
    assert len(reabiertas) == 1 and reabiertas[0]["alert_id"] != inc["alert_id"]
    assert len(alertas(c, "ALT-009", ids["ruta"], prefijo=rt.PREFIJO_CLAVE)) == 1      # el retraso no se recreó


def test_recuperacion_tardia_y_episodio_nuevo_de_punta_a_punta(c, ids, monkeypatch):
    """Vencen 1 y 2 → alerta A. Una ingesta tardía (2-nov 10:00) recupera el episodio. El 3 vence: solo
    no abre nada. El 4 vence: episodio nuevo → alerta B. A no se recrea; la incompletitud no se toca;
    repetir la verificación no duplica alertas ni notas."""
    evaluar = _incompletitud(c, ids, monkeypatch)
    evaluar()
    (inc,) = [a for a in alertas(c, "ALT-009", ids["ruta"]) if not a["clave_dedup"].startswith(rt.PREFIJO_CLAVE)]

    def retrasos():
        return alertas(c, "ALT-009", ids["ruta"], prefijo=rt.PREFIJO_CLAVE)

    def notas(aid):
        return [h[3] for h in historial(c, aid) if h[0] == "comentario"]

    r, _ = verificar(c, ids, datetime(2026, 11, 2, 6))                   # vencen 1 y 2
    assert r["insertadas"] == 1
    (a,) = retrasos()
    assert a["clave_dedup"] == rt.clave(ids["fuente"], ids["ruta"], date(2026, 11, 1))
    tarde = intento(c, ids, datetime(2026, 11, 2, 9), datetime(2026, 11, 2, 10))   # ingesta tardía
    r, _ = verificar(c, ids, datetime(2026, 11, 2, 12))
    assert r["insertadas"] == 0
    (nota,) = notas(a["alert_id"])
    assert "recuperado en 2026-11-02 10:00" in nota and f"por la ingesta {tarde}" in nota
    # el 3 vence sin ingesta: primer vencimiento tras la recuperación, solo
    r, _ = verificar(c, ids, datetime(2026, 11, 3, 6))
    assert r["insertadas"] == 0 and len(retrasos()) == 1 and len(notas(a["alert_id"])) == 1
    eps = r["rutas"][ids["ruta"]]["episodios"]
    assert [e["clave"] for e in eps] == [a["clave_dedup"]] and r["rutas"][ids["ruta"]]["rachas_sin_incumplimiento"] == 1
    # el 4 vence: segundo vencimiento consecutivo de la racha nueva → alerta B
    r, eid_b = verificar(c, ids, datetime(2026, 11, 4, 6))
    assert r["insertadas"] == 1
    a2, b = retrasos()
    assert a2["alert_id"] == a["alert_id"] and b["clave_dedup"] == rt.clave(ids["fuente"], ids["ruta"], date(2026, 11, 3))
    assert str(b["execution_id"]) == eid_b and b["valor_observado"] == 2
    assert "2026-11-03" in b["explicacion"] and "2026-11-04" in b["explicacion"]
    assert [h[0] for h in historial(c, b["alert_id"])] == ["creada"]
    # vencimientos originales conservados en la traza
    e = estados(r, ids)
    assert e["2026-11-01"] == ("vencido", "no_iniciada") and e["2026-11-02"] == ("vencido", "no_iniciada")
    assert r["rutas"][ids["ruta"]]["ciclos"][1]["recuperado_por"] == tarde
    # repetir no duplica alertas ni notas
    antes = {x["alert_id"]: len(historial(c, x["alert_id"])) for x in retrasos()}
    for _ in range(2):
        r, _ = verificar(c, ids, datetime(2026, 11, 4, 7))
        assert r["insertadas"] == 0 and r["notas_historial"] == 0
    assert {x["alert_id"]: len(historial(c, x["alert_id"])) for x in retrasos()} == antes
    # la incompletitud sigue intacta y nada de esto notificó
    assert [h[0] for h in historial(c, inc["alert_id"])] == ["creada"]
    assert {x["alert_id"]: x["estado"] for x in alertas(c, "ALT-009", ids["ruta"])}[inc["alert_id"]] == "abierta"
    assert c.execute(text("SELECT count(*) FROM ai_frecuencias.notification_log WHERE alert_id = ANY(:a)"),
                     {"a": [a["alert_id"], b["alert_id"], inc["alert_id"]]}).scalar() == 0


# ------------------------------------------------------------------ ALT-005 / ALT-007 / ALT-010 (M1)

def _completitud(c):
    existia = c.execute(text("SELECT to_regclass('ai_frecuencias.completitud_ruta_dia') IS NOT NULL")).scalar()
    if not existia:
        # Dentro de la transacción exterior: desaparece con el rollback. Nunca se hace DROP.
        c.execute(text("CREATE TABLE ai_frecuencias.completitud_ruta_dia (idruta int, fecha date, estado text)"))

    def marcar(ruta, dia, estado="completo"):
        c.execute(text("INSERT INTO ai_frecuencias.completitud_ruta_dia (idruta, fecha, estado) VALUES (:r, :f, :e)"),
                  {"r": ruta, "f": dia, "e": estado})
    return marcar


def _prediccion(c, ruta, filas):
    activo = repo.modelo_activo(c)
    eid = str(uuid.uuid4())
    c.execute(text("""INSERT INTO ai_frecuencias.execution_log (execution_id, tipo, estado, resumen)
                      VALUES (CAST(:e AS uuid), 'prediccion', 'exitosa', CAST(:r AS jsonb))"""),
              {"e": eid, "r": json.dumps({"modelo_activo": int(activo["model_id"])})})
    c.execute(text("""INSERT INTO ai_frecuencias.prediction (execution_id, idruta, franja_inicio, horizonte_min,
        demanda_pred, limite_inferior, limite_superior, nivel_intervalo, model_id, model_version, feature_version,
        data_cutoff, latency_ms) VALUES (CAST(:e AS uuid), :r, :t, 60, :p, 0, 500, 0.8, :m, :v, :fv, :dc, 5)"""),
              [{"e": eid, "r": ruta, "t": t, "p": p, "m": int(activo["model_id"]), "v": activo["version"],
                "fv": activo["feature_version"],
                "dc": t.normalize() - timedelta(days=1)} for t, p in filas])
    return activo


def _real(c, ruta, valores):
    c.execute(text("""INSERT INTO ai_frecuencias.demanda_franja (idruta, franja_inicio, pasajeros, programaciones,
                      programaciones_validas, lote) VALUES (:r, :t, :p, 1, 1, 'sintetico')"""),
              [{"r": ruta, "t": t, "p": p} for t, p in valores])


def _marca(c, m):
    c.execute(text("""INSERT INTO ai_frecuencias.watermark (tabla_origen, ultima_actualizacion, lote)
                      VALUES ('progvehiculos', :m, 'sintetico') ON CONFLICT (tabla_origen)
                      DO UPDATE SET ultima_actualizacion = EXCLUDED.ultima_actualizacion"""), {"m": m})


# prediction.idruta solo admite 2, 16 y 21: estas pruebas usan la ruta 16 y fechas que no usa ninguna otra
# suite; las aserciones se filtran por la ejecución de la prueba y todo se revierte al final.
RUTA_M1 = 16
DIA = pd.Timestamp("2025-11-19")


def _escenario_005_007(c, ids, evidencia: bool):
    ruta = RUTA_M1
    marcar = _completitud(c)
    if evidencia:
        marcar(ruta, DIA.date())
    _prediccion(c, ruta, [(DIA + pd.Timedelta(hours=6), 80), (DIA + pd.Timedelta(hours=6, minutes=30), 80),
                          (DIA + pd.Timedelta(hours=7), 80)])
    _real(c, ruta, [(DIA + pd.Timedelta(minutes=15 * k), 5 if k < 28 else 20) for k in range(24, 36)])
    marca = datetime(2025, 11, 24, 11)
    _marca(c, marca)
    eid = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    return eid, flujo.evaluar_alt_005_007(c, eid, None, marca, (ruta,))


def test_005_007_se_guardan_con_trazabilidad_y_no_duplican(c, ids):
    eid, r = _escenario_005_007(c, ids, evidencia=True)
    assert r["estado"] == "evaluada" and r["completitud"]["dias_completos"] >= 1, r
    filas = {(a["codigo"], a["franja_inicio"].replace(tzinfo=None)): a for a in alertas(c, ruta=RUTA_M1, eid=eid)}
    caida = filas[("ALT-005", DIA + pd.Timedelta(hours=6))]              # 20 reales frente a 80
    assert caida["valor_observado"] == Decimal(20) and caida["valor_esperado"] == Decimal(80)
    assert caida["severidad"] == "media" and caida["horizonte_min"] == 60 and caida["umbral_version"] == 1
    assert "predicción" in caida["explicacion"] and str(caida["execution_id"]) == eid
    assert ("ALT-007", DIA + pd.Timedelta(hours=6, minutes=30)) in filas     # 50 frente a 80: −37,5 %
    assert not [k for k in filas if k[1] == DIA + pd.Timedelta(hours=7)]      # 80 frente a 80: nada
    for a in filas.values():
        assert [h[0] for h in historial(c, a["alert_id"])] == ["creada"]
    datos, _total = consulta_api(c, codigo="ALT-005", idruta=RUTA_M1)
    assert caida["alert_id"] in {d["alert_id"] for d in datos}
    eid2 = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    r2 = flujo.evaluar_alt_005_007(c, eid2, None, datetime(2025, 11, 24, 11), (RUTA_M1,))
    assert r2["insertadas"] == 0 and alertas(c, ruta=RUTA_M1, eid=eid2) == []


def test_005_007_sin_evidencia_de_completitud_no_es_evaluable(c, ids):
    eid, r = _escenario_005_007(c, ids, evidencia=False)
    if r["completitud"]["dias_completos"]:
        pytest.skip("la base ya tiene evidencia de completitud para la ruta 16 en esta ventana")
    assert r["estado"] == "no_evaluable" and r["motivo"] == "completitud_desconocida"
    assert f"{RUTA_M1}:2025-11-19" in r["completitud"]["dias_sin_evidencia"]
    assert alertas(c, eid=eid) == []


def test_005_007_sin_tabla_de_completitud_no_es_evaluable(c, ids):
    if c.execute(text("SELECT to_regclass('ai_frecuencias.completitud_ruta_dia') IS NOT NULL")).scalar():
        pytest.skip("la tabla completitud_ruta_dia ya existe en esta base")
    r = flujo.evaluar_alt_005_007(c, "00000000-0000-0000-0000-000000000000", None, datetime(2025, 11, 24, 11),
                                  (RUTA_M1,))
    assert r["estado"] == "no_evaluable" and r["motivo"] == "falta_tabla_completitud_ruta_dia"


def _escenario_010(c, ids, degradado: bool):
    """Marca 2026-08-13 → corte 08-10: reciente [08-03, 08-10) y referencia [07-06, 08-03)."""
    ruta, marcar = RUTA_M1, _completitud(c)
    d, preds, reales = pd.Timestamp("2026-07-06"), [], []
    while d < pd.Timestamp("2026-08-10"):
        reciente = d >= pd.Timestamp("2026-08-03")
        for h in (6, 7, 8, 9, 10):
            preds.append((d + pd.Timedelta(hours=h), 80))
            reales += [(d + pd.Timedelta(hours=h, minutes=15 * k), 10 if (reciente and degradado) else 19)
                       for k in range(4)]
        marcar(ruta, d.date())
        d += pd.Timedelta(days=1)
    _prediccion(c, ruta, preds)
    _real(c, ruta, reales)
    _marca(c, datetime(2026, 8, 13))
    eid = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    return eid, flujo.evaluar_alt_010(c, eid, (RUTA_M1,))


def test_010_degradado_se_guarda_y_notifica_solo_lo_guardado(c, ids, monkeypatch):
    monkeypatch.setattr(servicios, "obtener_config", lambda: Config(
        dias_revision=3, notificaciones_simuladas=True, correo_destino="alt010@example.invalid", whatsapp_destino=None))
    eid, r = _escenario_010(c, ids, degradado=True)
    assert r["estado"] == "evaluada" and r["insertadas"] == 1, r
    (a,) = alertas(c, "ALT-010", RUTA_M1, eid=eid)
    assert a["severidad"] == "alta" and a["horizonte_min"] == 60 and a["umbral_version"] == 1
    assert float(a["valor_observado"]) == pytest.approx(1.0, abs=1e-3)
    assert float(a["valor_esperado"]) == pytest.approx(4 / 76, abs=1e-3)
    assert "alt010-v1" in a["explicacion"]
    assert [h[0] for h in historial(c, a["alert_id"])] == ["creada"]
    assert c.execute(text("SELECT count(*) FROM ai_frecuencias.notification_log WHERE alert_id = :a"),
                     {"a": a["alert_id"]}).scalar() == 1
    eid2 = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    r2 = flujo.evaluar_alt_010(c, eid2, (RUTA_M1,))
    assert r2["candidatas_regla"] == 1 and r2["insertadas"] == 0 and alertas(c, eid=eid2) == []


def test_010_sin_degradacion_no_alerta(c, ids):
    eid, r = _escenario_010(c, ids, degradado=False)
    assert r["estado"] == "evaluada" and r["insertadas"] == 0 and alertas(c, eid=eid) == []


# ------------------------------------------------------------------ ALT-008 (adaptadores sintéticos de lectura)

class FuenteSintetica:
    """Doble de un adaptador de solo lectura de Extreme. La prueba con las tablas reales de Extreme
    (timbradas_anio y progvehiculos, sintéticas) está en test_recorrido_automatico_e2e.py."""
    granularidad, solo_lectura, autorizada = "ruta_anio", True, True

    def __init__(self, origen, filas):
        self.origen, self._df = origen, pd.DataFrame(filas)

    def leer(self, periodos):
        return self._df[self._df.periodo.isin(periodos)].copy()


def test_008_compara_consolidado_independiente_y_alerta_por_periodo(c, ids):
    ruta = 16
    eid = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    ref = FuenteSintetica("sintetico.timbradas_anio", [
        {"idruta": ruta, "periodo": 2023, "subidas_consolidado": 1_050, "franjas": 10, "prop_franjas_un_viaje": 0.1},
        {"idruta": ruta, "periodo": 2024, "subidas_consolidado": 900, "franjas": 10, "prop_franjas_un_viaje": 1.0}])
    ev = FuenteSintetica("sintetico.progvehiculos", [
        {"idruta": ruta, "periodo": 2023, "subidas_viajes": 1_000, "viajes": 40},
        {"idruta": ruta, "periodo": 2024, "subidas_viajes": 7_800, "viajes": 300}])
    r = flujo.evaluar_alt_008(c, eid, ref, ev, [2023, 2024])
    assert r["estado"] == "evaluada" and r["periodos_comparados"] == 2 and r["insertadas"] == 1, r
    (a,) = alertas(c, "ALT-008", ruta, eid=eid)
    assert a["valor_observado"] == 900 and a["valor_esperado"] == 7800 and a["umbral_version"] == 1
    assert a["franja_inicio"].replace(tzinfo=None) == datetime(2024, 1, 1)       # identidad por ruta y año
    assert "-88%" in a["explicacion"] and "DECISIONES §9.1" in a["explicacion"]
    assert "sintetico.timbradas_anio" in a["explicacion"] and "sintetico.progvehiculos" in a["explicacion"]
    assert [h[0] for h in historial(c, a["alert_id"])] == ["creada"]
    eid2 = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    assert flujo.evaluar_alt_008(c, eid2, ref, ev, [2023, 2024])["insertadas"] == 0     # deduplicada
    datos, _ = consulta_api(c, codigo="ALT-008", idruta=ruta)
    assert a["alert_id"] in [d["alert_id"] for d in datos]


# ------------------------------------------------------------------ ALT-006 (hora real observada)

def _salidas_006(c, ids, dia):
    base = uuid.UUID(ids["tag"]).int % (2**62)
    filas = [  # (salida programada, inicio real observado, cancelada)
        (dia + timedelta(hours=6), dia + timedelta(hours=6, minutes=25), "N"),        # tardía +25
        (dia + timedelta(hours=6, minutes=5), dia + timedelta(hours=6, minutes=12), "N"),  # a tiempo +7
        (dia + timedelta(hours=7), None, "N"),                                         # sin inicio
        (dia + timedelta(hours=7, minutes=30), dia + timedelta(hours=7, minutes=40), "S")]  # cancelada
    c.execute(text("""INSERT INTO ai_frecuencias.viaje_fuente (idprogramacion, idruta, fechasalida, fechainicio,
                      fechafin, finalizado, cancelada, numeropasajeros, lote) VALUES (:v, 16, :s, :i, :s, 'S', :k, 10, 'sint')"""),
              [{"v": base + k, "s": s, "i": i, "k": k2} for k, (s, i, k2) in enumerate(filas)])
    return [base + k for k in range(len(filas))]


def test_006_salida_tardia_con_hora_real_alerta_y_sin_inicio_queda_indeterminada(c, ids):
    dia = datetime(2025, 11, 5)                            # fecha que no usa ninguna otra prueba
    prog = _salidas_006(c, ids, dia)
    marca_ant, marca = datetime(2025, 11, 7, 12), datetime(2025, 11, 9, 12)   # salidas de 11-04 12:00 a 11-06 12:00
    eid = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    r = flujo.evaluar_alt_006(c, eid, marca_ant, marca, (16,))
    assert r["estado"] == "evaluada" and r["evidencia_cobertura"] == "no_disponible"
    assert r["conteo_por_clase"].get("tardia", 0) >= 1 and r["conteo_por_clase"].get("indeterminada", 0) >= 1
    mias = [a for a in alertas(c, "ALT-006", 16, eid=eid)]
    (a,) = [x for x in mias if x["franja_inicio"].replace(tzinfo=None) == dia + timedelta(hours=6)]
    assert a["severidad"] == "alta" and a["valor_observado"] == 25 and a["umbral"] == 10
    assert f"{prog[0]} +25 min" in a["explicacion"] and "sin inicio registrado" not in a["explicacion"]
    assert not [x for x in mias if x["franja_inicio"].replace(tzinfo=None) == dia + timedelta(hours=7)]  # sin inicio
    assert [h[0] for h in historial(c, a["alert_id"])] == ["creada"]
    eid2 = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    flujo.evaluar_alt_006(c, eid2, marca_ant, marca, (16,))
    assert alertas(c, "ALT-006", 16, eid=eid2) == []                      # deduplicada
    datos, _ = consulta_api(c, codigo="ALT-006", idruta=16)
    assert a["alert_id"] in [d["alert_id"] for d in datos]


def test_006_salida_a_tiempo_no_alerta(c, ids):
    dia = datetime(2025, 11, 12)
    base = uuid.UUID(ids["tag"]).int % (2**62)
    c.execute(text("""INSERT INTO ai_frecuencias.viaje_fuente (idprogramacion, idruta, fechasalida, fechainicio, fechafin,
                      finalizado, cancelada, numeropasajeros, lote) VALUES (:v, 16, :s, :i, :s, 'S', 'N', 10, 'sint')"""),
              {"v": base, "s": dia + timedelta(hours=6), "i": dia + timedelta(hours=6, minutes=10)})   # +10: en tolerancia
    eid = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    r = flujo.evaluar_alt_006(c, eid, datetime(2025, 11, 14, 12), datetime(2025, 11, 16, 12), (16,))
    assert r["estado"] == "evaluada" and alertas(c, "ALT-006", 16, eid=eid) == []


# ------------------------------------------------------------------ completitud → ALT-005/007

class ProgramacionesSinteticas:
    """Doble de fuentes_extreme.ProgramacionesPorDia (lo que Extreme tiene programado por ruta y día)."""
    solo_lectura, origen = True, "sintetico.progvehiculos"

    def __init__(self, esperadas):
        self._e = esperadas

    def leer(self, desde, hasta):
        return pd.DataFrame(
            [{"idruta": r, "fecha": d, "programaciones": len(ids), "ids_programaciones": ids}
             for (r, d), ids in self._e.items()],
            columns=["idruta", "fecha", "programaciones", "ids_programaciones"],
        )


def _aplicar_propuesta_0010(c):
    """Aplica la PROPUESTA 0010 DENTRO de la transacción exterior (se revierte al final de la prueba)."""
    from pathlib import Path
    if c.execute(text("SELECT to_regclass('ai_frecuencias.completitud_ruta_dia') IS NOT NULL")).scalar():
        pytest.skip("completitud_ruta_dia ya existe en esta base: no se reemplaza")
    sql = (Path(__file__).resolve().parents[2] / "propuestas/migraciones/0010_completitud_ruta_dia.sql").read_text("utf-8")
    c.exec_driver_sql(sql)
    c.execute(text("SET LOCAL search_path TO ai_frecuencias"))


def _ingesta_ok(c, ids):
    c.execute(text("""INSERT INTO ai_frecuencias.execution_log (execution_id, tipo, estado, parametros, resumen,
                      correlation_id, iniciada_en, finalizada_en)
                      VALUES (gen_random_uuid(), 'ingesta', 'exitosa', '{"fuente": "progvehiculos"}'::jsonb,
                              '{"completa": true, "consulta_valida": true, "viajes": 5}'::jsonb, :t, now(), now())"""),
              {"t": ids["tag"]})


def test_completitud_llena_la_tabla_con_evidencia_y_desbloquea_005_007(c, ids):
    _aplicar_propuesta_0010(c)
    _ingesta_ok(c, ids)
    ruta, dia = RUTA_M1, DIA                                        # 2025-11-19
    # 12 viajes copiados del día: finalizados, con conteo (cobertura 100 %)
    base = uuid.UUID(ids["tag"]).int % (2**62)
    c.execute(text("""INSERT INTO ai_frecuencias.viaje_fuente (idprogramacion, idruta, fechasalida, fechainicio, fechafin,
                      finalizado, cancelada, numeropasajeros, lote) VALUES (:v, :r, :s, :s, :s, 'S', 'N', 5, 'sint')"""),
              [{"v": base + k, "r": ruta, "s": dia + pd.Timedelta(minutes=15 * k)} for k in range(24, 36)])
    marca = datetime(2025, 11, 24, 11)
    ids_correctos = list(range(base + 24, base + 36))
    referencia = ProgramacionesSinteticas({
        (ruta, dia.date()): ids_correctos,
        (ruta, (dia - pd.Timedelta(days=1)).date()): list(range(7)),
    })
    eid = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    r = flujo.evaluar_completitud(c, eid, marca, referencia, (ruta,))
    assert r["estado"] == "evaluada" and r["escritas"] == 35 and r["regla"] == "completitud-v2", r
    filas = {f.fecha: f for f in c.execute(text(
        "SELECT * FROM ai_frecuencias.completitud_ruta_dia WHERE idruta = :r"), {"r": ruta})}
    hoy = filas[dia.date()]
    assert hoy.estado == "completo" and hoy.evidencia["programaciones_referencia"] == 12
    assert hoy.evidencia["viajes_copiados"] == 12 and hoy.evidencia["cobertura_conteos"] == 1.0
    assert str(hoy.execution_id) == eid
    ayer = filas[(dia - pd.Timedelta(days=1)).date()]
    assert ayer.estado == "parcial" and "copia_incompleta" in ayer.evidencia["motivos"]   # Extreme 7, copia 0
    otro = filas[(dia - pd.Timedelta(days=2)).date()]
    assert otro.estado == "desconocido" and "sin_programaciones_en_la_referencia" in otro.evidencia["motivos"]
    # repetir no reescribe; un cambio deja la versión anterior en el historial
    eid2 = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    assert flujo.evaluar_completitud(c, eid2, marca, referencia, (ruta,))["escritas"] == 0
    referencia._e[(ruta, dia.date())] = [*ids_correctos[:-1], base + 100]
    eid3 = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    assert flujo.evaluar_completitud(c, eid3, marca, referencia, (ruta,))["escritas"] == 1
    (estado, evidencia) = c.execute(text(
        "SELECT estado, evidencia FROM ai_frecuencias.completitud_ruta_dia WHERE idruta = :r AND fecha = :f"
    ), {"r": ruta, "f": dia.date()}).one()
    assert estado == "parcial" and evidencia["programaciones_referencia"] == evidencia["viajes_copiados"]
    assert evidencia["ids_faltantes"] == evidencia["ids_sobrantes"] == 1
    (h,) = c.execute(text("SELECT estado, execution_id FROM ai_frecuencias.completitud_ruta_dia_historial "
                          "WHERE idruta = :r AND fecha = :f"), {"r": ruta, "f": dia.date()}).all()
    assert h.estado == "completo" and str(h.execution_id) == eid
    referencia._e[(ruta, dia.date())] = ids_correctos
    flujo.evaluar_completitud(c, repo.crear_ejecucion(c, "alertas", {}, ids["tag"]), marca, referencia, (ruta,))
    # recorrido: con la evidencia ESCRITA por el mecanismo, ALT-005/007 son evaluables y alertan
    _prediccion(c, ruta, [(dia + pd.Timedelta(hours=6), 80), (dia + pd.Timedelta(hours=6, minutes=30), 80),
                          (dia + pd.Timedelta(hours=7), 80)])
    _real(c, ruta, [(dia + pd.Timedelta(minutes=15 * k), 5 if k < 28 else 20) for k in range(24, 36)])
    _marca(c, marca)
    eid4 = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    r57 = flujo.evaluar_alt_005_007(c, eid4, None, marca, (ruta,))
    assert r57["estado"] == "evaluada" and r57["insertadas"] >= 2, r57
    codigos = {a["codigo"] for a in alertas(c, ruta=ruta, eid=eid4)}
    assert codigos == {"ALT-005", "ALT-007"}


def test_completitud_sin_tabla_calcula_pero_no_escribe(c, ids):
    if c.execute(text("SELECT to_regclass('ai_frecuencias.completitud_ruta_dia') IS NOT NULL")).scalar():
        pytest.skip("la tabla ya existe en esta base")
    _ingesta_ok(c, ids)
    eid = repo.crear_ejecucion(c, "alertas", {}, ids["tag"])
    r = flujo.evaluar_completitud(c, eid, datetime(2025, 11, 24, 11),
                                  ProgramacionesSinteticas({(RUTA_M1, DIA.date()): [1, 2, 3]}), (RUTA_M1,))
    assert r["estado"] == "no_evaluable" and r["motivo"] == "tabla_pendiente_de_aprobacion"
    assert r["escritas"] == 0 and sum(r["calculadas"].values()) == 35
