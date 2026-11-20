"""Esquemas de respuesta de G3 (GET /api/v1/quality/errors): ErrorCalidad, Paginacion y
PaginaErroresCalidad. Solo validación con Pydantic; no usan base de datos ni la API."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.api.esquemas import ErrorCalidad, PaginaErroresCalidad
from app.api.main import pagina

BOGOTA = timezone(timedelta(hours=-5))

CAMPOS = {
    "deteccion_id", "evento_id", "lote", "detectado_en", "idruta", "causa", "valor_original",
    "tabla_origen", "idprogramacion", "regla", "reglas_version", "valor_normalizado",
    "primer_lote", "primera_deteccion", "nuevo_en_lote",
}
NULABLES = ("idruta", "valor_original", "valor_normalizado")
NO_EXPUESTOS = ("id_original", "ultimo_lote", "ultima_deteccion")


def fila(**cambios) -> dict:
    """Una detección completa, como la devolvería la consulta acordada."""
    base = {
        "deteccion_id": 120, "evento_id": 45, "lote": "ingesta_20261119T080000_002",
        "detectado_en": datetime(2026, 11, 19, 8, 0, 5, tzinfo=BOGOTA), "idruta": 16,
        "causa": "más de 500 pasajeros en un viaje", "valor_original": "612",
        "tabla_origen": "progvehiculos", "idprogramacion": 990_000_001, "regla": "pasajeros_extremo",
        "reglas_version": "comun-1.5", "valor_normalizado": "612",
        "primer_lote": "ingesta_20261118T080000_001",
        "primera_deteccion": datetime(2026, 11, 18, 8, 0, 3, tzinfo=BOGOTA), "nuevo_en_lote": False,
    }
    return {**base, **cambios}


def test_instancia_completa_es_valida():
    e = ErrorCalidad(**fila())
    assert e.model_dump() == fila()
    json = e.model_dump(mode="json")
    assert json["detectado_en"] == "2026-11-19T08:00:05-05:00"
    assert json["primera_deteccion"] == "2026-11-18T08:00:03-05:00"


def test_valor_original_nan_se_conserva_como_texto():
    e = ErrorCalidad(**fila(valor_original="NaN", valor_normalizado=None))
    assert e.valor_original == "NaN" and isinstance(e.valor_original, str)
    assert e.model_dump(mode="json")["valor_original"] == "NaN"
    assert e.valor_normalizado is None


@pytest.mark.parametrize("campo", NULABLES)
def test_campos_nulables_aceptan_none(campo):
    e = ErrorCalidad(**fila(**{campo: None}))
    assert getattr(e, campo) is None
    assert e.model_dump(mode="json")[campo] is None


@pytest.mark.parametrize("campo", sorted(CAMPOS))
def test_falta_cualquier_campo_da_error(campo):
    datos = fila()
    del datos[campo]
    with pytest.raises(ValidationError) as err:
        ErrorCalidad(**datos)
    errores = err.value.errors()
    assert [(e["type"], e["loc"]) for e in errores] == [("missing", (campo,))]


def test_campos_no_expuestos_no_aparecen():
    extra = {"id_original": 7, "ultimo_lote": "ingesta_20261120T080000_001",
             "ultima_deteccion": datetime(2026, 11, 20, 8, 0, tzinfo=BOGOTA)}
    volcado = ErrorCalidad(**fila(**extra)).model_dump()
    assert set(volcado) == CAMPOS
    for campo in NO_EXPUESTOS:
        assert campo not in volcado


def test_pagina_vacia_es_valida():
    p = PaginaErroresCalidad(datos=[], paginacion={"pagina": 1, "tamano": 50, "total": 0, "paginas": 0})
    assert p.model_dump() == {"datos": [], "paginacion": {"pagina": 1, "tamano": 50, "total": 0, "paginas": 0}}


def test_salida_de_pagina_valida_contra_el_esquema():
    vacia = pagina([], 0, 1, 50)
    assert PaginaErroresCalidad.model_validate(vacia).model_dump() == vacia

    una = pagina([fila()], 1, 1, 50)
    assert PaginaErroresCalidad.model_validate(una).model_dump() == una


def test_json_schema_tiene_los_15_campos_obligatorios():
    esquema = ErrorCalidad.model_json_schema()
    assert len(CAMPOS) == 15
    assert set(esquema["properties"]) == CAMPOS
    assert set(esquema["required"]) == CAMPOS
    for campo in NO_EXPUESTOS:
        assert campo not in esquema["properties"]
