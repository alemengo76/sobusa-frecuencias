"""P1 (A1 + A2) en el servicio y el repositorio, sin base de datos (unittest.mock).

Comprueban la lógica: dueña o reutiliza según la reserva de la huella, conteo con RETURNING, una sola
transacción con registro aparte de la falla, y que las recomendaciones salen de lo guardado. Lo que
depende de PostgreSQL (bloqueos, rollback real, RETURNING real) está en
tests/integracion/test_trazabilidad.py y corre en una base desechable.
"""
import importlib.util
import json
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

if importlib.util.find_spec("sqlalchemy") is None:
    pytest.skip("requiere sqlalchemy", allow_module_level=True)

from app import servicios  # noqa: E402
from app.core.errores import Conflicto, NoEncontrado  # noqa: E402
from app.db import repositorio as repo  # noqa: E402
from app.ml import contrato  # noqa: E402
from tests.unit.test_servicio_prediccion import (  # noqa: E402,F401
    DIA,
    RUTAS,
    H,
    entorno,
    guardadas,
    repo_falso,
)


class Transacciones:
    """Sustituye a conexion.escribir(): cuenta las transacciones y cómo terminó cada una."""

    def __init__(self):
        self.c = MagicMock()
        self.resultados = []           # "commit" | "rollback:<Excepción>"

    @contextmanager
    def escribir(self):
        try:
            yield self.c
        except Exception as e:
            self.resultados.append(f"rollback:{type(e).__name__}")
            raise
        self.resultados.append("commit")


@contextmanager
def ejecucion(entorno, tx, reservar=True, duena="e0", insertar=None):  # noqa: F811
    insertar = insertar or (lambda c, e, df: len(df))
    with repo_falso(entorno, entorno["gbm"]), \
         patch.object(servicios, "escribir", tx.escribir), \
         patch.object(servicios.repo, "crear_ejecucion", side_effect=["e1", "e_fallo"]) as crear, \
         patch.object(servicios.repo, "reservar_conjunto", side_effect=reservar if callable(reservar) else None,
                      return_value=reservar) as reservar_m, \
         patch.object(servicios.repo, "duena_conjunto", return_value=duena), \
         patch.object(servicios.repo, "insertar_predicciones", side_effect=insertar) as insertar_m, \
         patch.object(servicios.repo, "cerrar_ejecucion") as cerrar:
        yield SimpleNamespace(crear=crear, reservar=reservar_m, insertar=insertar_m, cerrar=cerrar)


# ------------------------------------------------------------------ dueña o reutiliza

def test_duena_inserta_todo_normalizado_y_lo_declara(entorno):  # noqa: F811
    tx = Transacciones()
    with ejecucion(entorno, tx) as m:
        r = servicios.ejecutar_predicciones(RUTAS, DIA, H, "cid")
    assert tx.resultados == ["commit"]
    df = m.insertar.call_args.args[2]
    assert all(isinstance(v, Decimal) for v in df.demanda_pred)              # los valores redondeados
    assert r["predicciones"] == r["predicciones_guardadas"] == len(df) > 0
    assert r["execution_id_predicciones"] == "e1" and "reutiliza" not in r
    huella, eid, filas, formato = m.reservar.call_args.args[1:]
    assert eid == "e1" and filas == len(df) and formato == contrato.FORMATO_HUELLA and len(huella) == 64
    estado, resumen = m.cerrar.call_args.args[2:4]
    assert estado == "exitosa" and resumen["huella"] == huella and resumen["predicciones_guardadas"] == len(df)
    sentencias = [str(c.args[0]) for c in tx.c.execute.call_args_list]
    assert any("SET LOCAL lock_timeout = '30s'" in s for s in sentencias)


def test_misma_huella_reutiliza_sin_insertar(entorno):  # noqa: F811
    tx = Transacciones()
    with ejecucion(entorno, tx, reservar=False, duena="e0") as m:
        r = servicios.ejecutar_predicciones(RUTAS, DIA, H, "cid")
    m.insertar.assert_not_called()
    assert tx.resultados == ["commit"]
    assert r["predicciones_guardadas"] == 0 and r["predicciones"] > 0
    assert r["reutiliza"] == r["execution_id_predicciones"] == "e0"
    assert m.cerrar.call_args.args[3]["reutiliza"] == "e0"


