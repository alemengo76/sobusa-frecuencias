"""M1: emparejamiento predicción-real y métricas, con datos sintéticos (sin base de datos)."""
import math

import numpy as np
import pandas as pd
import pytest

from app.alertas import motor
from app.ml import comparacion_real as cr

DIA = pd.Timestamp("2025-10-08")
MARCA = pd.Timestamp("2025-10-20 00:00")   # corte = 2025-10-17 con margen de 3 días


def franjas(dias=("2025-10-01", "2025-10-08"), rutas=(16,), pasajeros=10.0, prog=1):
    filas = []
    for r in rutas:
        for d in dias:
            for t in pd.date_range(d, periods=96, freq="15min"):
                filas.append({"idruta": r, "franja_inicio": t, "pasajeros": pasajeros,
                              "programaciones": prog, "programaciones_validas": prog})
    return pd.DataFrame(filas)


def pred(**kw):
    base = {"prediction_id": 1, "execution_id": "e1", "idruta": 16, "sentido": "CICLO",
            "segmento_id": "TOTAL", "franja_inicio": DIA + pd.Timedelta(hours=8),
            "horizonte_min": 60, "demanda_pred": 30.0, "limite_inferior": 20.0,
            "limite_superior": 50.0, "nivel_intervalo": 0.8, "model_id": 7,
            "model_version": "v1", "feature_version": "fe31", "data_cutoff": DIA,
            "generated_at": pd.Timestamp("2025-11-01 10:00"),
            "registro_version": "v1", "registro_feature_version": "fe31"}
    base.update(kw)
    return base


def correr(filas, fr=None, marca=MARCA, margen=3):
    return cr.emparejar(pd.DataFrame(filas), franjas() if fr is None else fr, marca, margen)


# ---------------------------------------------------------------- real por ventana

def test_real_es_suma_de_las_franjas_de_la_ventana():
    fr = franjas()
    fr.loc[fr.franja_inicio == DIA + pd.Timedelta(hours=8, minutes=15), "pasajeros"] = 25.0
    claves = pd.DataFrame({"idruta": [16, 16, 16], "horizonte_min": [30, 60, 120],
                           "franja_inicio": [DIA + pd.Timedelta(hours=8)] * 3})
    r = cr.real_por_ventana(fr, claves).set_index("horizonte_min")
    assert r.demanda_real.to_dict() == {30: 35.0, 60: 55.0, 120: 95.0}
    assert r.programaciones_ventana.to_dict() == {30: 2, 60: 4, 120: 8}


def test_ventana_que_cruza_medianoche_exige_ambos_dias():
    p = correr([pred(franja_inicio=DIA + pd.Timedelta(hours=23, minutes=30), horizonte_min=60)])
    assert p.loc[0, "estado"] == "incompleto_cuarentena" and p.loc[0, "motivo"] == "dia_sin_datos"
    fr = franjas(dias=("2025-10-01", "2025-10-02", "2025-10-08", "2025-10-09"))
    p = correr([pred(franja_inicio=DIA + pd.Timedelta(hours=23, minutes=30), horizonte_min=60)], fr)
    assert p.loc[0, "estado"] == "valido" and p.loc[0, "demanda_real"] == 40.0


# ---------------------------------------------------------------- estados

def test_par_valido_y_sus_campos():
    p = correr([pred()])
    f = p.iloc[0]
    assert f.estado == "valido" and f.motivo == "ok"
    assert f.demanda_real == 40.0 and f.error == -10.0 and bool(f.dentro_intervalo)
    assert f.demanda_ingenuo_7d == 40.0
    assert f.fin_ventana == DIA + pd.Timedelta(hours=9)


def test_real_no_consolidado_es_no_disponible():
    # corte = marca - 3 días = 2025-10-08 08:30 < fin de ventana 09:00
    p = correr([pred()], marca=pd.Timestamp("2025-10-11 08:30"))
    assert (p.loc[0, "estado"], p.loc[0, "motivo"]) == ("no_disponible", "sin_consolidar")
    assert pd.isna(p.loc[0, "demanda_real"])
    p = correr([pred()], marca=pd.Timestamp("2025-10-11 09:00"))
    assert p.loc[0, "estado"] == "valido"


