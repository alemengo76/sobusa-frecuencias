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


# ---------------------------------------------------------------- ALT-006 (contrato alt006-v2)

M = pd.Timestamp("2025-12-20 12:00")            # marca de agua de prueba; consolidado: salida < 12-17 12:00


def _sal(filas):
    """filas: (idprogramacion, idruta, salida, inicio, cancelada, finalizado)"""
    return pd.DataFrame(filas, columns=["idprogramacion", "idruta", "fechasalida", "fechainicio", "cancelada",
                                        "finalizado"]).assign(
        fechasalida=lambda d: pd.to_datetime(d.fechasalida), fechainicio=lambda d: pd.to_datetime(d.fechainicio))


def _clase(fila):
    return motor.clasificar_salidas(_sal([fila]), U["ALT-006"], M, 3).clase.iloc[0]


@pytest.mark.parametrize("inicio, clase", [
    ("2025-12-10 06:20", "tardia"),
    ("2025-12-10 06:10", "a_tiempo"),                 # exactamente 10 min: dentro de tolerancia
    ("2025-12-10 06:10:01", "tardia"),
    ("2025-12-10 05:55", "a_tiempo"),                 # negativo dentro del rango: no es error
    ("2025-12-10 08:00", "tardia"),                   # 120 min: todavía tardía
    ("2025-12-10 08:01", "desfase_extremo"),
    ("2025-12-10 03:59", "desfase_extremo"),          # -121 min
    (None, "ausente"),                                # día consolidado sin inicio registrado
])
def test_alt_006_clases_por_desfase(inicio, clase):
    assert _clase((1, 2, "2025-12-10 06:00", inicio, "N", "S")) == clase


def test_alt_006_tolerancia_no_vencida_no_se_evalua_con_o_sin_inicio():
    assert _clase((1, 2, "2025-12-20 11:55", "2025-12-20 11:58", "N", "N")) == "no_evaluable"
    assert _clase((1, 2, "2025-12-20 11:55", None, "N", "N")) == "no_evaluable"
    assert _clase((1, 2, "2025-12-20 11:50", "2025-12-20 11:59", "N", "N")) == "a_tiempo"   # justo vencida


def test_alt_006_sin_inicio_no_consolidado_no_es_ausente():
    assert _clase((1, 2, "2025-12-18 08:00", None, "N", "N")) == "no_evaluable"     # dentro de los 3 días
    assert _clase((1, 2, "2025-12-17 11:59", None, "N", "N")) == "ausente"
    assert _clase((1, 2, "2025-12-17 12:00", None, "N", "N")) == "no_evaluable"     # borde del corte


def test_alt_006_finalizado_no_condiciona_la_evaluacion():
    for fin in ("S", "N", None, "X"):
        assert _clase((1, 2, "2025-12-10 06:00", "2025-12-10 06:25", "N", fin)) == "tardia"
        assert _clase((1, 2, "2025-12-10 06:00", None, "N", fin)) == "ausente"


def test_alt_006_canceladas_excluidas_aunque_tengan_inicio():
    assert _clase((1, 2, "2025-12-10 06:00", None, "S", "N")) == "cancelada"
    assert _clase((1, 2, "2025-12-10 06:00", "2025-12-10 06:40", "S", "N")) == "cancelada"
    assert motor.alt_006(_sal([(1, 2, "2025-12-10 06:00", None, "S", "N")]), U, M, 3) == []


def test_alt_006_estado_invalido_futura_y_fuera_de_alcance():
    assert _clase((1, 2, "2025-12-10 06:00", None, None, "S")) == "estado_invalido"
    assert _clase((1, 2, "2025-12-10 06:00", None, "X", "S")) == "estado_invalido"
    assert _clase((1, 2, "2025-12-20 12:30", None, "N", "N")) == "futura"
    assert motor.clasificar_salidas(_sal([(1, 7, "2025-12-10 06:00", None, "N", "S")]), 10, M, 3).empty


def test_alt_006_duplicados_se_excluyen_todos_y_se_diagnostican():
    s = _sal([(5, 2, "2025-12-10 06:00", "2025-12-10 06:30", "N", "S"),
              (5, 2, "2025-12-10 06:00", "2025-12-10 06:02", "N", "S"),
              (6, 2, "2025-12-10 06:05", "2025-12-10 06:06", "N", "S")])
    c = motor.clasificar_salidas(s, 10, M, 3)
    assert list(c.clase) == ["duplicada", "duplicada", "a_tiempo"]
    assert motor.alt_006(s, U, M, 3) == []                         # ninguna aparición del 5 cuenta
    d = motor.diagnostico_alt_006(s, U, M, 3)
    assert len(d["duplicada"]) == 2 and d["conteo_por_clase"]["duplicada"] == 2


