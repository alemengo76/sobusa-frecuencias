"""ALT-009 por retraso de ingesta: reglas puras (app/alertas/retraso_ingesta.py), sin base de datos.

Las horas de las políticas de estas pruebas (apertura 00:00, vencimiento 05:00) son valores CONTROLADOS
de prueba: la política operativa real está pendiente de validación (DECISIONES §16.2).
"""
# ruff: noqa: DTZ001  -- las fechas sin zona son hora local de Bogotá a propósito
from datetime import UTC, date, datetime, time

import pytest

from app.alertas import retraso_ingesta as rt

F = "progvehiculos"
P = rt.Politica(F, time(0, 0), time(5, 0), 0, "prueba-v1", date(2026, 11, 1))


def intento(eid, inicio, fin, estado="exitosa", completa=True, valida=True, viajes=10, rutas=(2, 16, 21),
            fuente=F, objetivo=None, declarado=None, evidencia=None):
    return rt.Intento(eid, inicio, fin, estado, fuente, tuple(rutas), objetivo, declarado, valida, completa,
                      viajes, evidencia)


# Verificador SINTÉTICO de autorización: acepta solo esta evidencia. No existe uno aprobado en operación.
EVIDENCIA_SINTETICA = "aut-sintetica-001"


def verificador_sintetico(i):
    return i.evidencia_autorizacion == EVIDENCIA_SINTETICA


def ciclos(intentos, ahora, politica=P, ruta=16, verificar=None):
    rt.marcar_autorizaciones(intentos, verificar)
    cs, no_usados = rt.ciclos_de_ruta(politica, ruta, intentos, ahora)
    return {c.fecha.day: c for c in cs}, no_usados


# ------------------------------------------------------------------ política

def test_sin_politica_completa_no_es_evaluable_y_no_inventa_valores():
    pol, motivo = rt.politica_desde_entorno({}, F)
    assert pol is None and motivo["estado"] == "no_evaluable"
    assert motivo["motivo"] == "politica_operativa_no_configurada" and set(motivo["faltan"]) == set(rt.ENV.values())
    parcial = {rt.ENV["vencimiento"]: "05:00"}
    pol, motivo = rt.politica_desde_entorno(parcial, F)
    assert pol is None and rt.ENV["vencimiento"] not in motivo["faltan"] and rt.ENV["apertura"] in motivo["faltan"]


def test_politica_explicita_y_validaciones():
    env = {rt.ENV["apertura"]: "00:00", rt.ENV["vencimiento"]: "05:00", rt.ENV["tolerancia_min"]: "30",
           rt.ENV["version"]: "prueba", rt.ENV["inicio_monitoreo"]: "2026-11-01"}
    pol, motivo = rt.politica_desde_entorno(env, F)
    assert motivo is None and pol.limite(date(2026, 11, 2)) == datetime(2026, 11, 2, 5, 30)
    for cambio, texto in (({rt.ENV["apertura"]: "06:00"}, "apertura"), ({rt.ENV["vencimiento"]: "5h"}, "hora"),
                          ({rt.ENV["tolerancia_min"]: "-1"}, "negativa"),
                          ({rt.ENV["apertura"]: "04:00", rt.ENV["tolerancia_min"]: "1440"}, "siguiente")):
        pol, motivo = rt.politica_desde_entorno({**env, **cambio}, F)
        assert pol is None and motivo["motivo"] == "politica_operativa_invalida" and texto in motivo["detalle"]


# ------------------------------------------------------------------ clasificación de cada ciclo

@pytest.mark.parametrize("kw, estado, causa", [
    ({}, "cumplido", None),
    ({"viajes": 0}, "cumplido", None),                               # sin novedades con consulta válida
    ({"estado": "fallida"}, "vencido", "fallida"),
    ({"completa": False}, "vencido", "parcial"),
    ({"valida": False, "viajes": 0}, "vencido", "sin_consulta_valida"),   # ausencia de datos inconcluyente
    ({"estado": "en_curso", "fin": None}, "vencido", "en_curso"),
])
def test_resultado_de_un_intento_dentro_del_plazo(kw, estado, causa):
    fin = kw.pop("fin", datetime(2026, 11, 2, 2))
    c, _ = ciclos([intento("a", datetime(2026, 11, 2, 1), fin, **kw)], datetime(2026, 11, 2, 12))
    assert (c[2].estado, c[2].causa) == (estado, causa)
    assert c[2].sin_novedades is (kw.get("viajes") == 0 and estado == "cumplido")


