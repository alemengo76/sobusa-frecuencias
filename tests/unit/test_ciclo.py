"""Despacho G4 con ingesta y lecturas simuladas, sin conexiones a PostgreSQL."""
from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, call

import pandas as pd
import pytest

from app import ciclo

DESDE = date(2026, 11, 15)
HASTA = date(2026, 11, 20)
MARCA = datetime(2026, 11, 21)


@pytest.fixture
def flujo(monkeypatch):
    eventos = Mock()
    conexion = object()

    @contextmanager
    def consultar_resultados():
        eventos.abrir_lectura()
        try:
            yield conexion
        finally:
            eventos.cerrar_lectura()

    def prohibido(*args, **kwargs):
        pytest.fail("G4 no debe construir ni persistir alertas")

    monkeypatch.setattr(ciclo, "consultar_resultados", consultar_resultados)
    monkeypatch.setattr(ciclo, "obtener_config", lambda: SimpleNamespace(dias_revision=3))
    monkeypatch.setattr(ciclo.ingesta, "correr", eventos.ingesta)
    monkeypatch.setattr(ciclo.repositorio, "viajes_fuente", eventos.viajes)
    monkeypatch.setattr(ciclo.pd, "read_sql", eventos.detecciones)
    monkeypatch.setattr(ciclo.motor, "alt_009", prohibido)
    eventos.viajes.return_value = pd.DataFrame([{
        "idprogramacion": 1, "idruta": 2, "fechasalida": datetime(2026, 11, 16, 6),
        "fechainicio": datetime(2026, 11, 16, 6), "fechafin": datetime(2026, 11, 16, 7),
        "finalizado": "S", "cancelada": "N", "numeropasajeros": None, "numerobajadas": None,
    }])
    eventos.detecciones.return_value = pd.DataFrame([{
        "idprogramacion": 1, "lote": "lote_prueba", "tabla_origen": "progvehiculos",
        "regla": "pasajeros_sin_conteo", "reglas_version": ciclo.motor.cuarentena.REGLAS_VERSION,
    }])
    return eventos, conexion


@pytest.mark.parametrize("pendiente", [False, True])
def test_diagnostica_tras_la_ingesta_con_marca_confirmada(flujo, pendiente):
    eventos, conexion = flujo
    eventos.ingesta.return_value = {"marca_agua": MARCA, "pendiente": pendiente}

    resultado = ciclo.correr(DESDE, HASTA)

    assert [evento[0] for evento in eventos.mock_calls] == [
        "ingesta", "abrir_lectura", "viajes", "detecciones", "cerrar_lectura",
    ]
    eventos.viajes.assert_called_once_with(
        conexion, ciclo.ingesta.RUTAS, datetime(2026, 11, 15), datetime(2026, 11, 20),
    )
    sql, conexion_detecciones = eventos.detecciones.call_args.args
    assert sql.text == ciclo.motor.CONSULTA_DETECCIONES_ALT009
    assert conexion_detecciones is conexion
    assert eventos.detecciones.call_args.kwargs["params"] == {
        "tabla_origen": "progvehiculos", "regla": "pasajeros_sin_conteo",
        "reglas_version": ciclo.motor.cuarentena.REGLAS_VERSION,
    }
    assert resultado["ingesta"] == eventos.ingesta.return_value
    alt009 = resultado["alt_009"]
    assert alt009["estado"] == "diagnosticada"
    assert alt009["diagnostico"]["marca_agua"] == str(MARCA)
    assert alt009["diagnostico"]["corte_consolidacion"] == "2026-11-18 00:00:00"
    assert alt009["cobertura"][0]["cobertura"] == 0
    assert alt009["cobertura"][0]["lotes_sin_conteo"] == {1: ["lote_prueba"]}


def test_fallo_de_ingesta_impide_el_despacho(flujo):
    eventos, _ = flujo
    eventos.ingesta.side_effect = RuntimeError("fallo de ingesta")

    with pytest.raises(RuntimeError, match="fallo de ingesta"):
        ciclo.correr(DESDE, HASTA)

    assert eventos.mock_calls == [call.ingesta()]


def test_sin_marca_confirmada_no_abre_lecturas(flujo):
    eventos, _ = flujo
    eventos.ingesta.return_value = {"marca_agua": None}

    resultado = ciclo.correr(DESDE, HASTA)

    assert resultado["alt_009"] == {"estado": "no_evaluable", "motivo": "sin_marca_confirmada"}
    assert eventos.mock_calls == [call.ingesta()]


def test_fallo_de_diagnostico_preserva_resultado_de_ingesta(flujo):
    eventos, _ = flujo
    eventos.ingesta.return_value = {"marca_agua": MARCA}
    eventos.detecciones.side_effect = RuntimeError("fallo de lectura")

    resultado = ciclo.correr(DESDE, HASTA)

    assert resultado["ingesta"] == eventos.ingesta.return_value
    assert resultado["alt_009"] == {"estado": "error", "motivo": "fallo_diagnostico_alt_009"}
    eventos.cerrar_lectura.assert_called_once_with()


@pytest.mark.parametrize("desde,hasta", [(HASTA, DESDE), (DESDE, DESDE), (MARCA, HASTA)])
def test_intervalo_invalido_no_ejecuta_ingesta(flujo, desde, hasta):
    eventos, _ = flujo

    with pytest.raises(ValueError, match="días completos"):
        ciclo.correr(desde, hasta)

    assert eventos.mock_calls == []