def test_alt_006_una_alerta_por_franja_con_evidencia():
    s = _sal([(11, 2, "2025-12-10 06:00", "2025-12-10 06:20", "N", "S"),     # tardía +20
              (12, 2, "2025-12-10 06:05", None, "N", "N"),                    # sin inicio registrado
              (13, 2, "2025-12-10 06:10", "2025-12-10 06:12", "N", "S"),     # a tiempo
              (14, 2, "2025-12-10 06:14", None, "S", "N"),                    # cancelada
              (15, 2, "2025-12-10 06:30", "2025-12-10 06:50", "N", "S"),     # otra franja
              (16, 16, "2025-12-10 06:00", "2025-12-10 06:30", "N", "S")])   # otra ruta
    a = motor.alt_006(s, U, M, 3)
    assert len(a) == 3 and len({x["clave_dedup"] for x in a}) == 3
    x = [x for x in a if x["idruta"] == 2 and x["franja_inicio"] == pd.Timestamp("2025-12-10 06:00")][0]
    assert x["severidad"] == "alta" and x["valor_observado"] == 20.0 and x["valor_esperado"] == 0
    assert x["umbral"] == 10.0 and x["puntuacion_anomalia"] == round(2 / 3, 3)
    e = x["explicacion"]
    assert "3 programaciones evaluadas" in e and "1 sin inicio registrado (12)" in e and "11 +20 min" in e
    assert "1 canceladas no evaluadas" in e and "alt006-v2" in e and "no prueba que el bus" in e
    assert "no se realizó" not in e
    assert x["clave_dedup"] == motor.clave_dedup("ALT-006", 2, pd.Timestamp("2025-12-10 06:00"), None)


def test_alt_006_solo_ausencias_sin_valor_observado():
    a = motor.alt_006(_sal([(21, 21, "2025-12-10 06:00", None, "N", "N")]), U, M, 3)
    assert len(a) == 1 and a[0]["valor_observado"] is None and a[0]["puntuacion_anomalia"] == 1.0


def test_alt_006_extremos_solo_en_diagnostico():
    s = _sal([(31, 2, "2025-12-10 06:00", "2025-12-10 09:00", "N", "S"),
              (32, 2, "2025-12-10 06:00", "2025-12-10 03:00", "N", "S")])
    assert motor.alt_006(s, U, M, 3) == []
    d = motor.diagnostico_alt_006(s, U, M, 3)
    assert [r["idprogramacion"] for r in d["desfase_extremo"]] == [31, 32]
    assert {r["desfase_min"] for r in d["desfase_extremo"]} == {180.0, -180.0}


def test_alt_006_umbral_viene_de_u_y_no_usa_el_reloj():
    s = _sal([(41, 2, "2025-12-10 06:00", "2025-12-10 06:12", "N", "S")])
    assert len(motor.alt_006(s, U, M, 3)) == 1
    assert motor.alt_006(s, {**U, "ALT-006": 15.0}, M, 3) == []
    assert motor.alt_006(s, U, M, 3) == motor.alt_006(s, U, M, 3)          # determinista


def test_alt_006_entrada_vacia():
    s = _sal([])
    assert motor.alt_006(s, U, M, 3) == [] and motor.diagnostico_alt_006(s, U, M, 3)["conteo_por_clase"] == {}


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


# ---------------------------------------------------------------- ALT-009 información incompleta (alt009-v1)

M9 = pd.Timestamp("2025-12-20 12:00")            # marca de prueba; consolidado: salida < 2025-12-17 12:00
DIA9 = "2025-12-10"


def _viaje(i, pax=50.0, ruta=2, salida=f"{DIA9} 06:00", inicio_min=5, fin_min=180, fin="S", cancelada="N",
           bajadas=10.0):
    """Viaje de viaje_fuente; inicio y fin en minutos desde la salida (None = sin dato)."""
    s = pd.Timestamp(salida)
    return {"idprogramacion": i, "idruta": ruta, "fechasalida": s,
            "fechainicio": None if inicio_min is None else s + pd.Timedelta(minutes=inicio_min),
            "fechafin": None if fin_min is None else s + pd.Timedelta(minutes=fin_min),
            "finalizado": fin, "cancelada": cancelada, "numeropasajeros": pax, "numerobajadas": bajadas}


