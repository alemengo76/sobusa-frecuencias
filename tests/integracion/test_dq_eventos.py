"""Errores de calidad como eventos + detecciones (migración 0007), contra el PostgreSQL real.

Cada prueba corre dentro de una transacción que se deshace al final (ROLLBACK): no deja nada en la
base, ni siquiera las pruebas que ejecutan el downgrade y el upgrade completos de la 0007.

  docker compose exec app pytest -q -p no:cacheprovider tests/integracion
"""
import importlib.util
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)

import psycopg  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from app import ingesta  # noqa: E402
from app.db.conexion import motor_escritura  # noqa: E402
from app.ingesta import SQL_EVENTO_CALIDAD, filas_calidad  # noqa: E402

SQL = Path(__file__).resolve().parents[2] / "migrations" / "sql"
VIAJE = 990_000_001          # viajes de prueba: no existen en progvehiculos

# Las 10 columnas de la tabla antigua, distinguiendo NULL de los textos 'NaN' / 'nan' / ''.
FILA = ("concat_ws('|', {id}, quote_nullable({lote}), quote_nullable({tabla_origen}), "
        "quote_nullable({idprogramacion}), quote_nullable({idruta}), quote_nullable({regla}), "
        "quote_nullable({causa}), quote_nullable({valor}), quote_nullable({reglas_version}), "
        "quote_nullable({creado_en} AT TIME ZONE 'UTC'))")
HUELLA_TABLA = ("SELECT coalesce(md5(string_agg(" + FILA.format(
    id="t.id", lote="t.lote", tabla_origen="t.tabla_origen", idprogramacion="t.idprogramacion",
    idruta="t.idruta", regla="t.regla", causa="t.causa", valor="t.valor", reglas_version="t.reglas_version",
    creado_en="t.creado_en") + ", E'\\n' ORDER BY t.id)), '') FROM data_quality_error t WHERE {filtro}")
HUELLA_MIGRADAS = ("SELECT coalesce(md5(string_agg(" + FILA.format(
    id="d.id_original", lote="d.lote", tabla_origen="e.tabla_origen", idprogramacion="e.idprogramacion",
    idruta="d.idruta", regla="e.regla", causa="d.causa", valor="d.valor_original",
    reglas_version="e.reglas_version", creado_en="d.detectado_en") + ", E'\\n' ORDER BY d.id_original)), '') "
    "FROM data_quality_deteccion d JOIN data_quality_error e ON e.id = d.evento_id WHERE d.id_original IS NOT NULL")


@pytest.fixture()
def c():
    try:
        conexion = motor_escritura().connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no hay conexión a PostgreSQL: {e}")
    t = conexion.begin()
    conexion.execute(text("SET LOCAL search_path TO ai_frecuencias"))
    if conexion.execute(text("SELECT version_num FROM alembic_version")).scalar() < "0007":
        t.rollback()
        conexion.close()
        pytest.skip("la base no está en la versión 0007")
    yield conexion
    t.rollback()                       # nada de esta prueba queda en la base
    conexion.close()


def q(c, sql, **p):
    return c.execute(text(sql), p).scalar()


def correr(c, archivo):
    with c.connection.driver_connection.cursor() as cur:
        cur.execute((SQL / archivo).read_text(encoding="utf-8"))
    c.execute(text("SET LOCAL search_path TO ai_frecuencias"))


def detectar(c, lote, valor, viaje=VIAJE, regla="pasajeros_extremo", version="comun-1.5", idruta=16, causa="c"):
    cuarentena = pd.DataFrame({"lote": [lote], "idprogramacion": [viaje], "idruta": [idruta], "regla": [regla],
                               "causa": [causa], "valor": pd.Series([valor], dtype=object)})
    c.execute(text(SQL_EVENTO_CALIDAD), filas_calidad(cuarentena, version))


def eventos(c, viaje=VIAJE):
    return [dict(f) for f in c.execute(text(
        "SELECT * FROM data_quality_error WHERE idprogramacion = :v ORDER BY id"), {"v": viaje}).mappings()]


def detecciones(c, viaje=VIAJE):
    return [dict(f) for f in c.execute(text(
        "SELECT d.* FROM data_quality_deteccion d JOIN data_quality_error e ON e.id = d.evento_id "
        "WHERE e.idprogramacion = :v ORDER BY d.id"), {"v": viaje}).mappings()]


# ------------------------------------------------------------------ estado de la base migrada

def test_base_migrada_es_consistente(c):
    assert q(c, """SELECT count(*) FROM (SELECT 1 FROM data_quality_error
                   GROUP BY tabla_origen, idprogramacion, regla, reglas_version, (valor IS NULL), coalesce(valor, '')
                   HAVING count(*) > 1) x""") == 0
    assert q(c, "SELECT count(*) FROM data_quality_error WHERE valor IN ('NaN', 'nan')") == 0
    assert q(c, "SELECT count(*) FROM data_quality_error e WHERE NOT EXISTS "
                "(SELECT 1 FROM data_quality_deteccion d WHERE d.evento_id = e.id)") == 0
    # la huella guardada por la 0007 se sigue reproduciendo con las detecciones migradas
    comentario = q(c, "SELECT obj_description('data_quality_deteccion'::regclass, 'pg_class')")
    guardada = comentario.split("huella_0007=")[1].split(";")[0]
    assert q(c, HUELLA_MIGRADAS) == guardada