def test_sin_intentos_es_no_iniciada_y_antes_del_plazo_pendiente():
    c, _ = ciclos([], datetime(2026, 11, 2, 4, 59))
    assert (c[1].estado, c[1].causa) == ("vencido", "no_iniciada")
    assert c[2].estado == "pendiente" and c[2].causa is None
    assert 3 not in c                                                # todavía no abre


def test_ingesta_tardia_recupera_su_ciclo_y_no_satisface_el_siguiente():
    """Empieza 04:50 del día 2 (ciclo 2), termina 05:30: el ciclo 2 queda vencido «tardía» y recuperado;
    aunque termina antes del vencimiento del día 3, NO satisface el ciclo 3."""
    i = intento("tarde", datetime(2026, 11, 2, 4, 50), datetime(2026, 11, 2, 5, 30))
    c, _ = ciclos([intento("ok", datetime(2026, 11, 1, 1), datetime(2026, 11, 1, 2)), i],
                  datetime(2026, 11, 3, 12))
    assert (c[2].estado, c[2].causa) == ("vencido", "tardia")
    assert c[2].recuperado_en == datetime(2026, 11, 2, 5, 30) and c[2].recuperado_por == "tarde"
    assert (c[3].estado, c[3].causa) == ("vencido", "no_iniciada")
    assert [x.execution_id for x in c[2].intentos] == ["tarde"] and c[3].intentos == []


def test_intento_despues_del_vencimiento_pertenece_al_ciclo_del_dia():
    c, _ = ciclos([intento("10am", datetime(2026, 11, 2, 10), datetime(2026, 11, 2, 11))], datetime(2026, 11, 3, 12))
    assert [x.execution_id for x in c[2].intentos] == ["10am"] and c[3].intentos == []
    # al vencer no había arrancado nada: la causa es «no_iniciada» y el intento queda como recuperación
    assert (c[2].causa, c[2].recuperado_por, c[3].causa) == ("no_iniciada", "10am", "no_iniciada")


def test_intento_antes_de_la_apertura_es_del_ciclo_anterior():
    pol = rt.Politica(F, time(2, 0), time(5, 0), 0, "prueba-v1", date(2026, 11, 1))
    c, _ = ciclos([intento("1am", datetime(2026, 11, 3, 1), datetime(2026, 11, 3, 1, 30))],
                  datetime(2026, 11, 3, 12), politica=pol)
    assert [x.execution_id for x in c[2].intentos] == ["1am"] and c[3].causa == "no_iniciada"


def test_tolerancia_extiende_el_plazo():
    pol = rt.Politica(F, time(0, 0), time(5, 0), 30, "prueba-v1", date(2026, 11, 1))
    c, _ = ciclos([intento("a", datetime(2026, 11, 2, 4, 50), datetime(2026, 11, 2, 5, 20))],
                  datetime(2026, 11, 2, 12), politica=pol)
    assert c[2].estado == "cumplido" and c[2].limite == datetime(2026, 11, 2, 5, 30)


def test_reintento_con_autorizacion_verificada_cuenta_para_el_ciclo_original():
    r = intento("reintento", datetime(2026, 11, 4, 9), datetime(2026, 11, 4, 9, 30),
                objetivo=date(2026, 11, 2), declarado="jefe de operación", evidencia=EVIDENCIA_SINTETICA)
    c, no_usados = ciclos([r], datetime(2026, 11, 4, 12), verificar=verificador_sintetico)
    assert [x.execution_id for x in c[2].intentos] == ["reintento"] and c[4].intentos == []
    assert (c[2].causa, c[2].recuperado_por) == ("no_iniciada", "reintento")   # empezó fuera de plazo
    assert c[4].causa == "no_iniciada" and no_usados == []


