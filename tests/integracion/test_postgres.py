"""Pruebas contra el PostgreSQL real (Docker). Se saltan si no hay base.

  docker compose up -d --build
  docker compose exec app pytest -q tests/integracion
"""
import importlib.util
import os

import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)

from sqlalchemy import text  # noqa: E402

from app.db.conexion import consultar_resultados  # noqa: E402


@pytest.fixture(scope="module")
def c():
    try:
        with consultar_resultados() as conexion:
            yield conexion
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no hay conexión a PostgreSQL: {e}")


def q(c, sql):
    return c.execute(text(sql)).scalar()


def test_migraciones_aplicadas(c):
    assert q(c, "SELECT version_num FROM ai_frecuencias.alembic_version") == "0007"
    assert q(c, "SELECT to_regclass('ai_frecuencias.viaje_fuente') IS NOT NULL")


def test_param_v2_vigente_y_v1_conservado(c):
    assert q(c, "SELECT count(*) FROM ai_frecuencias.parametro_operativo WHERE vigente AND version = 'param_v2'") == 12
    assert q(c, "SELECT count(*) FROM ai_frecuencias.parametro_operativo WHERE vigente AND version <> 'param_v2'") == 0
    assert q(c, "SELECT count(*) FROM ai_frecuencias.parametro_operativo WHERE version = 'param_v1'") >= 3
    assert float(q(c, "SELECT factor_carga_max FROM ai_frecuencias.parametro_operativo "
                      "WHERE vigente AND idruta = 16 AND tipo_dia = 'laboral'")) == pytest.approx(0.459)


def test_lee_la_base_local(c):
    assert q(c, "SELECT count(*) FROM ai_frecuencias.demanda_franja") > 0


def test_ingesta_con_comun15(c):
    if not q(c, "SELECT count(*) FROM ai_frecuencias.viaje_fuente"):
        pytest.skip("viaje_fuente vacía: corra python -m app.ingesta --reiniciar")
    versiones = {f[0] for f in c.execute(text(
        "SELECT DISTINCT reglas_version FROM ai_frecuencias.data_quality_error WHERE lote IN "
        "(SELECT DISTINCT lote FROM ai_frecuencias.viaje_fuente)"))}
    assert versiones <= {"comun-1.5"}


def test_modelo_activo_predice(c):
    from app import servicios
    from app.db import repositorio as repo
    modelo = repo.modelo_activo(c)
    if modelo is None:
        pytest.skip("no hay modelo activo")
    dia = q(c, "SELECT date_trunc('day', max(franja_inicio)) FROM ai_frecuencias.demanda_franja")
    pred, info = servicios.predecir(c, [2, 16, 21], dia, modelo, (30, 60, 120))
    assert not pred.empty and (pred.demanda_pred >= 0).all()
    if (modelo.get("hiperparametros") or {}).get("metodo") == "gbm_fe31":
        assert not info["respaldo"], info.get("motivo_respaldo")


def test_umbral_alt003_versionado(c):
    filas = c.execute(text("SELECT version, valor, vigente FROM ai_frecuencias.umbral "
                           "WHERE codigo = 'ALT-003' ORDER BY version")).all()
    vigentes = [f for f in filas if f.vigente]
    assert len(vigentes) == 1 and float(vigentes[0].valor) == 2.0
    assert any(float(f.valor) == 1.0 and not f.vigente for f in filas)      # la versión anterior se conserva