def test_sin_marca_de_agua_nada_es_comparable():
    p = correr([pred()], marca=None)
    assert (p.loc[0, "estado"], p.loc[0, "motivo"]) == ("no_disponible", "sin_marca_agua")


def test_franja_en_cuarentena_invalida_la_ventana_completa():
    fr = franjas()
    fr.loc[fr.franja_inicio == DIA + pd.Timedelta(hours=8, minutes=45), "pasajeros"] = np.nan
    p = correr([pred(), pred(prediction_id=2, horizonte_min=30)], fr)
    est = p.set_index("horizonte_min")
    assert (est.loc[60, "estado"], est.loc[60, "motivo"]) == ("incompleto_cuarentena", "franja_sin_medicion")
    assert est.loc[30, "estado"] == "valido"          # 08:00-08:30 no toca la franja NULL
    assert pd.isna(est.loc[60, "demanda_real"])        # no se imputa


def test_dia_no_cargado_es_incompleto():
    fr = franjas(dias=("2025-10-01",))
    p = correr([pred()], fr)
    assert (p.loc[0, "estado"], p.loc[0, "motivo"]) == ("incompleto_cuarentena", "dia_sin_datos")


def test_ventana_sin_salidas_no_tiene_correspondencia():
    fr = franjas()
    h = pd.Timedelta(hours=1)
    en_ventana = fr.franja_inicio.between(DIA + 8 * h, DIA + 8.75 * h)
    fr = fr[~en_ventana]
    p = correr([pred()], fr)
    assert (p.loc[0, "estado"], p.loc[0, "motivo"]) == ("sin_correspondencia", "sin_servicio")


def test_franja_sin_fila_aporta_cero_si_hay_otras_salidas():
    fr = franjas()
    fr = fr[fr.franja_inicio != DIA + pd.Timedelta(hours=8, minutes=15)]
    p = correr([pred()], fr)
    assert p.loc[0, "estado"] == "valido" and p.loc[0, "demanda_real"] == 30.0


@pytest.mark.parametrize("cambio, motivo", [
    ({"horizonte_min": 45}, "horizonte_invalido"),
    ({"franja_inicio": DIA + pd.Timedelta(hours=8, minutes=7)}, "franja_no_alineada"),
    ({"demanda_pred": -1.0, "limite_inferior": -2.0}, "valor_invalido"),
    ({"limite_superior": 25.0}, "limites_invalidos"),
    ({"data_cutoff": DIA + pd.Timedelta(hours=9)}, "informacion_futura"),
    ({"model_version": "v2"}, "version_distinta_al_registro"),
    ({"feature_version": "fe30"}, "version_distinta_al_registro"),
])
def test_inconsistentes(cambio, motivo):
    p = correr([pred(**cambio)])
    assert (p.loc[0, "estado"], p.loc[0, "motivo"]) == ("inconsistente", motivo)
    assert pd.isna(p.loc[0, "demanda_real"])


def test_repeticion_identica_conserva_la_primera_emitida():
    p = correr([pred(prediction_id=1, execution_id="e2", generated_at=pd.Timestamp("2025-11-02")),
                pred(prediction_id=2, execution_id="e1", generated_at=pd.Timestamp("2025-11-01"))])
    est = p.set_index("prediction_id").estado
    assert est[2] == "valido" and est[1] == "duplicado"
    assert p.set_index("prediction_id").motivo[1] == "repeticion"


def test_duplicado_con_valores_distintos_excluye_ambas():
    p = correr([pred(prediction_id=1), pred(prediction_id=2, execution_id="e2", demanda_pred=31.0)])
    assert set(p.estado) == {"inconsistente"}
    assert set(p.motivo) == {"duplicado_con_valores_distintos"}


