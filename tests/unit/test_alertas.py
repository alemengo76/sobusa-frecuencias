import pandas as pd
import pytest

from app.alertas import motor
from app.alertas.catalogo import CATALOGO

U = motor.umbrales_por_defecto()
CAMPOS = {"codigo", "severidad", "estado", "valor_observado", "valor_esperado", "umbral",
          "puntuacion_anomalia", "explicacion", "recomendacion", "clave_dedup"}


def test_catalogo_completo():
    assert sorted(CATALOGO) == [f"ALT-{i:03d}" for i in range(1, 11)]


def test_alt_001_alta_demanda():
    pred = pd.DataFrame({"idruta": [16, 16], "franja_inicio": pd.to_datetime(["2025-03-04 07:00"] * 2),
                         "horizonte_min": [60, 30], "demanda_pred": [1500, 500],
                         "demanda_referencia": [1000, 500]})
    a = motor.alt_001(pred, U)
    assert len(a) == 1 and a[0]["codigo"] == "ALT-001"
    assert CAMPOS <= set(a[0]) and "50%" in a[0]["explicacion"]


def rec(buses_actual, carga, recomendado=20, viable=True):
    return pd.DataFrame([{"idruta": 16, "franja_inicio": pd.Timestamp("2025-03-04 07:00"),
                          "horizonte_min": 60, "buses_hora_actual": buses_actual,
                          "carga_max_estimada_hora": carga, "capacidad_nominal": 50,
                          "ocupacion_objetivo": 0.75, "buses_hora_recomendado": recomendado,
                          "es_viable": viable}])


def test_alt_002_y_003_sobreocupacion_y_deficit():
    codigos = {a["codigo"] for a in motor.alt_002_003_004(rec(10, 480), U)}  # 96% de ocupación
    assert codigos == {"ALT-002", "ALT-003"}


def test_alt_004_frecuencia_excesiva():
    a = motor.alt_002_003_004(rec(20, 200, recomendado=6), U)  # 20% de ocupación
    assert [x["codigo"] for x in a] == ["ALT-004"]


def test_sin_frecuencia_actual_no_hay_alertas_de_ocupacion():
    assert motor.alt_002_003_004(rec(None, 480), U) == []


def test_alt_005_y_007():
    obs = pd.DataFrame({"idruta": 21, "franja_inicio": pd.to_datetime(["2025-03-04 07:00"] * 3),
                        "horizonte_min": [30, 60, 120], "demanda_pred": [100, 100, 100],
                        "demanda_real": [50, 150, 110]})
    assert [a["codigo"] for a in motor.alt_005_007(obs, U)] == ["ALT-005", "ALT-007"]


def test_alt_006_retraso_y_salida_no_realizada():
    s = pd.DataFrame({"idruta": 2, "idprogramacion": [1, 2, 3],
                      "fechasalida": pd.to_datetime(["2025-03-04 06:00"] * 3),
                      "fechainicio": pd.to_datetime(["2025-03-04 06:20", "2025-03-04 06:02", None]),
                      "cancelada": ["N", "N", "S"]})
    a = motor.alt_006(s, U)
    assert len(a) == 2 and all(x["severidad"] == "alta" for x in a)


def test_alt_008_consolidado_anomalo():
    c = pd.DataFrame({"idruta": [16], "periodo": ["2024"], "subidas_consolidado": [900_000],
                      "subidas_viajes": [7_800_000]})
    a = motor.alt_008(c, U)
    assert len(a) == 1 and "-88%" in a[0]["explicacion"]


def test_alt_009_y_010():
    cob = pd.DataFrame({"idruta": [2, 2], "fecha": pd.to_datetime(["2025-03-01", "2025-03-02"]),
                        "cobertura": [0.95, 0.70]})
    assert len(motor.alt_009(cob, U)) == 1
    d = pd.DataFrame({"idruta": [16], "horizonte_min": [60], "wape_reciente": [0.30],
                      "wape_referencia": [0.20]})
    assert motor.alt_010(d, U)[0]["codigo"] == "ALT-010"


def test_umbral_modificable_sin_recompilar():
    pred = pd.DataFrame({"idruta": [16], "franja_inicio": [pd.Timestamp("2025-03-04 07:00")],
                         "horizonte_min": [60], "demanda_pred": [1200],
                         "demanda_referencia": [1000]})
    assert motor.alt_001(pred, U) == []
    assert len(motor.alt_001(pred, {**U, "ALT-001": 1.1})) == 1


