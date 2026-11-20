"""M1: servicio evaluar() y consultas de solo lectura, con la base simulada (unittest.mock)."""
import re
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app import evaluacion_real as er
from app.core.errores import NoEncontrado, SolicitudInvalida
from app.db import lectura_real as lr
from tests.unit.test_comparacion_real import DIA, MARCA, franjas, pred

M = "app.evaluacion_real"


def _cfg(dias=3):
    return MagicMock(dias_revision=dias)


def _pred_df(filas):
    df = pd.DataFrame(filas)
    df["modelo_nombre"], df["modelo_estado"] = "gbm_fe31", "activo"
    # como llega de PostgreSQL con timezone=America/Bogota
    for col in ("franja_inicio", "data_cutoff", "generated_at"):
        df[col] = pd.to_datetime(df[col]).dt.tz_localize("America/Bogota")
    return df


def _correr(filas, marca=MARCA, **kw):
    with patch(f"{M}.obtener_config", return_value=_cfg()), \
         patch(f"{M}.lr.contar_predicciones", return_value=len(filas)), \
         patch(f"{M}.lr.predicciones", return_value=_pred_df(filas)) as lp, \
         patch(f"{M}.lr.franjas_reales", return_value=franjas()) as lf, \
         patch(f"{M}.lr.marca_agua",
               return_value=None if marca is None else {"ultima_actualizacion": marca, "lote": "L1"}), \
         patch(f"{M}.repo.ejecucion_duena", side_effect=lambda c, e: {"e1": "e1", "e2": "e1"}.get(e)):
        r = er.evaluar(MagicMock(), **kw)
    return r, lp, lf


def test_evaluar_completo():
    r, lp, lf = _correr([pred(), pred(prediction_id=2, horizonte_min=30)], model_id=7)
    assert r["pares"].estado.tolist() == ["valido", "valido"]
    assert r["parametros"]["corte_consolidacion"] == "2025-10-17T00:00:00"
    assert r["parametros"]["margen_consolidacion_dias"] == 3
    assert r["modelos"] == {7: {"nombre": "gbm_fe31", "version_registro": "v1", "estado": "activo"}}
    assert len(r["huella"]) == 64
    # el real se lee desde una semana antes (ingenuo) hasta 120 min después de la última franja
    _, rutas, ini, fin = lf.call_args.args
    assert rutas == [16] and ini == DIA + pd.Timedelta(hours=8) - pd.Timedelta(days=7)
    assert fin == DIA + pd.Timedelta(hours=10)
    assert lp.call_args.kwargs["model_id"] == 7


def test_ejecucion_que_reutiliza_lee_las_filas_de_la_duena():
    _, lp, _ = _correr([pred()], execution_id="e2")
    assert lp.call_args.kwargs["execution_id"] == "e1"


def test_ejecucion_inexistente_da_404():
    with pytest.raises(NoEncontrado):
        _correr([pred()], execution_id="no-existe")


def test_rango_invertido_y_limite_de_filas():
    with pytest.raises(SolicitudInvalida):
        er.evaluar(MagicMock(), desde=datetime(2025, 10, 9), hasta=datetime(2025, 10, 8))
    with patch(f"{M}.lr.contar_predicciones", return_value=lr.LIMITE_PREDICCIONES + 1), \
         pytest.raises(SolicitudInvalida):
        er.evaluar(MagicMock())


def test_sin_predicciones_no_falla():
    with patch(f"{M}.obtener_config", return_value=_cfg()), \
         patch(f"{M}.lr.contar_predicciones", return_value=0), \
         patch(f"{M}.lr.marca_agua", return_value=None):
        r = er.evaluar(MagicMock(), idruta=2)
    res = er.resumen_metricas(r)
    assert res["datos"] == [] and res["total"] == [] and res["conteo_estados"]["valido"] == 0
    assert res["parametros"]["corte_consolidacion"] is None


def test_resumen_y_pares_json():
    r, _, _ = _correr([pred(), pred(prediction_id=2, franja_inicio=DIA + pd.Timedelta(hours=8, minutes=7))])
    res = er.resumen_metricas(r, ("idruta",))
    assert res["conteo_estados"]["valido"] == 1 and res["conteo_estados"]["inconsistente"] == 1
    assert res["conteo_motivos"] == {"franja_no_alineada": 1, "ok": 1}
    assert res["datos"][0]["idruta"] == 16 and res["datos"][0]["mae"] == 10.0
    filas, total = er.pares_json(r["pares"], "inconsistente", 1, 50)
    assert total == 1 and filas[0]["motivo"] == "franja_no_alineada" and filas[0]["demanda_real"] is None
    assert filas[0]["detalle_motivo"] and filas[0]["dentro_intervalo"] is None
    filas, total = er.pares_json(r["pares"], None, 2, 1)
    assert total == 2 and len(filas) == 1