def test_se_usa_el_pronostico_con_data_cutoff_mas_reciente():
    p = correr([pred(prediction_id=1, data_cutoff=DIA - pd.Timedelta(days=1), demanda_pred=35.0),
                pred(prediction_id=2, execution_id="e2")])
    est = p.set_index("prediction_id")
    assert est.loc[2, "estado"] == "valido"
    assert (est.loc[1, "estado"], est.loc[1, "motivo"]) == ("duplicado", "pronostico_anterior")


def test_no_se_mezclan_horizontes_ni_modelos():
    filas = [pred(prediction_id=1), pred(prediction_id=2, horizonte_min=30, demanda_pred=20.0,
                                         limite_inferior=10.0),
             pred(prediction_id=3, model_id=8, model_version="b1", registro_version="b1",
                  demanda_pred=40.0, limite_inferior=40.0, limite_superior=40.0)]
    p = correr(filas)
    assert (p.estado == "valido").all()                       # distintos objetivos: no son duplicados
    m = {(g["model_id"], g["horizonte_min"]): g for g in cr.agregar(p)}
    assert set(m) == {(7, 30), (7, 60), (8, 60)}
    assert m[(8, 60)]["mae"] == 0.0 and m[(7, 60)]["mae"] == 10.0 and m[(7, 30)]["mae"] == 0.0
    tot = {g["model_id"]: g for g in cr.agregar(p, ())}
    assert tot[7]["n_validas"] == 2 and tot[8]["n_validas"] == 1


def test_predicciones_de_otra_ruta_no_usan_el_real_ajeno():
    p = correr([pred(idruta=2)])
    assert (p.loc[0, "estado"], p.loc[0, "motivo"]) == ("incompleto_cuarentena", "dia_sin_datos")


# ---------------------------------------------------------------- métricas

def test_metricas_conocidas():
    fr = franjas()
    filas = [pred(prediction_id=i, franja_inicio=DIA + pd.Timedelta(hours=h), demanda_pred=v,
                  limite_inferior=0.0, limite_superior=10.0 if v == 0 else max(v, 45.0))
             for i, (h, v) in enumerate([(6, 30.0), (7, 50.0), (8, 40.0), (9, 0.0)], start=1)]
    p = cr.emparejar(pd.DataFrame(filas), fr, MARCA, 3)          # real = 40 en todas
    g = cr.agregar(p, ())[0]
    err = np.array([10.0, 10.0, 0.0, 40.0])
    assert g["n_validas"] == 4
    assert g["mae"] == pytest.approx(err.mean())
    assert g["rmse"] == pytest.approx(math.sqrt((err ** 2).mean()))
    assert g["wape"] == pytest.approx(err.sum() / 160.0)
    smape = np.mean([20 / 70, 20 / 90, 0, 80 / 40])
    assert g["smape"] == pytest.approx(smape)
    assert g["sesgo"] == pytest.approx(np.mean([-10, 10, 0, -40]))
    assert g["cobertura_intervalo"] == pytest.approx(0.75)    # el de 0 con [0, 10] no cubre el real 40
    assert g["mae_ingenuo_7d"] == 0.0 and g["mae_relativo_ingenuo_7d"] is None


def test_sin_observaciones_validas_no_hay_metricas():
    p = correr([pred()], marca=None)
    g = cr.agregar(p, ())[0]
    assert g["n_validas"] == 0 and g["mae"] is None and g["rmse"] is None and g["wape"] is None
    assert "sin observaciones" in g["advertencia"]
    assert g["conteo_estados"]["no_disponible"] == 1


def test_wape_no_definido_si_el_real_suma_cero():
    fr = franjas(pasajeros=0.0)
    p = correr([pred(demanda_pred=0.0, limite_inferior=0.0), pred(prediction_id=2, horizonte_min=30,
                                                                  demanda_pred=5.0, limite_inferior=0.0)], fr)
    g = cr.agregar(p, ())[0]
    assert g["wape"] is None and g["mae"] == 2.5
    assert g["smape"] == pytest.approx((0 + 2.0) / 2)          # 0/0 cuenta como 0; 2·5/5 = 2
    assert "WAPE no definido" in g["advertencia"]


