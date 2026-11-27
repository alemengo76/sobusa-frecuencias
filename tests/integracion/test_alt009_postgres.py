"""Incompletitud ALT-009 con PostgreSQL real y rollback al final de cada prueba."""
import importlib.util
import os
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)
if os.environ.get("SOBUSA_BD_DESECHABLE") != "1":
    pytest.skip("requiere SOBUSA_BD_DESECHABLE=1", allow_module_level=True)

from sqlalchemy import text  # noqa: E402

from app import servicios  # noqa: E402
from app.calidad.cuarentena import REGLAS_VERSION  # noqa: E402
from app.core.config import Config  # noqa: E402
from app.db.conexion import motor_escritura  # noqa: E402
from app.ingesta import SQL_EVENTO_CALIDAD  # noqa: E402


@pytest.fixture
def base_desechable():
    with motor_escritura().connect() as c, c.begin() as tx:
        if not c.execute(text("SELECT current_database()")).scalar_one().startswith("sobusa_desechable"):
            pytest.skip("la base no es desechable")
        revisiones = c.execute(text("SELECT version_num FROM ai_frecuencias.alembic_version")).scalars().all()
        if len(revisiones) != 1:
            pytest.skip("se requiere exactamente una revisión Alembic")
        version = revisiones[0]
        if not isinstance(version, str) or re.fullmatch(r"[0-9]{4}", version) is None:
            pytest.skip("la revisión Alembic debe tener cuatro dígitos ASCII")
        if int(version) < 8:
            pytest.skip("requiere el esquema de la versión 0008 o posterior")
        c.execute(text("SET LOCAL search_path TO ai_frecuencias"))
        try:
            yield c
        finally:
            tx.rollback()


@pytest.fixture
def evaluar(base_desechable, monkeypatch):
    c = base_desechable
    identificador = uuid.uuid4()
    lote = f"alt009_prueba_{identificador.hex}"
    ruta = 1000 + identificador.int % (2**31 - 1000)
    viaje = identificador.int % (2**63 - 2)
    dia = datetime(2025, 12, 16)
    franja = pd.Timestamp(dia + timedelta(days=7, hours=8))
    eid = servicios.repo.crear_ejecucion(c, "frecuencias", {"lote": lote}, lote)
    c.execute(text("""
        INSERT INTO ai_frecuencias.watermark (tabla_origen, ultima_actualizacion, lote)
        VALUES ('progvehiculos', :marca, :lote)
        ON CONFLICT (tabla_origen) DO UPDATE
        SET ultima_actualizacion = EXCLUDED.ultima_actualizacion, lote = EXCLUDED.lote
    """), {"marca": dia + timedelta(days=4, hours=12), "lote": lote})
    c.execute(text("""
        INSERT INTO ai_frecuencias.viaje_fuente
            (idprogramacion, idruta, fechasalida, fechainicio, fechafin, finalizado,
             cancelada, numeropasajeros, lote)
        VALUES (:viaje, :ruta, :salida, :salida, :fin, 'S', 'N', :pasajeros, :lote)
    """), [{"viaje": viaje + i, "ruta": ruta, "salida": dia + timedelta(hours=6 + i),
            "fin": dia + timedelta(hours=8 + i), "pasajeros": pasajeros, "lote": lote}
           for i, pasajeros in enumerate((None, 40))])
    c.execute(text(SQL_EVENTO_CALIDAD),
              {"lote": lote, "tabla_origen": "progvehiculos", "idprogramacion": viaje, "idruta": ruta,
               "regla": "pasajeros_sin_conteo", "causa": "Conteo nulo", "valor": None,
               "reglas_version": REGLAS_VERSION, "valor_original": None})
    version = c.execute(text("""
        UPDATE ai_frecuencias.umbral SET valor = 0.8
        WHERE codigo = 'ALT-009' AND vigente RETURNING version
    """)).scalar_one()

    # Referencia controlada y destinatarios ficticios. Se conserva el notificador real.
    monkeypatch.setattr(servicios, "obtener_config", lambda: Config(
        dias_revision=3, notificaciones_simuladas=True, correo_destino="alt009@example.invalid",
        whatsapp_destino=None))
    monkeypatch.setattr(servicios.repo, "demanda_franja", lambda *_: pd.Series([10]))
    monkeypatch.setattr(servicios.baseline, "predecir", lambda *_: pd.DataFrame(
        {"franja_inicio": [franja], "horizonte_min": [60], "demanda_pred": [10]}))
    pred = pd.DataFrame({"idruta": [ruta], "franja_inicio": [franja], "horizonte_min": [60],
                         "demanda_pred": [100]})
    rec = pd.DataFrame([
        {"idruta": ruta, "franja_inicio": franja + timedelta(hours=i), "horizonte_min": 60,
         "buses_hora_actual": 1, "buses_hora_recomendado": recomendado, "es_viable": True,
         "carga_max_estimada_hora": carga, "capacidad_nominal": 100, "ocupacion_objetivo": 0.75}
        for i, (recomendado, carga) in enumerate(((10, 200), (1, 1)))
    ])

    def ejecutar():
        return servicios.evaluar_alertas(c, eid, pred, rec)

    return ejecutar, eid, lote, version