def test_la_huella_es_la_misma_al_repetir_y_cambia_si_cambia_el_historico(entorno):  # noqa: F811
    def huella_de(ent):
        tx = Transacciones()
        with ejecucion(ent, tx) as m:
            servicios.ejecutar_predicciones(RUTAS, DIA, H, "cid")
        return m.reservar.call_args.args[1]

    primera, segunda = huella_de(entorno), huella_de(entorno)
    assert primera == segunda
    crudo = entorno["crudo"].copy()
    previos = crudo.index[(crudo.idruta == 16) & (pd.to_datetime(crudo.fechasalida) < pd.Timestamp(DIA))
                          & crudo.numeropasajeros.notna()][-200:]
    crudo.loc[previos, "numeropasajeros"] = crudo.loc[previos, "numeropasajeros"] + 30
    assert huella_de({**entorno, "crudo": crudo}) != primera


# ------------------------------------------------------------------ fallos

def test_falla_al_insertar_revierte_y_registra_aparte(entorno):  # noqa: F811
    tx = Transacciones()

    def falla(c, e, df):
        raise repo.PrediccionesNoGuardadas("se enviaron 10 y la base devolvió 9")

    with ejecucion(entorno, tx, insertar=falla) as m:
        with pytest.raises(repo.PrediccionesNoGuardadas):
            servicios.ejecutar_predicciones(RUTAS, DIA, H, "cid")
    # 1.ª transacción: reserva + inserción -> revertida; 2.ª: solo el registro de la falla
    assert tx.resultados == ["rollback:PrediccionesNoGuardadas", "commit"]
    assert [c.args[1] for c in m.crear.call_args_list] == ["prediccion", "prediccion"]
    (llamada,) = m.cerrar.call_args_list                    # nunca se cerró como exitosa
    assert llamada.args[1:3] == ("e_fallo", "fallida") and "devolvió 9" in llamada.args[4]


def _error_del_controlador(clase: str, mensaje: str):
    """La excepción tal como llega al servicio: psycopg 3 (el controlador del proyecto) envuelta por
    SQLAlchemy en OperationalError, con el original en `orig`."""
    errores = pytest.importorskip("psycopg.errors")
    from sqlalchemy.exc import OperationalError
    return OperationalError("INSERT INTO prediction_set ...", {}, getattr(errores, clase)(mensaje))


def _lanza(error):
    def reservar(*a):
        raise error
    return reservar


def test_el_error_real_de_lock_timeout_trae_sqlstate_y_no_pgcode():
    e = _error_del_controlador("LockNotAvailable", "canceling statement due to lock timeout")
    assert e.orig.sqlstate == servicios.SQLSTATE_LOCK_TIMEOUT == "55P03"
    assert not hasattr(e.orig, "pgcode")          # pgcode es de psycopg2, no de este controlador


@pytest.mark.parametrize("clase, esperado", [("LockNotAvailable", True), ("QueryCanceled", False),
                                             ("DeadlockDetected", False)])
def test_es_lock_timeout_envuelto_y_sin_envolver(clase, esperado):
    e = _error_del_controlador(clase, "x")
    assert servicios._es_lock_timeout(e) is esperado          # como lo entrega SQLAlchemy
    assert servicios._es_lock_timeout(e.orig) is esperado     # el original de psycopg
    assert servicios._es_lock_timeout(RuntimeError("lock timeout")) is False


def test_lock_timeout_responde_409_y_registra_la_falla(entorno):  # noqa: F811
    tx = Transacciones()
    error = _error_del_controlador("LockNotAvailable", "canceling statement due to lock timeout")

    with ejecucion(entorno, tx, reservar=_lanza(error)) as m:
        with pytest.raises(Conflicto, match="Otra ejecución") as info:
            servicios.ejecutar_predicciones(RUTAS, DIA, H, "cid")
    assert info.value.__cause__ is error
    assert tx.resultados == ["rollback:OperationalError", "commit"]
    m.insertar.assert_not_called()
    assert m.cerrar.call_args.args[2] == "fallida" and "LockNotAvailable" in m.cerrar.call_args.args[4]