def test_mae_relativo_al_ingenuo_usa_las_mismas_filas():
    fr = franjas()
    fr.loc[fr.franja_inicio < pd.Timestamp("2025-10-02"), "pasajeros"] = 5.0   # semana anterior
    p = correr([pred(), pred(prediction_id=2, franja_inicio=DIA + pd.Timedelta(hours=10))], fr)
    g = cr.agregar(p, ())[0]
    assert g["n_ingenuo_7d"] == 2 and g["mae_ingenuo_7d"] == 20.0     # |40 - 20|
    assert g["mae_relativo_ingenuo_7d"] == pytest.approx(10.0 / 20.0)


def test_agrupaciones_y_rechazo_de_desconocidas():
    p = correr([pred(), pred(prediction_id=2, franja_inicio=DIA + pd.Timedelta(hours=17))])
    por_hora = cr.agregar(p, ("hora",))
    assert [g["hora"] for g in por_hora] == [8, 17]
    assert cr.agregar(p, ("dia", "tipo_dia"))[0]["dia"] == "2025-10-08"
    with pytest.raises(ValueError):
        cr.agregar(p, ("segmento",))


def test_huella_es_reproducible_y_sensible_al_real():
    a = correr([pred(), pred(prediction_id=2, horizonte_min=30)])
    b = correr([pred(prediction_id=2, horizonte_min=30), pred()])
    assert cr.huella(a, {"x": 1}) == cr.huella(b, {"x": 1})
    fr = franjas()
    fr.loc[fr.franja_inicio == DIA + pd.Timedelta(hours=8), "pasajeros"] = 11.0
    c = correr([pred(), pred(prediction_id=2, horizonte_min=30)], fr)
    assert cr.huella(c, {"x": 1}) != cr.huella(a, {"x": 1})


def test_zonas_horarias_se_normalizan_a_bogota():
    filas = [pred(franja_inicio=pd.Timestamp("2025-10-08 13:00", tz="UTC"),
                  data_cutoff=pd.Timestamp("2025-10-08 05:00", tz="UTC"),
                  generated_at=pd.Timestamp("2025-11-01 15:00", tz="UTC"))]
    p = correr(filas)
    assert p.loc[0, "franja_inicio"] == DIA + pd.Timedelta(hours=8) and p.loc[0, "estado"] == "valido"


def test_entrada_vacia():
    p = cr.emparejar(pd.DataFrame(columns=cr.COLUMNAS_PREDICCION), pd.DataFrame(), MARCA, 3)
    assert p.empty and cr.agregar(p) == [] and cr.conteo_estados(p)["valido"] == 0


# ---------------------------------------------------------------- salida para alertas

def test_salida_alimenta_alt_005_007_sin_cambiar_la_regla():
    fr = franjas()
    filas = [pred(prediction_id=1, demanda_pred=80.0, limite_superior=90.0),       # real 40: caída 50%
             pred(prediction_id=2, horizonte_min=30, demanda_pred=12.0, limite_inferior=5.0,
                  limite_superior=15.0)]                                            # real 20: +67%
    obs = cr.para_alt_005_007(correr(filas, fr))
    alertas = motor.alt_005_007(obs, motor.umbrales_por_defecto())
    assert sorted(a["codigo"] for a in alertas) == ["ALT-005", "ALT-007"]


def test_salida_para_alt_005_007_exige_un_solo_modelo():
    p = correr([pred(), pred(prediction_id=2, model_id=8, model_version="b1", registro_version="b1")])
    with pytest.raises(ValueError):
        cr.para_alt_005_007(p)