def _viajes(*filas):
    return pd.DataFrame(list(filas)).assign(fechainicio=lambda d: pd.to_datetime(d.fechainicio),
                                            fechafin=lambda d: pd.to_datetime(d.fechafin))


def _det(*filas):
    """filas: (idprogramacion, lote[, regla[, reglas_version[, tabla_origen]]])"""
    return pd.DataFrame([{"idprogramacion": f[0], "lote": f[1],
                          "regla": f[2] if len(f) > 2 else "pasajeros_sin_conteo",
                          "reglas_version": f[3] if len(f) > 3 else "comun-1.5",
                          "tabla_origen": f[4] if len(f) > 4 else "progvehiculos"} for f in filas])


def _cob(viajes, det=None):
    return motor.cobertura_alt_009(viajes, det, M9, 3)


def test_alt_009_nulo_y_negativo_cuentan_como_sin_conteo():
    v = _viajes(*[_viaje(i) for i in range(1, 9)], _viaje(101, pax=None), _viaje(102, pax=-3.0))
    c = _cob(v, _det((101, "L1"), (102, "L1")))
    assert len(c) == 1 and c.elegibles.iloc[0] == 10 and c.sin_conteo.iloc[0] == 2
    assert c.cobertura.iloc[0] == pytest.approx(0.8)
    assert c.valores_sin_conteo.iloc[0] == {101: None, 102: -3.0}
    a = motor.alt_009(c, U)
    assert len(a) == 1 and "101 (nulo; lotes L1)" in a[0]["explicacion"] and "102 (-3; lotes L1)" in a[0]["explicacion"]


def test_alt_009_conteo_valido_no_cuenta():
    c = _cob(_viajes(*[_viaje(i) for i in range(1, 6)]))
    assert c.cobertura.iloc[0] == 1.0 and c.ids_sin_conteo.iloc[0] == []
    assert motor.alt_009(c, U) == []


@pytest.mark.parametrize("viaje", [
    _viaje(201, pax=None, inicio_min=-2 * 24 * 60),                 # inicio_incoherente
    _viaje(202, pax=None, inicio_min=30, fin_min=10),               # duracion_negativa
    _viaje(203, pax=None, fin_min=25 * 60),                         # duracion_mayor_24h
    _viaje(204, pax=None, fin_min=3 * 24 * 60),                     # fin_tardio (y duración > 24 h)
    _viaje(205, pax=600.0),                                         # pasajeros_extremo
    _viaje(206, bajadas=600.0),                                     # bajadas_extremo
], ids=["inicio_incoherente", "duracion_negativa", "duracion_mayor_24h", "fin_tardio", "pasajeros_extremo",
        "bajadas_extremo"])
def test_alt_009_otras_reglas_de_calidad_no_son_sin_conteo(viaje):
    c = _cob(_viajes(_viaje(1), viaje))
    assert c.sin_conteo.iloc[0] == 0 and c.cobertura.iloc[0] == 1.0
    assert motor.alt_009(c, U) == []


def test_alt_009_no_elegibles_se_excluyen():
    v = _viajes(*[_viaje(i) for i in range(1, 10)], _viaje(301, pax=None, cancelada="S"),
                _viaje(302, pax=None, fin="N"))
    c = _cob(v)
    assert c.elegibles.iloc[0] == 9 and c.sin_conteo.iloc[0] == 0 and c.cobertura.iloc[0] == 1.0


def test_alt_009_umbral_estricto_y_leido_de_u():
    v = _viajes(*[_viaje(i) for i in range(1, 10)], _viaje(401, pax=None))
    c = _cob(v)
    assert c.cobertura.iloc[0] == pytest.approx(0.9)
    assert motor.alt_009(c, U) == []                                  # 0,90 no es < 0,90
    assert len(motor.alt_009(c, {**U, "ALT-009": 0.95})) == 1
    justo = pd.DataFrame({"idruta": [2], "fecha": [pd.Timestamp(DIA9)], "cobertura": [0.8999]})
    assert len(motor.alt_009(justo, U)) == 1


def test_alt_009_varios_en_el_mismo_lote_una_alerta():
    v = _viajes(_viaje(1), _viaje(501, pax=None), _viaje(502, pax=None), _viaje(503, pax=None))
    a = motor.alt_009(_cob(v, _det((501, "L1"), (502, "L1"), (503, "L1"))), U)
    assert len(a) == 1
    e = a[0]["explicacion"]
    assert "3 de 4 viajes elegibles sin conteo usable" in e
    assert all(f"{i} (nulo; lotes L1)" in e for i in (501, 502, 503))