def test_reintento_sin_autorizacion_se_conserva_y_no_satisface():
    r = intento("sin_aut", datetime(2026, 11, 2, 1), datetime(2026, 11, 2, 2), objetivo=date(2026, 11, 2))
    c, no_usados = ciclos([r], datetime(2026, 11, 2, 12))
    assert c[2].estado == "vencido" and c[2].intentos == []
    assert [(n["execution_id"], n["motivo"]) for n in no_usados] == [("sin_aut", "reintento_sin_autorizacion")]
    (ep,) = rt.episodios(16, list(c.values()), [r], F)
    assert ep.recuperado_en is None                                  # tampoco recupera el episodio


@pytest.mark.parametrize("verificar", [None, verificador_sintetico])
def test_nombre_declarado_sin_autorizacion_verificada_no_satisface_ni_recupera(verificar):
    """El nombre escrito en --autorizado-por es solo una declaración: sin verificador aprobado (None) o
    sin evidencia que el verificador acepte, el intento queda para auditoría y no cuenta."""
    r = intento("declarado", datetime(2026, 11, 2, 1), datetime(2026, 11, 2, 2), objetivo=date(2026, 11, 2),
                declarado="jefe de operación", evidencia=None if verificar else EVIDENCIA_SINTETICA)
    c, no_usados = ciclos([r], datetime(2026, 11, 3, 12), verificar=verificar)
    assert c[2].estado == "vencido" and c[2].intentos == [] and c[2].recuperado_en is None
    (n,) = no_usados
    assert n["motivo"] == "autorizacion_declarada_no_verificada" and n["autorizacion_declarada_por"] == "jefe de operación"
    assert n["autorizacion_verificada"] is False
    (ep,) = rt.episodios(16, list(c.values()), [r], F)
    assert ep.recuperado_en is None


def test_otra_fuente_u_otra_ruta_no_cuentan():
    c, _ = ciclos([intento("x", datetime(2026, 11, 2, 1), datetime(2026, 11, 2, 2), fuente="otra"),
                   intento("y", datetime(2026, 11, 2, 1), datetime(2026, 11, 2, 2), rutas=(2,))],
                  datetime(2026, 11, 2, 12))
    assert c[2].causa == "no_iniciada"


def test_horas_con_zona_se_llevan_a_bogota():
    i = intento("utc", datetime(2026, 11, 2, 9, 0, tzinfo=UTC), datetime(2026, 11, 2, 9, 50, tzinfo=UTC))
    i.inicio, i.fin = rt.local(i.inicio), rt.local(i.fin)            # 04:00 y 04:50 en Bogotá
    c, _ = ciclos([i], datetime(2026, 11, 2, 12))
    assert c[2].estado == "cumplido"


def test_intento_desde_fila_exige_evidencia_explicita():
    fila = {"execution_id": "e", "estado": "exitosa", "iniciada_en": datetime(2026, 11, 2, 6, tzinfo=UTC),
            "finalizada_en": datetime(2026, 11, 2, 7, tzinfo=UTC),
            "parametros": {"fuente": F, "alcance": {"rutas": [2, 16, 21]}}, "resumen": {"viajes": 0}}
    i = rt.intento_desde_fila(fila)
    assert i.inicio == datetime(2026, 11, 2, 1) and i.rutas == (2, 16, 21)
    assert not i.satisfactorio and i.resultado() == "sin_consulta_valida"   # faltan consulta_valida/completa


# ------------------------------------------------------------------ episodios e identidad

def _escenario(ahora, extra=()):
    base = [intento("d1", datetime(2026, 11, 1, 1), datetime(2026, 11, 1, 2)),
            intento("d2", datetime(2026, 11, 2, 1), datetime(2026, 11, 2, 1, 30), estado="fallida"),
            *extra]
    cs, _ = rt.ciclos_de_ruta(P, 16, base, ahora)
    return rt.episodios(16, cs, base, F)


def test_un_solo_vencido_no_es_incumplimiento_y_dos_si():
    (ep,) = _escenario(datetime(2026, 11, 2, 12))
    assert not ep.incumplimiento and [c.fecha.day for c in ep.ciclos] == [2]
    (ep,) = _escenario(datetime(2026, 11, 3, 12))                    # día 3 no iniciada
    assert ep.incumplimiento and [c.causa for c in ep.ciclos] == ["fallida", "no_iniciada"]
    assert not ep.truncado and ep.recuperado_en is None