def test_salida_alimenta_alt_010_con_dos_periodos_prediccion_real():
    reciente = [{"model_id": 7, "idruta": 16, "horizonte_min": 60, "wape": 0.30}]
    referencia = [{"model_id": 7, "idruta": 16, "horizonte_min": 60, "wape": 0.20},
                  {"model_id": 7, "idruta": 16, "horizonte_min": 30, "wape": None}]
    d = cr.para_alt_010(reciente, referencia)
    assert d.to_dict("records") == [{"idruta": 16, "horizonte_min": 60, "wape_reciente": 0.30,
                                     "wape_referencia": 0.20}]
    alertas = motor.alt_010(d, motor.umbrales_por_defecto())
    assert [a["codigo"] for a in alertas] == ["ALT-010"]
    with pytest.raises(ValueError):
        cr.para_alt_010(reciente, [{"model_id": 8, "idruta": 16, "horizonte_min": 60, "wape": 0.2}])


# ---------------------------------------------------------------- m1-1.1: cero operativo (FE comun-1.5)

def horario(model_id=7, ruta=16, desde="05:00", hasta="20:45", tipos=("laboral", "sabado", "dom_festivo")):
    """Horario de operación como el del artefacto: (id_ruta, tipo_dia, hhmm, operativa)."""
    franjas = pd.date_range("2000-01-01", periods=96, freq="15min").strftime("%H:%M")
    filas = [{"model_id": model_id, "id_ruta": str(ruta), "tipo_dia": t, "hhmm": h,
              "operativa": desde <= h <= hasta} for t in ("laboral", "sabado", "dom_festivo")
             for h in franjas]
    h = pd.DataFrame(filas)
    h.loc[~h.tipo_dia.isin(tipos), "operativa"] = False
    return h


def _sin_salidas(fr, inicio, fin):
    return fr[~fr.franja_inicio.between(inicio, fin)]


def test_ventana_operativa_sin_salidas_vale_cero():
    h = pd.Timedelta(hours=1)
    fr = _sin_salidas(franjas(), DIA + 8 * h, DIA + 8.75 * h)
    p = cr.emparejar(pd.DataFrame([pred(limite_inferior=0.0)]), fr, MARCA, 3, horario())
    f = p.iloc[0]
    assert (f.estado, f.motivo) == ("valido", "cero_operativo")
    assert f.demanda_real == 0.0 and f.error == 30.0 and bool(f.dentro_intervalo)
    assert f.programaciones_ventana == 0
    g = cr.agregar(p, ())[0]
    assert g["n_validas"] == 1 and g["mae"] == 30.0 and g["wape"] is None   # Σy = 0


def test_ventana_fuera_de_horario_sin_salidas_no_se_evalua():
    h = pd.Timedelta(hours=1)
    fr = _sin_salidas(franjas(), DIA + 3 * h, DIA + 3.75 * h)
    p = cr.emparejar(pd.DataFrame([pred(franja_inicio=DIA + 3 * h)]), fr, MARCA, 3, horario())
    assert (p.loc[0, "estado"], p.loc[0, "motivo"]) == ("sin_correspondencia", "fuera_de_horario")
    assert pd.isna(p.loc[0, "demanda_real"])


def test_sin_horario_no_se_decide_el_cero():
    h = pd.Timedelta(hours=1)
    fr = _sin_salidas(franjas(), DIA + 8 * h, DIA + 8.75 * h)
    p = cr.emparejar(pd.DataFrame([pred()]), fr, MARCA, 3, horario=None)
    assert (p.loc[0, "estado"], p.loc[0, "motivo"]) == ("sin_correspondencia", "sin_servicio")
    # el horario es por modelo: el modelo 8 no tiene horario aunque el 7 sí
    p = cr.emparejar(pd.DataFrame([pred(), pred(prediction_id=2, model_id=8, model_version="b1",
                                               registro_version="b1")]), fr, MARCA, 3, horario(model_id=7))
    m = p.set_index("model_id").motivo
    assert m[7] == "cero_operativo" and m[8] == "sin_servicio"