def test_otro_error_operacional_no_se_convierte_en_409(entorno):  # noqa: F811
    tx = Transacciones()
    error = _error_del_controlador("QueryCanceled", "canceling statement due to statement timeout")

    with ejecucion(entorno, tx, reservar=_lanza(error)):
        with pytest.raises(type(error)):
            servicios.ejecutar_predicciones(RUTAS, DIA, H, "cid")
    assert tx.resultados == ["rollback:OperationalError", "commit"]


def test_si_registrar_la_falla_tambien_falla_se_conserva_el_error_original(entorno):  # noqa: F811
    tx = Transacciones()

    def falla(c, e, df):
        raise repo.PrediccionesNoGuardadas("original")

    with ejecucion(entorno, tx, insertar=falla) as m:
        m.crear.side_effect = ["e1", RuntimeError("la base no responde")]
        with pytest.raises(repo.PrediccionesNoGuardadas, match="original"):
            servicios.ejecutar_predicciones(RUTAS, DIA, H, "cid")


# ------------------------------------------------------------------ frecuencias sobre lo guardado

def test_frecuencias_recomienda_sobre_lo_guardado():
    tx = Transacciones()
    guardado = pd.DataFrame({"prediction_id": [11, 12], "idruta": [16, 16]})
    resultado = {"execution_id": "e2", "estado": "exitosa", "predicciones": 2, "predicciones_guardadas": 0,
                 "execution_id_predicciones": "e1", "reutiliza": "e1"}
    with patch.object(servicios, "escribir", tx.escribir), \
         patch.object(servicios, "ejecutar_predicciones", return_value=resultado), \
         patch.object(servicios, "predicciones_de_ejecucion", return_value=guardado) as leer, \
         patch.object(servicios, "calcular_recomendaciones",
                      return_value=pd.DataFrame({"es_viable": [True, True]})) as recomendar, \
         patch.object(servicios, "evaluar_alertas", return_value=0) as alertas, \
         patch.object(servicios.repo, "crear_ejecucion", return_value="f1") as crear, \
         patch.object(servicios.repo, "cerrar_ejecucion") as cerrar:
        r = servicios.ejecutar_frecuencias([16], DIA, [60], "puntual", "cid")
    leer.assert_called_once_with(tx.c, "e2")
    assert recomendar.call_args.args[2] is guardado and alertas.call_args.args[2] is guardado
    parametros = crear.call_args.args[2]
    assert parametros["prediccion"] == "e2" and parametros["prediccion_filas"] == "e1"
    assert r["execution_id_predicciones"] == "e1" and cerrar.call_args.args[2] == "exitosa"


def test_recomendar_sin_prediction_id_se_rechaza():
    pred = pd.DataFrame({"idruta": [16], "demanda_pred": [10.0]})
    with pytest.raises(RuntimeError, match="predicciones guardadas"):
        servicios.calcular_recomendaciones(MagicMock(), "e", pred, "puntual")
    with pytest.raises(RuntimeError, match="predicciones guardadas"):
        servicios.calcular_recomendaciones(MagicMock(), "e", pred.assign(prediction_id=[None]), "puntual")


def _guardadas_falsas():
    """Predicciones como las devuelve predicciones_de_ejecucion: con prediction_id propio por fila."""
    from app.ml.falsas import generar_predicciones_falsas
    p = generar_predicciones_falsas(seed=3, horas=2, rutas=(16, 21))
    return p.assign(prediction_id=range(1000, 1000 + len(p)), model_id=2)


def _parametros_falsos():
    from app.optimizacion.frecuencias import ParametrosRuta
    return {r: ParametrosRuta(idruta=r, capacidad_nominal=55, ocupacion_objetivo=0.75, ciclo_min=200,
                              factor_carga_max=0.4, flota_tope=99) for r in (16, 21)}


