import math

import pandas as pd
import pytest

from app.ml.falsas import generar_predicciones_falsas
from app.optimizacion.frecuencias import (ParametroInvalido, ParametrosRuta,
                                          frecuencia_observada, recomendar,
                                          recomendar_fila)


def params(**kw):
    base = dict(idruta=16, capacidad_nominal=55, ocupacion_objetivo=0.75, ciclo_min=239,
                factor_carga_max=0.40, flota_tope=99)
    base.update(kw)
    return ParametrosRuta(**base)


def test_formula_ruta16_pico_da_escala_realista():
    # 2.000 subidas por hora en pico (ruta 16 laboral)
    r = recomendar_fila(2000, 60, params())
    assert r["carga_max_estimada_hora"] == 800
    assert r["buses_hora_recomendado"] == math.ceil(800 / (55 * 0.75))  # 20
    assert r["headway_recomendado_min"] == 3.0
    assert r["vehiculos_requeridos"] == 80
    assert r["es_viable"]


def test_sin_factor_de_carga_la_recomendacion_es_absurda():
    r = recomendar_fila(2000, 60, params(factor_carga_max=1.0))
    assert not r["es_viable"]
    assert "tope de flota" in r["motivo_no_viable"]
    assert r["buses_hora_recomendado"] is None   # no se fuerza una cifra


def test_horizonte_convierte_a_demanda_por_hora():
    r30 = recomendar_fila(1000, 30, params())
    r60 = recomendar_fila(2000, 60, params())
    assert r30["demanda_hora"] == r60["demanda_hora"] == 2000


@pytest.mark.parametrize("cap", [0, -5, None])
def test_capacidad_no_positiva_es_error(cap):
    with pytest.raises(ParametroInvalido, match="capacidad"):
        recomendar_fila(100, 60, params(capacidad_nominal=cap))


@pytest.mark.parametrize("occ", [0.5, 0.9])
def test_ocupacion_fuera_de_rango_es_error(occ):
    with pytest.raises(ParametroInvalido, match="ocupación"):
        recomendar_fila(100, 60, params(ocupacion_objetivo=occ))


def test_demanda_cero_mantiene_servicio_minimo():
    r = recomendar_fila(0, 60, params())
    assert r["buses_hora_recomendado"] == 1


def test_headway_bajo_el_minimo_no_es_viable():
    r = recomendar_fila(20000, 60, params(flota_tope=None))
    assert not r["es_viable"] and "headway" in r["motivo_no_viable"]


def test_recomendar_cumple_contrato_y_compara_con_actual():
    pred = generar_predicciones_falsas(seed=1, horas=2)
    actual = pred[["idruta", "franja_inicio"]].drop_duplicates().assign(buses_hora_actual=12)
    par = {r: params(idruta=r) for r in (2, 16, 21)}
    rec = recomendar(pred, par, actual)
    assert len(rec) == len(pred)
    viables = rec[rec.es_viable]
    assert (viables.delta_buses_hora == viables.buses_hora_recomendado - 12).all()


def test_falta_parametro_de_ruta():
    pred = generar_predicciones_falsas(seed=1, horas=1)
    with pytest.raises(ParametroInvalido):
        recomendar(pred, {16: params()})


def test_frecuencia_observada_cuenta_salidas_por_hora():
    salidas = pd.DataFrame({"idruta": 16, "fechasalida": pd.date_range(
        "2025-03-04 06:00", periods=20, freq="3min")})
    f = frecuencia_observada(salidas)
    assert f.loc[f.franja_inicio == pd.Timestamp("2025-03-04 06:00"),
                 "buses_hora_actual"].item() == 20
