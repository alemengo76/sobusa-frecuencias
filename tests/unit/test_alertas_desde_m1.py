"""ALT-005/ALT-007 desde M1, evaluadas tras la ingesta, en modo simulación (unittest.mock)."""
import inspect
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app import servicios
from app.alertas import desde_m1, motor
from app.core.errores import Conflicto, SolicitudInvalida
from app.ml import comparacion_real as cr
from tests.unit.test_comparacion_real import DIA, MARCA, franjas, pred

M = "app.alertas.desde_m1"
H = pd.Timedelta(hours=1)
U = {"ALT-005": 0.4, "ALT-007": 0.35}
VERSIONES = {"ALT-005": 1, "ALT-007": 1}
MARCA_PREVIA = pd.Timestamp("2025-10-10")

# real = 10 pasajeros por franja: 40 en 60 min, 20 en 30 min
FILAS = [
    pred(prediction_id=1, demanda_pred=80.0, limite_superior=90.0),                    # 60': -50% → ALT-005
    pred(prediction_id=2, franja_inicio=DIA + 10 * H, demanda_pred=25.0, limite_inferior=5.0,
         limite_superior=35.0),                                                         # 60': +60% → ALT-007
    pred(prediction_id=3, franja_inicio=DIA + 12 * H, demanda_pred=41.0),               # 60': sin alerta
    pred(prediction_id=4, horizonte_min=30, demanda_pred=60.0, limite_superior=70.0),  # 30': no monitoreado
    pred(prediction_id=5, idruta=21, model_id=1, model_version="base", registro_version="base",
         feature_version="fe31", demanda_pred=80.0, limite_superior=90.0, execution_id="e1"),  # respaldo
]


def _pares(filas=FILAS):
    return cr.emparejar(pd.DataFrame(filas), franjas(rutas=(16, 21)), MARCA, 3)


def _evaluacion(filas=FILAS):
    return {"pares": _pares(filas), "parametros": {"reglas": cr.VERSION_REGLAS}, "modelos": {},
            "huella": "h" * 64}


EJECUCIONES = [{"execution_id": "e1", "resumen": {"modelo_activo": 7, "respaldo": True,
                                                  "filas_por_modelo": {"7": 4, "1": 1}}}]


def _tras_ingesta(filas=FILAS, abiertas=(), umbrales=None, ejecuciones=EJECUCIONES,
                  marca_anterior=MARCA_PREVIA, marca_nueva=MARCA, vigente=MARCA, **kw):
    c = MagicMock()
    with patch(f"{M}.evaluacion_real.evaluar", return_value=_evaluacion(filas)) as ev, \
         patch(f"{M}.repo.modelo_activo", return_value={"model_id": 7}), \
         patch(f"{M}.lr.ejecuciones_prediccion_de_modelo", return_value=list(ejecuciones)) as ej, \
         patch(f"{M}.lr.marca_agua", return_value={"ultima_actualizacion": vigente}), \
         patch(f"{M}.obtener_config", return_value=MagicMock(dias_revision=3)), \
         patch(f"{M}.repo.umbrales_vigentes", return_value=(umbrales or U, VERSIONES)), \
         patch(f"{M}.repo.claves_alertas_abiertas", return_value=set(abiertas)), \
         patch(f"{M}.repo.insertar_alertas") as ins, \
         patch("app.servicios.notificar") as noti:
        r = desde_m1.evaluar_tras_ingesta(c, marca_anterior, marca_nueva, **kw)
    return r, ev, ej, ins, noti, c


# ------------------------------------------------------------------ piezas puras

def test_ventana_tras_ingesta():
    a, n = desde_m1.ventana_tras_ingesta(pd.Timestamp("2025-10-10"), pd.Timestamp("2025-10-20"), 3)
    assert (a, n) == (pd.Timestamp("2025-10-07"), pd.Timestamp("2025-10-17"))
    a, n = desde_m1.ventana_tras_ingesta(None, pd.Timestamp("2025-10-20"), 3)
    assert a is None and n == pd.Timestamp("2025-10-17")


def test_regla_actual_sin_cambios_y_separada_por_modelo():
    pares = _pares()
    cand = desde_m1.alertas_de_pares(pares, U, VERSIONES, "h" * 64, modelo_activo=7)
    # mismas alertas que motor.alt_005_007 aplicado directo a los pares válidos de 60 min
    for mid in (7, 1):
        g = pares[pares.estado.eq("valido") & pares.horizonte_min.eq(60) & pares.model_id.eq(mid)]
        directo = motor.alt_005_007(cr.para_alt_005_007(g), U)
        propios = [a for a in cand if a["trazabilidad"]["model_id"] == mid]
        assert [(a["codigo"], a["clave_dedup"], a["valor_observado"], a["umbral"]) for a in propios] == \
               [(a["codigo"], a["clave_dedup"], a["valor_observado"], a["umbral"]) for a in directo]
    assert sorted((a["codigo"], a["trazabilidad"]["prediction_id"], a["trazabilidad"]["origen_modelo"])
                  for a in cand) == [("ALT-005", 1, "modelo_activo"), ("ALT-005", 5, "respaldo"),
                                     ("ALT-007", 2, "modelo_activo")]
    assert all(a["horizonte_min"] == 60 for a in cand)


def test_trazabilidad_en_explicacion_y_estructurada():
    a = next(x for x in desde_m1.alertas_de_pares(_pares(), U, VERSIONES, "a" * 64, 7)
             if x["trazabilidad"]["prediction_id"] == 1)
    t = a["trazabilidad"]
    assert t == {"model_id": 7, "model_version": "v1", "origen_modelo": "modelo_activo", "prediction_id": 1,
                 "execution_id_prediccion": "e1", "umbral_version": 1, "reglas_m1": cr.VERSION_REGLAS,
                 "huella_m1": "a" * 64}
    assert "predicción 1" in a["explicacion"] and "ejecución e1" in a["explicacion"]
    assert "umbral v1" in a["explicacion"] and a["umbral_version"] == 1
    assert a["explicacion"].startswith("Se registraron 40 subidas")      # el texto de la regla no cambia


