"""Ciclo diario y persistencia de alertas posteriores, sin PostgreSQL (dependencias simuladas)."""
# ruff: noqa: DTZ001  -- las fechas sin zona son hora local de Bogotá a propósito
import importlib.util
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

if importlib.util.find_spec("sqlalchemy") is None:
    pytest.skip("requiere sqlalchemy", allow_module_level=True)

from app import ciclo_alertas as ciclo  # noqa: E402
from app.alertas import flujo  # noqa: E402
from app.alertas import retraso_ingesta as rt  # noqa: E402

MARCA = datetime(2026, 11, 21)


@pytest.fixture
def pasos(monkeypatch):
    p = MagicMock()
    p._marca_vigente.return_value = datetime(2026, 11, 20)
    p._registrar_inicio.return_value = "eid-ing"
    p._alertas.return_value = {"insertadas": 0}
    for nombre in ("_marca_vigente", "_registrar_inicio", "_cerrar", "_alertas"):
        monkeypatch.setattr(ciclo, nombre, getattr(p, nombre))
    monkeypatch.setattr(ciclo.ingesta, "correr", p.ingesta)
    return p


def test_el_intento_se_registra_antes_de_ingerir_y_se_cierra_con_evidencia(pasos):
    pasos.ingesta.return_value = {"marca_agua": MARCA, "pendiente": False, "viajes": 0, "ventanas": 1}
    r = ciclo.correr()
    assert [c[0] for c in pasos.mock_calls] == ["_marca_vigente", "_registrar_inicio", "ingesta", "_cerrar", "_alertas"]
    parametros = pasos._registrar_inicio.call_args.args[0]
    assert parametros == {"fuente": "progvehiculos", "alcance": {"rutas": [2, 16, 21]}}
    eid, estado, resumen = pasos._cerrar.call_args.args[:3]
    assert (eid, estado) == ("eid-ing", "exitosa")
    assert resumen["consulta_valida"] is True and resumen["completa"] is True and resumen["sin_novedades"] is True
    assert resumen["alcance_consultado"] == {"rutas": [2, 16, 21], "hasta_marca": MARCA}
    assert pasos._alertas.call_args.args[:2] == (datetime(2026, 11, 20), MARCA)
    assert r["execution_id_ingesta"] == "eid-ing"


def test_sin_ventanas_la_ausencia_de_datos_no_acredita_consulta_valida():
    r = ciclo.resumen_ingesta({"ventanas": 0, "viajes": 0, "marca_agua": None})
    assert r["consulta_valida"] is False and r["completa"] is False and r["sin_novedades"] is False


def test_ingesta_pendiente_queda_como_parcial(pasos):
    pasos.ingesta.return_value = {"marca_agua": MARCA, "pendiente": True, "viajes": 5, "ventanas": 2}
    ciclo.correr()
    assert pasos._cerrar.call_args.args[2]["completa"] is False


def test_reintento_manual_registra_ciclo_original_y_solo_la_declaracion(pasos):
    pasos.ingesta.return_value = {"marca_agua": MARCA, "pendiente": False, "viajes": 3, "ventanas": 1}
    ciclo.correr(date(2026, 11, 3), "  jefe de operación ", correlation_id="corrida-x")
    p, cid = pasos._registrar_inicio.call_args.args
    assert p["ciclo_objetivo"] == "2026-11-03" and p["autorizacion_declarada_por"] == "jefe de operación"
    assert "autorizado_por" not in p and "evidencia_autorizacion" not in p      # nada se da por verificado
    assert cid == "corrida-x" and pasos._alertas.call_args.args[2] == "corrida-x"   # se propaga a todo
    assert ciclo.parametros_intento(date(2026, 11, 3), "  ")["autorizacion_declarada_por"] is None


def test_ingesta_fallida_se_registra_verifica_retraso_y_relanza(pasos):
    pasos.ingesta.side_effect = RuntimeError("caída")
    with pytest.raises(RuntimeError, match="caída"):
        ciclo.correr()
    eid, estado, resumen, error = pasos._cerrar.call_args.args
    assert (eid, estado, resumen["completa"], resumen["consulta_valida"]) == ("eid-ing", "fallida", False, False)
    assert "caída" in error
    assert pasos._alertas.call_args.args[1] is None                   # sin marca nueva, pero se verifica


def test_politica_del_entorno_sin_valores_por_defecto(monkeypatch):
    for v in rt.ENV.values():
        monkeypatch.delenv(v, raising=False)
    pol, motivo = ciclo.politica()
    assert pol is None and motivo["motivo"] == "politica_operativa_no_configurada"


def _conexion():
    c = MagicMock()
    c.invalidated = False
    c.in_transaction.return_value = True
    return c