# ------------------------------------------------------------------ comportamiento de la ingesta

def test_mismo_error_mismo_valor_en_dos_lotes(c):
    detectar(c, "prueba_A", "97199")
    primero = eventos(c)[0]
    detectar(c, "prueba_B", "97199")
    (e,) = eventos(c)
    assert e["lote"] == "prueba_A" and e["ultimo_lote"] == "prueba_B"
    assert e["creado_en"] == primero["creado_en"] and e["ultima_deteccion"] >= primero["ultima_deteccion"]
    assert [d["lote"] for d in detecciones(c)] == ["prueba_A", "prueba_B"]


def test_cambia_el_valor_es_otro_evento(c):
    detectar(c, "prueba_A", "97199")
    detectar(c, "prueba_B", "650")
    assert [e["valor"] for e in eventos(c)] == ["97199", "650"]


def test_deja_de_incumplir_no_crea_nada(c):
    detectar(c, "prueba_A", "97199")             # en el lote B el viaje ya no está en cuarentena
    (e,) = eventos(c)
    assert e["ultimo_lote"] == "prueba_A" and len(detecciones(c)) == 1


def test_version_nueva_es_otro_evento(c):
    detectar(c, "prueba_A", "97199")
    detectar(c, "prueba_B", "97199", version="comun-1.6")
    assert [e["reglas_version"] for e in eventos(c)] == ["comun-1.5", "comun-1.6"]


def test_null_y_null_son_el_mismo_evento(c):
    detectar(c, "prueba_A", None)
    detectar(c, "prueba_B", None)
    (e,) = eventos(c)
    assert e["valor"] is None and e["ultimo_lote"] == "prueba_B"


def test_texto_nan_se_guarda_como_null(c):
    detectar(c, "prueba_A", None)
    detectar(c, "prueba_B", "NaN")
    (e,) = eventos(c)
    assert e["valor"] is None
    assert [d["valor_original"] for d in detecciones(c)] == [None, "NaN"]


@pytest.mark.parametrize("otro", ["", "5"])
def test_null_no_se_confunde_con_texto(c, otro):
    detectar(c, "prueba_A", None)
    detectar(c, "prueba_B", otro)
    assert [e["valor"] for e in eventos(c)] == [None, otro]


def test_reintento_del_mismo_lote(c):
    detectar(c, "prueba_A", "97199")
    detectar(c, "prueba_A", "97199")
    assert len(eventos(c)) == 1 and len(detecciones(c)) == 1


def test_otra_ruta_y_causa_quedan_en_la_deteccion(c):
    detectar(c, "prueba_A", "97199", idruta=16, causa="causa A")
    detectar(c, "prueba_B", "97199", idruta=21, causa="causa B")
    detectar(c, "prueba_B", "97199", idruta=21, causa="causa B")         # reintento de B
    (e,) = eventos(c)
    assert e["idruta"] == 16 and e["causa"] == "causa A"                  # el evento: primera detección
    assert [(d["idruta"], d["causa"]) for d in detecciones(c)] == [(16, "causa A"), (21, "causa B")]


def test_idprogramacion_nulo_se_rechaza(c):
    sp = c.begin_nested()
    with pytest.raises(DBAPIError):
        c.execute(text(SQL_EVENTO_CALIDAD), {"lote": "prueba", "tabla_origen": "progvehiculos",
                                             "idprogramacion": None, "idruta": 16, "regla": "x", "causa": "c",
                                             "valor": "1", "reglas_version": "comun-1.5", "valor_original": "1"})
    sp.rollback()


def test_ingesta_dos_veces_no_cambia_el_numero_de_filas(c):
    cuarentena = pd.DataFrame({"lote": "prueba_A", "idprogramacion": [VIAJE, VIAJE + 1, VIAJE + 2],
                               "idruta": 16, "regla": ["pasajeros_extremo", "pasajeros_sin_conteo", "fin_tardio"],
                               "causa": "c", "valor": pd.Series(["97199", None, "NaN"], dtype=object)})
    antes = (q(c, "SELECT count(*) FROM data_quality_error"), q(c, "SELECT count(*) FROM data_quality_deteccion"))
    fin = datetime(2025, 12, 1)
    ingesta._guardar(c, [], None, cuarentena, fin, 0, "prueba_A")
    una = (q(c, "SELECT count(*) FROM data_quality_error"), q(c, "SELECT count(*) FROM data_quality_deteccion"))
    ingesta._guardar(c, [], None, cuarentena, fin, 0, "prueba_A")      # la misma carga otra vez
    assert (q(c, "SELECT count(*) FROM data_quality_error"), q(c, "SELECT count(*) FROM data_quality_deteccion")) == una
    assert una == (antes[0] + 3, antes[1] + 3)
    assert q(c, "SELECT count(*) FROM data_quality_error WHERE idprogramacion >= :v AND valor IS NULL", v=VIAJE) == 2