def test_propuesta_alt_007_por_intervalo_no_genera_alertas():
    p = desde_m1.propuesta_alt_007_intervalo(_pares(), u=U)
    assert p["activa"] is False and p["filas_validas"] == 4
    # pred 2: real 40 > límite 35 → A; pred 1 y 5: real 40 < límite 20? no (20 ≤ 40) → no B
    assert p["variante_A_por_encima"] == 1 and p["variante_B_fuera_del_intervalo"] == 1
    assert p["actual_35pct"] == 1 and p["coinciden_A_y_actual"] == 1
    assert p["ejemplos_A"][0]["prediction_id"] == 2
    assert p["ejemplos_A"][0]["puntuacion"] == pytest.approx((40 - 35) / (35 - 25))
    # pronóstico 45 con intervalo [10, 60]: error +11% (no es ALT-007 actual) y dentro del intervalo
    q = desde_m1.propuesta_alt_007_intervalo(_pares([pred(demanda_pred=36.0, limite_inferior=10.0,
                                                          limite_superior=38.0)]), u=U)
    assert (q["variante_A_por_encima"], q["actual_35pct"], q["solo_A"]) == (1, 0, 1)


# ------------------------------------------------------------------ servicio tras la ingesta

def test_tras_ingesta_simula_sin_escribir_ni_notificar():
    r, ev, ej, ins, noti, c = _tras_ingesta()
    assert r["simulacion"] and not r["escrito"] and not r["notificado"]
    ej.assert_called_once_with(c, 7)
    kw = ev.call_args.kwargs
    assert kw["execution_ids"] == ["e1"]
    assert kw["desde"] == pd.Timestamp("2025-10-07") - pd.Timedelta(minutes=120)
    assert kw["hasta"] == pd.Timestamp("2025-10-17")
    assert r["corte_anterior"] == "2025-10-07T00:00:00" and r["corte_nuevo"] == "2025-10-17T00:00:00"
    assert r["por_codigo"] == {"ALT-005": 2, "ALT-007": 1}
    assert r["por_origen"] == {"modelo_activo": 2, "respaldo": 1}
    assert r["ejecuciones_con_respaldo"] == 1
    ins.assert_not_called()
    noti.assert_not_called()
    c.execute.assert_not_called()


def test_solo_ventanas_que_esta_ingesta_volvio_consolidadas():
    # Con la marca anterior en 2025-10-11 09:00 el corte anterior es 2025-10-08 09:00: las ventanas
    # 08:00-09:00 (pred 1 y 5) ya eran evaluables en la ingesta previa y no se repiten. Un minuto
    # antes, todavía no lo eran y entran en esta.
    r, *_ = _tras_ingesta(marca_anterior=pd.Timestamp("2025-10-11 09:00"))
    assert {a["trazabilidad"]["prediction_id"] for a in r["alertas"]} == {2}
    r, *_ = _tras_ingesta(marca_anterior=pd.Timestamp("2025-10-11 08:59"))
    assert {a["trazabilidad"]["prediction_id"] for a in r["alertas"]} == {1, 2, 5}


def test_marca_sin_avance_no_evalua_nada():
    r, ev, *_ = _tras_ingesta(marca_anterior=MARCA)
    assert r["candidatas"] == 0 and r["motivo_vacio"] == "no hay ventanas nuevas consolidadas"
    ev.assert_not_called()


def test_idempotente_y_deduplicado():
    r1, *_ = _tras_ingesta()
    r2, *_ = _tras_ingesta()
    assert r1 == r2
    abierta = r1["alertas"][0]["clave_dedup"]
    r3, *_ = _tras_ingesta(abiertas={abierta})
    assert r3["candidatas"] == 3 and r3["nuevas_tras_deduplicar"] == 2
    assert abierta not in {a["clave_dedup"] for a in r3["alertas"]}


def test_umbral_vigente_cambia_el_resultado_sin_tocar_la_regla():
    r, *_ = _tras_ingesta(umbrales={"ALT-005": 0.6, "ALT-007": 0.35})
    # con caída mínima de 60%, las caídas de -50% las cuenta la regla actual como ALT-007
    assert r["por_codigo"] == {"ALT-005": 0, "ALT-007": 3}


def test_sin_ejecuciones_del_modelo_activo():
    r, ev, *_ = _tras_ingesta(ejecuciones=[])
    assert r["candidatas"] == 0 and "no hay ejecuciones" in r["motivo_vacio"]
    ev.assert_not_called()


def test_errores():
    with pytest.raises(Conflicto):
        _tras_ingesta(modo="real")                       # el modo real no está autorizado
    with pytest.raises(SolicitudInvalida):
        _tras_ingesta(horizontes=(45,))
    with pytest.raises(Conflicto):
        _tras_ingesta(vigente=pd.Timestamp("2025-10-21"))  # la marca no es la que dejó la ingesta
    with patch(f"{M}.repo.modelo_activo", return_value=None), pytest.raises(Conflicto):
        desde_m1.evaluar_tras_ingesta(MagicMock(), None, MARCA)


def test_no_esta_enganchada_al_flujo_y_alt_001_004_no_cambian():
    assert "desde_m1" not in inspect.getsource(servicios)
    fuente = inspect.getsource(servicios.evaluar_alertas)
    assert "alt_005_007" not in fuente and "alt_001" in fuente and "alt_002_003_004" in fuente