def test_alertas_tras_ingesta_aisla_cada_evaluacion_en_su_savepoint(monkeypatch):
    c = _conexion()
    monkeypatch.setattr(flujo.repo, "crear_ejecucion", lambda *a: "eid-al")
    monkeypatch.setattr(flujo.repo, "cerrar_ejecucion", MagicMock())
    monkeypatch.setattr(flujo, "evaluar_completitud", MagicMock(return_value={"estado": "evaluada", "escritas": 3}))
    monkeypatch.setattr(flujo, "verificar_retraso", MagicMock(return_value={"estado": "evaluada", "insertadas": 3}))
    monkeypatch.setattr(flujo, "evaluar_alt_005_007", MagicMock(side_effect=RuntimeError("m1")))
    monkeypatch.setattr(flujo, "evaluar_alt_010", MagicMock(return_value={"estado": "evaluada", "insertadas": 1}))
    monkeypatch.setattr(flujo, "evaluar_alt_006", MagicMock(return_value={"estado": "evaluada", "insertadas": 2}))
    r = flujo.alertas_tras_ingesta(c, None, MARCA, "cid")
    assert r["alt_005_007"]["estado"] == "error" and "m1" in r["alt_005_007"]["motivo"]
    assert r["alt_009_retraso"]["insertadas"] == 3 and r["alt_010"]["insertadas"] == 1 and r["alt_006"]["insertadas"] == 2
    assert r["alt_008"]["motivo"] == "fuente_no_autorizada_o_no_de_solo_lectura"     # ALT008_REFERENCIA sin habilitar
    assert r["insertadas"] == 6 and r["completitud"]["escritas"] == 3
    assert c.begin_nested.call_count == 6
    flujo.repo.cerrar_ejecucion.assert_called_once()


def test_un_fallo_del_propio_savepoint_se_propaga():
    c = _conexion()
    c.begin_nested.side_effect = RuntimeError("no se pudo abrir el savepoint")
    with pytest.raises(RuntimeError, match="savepoint"):
        flujo.en_savepoint(c, "x", lambda: {"estado": "evaluada"})


def test_persistir_no_notifica_009_y_solo_lo_guardado(monkeypatch):
    candidatas = [{"codigo": "ALT-009", "severidad": "alta", "clave_dedup": "a"},
                  {"codigo": "ALT-010", "severidad": "alta", "clave_dedup": "b"},
                  {"codigo": "ALT-005", "severidad": "media", "clave_dedup": "c"},
                  {"codigo": "ALT-010", "severidad": "alta", "clave_dedup": "d"},     # ya estaba (None)
                  {"codigo": "ALT-010", "severidad": "alta", "clave_dedup": "abierta"}]
    monkeypatch.setattr(flujo.repo, "claves_alertas_abiertas", lambda c: {"abierta"})
    ids = iter([1, 2, 3, None])
    monkeypatch.setattr(flujo.repo, "insertar_alerta", lambda c, e, a, v: next(ids))
    enviado = MagicMock()
    monkeypatch.setattr("app.servicios.notificar", enviado)
    r = flujo.persistir(MagicMock(), "e", candidatas, {})
    assert [a["alert_id"] for a in enviado.call_args.args[1]] == [2]
    assert r == {"candidatas": 5, "tras_deduplicar": 4, "insertadas": 3, "alert_ids": [1, 2, 3], "notificadas": 1}


class _Fuente:
    def __init__(self, origen, autorizada=True, solo_lectura=True, granularidad="ruta_anio"):
        self.origen, self.autorizada, self.solo_lectura, self.granularidad = origen, autorizada, solo_lectura, granularidad

    def leer(self, periodos):
        raise AssertionError("no debe leer si las fuentes no son independientes")


@pytest.mark.parametrize("ref, ev, motivo", [
    (None, _Fuente("public.progvehiculos"), "sin_fuente_consolidado_autorizada"),
    (_Fuente("public.timbradas_anio", autorizada=False), _Fuente("public.progvehiculos"),
     "fuente_no_autorizada_o_no_de_solo_lectura"),
    (_Fuente("public.timbradas_anio", solo_lectura=False), _Fuente("public.progvehiculos"),
     "fuente_no_autorizada_o_no_de_solo_lectura"),
    (_Fuente("ai_frecuencias.demanda_franja"), _Fuente("public.progvehiculos"), "fuente_es_el_agregado_local"),
    (_Fuente("public.timbradas_anio"), _Fuente("ai_frecuencias.viaje_fuente"), "fuente_es_el_agregado_local"),
    (_Fuente("public.progvehiculos"), _Fuente("public.progvehiculos"), "referencia_y_evaluado_son_la_misma_tabla"),
    (_Fuente("public.timbradas_anio", granularidad="ruta_dia"), _Fuente("public.progvehiculos"), "granularidad_distinta"),
])
def test_alt_008_exige_fuentes_independientes_autorizadas_y_de_solo_lectura(ref, ev, motivo):
    c = MagicMock()
    assert flujo.evaluar_alt_008(c, "e", ref, ev, [2024])["motivo"] == motivo
    c.execute.assert_not_called()


def test_alt_008_solo_compara_anios_cerrados_y_consolidados():
    assert flujo.periodos_cerrados_alt008(datetime(2025, 12, 20), 3) == [2023, 2024]
    assert flujo.periodos_cerrados_alt008(datetime(2026, 1, 3), 3) == [2023, 2024]      # corte 31-dic
    assert flujo.periodos_cerrados_alt008(datetime(2026, 1, 4), 3) == [2023, 2024, 2025]
    assert flujo.periodos_cerrados_alt008(None, 3) == []