def test_alt_009_mismo_viaje_en_dos_lotes_cuenta_una_vez():
    v = _viajes(_viaje(1), _viaje(601, pax=None))
    c = _cob(v, _det((601, "L2"), (601, "L1"), (601, "L1")))
    assert c.sin_conteo.iloc[0] == 1 and c.lotes_sin_conteo.iloc[0] == {601: ["L1", "L2"]}
    assert "601 (nulo; lotes L1, L2)" in motor.alt_009(c, U)[0]["explicacion"]


def test_alt_009_sin_deteccion_y_detecciones_ajenas():
    v = _viajes(_viaje(1), _viaje(701, pax=None), _viaje(702, pax=None))
    det = _det((702, "L9", "fin_tardio"), (703, "L1"))               # otra regla y viaje sin conteo ausente
    det.loc[len(det)] = {"idprogramacion": 701, "lote": "L0", "regla": "pasajeros_sin_conteo",
                         "reglas_version": "calidad_v1", "tabla_origen": "progvehiculos"}   # otra versión
    c = _cob(v, det)
    assert c.ids_sin_deteccion.iloc[0] == [701, 702]
    assert "701 (nulo; sin detección registrada)" in motor.alt_009(c, U)[0]["explicacion"]
    d = motor.diagnostico_alt_009(v, det, M9, 3)
    assert d["ids_sin_deteccion"] == [701, 702] and d["detecciones_sin_viaje_sin_conteo"] == [703]


@pytest.mark.parametrize("fila, motivo", [
    ((1101, "L1", "pasajeros_sin_conteo", "calidad_v1"), "otra versión de reglas"),
    ((1101, "L1", "pasajeros_sin_conteo", "comun-1.5", "timbradas_anio"), "otra tabla de origen"),
    ((1101, "L1", "pasajeros_extremo"), "otra regla"),
], ids=["version", "tabla_origen", "regla"])
def test_alt_009_solo_detecciones_de_progvehiculos_sin_conteo_comun_1_5(fila, motivo):
    v = _viajes(_viaje(1), _viaje(1101, pax=None))
    c = _cob(v, _det(fila))
    assert c.lotes_sin_conteo.iloc[0] == {1101: []}, motivo
    assert c.ids_sin_deteccion.iloc[0] == [1101]
    assert motor.cuarentena.REGLAS_VERSION == "comun-1.5"


@pytest.mark.parametrize("columna", ["reglas_version", "tabla_origen", "regla", "lote", "idprogramacion"])
def test_alt_009_detecciones_sin_columnas_obligatorias_se_rechazan(columna):
    v = _viajes(_viaje(1), _viaje(1201, pax=None))
    det = _det((1201, "L1")).drop(columns=columna)
    with pytest.raises(ValueError, match=columna):
        _cob(v, det)
    with pytest.raises(ValueError, match=columna):
        motor.diagnostico_alt_009(v, det, M9, 3)


def test_alt_009_consulta_de_detecciones_une_por_evento_id_y_filtra():
    q = " ".join(motor.CONSULTA_DETECCIONES_ALT009.split())
    assert "FROM ai_frecuencias.data_quality_deteccion AS d" in q
    assert "JOIN ai_frecuencias.data_quality_error AS e ON e.id = d.evento_id" in q
    assert "e.tabla_origen = :tabla_origen AND e.regla = :regla AND e.reglas_version = :reglas_version" in q
    assert q.lstrip().startswith("SELECT") and not any(
        p in q.upper() for p in ("INSERT", "UPDATE", "DELETE"))


def test_alt_009_viajes_vacios_validan_las_detecciones():
    vacio = _viajes(_viaje(1)).iloc[0:0]
    for columna in motor.COLUMNAS_DETECCIONES:
        with pytest.raises(ValueError, match=columna):
            motor.cobertura_alt_009(vacio, _det((1401, "L1")).drop(columns=columna), M9, 3)
    valida = motor.cobertura_alt_009(vacio, _det((1401, "L1")), M9, 3)
    sin_fuente = motor.cobertura_alt_009(vacio, None, M9, 3)
    for c in (valida, sin_fuente):
        assert c.empty and list(c.columns)[:5] == ["idruta", "fecha", "elegibles", "sin_conteo", "cobertura"]
        assert motor.alt_009(c, U) == []


def test_alt_009_diagnostico_sin_viajes_reporta_detecciones():
    vacio = _viajes(_viaje(1)).iloc[0:0]
    d = motor.diagnostico_alt_009(vacio, _det((1301, "L1"), (1302, "L2"), (1303, "L3", "fin_tardio")), M9, 3)
    assert d["detecciones_sin_viaje_sin_conteo"] == [1301, 1302]
    assert d["dias_no_consolidados"] == [] and d["ids_sin_deteccion"] == []