def test_la_identidad_se_conserva_cuando_el_episodio_crece():
    (e3,) = _escenario(datetime(2026, 11, 3, 12))
    (e5,) = _escenario(datetime(2026, 11, 5, 12))
    assert len(e5.ciclos) == 4 and e3.primero.fecha == e5.primero.fecha == date(2026, 11, 2)
    assert rt.clave(F, 16, e3.primero.fecha) == rt.clave(F, 16, e5.primero.fecha)
    assert rt.primer_ciclo_de_clave(rt.clave(F, 16, e3.primero.fecha)) == date(2026, 11, 2)


def test_recuperacion_tardia_parte_la_racha_aunque_no_haya_ciclo_cumplido():
    """Vencen 1 y 2; una ingesta tardía del ciclo 2 (termina el 2 a las 10:00) recupera el episodio. El
    ciclo 3 vence: por sí solo es una racha nueva de 1 (sin incumplimiento); con el 4 suma 2."""
    tarde = intento("tarde", datetime(2026, 11, 2, 9), datetime(2026, 11, 2, 10))
    cs, _ = rt.ciclos_de_ruta(P, 16, [tarde], datetime(2026, 11, 3, 12))
    e1, e2 = rt.episodios(16, cs, [tarde], F)
    assert [c.fecha.day for c in e1.ciclos] == [1, 2] and e1.incumplimiento
    assert (e1.recuperado_en, e1.recuperado_por) == (datetime(2026, 11, 2, 10), "tarde")
    assert [c.fecha.day for c in e2.ciclos] == [3] and not e2.incumplimiento and not e2.truncado
    cs, _ = rt.ciclos_de_ruta(P, 16, [tarde], datetime(2026, 11, 4, 12))
    e1b, e2b = rt.episodios(16, cs, [tarde], F)
    assert [c.fecha.day for c in e1b.ciclos] == [1, 2] and [c.fecha.day for c in e2b.ciclos] == [3, 4]
    assert e2b.incumplimiento and e2b.recuperado_en is None
    # vencimientos originales: al vencer el ciclo 2 no había arrancado nada; el intento queda como recuperación
    assert [c.causa for c in e1b.ciclos] == ["no_iniciada", "no_iniciada"] and e1b.ciclos[1].recuperado_por == "tarde"


def test_recuperacion_y_racha_nueva_tiene_otra_identidad():
    extra = (intento("d4", datetime(2026, 11, 4, 1), datetime(2026, 11, 4, 2)),)   # recupera
    eps = _escenario(datetime(2026, 11, 7, 12), extra)
    assert [len(e.ciclos) for e in eps] == [2, 3]                     # 2-3 y 5-7
    assert eps[0].recuperado_en == datetime(2026, 11, 4, 2) and eps[0].recuperado_por == "d4"
    assert eps[1].recuperado_en is None
    assert rt.clave(F, 16, eps[0].primero.fecha) != rt.clave(F, 16, eps[1].primero.fecha)


def test_racha_que_empieza_en_el_primer_ciclo_observado_queda_marcada():
    cs, _ = rt.ciclos_de_ruta(P, 16, [], datetime(2026, 11, 3, 12))
    (ep,) = rt.episodios(16, cs, [], F)
    assert ep.truncado and ep.incumplimiento


def test_alerta_y_notas_documentan_sin_atribuir_la_causa():
    (ep,) = _escenario(datetime(2026, 11, 3, 12))
    a = rt.alerta(ep, P, rt.clave(F, 16, ep.primero.fecha))
    assert a["codigo"] == "ALT-009" and a["severidad"] == "alta" and a["idruta"] == 16
    assert a["valor_observado"] == 2.0 and a["umbral"] == 2.0 and a["horizonte_min"] is None
    assert a["clave_dedup"].startswith("ALT-009|retraso|progvehiculos|ruta:16|")
    e = a["explicacion"]
    for texto in ("progvehiculos|ruta:16|2026-11-02", "fallida", "no_iniciada", "prueba-v1", "no se atribuye",
                  "no certifica", "Crítica", "pendiente de aprobación", rt.REGLA):
        assert texto in e
    assert rt.nota_crecimiento(ep).startswith(rt.MARCA_NOTA) and "ciclos_vencidos=2" in rt.nota_crecimiento(ep)