@pytest.mark.parametrize("caso, estado, motivo", [
    ("dia_no_cargado", "incompleto_cuarentena", "dia_sin_datos"),
    ("franja_null", "incompleto_cuarentena", "franja_sin_medicion"),
    ("sin_consolidar", "no_disponible", "sin_consolidar"),
    ("sin_marca", "no_disponible", "sin_marca_agua"),
])
def test_faltantes_y_cuarentena_nunca_se_vuelven_cero(caso, estado, motivo):
    h = pd.Timedelta(hours=1)
    fr, marca = franjas(), MARCA
    if caso == "dia_no_cargado":
        fr = fr[fr.franja_inicio < DIA]                      # el día no está cargado
    elif caso == "franja_null":
        fr = _sin_salidas(fr, DIA + 8 * h, DIA + 8.75 * h)
        fr = pd.concat([fr, pd.DataFrame([{"idruta": 16, "franja_inicio": DIA + 8.5 * h, "pasajeros": np.nan,
                                           "programaciones": 1, "programaciones_validas": 0}])])
    elif caso == "sin_consolidar":
        fr = _sin_salidas(fr, DIA + 8 * h, DIA + 8.75 * h)
        marca = pd.Timestamp("2025-10-11 08:30")
    else:
        fr = _sin_salidas(fr, DIA + 8 * h, DIA + 8.75 * h)
        marca = None
    p = cr.emparejar(pd.DataFrame([pred()]), fr, marca, 3, horario())
    assert (p.loc[0, "estado"], p.loc[0, "motivo"]) == (estado, motivo)
    assert pd.isna(p.loc[0, "demanda_real"])


def test_horario_usa_el_tipo_de_dia_del_calendario_fe():
    """2025-08-07 es festivo (dom_festivo en el calendario del FE): con un horario que solo opera
    los días laborales, ese jueves la ventana sin salidas no es un cero operativo; el 06 sí."""
    jue, mie = pd.Timestamp("2025-08-07"), pd.Timestamp("2025-08-06")
    h = pd.Timedelta(hours=1)
    fr = franjas(dias=("2025-07-30", "2025-07-31", "2025-08-06", "2025-08-07"))
    for d in (jue, mie):
        fr = _sin_salidas(fr, d + 8 * h, d + 8.75 * h)
    filas = [pred(prediction_id=1, franja_inicio=jue + 8 * h, data_cutoff=jue),
             pred(prediction_id=2, franja_inicio=mie + 8 * h, data_cutoff=mie)]
    p = cr.emparejar(pd.DataFrame(filas), fr, pd.Timestamp("2025-09-01"), 3, horario(tipos=("laboral",)))
    m = p.set_index("prediction_id").motivo
    assert m[1] == "fuera_de_horario" and m[2] == "cero_operativo"


def test_ingenuo_usa_el_mismo_cero_operativo():
    h = pd.Timedelta(hours=1)
    semana_antes = DIA - pd.Timedelta(days=7)
    fr = _sin_salidas(franjas(), semana_antes + 8 * h, semana_antes + 8.75 * h)
    p = cr.emparejar(pd.DataFrame([pred()]), fr, MARCA, 3, horario())
    assert p.loc[0, "demanda_ingenuo_7d"] == 0.0 and p.loc[0, "demanda_real"] == 40.0
    p = cr.emparejar(pd.DataFrame([pred()]), fr, MARCA, 3, None)
    assert pd.isna(p.loc[0, "demanda_ingenuo_7d"])          # sin horario no se asume el 0


def test_cero_operativo_alimenta_alt_005_007():
    """Una caída a 0 en franja operativa es exactamente el caso de ALT-005: debe llegar a la regla."""
    h = pd.Timedelta(hours=1)
    fr = _sin_salidas(franjas(), DIA + 8 * h, DIA + 8.75 * h)
    p = cr.emparejar(pd.DataFrame([pred(limite_inferior=0.0)]), fr, MARCA, 3, horario())
    obs = cr.para_alt_005_007(p)
    assert len(obs) == 1 and obs.demanda_real.iloc[0] == 0.0
    assert [a["codigo"] for a in motor.alt_005_007(obs, motor.umbrales_por_defecto())] == ["ALT-005"]