def test_recomendar_propaga_el_prediction_id_de_la_misma_fila():
    """Revisa frecuencias.recomendar: cada salida lleva el prediction_id de la fila de entrada de la que
    salió, aun con la entrada desordenada y con la unión de la frecuencia actual (RF-008)."""
    pred = _guardadas_falsas().sample(frac=1, random_state=7).reset_index(drop=True)
    actual = pred[["idruta", "franja_inicio"]].drop_duplicates().assign(buses_hora_actual=10)
    rec = servicios.recomendar(pred, _parametros_falsos(), actual)
    clave = list(contrato.CLAVE_PREDICCION)
    unido = rec.merge(pred[["prediction_id", *clave]], on="prediction_id", suffixes=("", "_pred"),
                      validate="one_to_one")
    assert len(unido) == len(rec) == len(pred) and rec.prediction_id.is_unique
    for k in clave:
        assert (unido[k] == unido[f"{k}_pred"]).all(), k


def _calcular_con(rec_falsa):
    pred = _guardadas_falsas()
    with patch.object(servicios, "_parametros",
                      return_value=SimpleNamespace(rutas=lambda: {16, 21})), \
         patch.object(servicios, "_frecuencia_actual", return_value=None), \
         patch.object(servicios, "recomendar", side_effect=lambda p, *a: rec_falsa(p)), \
         patch.object(servicios.repo, "insertar_recomendaciones") as insertar:
        try:
            servicios.calcular_recomendaciones(MagicMock(), "e", pred, "puntual")
        except RuntimeError:
            pass
    return insertar


def _rec_de(p):
    return p[["prediction_id", *contrato.CLAVE_PREDICCION]].assign(es_viable=True).reset_index(drop=True)


@pytest.mark.parametrize("dano, mensaje", [
    (lambda r: r.assign(prediction_id=r.prediction_id.to_numpy()[::-1]), "otro valor de"),   # cruzados
    (lambda r: r.iloc[1:], "una a una"),                                                      # falta una
    (lambda r: pd.concat([r, r.iloc[[0]]]), "una a una"),                                     # repetida
    (lambda r: r.assign(horizonte_min=r.horizonte_min.where(r.index != 0, 999)), "horizonte_min"),
])
def test_recomendaciones_mal_enlazadas_no_se_guardan(dano, mensaje):
    with pytest.raises(RuntimeError, match=mensaje):
        servicios._verificar_enlace(_guardadas_falsas(), dano(_rec_de(_guardadas_falsas())))
    insertar = _calcular_con(lambda p: dano(_rec_de(p)))
    insertar.assert_not_called()


def test_recomendaciones_bien_enlazadas_se_guardan():
    insertar = _calcular_con(_rec_de)
    insertar.assert_called_once()


def test_predicciones_de_ejecucion_lee_las_de_la_duena():
    filas = [{"prediction_id": 5, "idruta": 16,
              "franja_inicio": pd.Timestamp("2025-12-03 06:00", tz="America/Bogota"),
              "demanda_pred": Decimal("10.50"), "limite_inferior": Decimal("9.00"),
              "limite_superior": Decimal("12.00")}]
    with patch.object(servicios.repo, "ejecucion_duena", return_value="e1") as duena, \
         patch.object(servicios.repo, "consultar", return_value=(filas, 1)) as consultar:
        df = servicios.predicciones_de_ejecucion(MagicMock(), "e2")
    duena.assert_called_once()
    assert consultar.call_args.args[2] == {"execution_id": "e1"}
    assert df.prediction_id.tolist() == [5] and df.franja_inicio.iloc[0] == pd.Timestamp("2025-12-03 06:00")
    assert df.demanda_pred.dtype == float

def test_predicciones_de_ejecucion_rechaza_lectura_incompleta():
    filas = [{"prediction_id": 5, "idruta": 16,
              "franja_inicio": pd.Timestamp("2025-12-03 06:00", tz="America/Bogota"),
              "demanda_pred": Decimal("10.50"), "limite_inferior": Decimal("9.00"),
              "limite_superior": Decimal("12.00")}]

    with patch.object(servicios.repo, "ejecucion_duena", return_value="e1"), \
         patch.object(servicios.repo, "consultar", return_value=(filas, 100001)):
        with pytest.raises(RuntimeError, match="Lectura incompleta de predicciones"):
            servicios.predicciones_de_ejecucion(MagicMock(), "e2")

