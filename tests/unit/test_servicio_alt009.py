"""Integración de ALT-009 con un doble de persistencia, sin conectar a una base."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from app import servicios
from tests.unit.test_alertas import _det, _viaje, _viajes

DIA = pd.Timestamp("2025-12-16")
MARCA = pd.Timestamp("2025-12-20 12:00")
ABIERTAS = {"abierta", "reconocida", "asignada"}


class Conexion:
    invalidated = False

    def in_transaction(self):
        return True

    def __init__(self):
        self.alertas = {}
        self.historial = []
        self.fallar_historial_alt009 = False

    @contextmanager
    def begin_nested(self):
        anterior = deepcopy((self.alertas, self.historial))
        try:
            yield
        except Exception:
            self.alertas, self.historial = anterior
            raise

    def execute(self, consulta, parametros=None):
        sql = str(consulta)
        p = parametros or {}
        if sql.startswith("SELECT clave_dedup"):
            return [(a["clave_dedup"],) for a in self.alertas.values() if a["estado"] in ABIERTAS]
        if "INSERT INTO alert (" in sql:
            repetida = any(a["clave_dedup"] == p["clave_dedup"] and a["estado"] in ABIERTAS
                           for a in self.alertas.values())
            if repetida:
                return Mock(scalar=Mock(return_value=None))
            aid = len(self.alertas) + 1
            self.alertas[aid] = dict(p, alert_id=aid, estado="abierta")
            return Mock(scalar=Mock(return_value=aid))
        if "INSERT INTO alert_history" in sql:
            if self.fallar_historial_alt009 and self.alertas[p["a"]]["codigo"] == "ALT-009":
                raise RuntimeError("falló la escritura del historial")
            self.historial.append(p.copy())
            return None
        if "SELECT * FROM ai_frecuencias.alert WHERE" in sql:
            return Mock(mappings=Mock(return_value=Mock(first=Mock(return_value=self.alertas[p["a"]]))))
        if "UPDATE alert SET estado" in sql:
            self.alertas[p["a"]]["estado"] = p["e"]
            return None
        if "SELECT * FROM ai_frecuencias.alert_history" in sql:
            return Mock(mappings=Mock(return_value=[h for h in self.historial if h["a"] == p["a"]]))
        raise AssertionError(sql)


@pytest.fixture
def flujo(monkeypatch):
    c = Conexion()
    pred = pd.DataFrame({"idruta": [2], "franja_inicio": [pd.Timestamp("2025-12-22")], "horizonte_min": [60]})
    marca = Mock(return_value={"ultima_actualizacion": MARCA})
    monkeypatch.setattr(servicios.lectura_real, "marca_agua", marca)
    monkeypatch.setattr(servicios, "obtener_config", lambda: SimpleNamespace(dias_revision=3))
    monkeypatch.setattr(servicios.repo, "umbrales_vigentes", lambda _: ({"ALT-009": 0.6}, {"ALT-009": 4}))
    monkeypatch.setattr(servicios.repo, "demanda_franja", lambda *_: pd.Series(dtype=float))
    viajes = _viajes(_viaje(501, None, salida="2025-12-16 06:00"),
                     _viaje(502, 50, salida="2025-12-16 07:00"),
                     _viaje(503, None, salida="2025-12-17 06:00"))

    def viajes_en_rango(_c, rutas, inicio, fin):
        return viajes[viajes.idruta.isin(rutas) & viajes.fechasalida.ge(inicio) & viajes.fechasalida.lt(fin)]

    lectura = Mock(side_effect=viajes_en_rango)
    detecciones = Mock(return_value=_det((501, "L1")))
    monkeypatch.setattr(servicios.repo, "viajes_fuente", lectura)
    monkeypatch.setattr(servicios.pd, "read_sql", detecciones)
    frecuencia = servicios.motor._alerta("ALT-003", 2, 1, 5, 2, 2, "Déficit",
                                          pred.franja_inicio.iloc[0], 60, severidad="alta")
    monkeypatch.setattr(servicios.motor, "alt_001", lambda *_: [])
    monkeypatch.setattr(servicios.motor, "alt_002_003_004", lambda *_: [frecuencia])
    notificar = Mock()
    monkeypatch.setattr(servicios, "notificar", notificar)

    @contextmanager
    def escribir():
        yield c

    monkeypatch.setattr(servicios, "escribir", escribir)
    return SimpleNamespace(c=c, marca=marca, lectura=lectura, detecciones=detecciones, notificar=notificar,
                           evaluar=lambda: servicios.evaluar_alertas(c, "ejecucion", pred, pd.DataFrame()))


def test_alt009_persiste_ultimo_dia_consolidado_y_no_notifica(flujo):
    assert flujo.evaluar() == 2
    flujo.lectura.assert_called_once_with(flujo.c, [2], DIA, DIA + pd.Timedelta(days=1))
    consulta, _ = flujo.detecciones.call_args.args
    assert "e.idprogramacion = ANY(:ids)" in str(consulta)
    assert flujo.detecciones.call_args.kwargs["params"]["ids"] == [501, 502]
    frecuencia, incompleta = flujo.c.alertas.values()
    assert incompleta["codigo"] == "ALT-009"
    assert incompleta["franja_inicio"] == DIA
    assert incompleta["valor_observado"] == 0.5
    assert incompleta["umbral_version"] == 4
    assert "501 (nulo; lotes L1)" in incompleta["explicacion"]
    assert flujo.c.historial == [{"a": 1}, {"a": 2}]
    assert [a["clave_dedup"] for a in flujo.notificar.call_args.args[1]] == [frecuencia["clave_dedup"]]


def test_alt009_sin_marca_conserva_alerta_de_frecuencia(flujo):
    flujo.marca.return_value = None
    assert flujo.evaluar() == 1
    flujo.lectura.assert_not_called()
    flujo.detecciones.assert_not_called()
    assert [a["codigo"] for a in flujo.c.alertas.values()] == ["ALT-003"]


def test_alt009_no_evalua_viajes_de_dia_sin_consolidar(flujo):
    flujo.marca.return_value = {"ultima_actualizacion": pd.Timestamp("2025-12-19 12:00")}
    assert flujo.evaluar() == 1
    flujo.lectura.assert_called_once_with(flujo.c, [2], DIA - pd.Timedelta(days=1), DIA)
    flujo.detecciones.assert_not_called()


def test_alt009_revierte_insercion_parcial_y_conserva_otras_alertas(flujo, caplog):
    flujo.c.fallar_historial_alt009 = True
    assert flujo.evaluar() == 1
    assert [a["codigo"] for a in flujo.c.alertas.values()] == ["ALT-003"]
    assert flujo.c.historial == [{"a": 1}]
    assert "Falló la evaluación de incompletitud ALT-009" in caplog.text


def test_alt009_no_oculta_fallo_al_abrir_savepoint(flujo, monkeypatch):
    monkeypatch.setattr(flujo.c, "begin_nested", Mock(side_effect=RuntimeError("falló SAVEPOINT")))
    with pytest.raises(RuntimeError, match="falló SAVEPOINT"):
        flujo.evaluar()


def test_alt009_deduplica_y_reaparece_tras_cierre_individual(flujo):
    assert flujo.evaluar() == 2
    assert flujo.evaluar() == 0
    anterior = flujo.c.alertas[2]
    otra = dict(anterior, alert_id=3, clave_dedup="otra-clave")
    flujo.c.alertas[3] = otra
    servicios.cambiar_estado_alerta(2, "cerrada", "operador", "revisada", None)
    assert otra["estado"] == "abierta"
    assert all(h["a"] != 3 for h in flujo.c.historial)
    assert flujo.evaluar() == 1
    assert anterior["estado"] == "cerrada"
    assert flujo.c.alertas[4]["clave_dedup"] == anterior["clave_dedup"]
    assert flujo.c.alertas[4]["estado"] == "abierta"
