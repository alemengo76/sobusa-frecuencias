"""E01 en el servicio de la API: el GBM activo predice y la línea base queda de respaldo.

Sin base de datos: el repositorio se reemplaza con unittest.mock y los viajes son sintéticos.
"""
import importlib.util
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

if importlib.util.find_spec("sqlalchemy") is None:
    pytest.skip("requiere sqlalchemy", allow_module_level=True)

from app import servicios  # noqa: E402
from app.calidad.cuarentena import evaluar_programaciones  # noqa: E402
from app.core.errores import Conflicto  # noqa: E402
from app.ingesta import agregar_por_franja  # noqa: E402
from app.ml import contrato  # noqa: E402
from app.ml import modelo_gbm as G  # noqa: E402
from tests.unit import sinteticos  # noqa: E402
from tests.unit.test_parametros import filas_param_v2  # noqa: E402

DIA = (sinteticos.INICIO + pd.Timedelta(days=70)).to_pydatetime()
RUTAS = [2, 16, 21]
H = (30, 60, 120)
BASE = {"model_id": 1, "nombre": "linea_base_dia_hora", "version": "baseline_dh_v1", "tipo": "baseline",
        "estado": "retirado", "hiperparametros": {"semanas": 4}, "feature_version": "sobusa-fe-2.1-d1",
        "metricas": {}}


@pytest.fixture(scope="module")
def entorno(tmp_path_factory):
    crudo = sinteticos.viajes()
    art, _ = sinteticos.artefacto(crudo)
    carpeta = tmp_path_factory.mktemp("modelos")
    ruta, sha = G.guardar(art, carpeta, "gbm_fe31_prueba")
    validas, _ = evaluar_programaciones(crudo, "l")
    franjas = agregar_por_franja(validas, crudo, "l")
    gbm = {"model_id": 7, "nombre": "gbm_fe31", "version": "gbm_fe31_prueba", "tipo": "candidato",
           "estado": "activo", "hiperparametros": {"metodo": "gbm_fe31"}, "feature_version": "sobusa-fe-3.1",
           "metricas": {}, "artefacto_ruta": ruta.name, "artefacto_sha256": sha}
    return {"crudo": crudo, "franjas": franjas, "carpeta": str(carpeta), "gbm": gbm}


def _demanda_franja(franjas):
    def f(_c, idruta, desde, hasta):
        g = franjas[(franjas.idruta == idruta) & (franjas.franja_inicio >= desde) & (franjas.franja_inicio < hasta)]
        s = pd.Series(g.pasajeros.astype(float).to_numpy(), index=pd.DatetimeIndex(g.franja_inicio)).sort_index()
        return s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="15min"))
    return f


def _viajes(crudo, rutas_sin_datos=()):
    def f(_c, idrutas, desde, hasta):
        v = crudo[crudo.idruta.isin(idrutas) & ~crudo.idruta.isin(rutas_sin_datos)]
        return v[(v.fechasalida >= desde) & (v.fechasalida < hasta)]
    return f


@contextmanager
def repo_falso(entorno, activo, respaldo=BASE, rutas_sin_datos=()):
    cfg = MagicMock(modelos_dir=entorno["carpeta"], metodo_respaldo="dia_hora_4sem")
    G.cargar.cache_clear()
    with patch.object(servicios.repo, "viajes_fuente", _viajes(entorno["crudo"], rutas_sin_datos)), \
         patch.object(servicios.repo, "demanda_franja", _demanda_franja(entorno["franjas"])), \
         patch.object(servicios.repo, "modelo_respaldo", return_value=respaldo), \
         patch.object(servicios.repo, "modelo_activo", return_value=activo), \
         patch.object(servicios, "obtener_config", return_value=cfg):
        yield


def _validar(pred):
    contrato.validar_predicciones(pred.assign(franja_inicio=pd.to_datetime(pred.franja_inicio)))
    assert np.isfinite(pred.demanda_pred).all() and (pred.demanda_pred >= 0).all()


def test_gbm_activo_predice_con_el_modelo_entrenado(entorno):
    with repo_falso(entorno, entorno["gbm"]):
        pred, info = servicios.predecir(MagicMock(), RUTAS, DIA, entorno["gbm"], H)
    assert not info["respaldo"]
    assert set(pred.model_id) == {7} and set(pred.model_version) == {"gbm_fe31_prueba"}
    assert set(pred.feature_version) == {"sobusa-fe-3.1"} and set(pred.idruta) == set(RUTAS)
    assert pred.data_cutoff.eq(pd.Timestamp(DIA).normalize()).all()
    _validar(pred)


def test_artefacto_alterado_usa_la_linea_base(entorno):
    malo = {**entorno["gbm"], "artefacto_sha256": "0" * 64}
    with repo_falso(entorno, malo):
        pred, info = servicios.predecir(MagicMock(), RUTAS, DIA, malo, H)
    assert info["respaldo"] and "hash" in info["motivo_respaldo"]
    assert set(pred.model_id) == {1} and set(pred.idruta) == set(RUTAS)
    _validar(pred)