def test_predicciones_de_ejecucion_inexistente_es_404():
    with patch.object(servicios.repo, "ejecucion_duena", return_value=None):
        with pytest.raises(NoEncontrado):
            servicios.predicciones_de_ejecucion(MagicMock(), "no-existe")


# ------------------------------------------------------------------ repositorio

def _normalizadas():
    from app.ml.falsas import generar_predicciones_falsas
    return contrato.normalizar_predicciones(generar_predicciones_falsas(seed=2, horas=1).assign(model_id=7))


def test_insertar_predicciones_envia_valores_exactos_y_cuenta_con_returning():
    df = _normalizadas()
    c = MagicMock()
    c.execute.return_value.all.return_value = [(i,) for i in range(len(df))]
    assert repo.insertar_predicciones(c, "e1", df) == len(df)
    sql, params = c.execute.call_args.args
    assert "RETURNING prediction_id" in str(sql) and "ON CONFLICT" not in str(sql)
    filas = json.loads(params["filas"])
    assert params["eid"] == "e1" and len(filas) == len(df)
    assert filas[0]["demanda_pred"] == format(df.demanda_pred.iloc[0], "f")       # texto exacto: "123.45"
    assert filas[0]["franja_inicio"] == df.franja_inicio.iloc[0].strftime("%Y-%m-%dT%H:%M:%S")
    assert isinstance(filas[0]["model_id"], int) and isinstance(filas[0]["idruta"], int)


def test_insertar_predicciones_falla_si_la_base_no_devuelve_todas():
    df = _normalizadas()
    c = MagicMock()
    c.execute.return_value.all.return_value = [(1,)]
    with pytest.raises(repo.PrediccionesNoGuardadas, match="devolvió 1"):
        repo.insertar_predicciones(c, "e1", df)


def test_reservar_conjunto_distingue_duena_de_reutiliza():
    c = MagicMock()
    c.execute.return_value.scalar.return_value = "e1"
    assert repo.reservar_conjunto(c, "a" * 64, "e1", 3, "huella-pred/1") is True
    c.execute.return_value.scalar.return_value = None
    assert repo.reservar_conjunto(c, "a" * 64, "e2", 3, "huella-pred/1") is False
    assert "ON CONFLICT (huella) DO NOTHING" in str(c.execute.call_args.args[0])


def test_ejecucion_duena():
    c = MagicMock()
    c.execute.return_value.first.return_value = ("e1",)
    assert repo.ejecucion_duena(c, "e2") == "e1"
    c.execute.return_value.first.return_value = (None,)
    assert repo.ejecucion_duena(c, "e2") == "e2"
    c.execute.return_value.first.return_value = None
    assert repo.ejecucion_duena(c, "x") is None

def test_frecuencias_conserva_error_original_si_falla_registro(entorno):  # noqa: F811
    tx = Transacciones()
    resultado = {
        "execution_id": "p1",
        "estado": "exitosa",
        "predicciones": 2,
        "predicciones_guardadas": 2,
        "execution_id_predicciones": "p1",
    }
    error_original = ValueError("fallo al recuperar predicciones")

    with patch.object(servicios, "escribir", tx.escribir), \
         patch.object(servicios, "ejecutar_predicciones", return_value=resultado), \
         patch.object(servicios.repo, "crear_ejecucion",
                      side_effect=["f1", RuntimeError("falló el registro")]) as crear, \
         patch.object(servicios, "predicciones_de_ejecucion",
                      side_effect=error_original), \
         patch.object(servicios.log, "exception") as registrar_log:
        with pytest.raises(ValueError, match="fallo al recuperar predicciones") as info:
            servicios.ejecutar_frecuencias([16], DIA, [60], "puntual", "cid")

    assert info.value is error_original
    assert tx.resultados == ["rollback:ValueError", "rollback:RuntimeError"]
    assert [llamada.args[1] for llamada in crear.call_args_list] == [
        "frecuencias", "frecuencias"
    ]
    registrar_log.assert_called_once()