# ------------------------------------------------------------------ downgrade y upgrade de la 0007

def ids_originales(c) -> list[int]:
    return [f[0] for f in c.execute(text(
        "SELECT id_original FROM data_quality_deteccion WHERE id_original IS NOT NULL"))]


def test_downgrade_reconstruye_las_filas_originales_e_upgrade_vuelve(c):
    huella = q(c, HUELLA_MIGRADAS)
    ids = ids_originales(c)
    total = q(c, "SELECT count(*) FROM data_quality_deteccion")
    correr(c, "0007_dq_eventos_downgrade.sql")
    assert q(c, "SELECT to_regclass('data_quality_deteccion')") is None
    assert q(c, HUELLA_TABLA.format(filtro="t.id = ANY(:ids)"), ids=ids) == huella   # 10 columnas idénticas
    assert q(c, "SELECT count(*) FROM data_quality_error") == total          # una fila por detección
    correr(c, "0007_dq_eventos.sql")                                          # y se puede volver a migrar
    assert q(c, "SELECT count(*) FROM data_quality_deteccion") == total


def test_downgrade_con_detecciones_nuevas(c):
    huella = q(c, HUELLA_MIGRADAS)
    ids = ids_originales(c)
    detectar(c, "prueba_A", "97199")
    detectar(c, "prueba_B", "97199")
    detectar(c, "prueba_C", "650")         # otro lote: la forma antigua admite una fila por lote, viaje y regla
    total = q(c, "SELECT count(*) FROM data_quality_deteccion")
    usado = q(c, "SELECT last_value FROM data_quality_error_id_seq")
    correr(c, "0007_dq_eventos_downgrade.sql")
    assert q(c, "SELECT count(*) FROM data_quality_error") == total           # una fila por detección
    filas = c.execute(text("SELECT id, lote, valor FROM data_quality_error WHERE idprogramacion = :v ORDER BY id"),
                      {"v": VIAJE}).all()
    assert [(f.lote, f.valor) for f in filas] == [("prueba_A", "97199"), ("prueba_B", "97199"), ("prueba_C", "650")]
    assert all(f.id > usado for f in filas)                                    # ids nuevos, nunca reutilizados
    assert q(c, HUELLA_TABLA.format(filtro="t.id = ANY(:ids)"), ids=ids) == huella


def test_downgrade_se_niega_si_la_forma_antigua_no_alcanza(c):
    detectar(c, "prueba_A", "700", version="comun-1.5")
    detectar(c, "prueba_A", "700", version="comun-1.6")      # mismo lote, viaje y regla: dos eventos
    sp = c.begin_nested()
    # correr() usa el cursor de psycopg (sin SQLAlchemy): el error no llega envuelto en DBAPIError
    with pytest.raises(psycopg.errors.RaiseException, match="No se puede revertir la 0007"):
        correr(c, "0007_dq_eventos_downgrade.sql")
    sp.rollback()
    assert q(c, "SELECT to_regclass('data_quality_deteccion')") is not None    # no cambió nada


def test_secuencia_no_baja_en_el_downgrade(c):
    c.execute(text("SELECT setval('data_quality_error_id_seq', (SELECT max(id) FROM data_quality_error) + 50000)"))
    alto = q(c, "SELECT last_value FROM data_quality_error_id_seq")
    correr(c, "0007_dq_eventos_downgrade.sql")
    assert q(c, "SELECT last_value FROM data_quality_error_id_seq") >= alto
    assert q(c, "SELECT nextval('data_quality_error_id_seq')") > q(c, "SELECT max(id) FROM data_quality_error")


def test_secuencia_atrasada_se_corrige_en_el_upgrade(c):
    correr(c, "0007_dq_eventos_downgrade.sql")
    maximo = q(c, "SELECT max(id) FROM data_quality_error")
    c.execute(text("SELECT setval('data_quality_error_id_seq', 1)"))          # secuencia por debajo de los ids
    correr(c, "0007_dq_eventos.sql")
    assert q(c, "SELECT last_value FROM data_quality_error_id_seq") >= maximo


def test_verificacion_rechaza_una_huella_distinta(c):
    texto = (SQL / "0007_dq_eventos.sql").read_text(encoding="utf-8")
    bloque = texto[texto.index("-- [verificacion]"):texto.index("-- [/verificacion]")]
    filas = q(c, "SELECT count(*) FROM data_quality_deteccion WHERE id_original IS NOT NULL")
    maximo = q(c, "SELECT coalesce(max(id_original), 0) FROM data_quality_deteccion")
    c.execute(text("SELECT set_config('sobusa.dq0007_filas', :f, true), set_config('sobusa.dq0007_max_id', :m, true), "
                   "set_config('sobusa.dq0007_huella', 'huella_alterada', true)"), {"f": str(filas), "m": str(maximo)})
    sp = c.begin_nested()
    with pytest.raises(psycopg.errors.RaiseException, match="no reconstruyen la tabla original"):
        with c.connection.driver_connection.cursor() as cur:
            cur.execute(bloque)
    sp.rollback()
