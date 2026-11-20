"""ALT-010 «Modelo degradado» desde M1 (regla alt010-v1). Solo datos sintéticos."""
from unittest.mock import patch

import pandas as pd
import pytest

from app.alertas import alt010_desde_m1 as A
from app.alertas import motor
from app.ml import comparacion_real as cr

U = motor.umbrales_por_defecto()                      # ALT-010 = 0.20
MODELO = {"model_id": 2, "version": "gbm_v"}
MARCA = pd.Timestamp("2026-03-04 11:00")               # corte 2026-03-01 -> reciente [02-22, 03-01)
REC0, C = pd.Timestamp("2026-02-22"), pd.Timestamp("2026-03-01")
REF0 = pd.Timestamp("2026-01-25")


def _pares(desde, dias, wape, ruta=16, model_id=2, version="gbm_v", horizonte=60, horas=range(6, 21),
           estado="valido", real=100.0, pid0=0):
    """Pares de M1 con WAPE exacto: |pred - real| = wape * real en cada ventana."""
    filas = []
    for d in range(dias):
        for h in horas:
            t = pd.Timestamp(desde) + pd.Timedelta(days=d, hours=h)
            pred = real * (1 + wape)
            filas.append({"prediction_id": pid0 + len(filas) + 1, "model_id": model_id, "model_version": version,
                          "idruta": ruta, "horizonte_min": horizonte, "franja_inicio": t, "estado": estado,
                          "motivo": "ok" if estado == "valido" else "franja_sin_medicion",
                          "demanda_real": real if estado == "valido" else None, "demanda_pred": pred,
                          "error": pred - real, "limite_inferior": pred * 0.8, "limite_superior": pred * 1.2,
                          "dentro_intervalo": True, "nivel_intervalo": 0.8, "demanda_ingenuo_7d": None})
    return pd.DataFrame(filas)


def _escenario(w_ref, w_rec, ruta=16, dias_ref=28, dias_rec=7, **kw):
    return pd.concat([_pares(REF0, dias_ref, w_ref, ruta=ruta, **kw),
                      _pares(REC0, dias_rec, w_rec, ruta=ruta, pid0=10_000, **kw)], ignore_index=True)


def _ev(p, **kw):
    return A.evaluar(p, MODELO, MARCA, 3, U, {"ALT-010": 1}, **kw)


def _ruta(r, ruta=16):
    return [f for f in r["rutas"] if f["idruta"] == ruta][0]


def test_ventanas_contiguas_sin_solapamiento_ancladas_en_el_corte_de_m1():
    v = A.ventanas(MARCA, 3)
    assert v["corte"] == C == cr.corte_consolidacion(MARCA, 3).normalize()
    assert v["reciente"] == (REC0, C) and v["referencia"] == (REF0, REC0)
    assert (v["reciente"][1] - v["reciente"][0]).days == 7 and (v["referencia"][1] - v["referencia"][0]).days == 28
    assert A.ventanas(None, 3) is None


def test_degradacion_superior_al_20_genera_una_alerta_con_evidencia():
    r = _ev(_escenario(0.20, 0.25))                      # +25 %
    assert len(r["alertas"]) == 1 and _ruta(r)["estado"] == "evaluada"
    a = r["alertas"][0]
    assert a["codigo"] == "ALT-010" and a["idruta"] == 16 and a["horizonte_min"] == 60
    assert a["valor_observado"] == 0.25 and a["valor_esperado"] == 0.2 and a["umbral"] == 0.2
    assert a["severidad"] == motor.CATALOGO["ALT-010"]["severidad"] and a["umbral_version"] == 1
    e = a["explicacion"]
    for txt in ("peor que el de referencia", "Referencia (28 días anteriores)", "Modelo 2 (gbm_v)", "[2026-02-22, 2026-03-01)",
                "[2026-01-25, 2026-02-22)", "105 observaciones válidas en 7 días",
                "420 observaciones válidas en 28 días", "Umbral v1", "alt010-v1", "corte de consolidación 2026-03-01"):
        assert txt in e, txt
    assert "validación" not in e
    assert a["clave_dedup"] == motor.clave_dedup("ALT-010", 16, None, 60)


@pytest.mark.parametrize("w_rec, alerta", [(0.24, False), (0.2401, True), (0.18, False)])
def test_umbral_estricto(w_rec, alerta):
    assert bool(_ev(_escenario(0.20, w_rec))["alertas"]) is alerta     # 0,24 = exactamente +20 %


def test_umbral_viene_de_u():
    p = _escenario(0.20, 0.25)
    assert A.evaluar(p, MODELO, MARCA, 3, {**U, "ALT-010": 0.30})["alertas"] == []


def test_menos_de_30_observaciones_no_evalua():
    p = pd.concat([_pares(REF0, 28, 0.2), _pares(REC0, 7, 0.5, horas=range(6, 10), pid0=10_000)])   # 28 obs
    r = _ev(p)
    assert r["alertas"] == [] and _ruta(r)["estado"] == "sin_evidencia_suficiente"
    assert _ruta(r)["n_validas_reciente"] == 28