def test_alt_009_solo_dias_consolidados():
    v = _viajes(_viaje(800, salida="2025-12-16 23:59"), _viaje(805, pax=None, salida="2025-12-16 22:00"),
                _viaje(801, pax=None, salida="2025-12-17 11:59"), _viaje(802, salida="2025-12-17 11:59"),
                _viaje(803, pax=None, salida="2025-12-17 12:00"), _viaje(804, pax=None, salida="2025-12-19 06:00"))
    c = _cob(v)
    # El 16 termina antes del corte (17 12:00): se evalúa. El 17 está partido por el corte: no se evalúa
    # ni siquiera con sus salidas anteriores a las 12:00.
    assert list(c.fecha) == [pd.Timestamp("2025-12-16")] and c.ids_sin_conteo.iloc[0] == [805]
    d = motor.diagnostico_alt_009(v, None, M9, 3)
    assert d["corte_consolidacion"] == "2025-12-17 12:00:00"
    assert d["dias_no_consolidados"] == [{"idruta": 2, "fecha": "2025-12-17", "viajes": 3},
                                         {"idruta": 2, "fecha": "2025-12-19", "viajes": 1}]
    assert 801 not in d["ids_sin_deteccion"]                          # su día no se evalúa


def test_alt_009_dia_que_termina_justo_en_el_corte_se_evalua():
    marca = pd.Timestamp("2025-12-20 00:00")                         # corte = 2025-12-17 00:00
    v = _viajes(_viaje(851, pax=None, salida="2025-12-16 23:59"), _viaje(852, salida="2025-12-16 06:00"),
                _viaje(853, pax=None, salida="2025-12-17 00:00"))
    c = motor.cobertura_alt_009(v, None, marca, 3)
    assert list(c.fecha) == [pd.Timestamp("2025-12-16")] and c.ids_sin_conteo.iloc[0] == [851]


def test_alt_009_dia_sin_elegibles_va_al_diagnostico():
    v = _viajes(_viaje(901, pax=None, cancelada="S"), _viaje(902, fin="N"))
    assert _cob(v).empty and motor.alt_009(_cob(v), U) == []
    assert motor.diagnostico_alt_009(v, None, M9, 3)["dias_sin_elegibles"] == [{"idruta": 2, "fecha": DIA9}]


def test_alt_009_severidad_alta_clave_y_rutas():
    v = _viajes(_viaje(1), _viaje(1001, pax=None), _viaje(2, ruta=16), _viaje(1002, ruta=16, pax=None),
                _viaje(3, salida="2025-12-11 06:00"), _viaje(1003, pax=None, salida="2025-12-11 06:00"))
    a = motor.alt_009(_cob(v), U)
    assert len(a) == 3 and {x["severidad"] for x in a} == {"alta"}
    assert all(x["puntuacion_anomalia"] < 1 for x in a)              # alta por la regla, no por la puntuación
    x = next(x for x in a if x["idruta"] == 2 and x["franja_inicio"] == pd.Timestamp(DIA9))
    assert x["clave_dedup"] == motor.clave_dedup("ALT-009", 2, pd.Timestamp(DIA9), None)
    assert len({x["clave_dedup"] for x in a}) == 3
    assert x["valor_observado"] == 0.5 and x["valor_esperado"] == 1.0 and x["umbral"] == 0.9
    assert x["puntuacion_anomalia"] == round((0.9 - 0.5) / 0.9, 3)
    assert "alt009-v1" in x["explicacion"] and "2025-12-17 12:00:00" in x["explicacion"]
    assert motor.alt_009(_cob(v), U) == a                             # determinista


def test_alt_009_tabla_antigua_sin_evidencia_sigue_funcionando():
    cob = pd.DataFrame({"idruta": [2], "fecha": [pd.Timestamp("2025-03-02")], "cobertura": [0.70]})
    (a,) = motor.alt_009(cob, U)
    assert a["severidad"] == "alta"
    assert a["explicacion"] == "El 2025-03-02 solo el 70% de los viajes tiene conteo válido."


def test_alt_009_entradas_vacias():
    vacio = _viajes(_viaje(1)).iloc[0:0]
    assert _cob(vacio).empty and motor.alt_009(_cob(vacio), U) == []
    d = motor.diagnostico_alt_009(vacio, None, M9, 3)
    assert d["dias_no_consolidados"] == [] and d["dias_sin_elegibles"] == [] and d["ids_sin_deteccion"] == []


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