def test_consultas_son_de_solo_lectura_y_con_parametros():
    c = MagicMock()
    c.execute.return_value.mappings.return_value.all.return_value = []
    c.execute.return_value.scalar.return_value = 0
    lr.predicciones(c, model_id=7, execution_id="e1", idruta=16, horizonte_min=60,
                    desde=datetime(2025, 10, 1), hasta=datetime(2025, 10, 2))
    lr.contar_predicciones(c, idruta=16)
    lr.franjas_reales(c, [16], datetime(2025, 10, 1), datetime(2025, 10, 2))
    lr.marca_agua(c)
    lr.ejecuciones_prediccion_de_modelo(c, 2)
    lr.contar_predicciones(c, execution_ids=["e1", "e2"])
    for llamada in c.execute.call_args_list:
        sql = str(llamada.args[0]).upper()
        assert sql.lstrip().startswith("SELECT")
        assert not any(p in sql for p in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP"))
        tablas = re.findall(r"(?:FROM|JOIN)\s+([\w.]+)", sql)
        assert tablas and all(t.startswith("AI_FRECUENCIAS.") for t in tablas)   # nunca tablas de Extreme
    params = c.execute.call_args_list[0].args[1]
    assert params == {"model_id": 7, "execution_id": "e1", "idruta": 16, "horizonte_min": 60,
                      "desde": datetime(2025, 10, 1), "hasta": datetime(2025, 10, 2)}


# ---------------------------------------------------------------- m1-1.1: horario para el cero operativo

def _registro():
    return [{"model_id": 2, "estado": "activo", "version": "gbm_v", "artefacto_ruta": "gbm.pkl",
             "artefacto_sha256": "abc", "hiperparametros": {"metodo": "gbm_fe31"}},
            {"model_id": 1, "estado": "retirado", "version": "base", "artefacto_ruta": None,
             "artefacto_sha256": None, "hiperparametros": {"metodo": "dia_hora_4sem"}}]


def test_horario_propio_y_del_activo_para_la_linea_base():
    from tests.unit.test_comparacion_real import horario
    art = MagicMock(horario=horario(model_id=0).drop(columns="model_id"))
    with patch(f"{M}.repo.listar_modelos", return_value=_registro()), \
         patch(f"{M}.modelo_gbm.cargar", return_value=art) as cargar, \
         patch(f"{M}.obtener_config", return_value=MagicMock(modelos_dir="/m")):
        h, fuente, avisos = er.horarios_por_modelo(MagicMock(), [1, 2])
    assert set(h.model_id) == {1, 2} and avisos == []
    assert fuente[2]["origen"] == "propio" and fuente[1]["origen"] == "modelo_activo"
    assert fuente[1]["artefacto_sha256"] == "abc"
    cargar.assert_called_with("/m/gbm.pkl", "abc")          # hash verificado al cargar


def test_sin_artefacto_utilizable_queda_aviso_y_no_se_asume_cero():
    from app.ml import modelo_gbm
    with patch(f"{M}.repo.listar_modelos", return_value=_registro()), \
         patch(f"{M}.modelo_gbm.cargar", side_effect=modelo_gbm.ModeloNoDisponible("hash distinto")), \
         patch(f"{M}.obtener_config", return_value=MagicMock(modelos_dir="/m")):
        h, fuente, avisos = er.horarios_por_modelo(MagicMock(), [1, 2])
    assert h is None and fuente == {} and len(avisos) == 2


def test_evaluar_aplica_el_horario_y_lo_registra():
    from tests.unit.test_comparacion_real import horario
    fr = franjas()
    h = pd.Timedelta(hours=1)
    fr = fr[~fr.franja_inicio.between(DIA + 8 * h, DIA + 8.75 * h)]
    fuente = {7: {"origen": "propio", "version": "v1", "artefacto_sha256": "x"}}
    with patch(f"{M}.horarios_por_modelo", return_value=(horario(), fuente, [])), \
         patch(f"{M}.lr.franjas_reales", return_value=fr), \
         patch(f"{M}.obtener_config", return_value=_cfg()), \
         patch(f"{M}.lr.contar_predicciones", return_value=1), \
         patch(f"{M}.lr.predicciones", return_value=_pred_df([pred(limite_inferior=0.0)])), \
         patch(f"{M}.lr.marca_agua", return_value={"ultima_actualizacion": MARCA, "lote": "L"}):
        r = er.evaluar(MagicMock(), model_id=7)
    assert r["pares"].motivo.tolist() == ["cero_operativo"]
    assert r["parametros"]["horario_operacion"][7]["artefacto_sha256"] == "x"
    assert r["parametros"]["reglas"] == "m1-1.1"


def test_varias_ejecuciones_se_resuelven_a_sus_duenas():
    _, lp, _ = _correr([pred()], execution_ids=["e1", "e2"])          # e2 reutiliza las filas de e1
    assert lp.call_args.kwargs["execution_ids"] == ["e1"]