def test_30_observaciones_en_pocos_dias_no_bastan():
    p = pd.concat([_pares(REF0, 28, 0.2), _pares(REC0, 3, 0.5, pid0=10_000)])    # 45 obs en 3 días
    r = _ev(p)
    assert r["alertas"] == [] and _ruta(r)["dias_reciente"] == 3
    q = pd.concat([_pares(REF0, 13, 0.2), _pares(REC0, 7, 0.5, pid0=10_000)])    # referencia 13 días
    assert _ev(q)["alertas"] == [] and _ruta(_ev(q))["dias_referencia"] == 13
    ok = pd.concat([_pares(REF0 + pd.Timedelta(days=14), 14, 0.2), _pares(REC0, 4, 0.5, pid0=10_000)])
    assert len(_ev(ok)["alertas"]) == 1                                              # mínimos justos


def test_pares_no_validos_no_cuentan():
    p = pd.concat([_escenario(0.20, 0.25), _pares(REC0, 7, 5.0, estado="incompleto_cuarentena", pid0=50_000)])
    r = _ev(p)
    assert _ruta(r)["n_validas_reciente"] == 105 and r["alertas"][0]["valor_observado"] == 0.25


def test_wape_de_referencia_cero_o_indefinido_no_alerta():
    p = pd.concat([_pares(REF0, 28, 0.0), _pares(REC0, 7, 0.5, pid0=10_000)])
    r = _ev(p)
    assert r["alertas"] == [] and _ruta(r)["estado"] == "wape_referencia_cero"
    q = pd.concat([_pares(REF0, 28, 0.2, real=0.0), _pares(REC0, 7, 0.5, pid0=10_000)])   # Σ real = 0
    assert _ev(q)["alertas"] == [] and _ruta(_ev(q))["estado"] == "wape_referencia_cero"


def test_separacion_estricta_por_modelo_y_version():
    p = pd.concat([_escenario(0.20, 0.21),                                   # activo: sin degradación
                   _pares(REC0, 7, 0.9, model_id=1, version="base", pid0=20_000),   # respaldo degradado
                   _pares(REC0, 7, 0.9, version="gbm_v_otra", pid0=30_000)])        # otra versión degradada
    r = _ev(p)
    assert r["alertas"] == [] and r["excluidos"]["otro_modelo"] == 105 and r["excluidos"]["otra_version"] == 105
    assert _ruta(r)["wape_reciente"] == pytest.approx(0.21)


def test_solo_horizonte_60_y_rutas_independientes():
    p = pd.concat([_escenario(0.20, 0.25, ruta=16), _escenario(0.20, 0.21, ruta=2),
                   _pares(REC0, 7, 0.9, horizonte=30, pid0=40_000)])
    r = _ev(p)
    assert [a["idruta"] for a in r["alertas"]] == [16] and r["excluidos"]["otro_horizonte"] == 105
    assert {f["idruta"]: f["estado"] for f in r["rutas"]} == {2: "evaluada", 16: "evaluada"}


def test_franjas_anteriores_a_2026_se_excluyen_en_modo_real():
    marca = pd.Timestamp("2026-01-23 11:58")                                # corte 2026-01-20
    p = pd.concat([_pares("2025-12-16", 28, 0.20), _pares("2026-01-13", 7, 0.5, pid0=10_000)])
    r = A.evaluar(p, MODELO, marca, 3, U)
    assert r["alertas"] == [] and r["excluidos"]["antes_de_inicio"] == 16 * 15
    assert _ruta(r)["estado"] == "sin_evidencia_suficiente"                 # solo quedan 12 días de referencia
    assert len(A.evaluar(p, MODELO, marca, 3, U, excluir_antes=None)["alertas"]) == 1


def test_lo_posterior_al_corte_no_entra():
    p = pd.concat([_escenario(0.20, 0.20), _pares(C, 3, 5.0, pid0=60_000)])   # datos provisionales
    r = _ev(p)
    assert r["alertas"] == [] and _ruta(r)["n_validas_reciente"] == 105


def test_sin_marca_o_sin_pares():
    assert A.evaluar(_escenario(0.2, 0.5), MODELO, None, 3, U)["alertas"] == []
    r = _ev(pd.DataFrame())
    assert r["alertas"] == [] and r["rutas"] == []


def test_determinista_y_deduplicacion_por_ruta_y_horizonte():
    p = _escenario(0.20, 0.30)
    a1, a2 = _ev(p)["alertas"], _ev(p.sample(frac=1, random_state=3))["alertas"]
    assert a1 == a2
    clave = a1[0]["clave_dedup"]
    assert motor.deduplicar(a1, {clave}) == []
    otra_marca = A.evaluar(_escenario(0.20, 0.30), MODELO, MARCA + pd.Timedelta(days=7), 3, U)
    assert otra_marca["rutas"] == [] or all(x["clave_dedup"] == clave for x in otra_marca["alertas"])


def test_reutiliza_agregar_y_para_alt_010_de_m1():
    p = _escenario(0.20, 0.25)
    with patch.object(cr, "agregar", wraps=cr.agregar) as ag, patch.object(cr, "para_alt_010", wraps=cr.para_alt_010) as pa:
        _ev(p)
    assert ag.call_count == 4 and pa.call_count == 1


def test_motor_sigue_aceptando_la_entrada_minima():
    d = pd.DataFrame({"idruta": [16], "horizonte_min": [60], "wape_reciente": [0.30], "wape_referencia": [0.20]})
    a = motor.alt_010(d, U)
    assert len(a) == 1 and "referencia" in a[0]["explicacion"]


def test_simular_solo_en_modo_simulacion():
    from app.core.errores import Conflicto
    with pytest.raises(Conflicto):
        A.simular(object(), modo="real")