def test_artefacto_ausente_usa_la_linea_base(entorno):
    sin = {**entorno["gbm"], "artefacto_ruta": "no_existe.pkl"}
    with repo_falso(entorno, sin):
        pred, info = servicios.predecir(MagicMock(), [16], DIA, sin, H)
    assert info["respaldo"] and set(pred.model_id) == {1}


def test_respaldo_por_ruta_cuando_una_no_tiene_viajes(entorno):
    with repo_falso(entorno, entorno["gbm"], rutas_sin_datos=(21,)):
        pred, info = servicios.predecir(MagicMock(), RUTAS, DIA, entorno["gbm"], H)
    assert info["respaldo"] and info["rutas_respaldo"] == [21]
    assert set(pred[pred.idruta == 21].model_id) == {1}
    assert set(pred[pred.idruta != 21].model_id) == {7}
    _validar(pred)


def test_sin_linea_base_registrada_es_conflicto(entorno):
    malo = {**entorno["gbm"], "artefacto_sha256": "x"}
    with repo_falso(entorno, malo, respaldo=None), pytest.raises(Conflicto):
        servicios.predecir(MagicMock(), [16], DIA, malo, H)


@pytest.mark.parametrize("metodo", ["dia_hora_4sem", "dia_hora_8sem", "ultima_semana"])
def test_lineas_base_siguen_funcionando(entorno, metodo):
    base = {**BASE, "estado": "activo", "hiperparametros": {"metodo": metodo}}
    with repo_falso(entorno, base):
        pred, info = servicios.predecir(MagicMock(), RUTAS, DIA, base, H)
    assert not info["respaldo"] and set(pred.model_id) == {1}
    _validar(pred)


def test_ejecucion_guarda_filas_con_el_modelo_que_las_produjo(entorno):
    c = MagicMock()

    @contextmanager
    def escribir():
        yield c

    with repo_falso(entorno, entorno["gbm"], rutas_sin_datos=(2,)), \
         patch.object(servicios, "escribir", escribir), \
         patch.object(servicios.repo, "crear_ejecucion", return_value="e1"), \
         patch.object(servicios.repo, "cerrar_ejecucion") as cerrar:
        r = servicios.ejecutar_predicciones(RUTAS, DIA, H, "cid")
    assert r["estado"] == "exitosa" and r["respaldo"] and r["predicciones"] > 0
    filas = c.execute.call_args.args[1]
    por_ruta = pd.DataFrame(filas).groupby("idruta").model_id.unique().apply(list).to_dict()
    assert por_ruta == {2: [1], 16: [7], 21: [7]}
    resumen = cerrar.call_args.args[3]
    assert resumen["modelo_respaldo"] == 1 and resumen["filas_por_modelo"][7] > 0


def test_no_se_activa_un_gbm_que_no_carga(entorno):
    malo = {**entorno["gbm"], "model_id": 9, "artefacto_sha256": "0" * 64}
    with repo_falso(entorno, BASE), patch.object(servicios.repo, "listar_modelos", return_value=[malo]):
        with pytest.raises(Conflicto, match="No se puede activar"):
            servicios.verificar_activable(MagicMock(), 9)
    with repo_falso(entorno, BASE), patch.object(servicios.repo, "listar_modelos", return_value=[entorno["gbm"]]):
        servicios.verificar_activable(MagicMock(), 7)


def test_recomendaciones_con_factores_medidos(entorno):
    with repo_falso(entorno, entorno["gbm"]):
        pred, _ = servicios.predecir(MagicMock(), RUTAS, DIA, entorno["gbm"], H)
    sabado = pd.Timestamp(DIA) + timedelta(days=(5 - pd.Timestamp(DIA).dayofweek) % 7)
    pred_sab = pred.assign(franja_inicio=pred.franja_inicio + (sabado - pd.Timestamp(DIA).normalize()))
    c = MagicMock()
    with patch.object(servicios.repo, "parametros_vigentes", return_value=filas_param_v2()), \
         patch.object(servicios, "_frecuencia_actual", return_value=None), \
         patch.object(servicios.repo, "insertar_recomendaciones"):
        rec = servicios.calcular_recomendaciones(c, "e", pd.concat([pred, pred_sab]), "puntual")
    rec["dia"] = pd.to_datetime(rec.franja_inicio).dt.normalize()
    tipo_dia_d = "laboral" if pd.Timestamp(DIA).dayofweek < 5 else "sabado"
    from tests.unit.test_parametros import ESPERADO
    for (r, d), g in rec.groupby(["idruta", "dia"]):
        tipo = "sabado" if d == sabado.normalize() else tipo_dia_d
        assert g.factor_carga_max.eq(ESPERADO[(r, tipo)]).all(), (r, d)
        assert g.parametros_version.eq(f"param_v2:{tipo}").all()
