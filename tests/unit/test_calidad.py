import numpy as np
import pandas as pd

from app.calidad.capacidad import completar_capacidad
from app.calidad.cuarentena import evaluar_programaciones


def test_prioridad_de_fuentes_de_capacidad():
    veh = pd.DataFrame({"idvehiculo": ["A", "B", "C", "D"],
                        "placa": ["wpw119 ", "UYP726", "XXX111", "YYY222"],
                        "capacidad": [60, 0, 0, 0],
                        "n_sentados": [0, 37, 30, 0], "n_depie": [0, 10, 20, 0]})
    rep = pd.DataFrame({"placa": ["WPW119", "UYP726"], "plazas": [50, 72]})
    v = completar_capacidad(veh, rep).set_index("idvehiculo")
    assert v.loc["A", "capacidad_fuente"] == "catalogo_capacidad" and v.loc["A", "capacidad_completa"] == 60
    assert v.loc["B", "capacidad_fuente"] == "reporte_extreme" and v.loc["B", "capacidad_completa"] == 72
    assert v.loc["C", "capacidad_fuente"] == "catalogo_plazas" and v.loc["C", "capacidad_completa"] == 50
    assert v.loc["D", "capacidad_fuente"] == "sin_dato" and np.isnan(v.loc["D", "capacidad_completa"])
    assert (v.capacidad == [60, 0, 0, 0]).all()   # el original no se toca


def test_cuarentena_con_causa_y_sin_imputar():
    """E02: la ingesta aplica comun-1.5 (no calidad_v1) y registra regla, causa y valor por viaje."""
    p = pd.DataFrame({
        "idprogramacion": [1, 2, 3, 4, 5, 6, 7], "idruta": 16,
        "fechasalida": pd.Timestamp("2024-05-20 06:00"),
        "fechainicio": pd.to_datetime(["2024-05-20 06:00"] * 6 + ["2014-05-20 06:00"]),
        "fechafin": pd.to_datetime(["2024-05-20 10:00"] * 4 + ["2024-05-26 10:00"] + ["2024-05-20 10:00"] * 2),
        "finalizado": "S", "cancelada": "N",
        "numeropasajeros": [90, np.nan, 97199, -3, 80, 70, 60],
        "numerobajadas": [90, 0, 0, 0, 0, 900, 0]})
    original = p.copy()
    validas, q = evaluar_programaciones(p, lote="prueba")
    assert validas.idprogramacion.tolist() == [1, 6]          # 6: bajadas extremas no afectan pasajeros
    reglas = q.groupby("idprogramacion").regla.apply(set).to_dict()
    assert reglas == {2: {"pasajeros_sin_conteo"}, 3: {"pasajeros_extremo"}, 4: {"pasajeros_sin_conteo"},
                      5: {"duracion_mayor_24h", "fin_tardio"}, 6: {"bajadas_extremo"},
                      7: {"inicio_incoherente", "duracion_mayor_24h"}}
    assert q.lote.eq("prueba").all() and q.causa.notna().all()
    assert q.set_index("idprogramacion").loc[3, "valor"] == "97199"
    pd.testing.assert_frame_equal(p, original)                  # no se imputa ni se modifica


def test_una_sola_referencia_de_calidad():
    from app.calidad import cuarentena
    from app.fe31 import rc
    assert cuarentena.REGLAS_VERSION == rc.REGLAS["version"] == "comun-1.5"
    texto = open(cuarentena.__file__, encoding="utf-8").read()
    assert '"CAL-' not in texto and "FACTOR_EXTREMO" not in texto     # sin reglas propias


def test_ingesta_y_feature_engineering_marcan_igual():
    """Los viajes válidos de la ingesta son exactamente los pax_valido del FE (mismas reglas)."""
    from app.fe31 import rc
    from app.fe31.viajes import a_formato_fe
    rng = np.random.default_rng(3)
    n = 2000
    sal = pd.Timestamp("2025-02-03 05:00") + pd.to_timedelta(rng.integers(0, 60 * 24 * 10, n), "min")
    dur = rng.choice([200, 230, -10, 2000, 4000], n, p=[.6, .3, .03, .04, .03])
    p = pd.DataFrame({
        "idprogramacion": np.arange(n), "idruta": rng.choice([2, 16, 21], n), "fechasalida": sal,
        "fechainicio": sal, "fechafin": sal + pd.to_timedelta(dur, "min"),
        "finalizado": rng.choice(["S", "N", " s"], n, p=[.9, .05, .05]),
        "cancelada": rng.choice(["N", "S"], n, p=[.93, .07]),
        "numeropasajeros": rng.choice([50.0, np.nan, -1.0, 600.0], n, p=[.9, .04, .03, .03]),
        "numerobajadas": 10.0})
    validas, q = evaluar_programaciones(p, "l")
    P, _ = rc.aplicar_cuarentena(a_formato_fe(p))
    assert set(validas.idprogramacion) == set(P.id_programacion[P.pax_valido])
    assert set(P.id_programacion[P.pax_bloquea]) <= set(q.idprogramacion)
    assert set(q.idprogramacion[q.regla.eq("pasajeros_sin_conteo")]) == \
        set(P.id_programacion[P.pax_bloquea & ~P.cuarentena_etiqueta])