def test_deduplicacion():
    pred = pd.DataFrame({"idruta": [16], "franja_inicio": [pd.Timestamp("2025-03-04 07:00")],
                         "horizonte_min": [60], "demanda_pred": [2000], "demanda_referencia": [1000]})
    a = motor.alt_001(pred, U)
    assert motor.deduplicar(a + a, set()) == a
    assert motor.deduplicar(a, {a[0]["clave_dedup"]}) == []


@pytest.mark.parametrize("actual,nuevo,ok", [("abierta", "reconocida", True),
                                             ("reconocida", "cerrada", True),
                                             ("cerrada", "abierta", False),
                                             ("descartada", "reconocida", False)])
def test_transiciones(actual, nuevo, ok):
    if ok:
        motor.validar_transicion(actual, nuevo)
    else:
        with pytest.raises(motor.TransicionInvalida):
            motor.validar_transicion(actual, nuevo)


# ----------------------------------------------------------------- alertas de frecuencia solo a 60 min

import re  # noqa: E402
from pathlib import Path  # noqa: E402

from app.alertas.catalogo import HORIZONTE_ALERTAS_FRECUENCIA  # noqa: E402

U_V2 = {**U, "ALT-003": 2.0}          # umbral vigente en la base desde la migración 0005


def rec_h(horizonte, buses_actual, carga, recomendado):
    return rec(buses_actual, carga, recomendado).assign(horizonte_min=horizonte)


def codigos(df, u=U_V2):
    return [a["codigo"] for a in motor.alt_002_003_004(df, u)]


def test_horizonte_de_alertas_de_frecuencia_es_60():
    assert HORIZONTE_ALERTAS_FRECUENCIA == 60


@pytest.mark.parametrize("h,esperado", [(30, []), (60, ["ALT-002"]), (120, [])])
def test_alt_002_solo_a_60_min(h, esperado):
    assert codigos(rec_h(h, 10, 480, recomendado=10)) == esperado      # 96 % de ocupación, sin déficit


@pytest.mark.parametrize("h,actual,recomendado,esperado", [
    (30, 10, 14, []),                 # déficit de 4, pero a 30 min no se evalúa
    (120, 10, 14, []),
    (60, 10, 12, ["ALT-003"]),        # déficit de 2 buses/h: alerta con el umbral 2,0
    (60, 10, 11, []),                 # déficit de 1 bus/h: ya no alcanza
])
def test_alt_003_solo_a_60_min_y_desde_2_buses(h, actual, recomendado, esperado):
    assert codigos(rec_h(h, actual, 300, recomendado)) == esperado     # 60 % de ocupación


@pytest.mark.parametrize("h,esperado", [(30, []), (60, ["ALT-004"]), (120, [])])
def test_alt_004_solo_a_60_min(h, esperado):
    assert codigos(rec_h(h, 20, 200, recomendado=6)) == esperado       # 20 % de ocupación


def test_no_borra_recomendaciones_de_otros_horizontes():
    r = pd.concat([rec_h(h, 10, 480, 20) for h in (30, 60, 120)], ignore_index=True)
    copia = r.copy()
    alertas = motor.alt_002_003_004(r, U_V2)
    pd.testing.assert_frame_equal(r, copia)
    assert {a["horizonte_min"] for a in alertas} == {60}
    assert {a["codigo"] for a in alertas} == {"ALT-002", "ALT-003"}


def test_horizonte_none_evalua_todos():
    r = pd.concat([rec_h(h, 10, 480, 20) for h in (30, 60, 120)], ignore_index=True)
    assert {a["horizonte_min"] for a in motor.alt_002_003_004(r, U_V2, horizonte=None)} == {30, 60, 120}


def test_alt_001_sigue_en_todos_los_horizontes():
    pred = pd.DataFrame({"idruta": 16, "franja_inicio": pd.Timestamp("2025-03-04 07:00"),
                         "horizonte_min": [30, 60, 120], "demanda_pred": [1500] * 3,
                         "demanda_referencia": [1000] * 3})
    assert sorted(a["horizonte_min"] for a in motor.alt_001(pred, U_V2)) == [30, 60, 120]


def test_umbral_alt003_nuevo_en_migracion_y_no_en_codigo():
    raiz = Path(__file__).resolve().parents[2]
    sql = (raiz / "migrations" / "sql" / "0005_umbral_alt003_v2.sql").read_text(encoding="utf-8")
    assert re.search(r"'ALT-003',\s*'deficit_buses_hora',\s*2\.0", sql)
    assert "SET vigente = false" in sql and "max(version) + 1" in sql   # versión nueva, la vieja se conserva
    assert CATALOGO["ALT-003"]["valor"] == 1.0                           # el catálogo (semilla) no cambia
    assert "2.0" not in (raiz / "app" / "alertas" / "motor.py").read_text(encoding="utf-8")
