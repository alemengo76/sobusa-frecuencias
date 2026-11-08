"""Migración 0006 (reconciliación de alertas) contra el PostgreSQL real, SIN dejar cambios.

Cada prueba corre dentro de una transacción que se deshace al final (ROLLBACK): crea alertas de
prueba, ejecuta el SQL de upgrade y de downgrade de la 0006 y revisa el resultado.

  docker compose exec app pytest -q -p no:cacheprovider tests/integracion
"""
import importlib.util
import os
import uuid
from pathlib import Path

import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)

from sqlalchemy import text  # noqa: E402

from app.db.conexion import motor_escritura  # noqa: E402

SQL = Path(__file__).resolve().parents[2] / "migrations" / "sql"
ABIERTOS = ("abierta", "reconocida", "asignada")


@pytest.fixture()
def c():
    try:
        conexion = motor_escritura().connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no hay conexión a PostgreSQL: {e}")
    t = conexion.begin()
    conexion.execute(text("SET LOCAL search_path TO ai_frecuencias"))
    if conexion.execute(text("SELECT version_num FROM alembic_version")).scalar() < "0006":
        t.rollback()
        conexion.close()
        pytest.skip("la base no está en la versión 0006")
    yield conexion
    t.rollback()                       # nada de esta prueba queda en la base
    conexion.close()


def correr(c, archivo):
    with c.connection.driver_connection.cursor() as cur:
        cur.execute((SQL / archivo).read_text(encoding="utf-8"))
    c.execute(text("SET LOCAL search_path TO ai_frecuencias"))


def alerta(c, codigo, horizonte, observado, esperado, estado="abierta", usuario=None, clave=None):
    clave = clave or f"prueba-0006-{uuid.uuid4().hex[:12]}"
    aid = c.execute(text("""
        INSERT INTO alert (codigo, severidad, estado, idruta, franja_inicio, horizonte_min, valor_observado,
                           valor_esperado, umbral, umbral_version, explicacion, recomendacion,
                           usuario_asignado, clave_dedup)
        VALUES (:co, 'media', :e, 16, '2025-12-10 07:00', :h, :o, :es, 1.0, 1, 'prueba', 'prueba', :u, :k)
        RETURNING alert_id"""), {"co": codigo, "e": estado, "h": horizonte, "o": observado, "es": esperado,
                                 "u": usuario, "k": clave}).scalar()
    c.execute(text("INSERT INTO alert_history (alert_id, accion, estado_nuevo, usuario) "
                   "VALUES (:a, 'creada', 'abierta', 'prueba')"), {"a": aid})
    return aid


def fila(c, aid):
    return dict(c.execute(text("SELECT * FROM alert WHERE alert_id = :a"), {"a": aid}).mappings().one())


def historial(c, aid, usuario):
    return [dict(f) for f in c.execute(text(
        "SELECT * FROM alert_history WHERE alert_id = :a AND usuario = :u ORDER BY id"),
        {"a": aid, "u": usuario}).mappings()]


def test_estado_actual_cumple_la_regla_vigente(c):
    """En la base ya migrada no queda ninguna alerta abierta que la regla vigente no generaría."""
    malas = c.execute(text("""
        SELECT count(*) FROM alert a, umbral u
        WHERE u.codigo = 'ALT-003' AND u.vigente AND a.estado IN ('abierta', 'reconocida', 'asignada')
          AND a.codigo IN ('ALT-002', 'ALT-003', 'ALT-004')
          AND (a.horizonte_min <> 60 OR (a.codigo = 'ALT-003' AND a.valor_esperado - a.valor_observado < u.valor))
    """)).scalar()
    assert malas == 0
    sin_motivo = c.execute(text("""
        SELECT count(*) FROM alert a WHERE a.estado = 'descartada' AND NOT EXISTS (
            SELECT 1 FROM alert_history h WHERE h.alert_id = a.alert_id AND h.estado_nuevo = 'descartada'
              AND h.comentario IS NOT NULL)""")).scalar()
    assert sin_motivo == 0


def test_upgrade_descarta_lo_que_no_aplica_y_conserva_lo_valido(c):
    h30 = alerta(c, "ALT-003", 30, 10, 14)
    deficit1 = alerta(c, "ALT-003", 60, 10, 11)
    valida = alerta(c, "ALT-003", 60, 8, 11, estado="asignada", usuario="ana")
    alt002_120 = alerta(c, "ALT-002", 120, 0.95, 0.75)
    antes = fila(c, valida)
    correr(c, "0006_reconciliar_alertas.sql")

    for aid, motivo in [(h30, "60 min"), (deficit1, "déficit de esta alerta es 1"), (alt002_120, "120 min")]:
        assert fila(c, aid)["estado"] == "descartada"
        (h,) = historial(c, aid, "migracion_0006")
        assert h["accion"] == "cambio_estado" and h["estado_nuevo"] == "descartada" and motivo in h["comentario"]

    assert fila(c, valida) == antes                     # misma fila: estado, usuario, umbral_version, fechas
    assert fila(c, valida)["umbral_version"] == 1
    (h,) = historial(c, valida, "migracion_0006")
    assert h["accion"] == "comentario" and "Revalidada" in h["comentario"]


def test_downgrade_no_pisa_decisiones_posteriores(c):
    simple = alerta(c, "ALT-003", 30, 10, 14)
    reconocida = alerta(c, "ALT-002", 120, 0.95, 0.75, estado="reconocida", usuario="ana")
    comentada = alerta(c, "ALT-004", 30, 0.2, 0.75)
    clave = f"prueba-0006-{uuid.uuid4().hex[:12]}"
    ocupada = alerta(c, "ALT-002", 30, 0.95, 0.75, clave=clave)
    correr(c, "0006_reconciliar_alertas.sql")

    # Después de la migración: un supervisor comenta una, y otra situación vuelve a tener alerta abierta
    c.execute(text("INSERT INTO alert_history (alert_id, accion, comentario, usuario) "
                   "VALUES (:a, 'comentario', 'confirmo que ya no aplica', 'ana')"), {"a": comentada})
    nueva = alerta(c, "ALT-002", 60, 0.95, 0.75, clave=clave)
    correr(c, "0006_reconciliar_alertas_downgrade.sql")

    assert fila(c, simple)["estado"] == "abierta"
    r = fila(c, reconocida)
    assert r["estado"] == "reconocida" and r["usuario_asignado"] == "ana"      # vuelve a su estado previo
    assert fila(c, comentada)["estado"] == "descartada"                        # tuvo cambios después
    assert fila(c, ocupada)["estado"] == "descartada"                          # su clave ya tiene otra abierta
    assert fila(c, nueva)["estado"] == "abierta"
    (h,) = historial(c, simple, "migracion_0006_reversion")
    assert h["estado_antes"] == "descartada" and h["estado_nuevo"] == "abierta"
    assert historial(c, simple, "migracion_0006")                              # el historial no se borra