def _alertas(c, eid):
    filas = c.execute(text("""
        SELECT alert_id, codigo, severidad, valor_observado, umbral, umbral_version, explicacion
        FROM ai_frecuencias.alert WHERE execution_id = :eid
    """), {"eid": eid}).mappings()
    return {fila["codigo"]: fila for fila in filas}


def _historial(c, alert_id):
    return c.execute(text("""
        SELECT accion, estado_nuevo FROM ai_frecuencias.alert_history WHERE alert_id = :id
    """), {"id": alert_id}).all()


def test_alt009_persiste_con_historial_y_no_notifica(base_desechable, evaluar):
    c = base_desechable
    ejecutar, eid, lote, version = evaluar
    assert ejecutar() == 5
    alertas = _alertas(c, eid)
    alt009 = alertas["ALT-009"]
    assert alt009["severidad"] == "alta"
    assert alt009["valor_observado"] == Decimal("0.5")
    assert alt009["umbral"] == Decimal("0.8")
    assert alt009["umbral_version"] == version
    assert lote in alt009["explicacion"]
    assert _historial(c, alt009["alert_id"]) == [("creada", "abierta")]
    codigos_notificados = c.execute(text("""
        SELECT a.codigo FROM ai_frecuencias.notification_log n
        JOIN ai_frecuencias.alert a ON a.alert_id = n.alert_id
        WHERE a.execution_id = :eid
    """), {"eid": eid}).scalars().all()
    assert "ALT-001" in codigos_notificados
    assert "ALT-009" not in codigos_notificados


def test_error_sql_revierte_alt009_y_conserva_alt001_a_alt004(base_desechable, evaluar, monkeypatch, caplog):
    c = base_desechable
    ejecutar, eid, _, _ = evaluar
    insertar = servicios.repo.insertar_alertas
    ids_revertidos = []

    def insertar_con_error(conexion, ejecucion, alertas, versiones):
        cantidad = insertar(conexion, ejecucion, alertas, versiones)
        if any(a["codigo"] == "ALT-009" for a in alertas):
            aid = _alertas(conexion, ejecucion)["ALT-009"]["alert_id"]
            assert _historial(conexion, aid) == [("creada", "abierta")]
            ids_revertidos.append(aid)
            conexion.execute(text("SELECT 1 / 0"))
        return cantidad

    monkeypatch.setattr(servicios.repo, "insertar_alertas", insertar_con_error)
    assert ejecutar() == 4
    [aid] = ids_revertidos
    errores = [r.exc_info[1] for r in caplog.records
               if r.getMessage() == "Falló la evaluación de incompletitud ALT-009" and r.exc_info]
    [error] = errores
    assert error.orig.sqlstate == "22012"
    alertas = _alertas(c, eid)
    assert set(alertas) == {"ALT-001", "ALT-002", "ALT-003", "ALT-004"}
    for alerta in alertas.values():
        assert _historial(c, alerta["alert_id"]) == [("creada", "abierta")]
    assert _historial(c, aid) == []
    servicios.repo.cerrar_ejecucion(c, eid, "exitosa", {})
    assert servicios.repo.obtener_ejecucion(c, eid)["estado"] == "exitosa"
