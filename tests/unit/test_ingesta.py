"""La agregación es idempotente y marca como incompletas las franjas con cuarentena."""
import importlib.util
import time
import tracemalloc

import numpy as np
import pandas as pd
import pytest

if importlib.util.find_spec("sqlalchemy") is None:
    pytest.skip("requiere sqlalchemy", allow_module_level=True)

from app.calidad.cuarentena import evaluar_programaciones  # noqa: E402
from app.ingesta import agregar_por_franja  # noqa: E402


def viajes(n=8, semilla=0):
    rng = np.random.default_rng(semilla)
    return pd.DataFrame({
        "idprogramacion": np.arange(n), "idruta": 16, "idvehiculo": "V",
        "fechasalida": pd.Timestamp("2025-03-04 06:00") + pd.to_timedelta(rng.integers(0, 60, n), "min"),
        "fechainicio": pd.NaT, "fechafin": pd.NaT, "finalizado": "S", "cancelada": "N",
        "numeropasajeros": rng.integers(20, 120, n).astype(float)})


def test_carga_repetida_no_duplica():
    v = viajes()
    validas, _ = evaluar_programaciones(v, "l1")
    a = agregar_por_franja(validas, v, "l1")
    doble = pd.concat([v, v])
    validas2, _ = evaluar_programaciones(doble.drop_duplicates("idprogramacion"), "l2")
    b = agregar_por_franja(validas2, doble.drop_duplicates("idprogramacion"), "l2")
    assert a.drop(columns="lote").equals(b.drop(columns="lote"))
    assert a.pasajeros.sum() == v.numeropasajeros.sum()


def test_franja_con_cuarentena_queda_sin_medicion():
    v = viajes()
    v.loc[0, "numeropasajeros"] = np.nan
    validas, q = evaluar_programaciones(v, "l1")
    f = agregar_por_franja(validas, v, "l1")
    franja = pd.Timestamp(v.loc[0, "fechasalida"]).floor("15min")
    assert pd.isna(f.set_index("franja_inicio").loc[franja, "pasajeros"])
    assert len(q) == 1


def test_ingesta_100000_registros_tiempo_y_memoria():
    v = viajes(100_000)
    tracemalloc.start()
    t0 = time.perf_counter()
    validas, q = evaluar_programaciones(v, "carga")
    agregar_por_franja(validas, v, "carga")
    segundos = time.perf_counter() - t0
    _, pico = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\n100.000 registros: {segundos:.2f} s, memoria pico {pico / 2**20:.1f} MB")
    assert segundos < 30


# ------------------------------------------------------------------ ventanas de la marca de agua

from datetime import datetime, timedelta  # noqa: E402

from app.ingesta import tramos_contiguos, ventanas_de_marca  # noqa: E402


def test_ventanas_cubren_todo_sin_huecos_ni_solapes():
    a, b = datetime(2023, 1, 1), datetime(2023, 4, 15, 7, 50)
    v = ventanas_de_marca(a, b, 31)
    assert v[0][0] == a and v[-1][1] == b
    assert all(v[i][1] == v[i + 1][0] for i in range(len(v) - 1))
    assert all(fin - ini <= timedelta(days=31) for ini, fin in v)


def test_viaje_liquidado_tarde_no_se_pierde_al_procesar_por_lotes():
    """El error anterior: procesar 3 días de salida avanzaba la marca hasta la liquidación
    más tardía y saltaba cambios de días no procesados. Con ventanas de marca, cada cambio
    cae exactamente en una ventana, sin importar su día de salida."""
    marcas = [datetime(2023, 1, 2, 9), datetime(2023, 1, 9, 7, 50),   # día 1, liquidado tarde
              datetime(2023, 1, 5, 10), datetime(2023, 1, 6, 11)]      # días 4 y 5
    v = ventanas_de_marca(datetime(2023, 1, 1), max(marcas), 3)
    for m in marcas:
        assert sum(ini < m <= fin for ini, fin in v) == 1


def test_tramos_contiguos():
    d = [datetime(2023, 1, 3), datetime(2023, 1, 1), datetime(2023, 1, 2), datetime(2023, 1, 9)]
    assert tramos_contiguos(d) == [(datetime(2023, 1, 1), datetime(2023, 1, 4)),
                                   (datetime(2023, 1, 9), datetime(2023, 1, 10))]
