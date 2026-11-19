"""errores_calidad_por_lote() (G3) con una conexión falsa: sin PostgreSQL.

Se comprueba el SQL y los parámetros que la función ENVÍA, no que PostgreSQL los acepte: eso lo
cubren las pruebas de integración contra una base desechable."""
import re
from types import MappingProxyType

import pytest

from app.api.esquemas import ErrorCalidad
from app.db import repositorio as repo

REGEX = "'^_[0123456789]{3,}$'"
VENTANA = "(d.lote = CAST(:lote AS text))"
EJECUCION = ("(starts_with(d.lote, CAST(:lote AS text))"
             " AND substr(d.lote, char_length(CAST(:lote AS text)) + 1) ~ '^_[0123456789]{3,}$')")
FROM_JOIN = ("FROM ai_frecuencias.data_quality_deteccion d "
             "JOIN ai_frecuencias.data_quality_error e ON e.id = d.evento_id")
COLUMNA = {"idruta": "d.idruta", "regla": "e.regla",
           "tabla_origen": "e.tabla_origen", "reglas_version": "e.reglas_version",
           "causa": "d.causa"}
CAUSA = "más de N pasajeros en un viaje (texto sintético)"


class _Resultado:
    def __init__(self, total=None, filas=()):
        self._total, self._filas = total, list(filas)

    def scalar(self):
        return self._total

    def mappings(self):
        return iter(self._filas)


class ConexionFalsa:
    """Guarda (sql, params) de cada execute y responde como SQLAlchemy: scalar() al conteo,
    mappings() a la página."""

    def __init__(self, total=0, filas=()):
        self.total, self.filas, self.llamadas = total, filas, []

    def execute(self, sentencia, params):
        sql = sentencia.text                       # TextClause de sqlalchemy.text()
        self.llamadas.append((sql, dict(params)))
        if sql.startswith("SELECT count(*) "):
            return _Resultado(total=self.total)
        return _Resultado(filas=self.filas)


def ejecutar(lote="ingesta_20261119T080000", alcance="auto", filtros=None, pagina=1, tamano=50,
             total=0, filas=()):
    c = ConexionFalsa(total, filas)
    resultado = repo.errores_calidad_por_lote(c, lote, alcance, filtros, pagina, tamano)
    assert len(c.llamadas) == 2, "debe ejecutar exactamente conteo + página"
    (sql_conteo, p_conteo), (sql_pagina, p_pagina) = c.llamadas
    assert sql_conteo.startswith("SELECT count(*) ") and not sql_pagina.startswith("SELECT count(*)")
    return resultado, sql_conteo, p_conteo, sql_pagina, p_pagina


def where_de(sql: str) -> str:
    """Tramo 'WHERE ...' sin FROM/JOIN ni ORDER BY."""
    tramo = sql.split(FROM_JOIN + " ", 1)[1]
    return tramo.split(" ORDER BY ", 1)[0]


def profundidad(texto: str, posicion: int) -> int:
    return texto[:posicion].count("(") - texto[:posicion].count(")")


def envuelta(cond: str) -> bool:
    """True si el primer '(' cierra exactamente en el último carácter."""
    if not (cond.startswith("(") and cond.endswith(")")):
        return False
    nivel = 0
    for i, ch in enumerate(cond):
        nivel += ch == "("
        nivel -= ch == ")"
        if nivel == 0 and i < len(cond) - 1:
            return False
    return nivel == 0


# ------------------------------------------------------------------ constantes de alcance

def test_cond_lote_tiene_los_tres_alcances_exactos():
    assert set(repo.COND_LOTE) == {"ventana", "ejecucion", "auto"}
    assert repo.COND_LOTE["ventana"] == VENTANA
    assert repo.COND_LOTE["ejecucion"] == EJECUCION
    assert repo.COND_LOTE["auto"] == "(" + VENTANA + " OR " + EJECUCION + ")"


@pytest.mark.parametrize("alcance", ["ventana", "ejecucion", "auto"])
def test_cada_condicion_va_completa_entre_parentesis(alcance):
    assert envuelta(repo.COND_LOTE[alcance])


def test_expresion_regular_exacta():
    assert repo.COND_LOTE["ejecucion"].count(REGEX) == 1
    assert repo.COND_LOTE["auto"].count(REGEX) == 1
    assert REGEX not in repo.COND_LOTE["ventana"]


def test_cast_y_starts_with():
    assert "d.lote = CAST(:lote AS text)" in repo.COND_LOTE["ventana"]
    assert "starts_with(d.lote, CAST(:lote AS text))" in repo.COND_LOTE["ejecucion"]
    assert "char_length(CAST(:lote AS text))" in repo.COND_LOTE["ejecucion"]


@pytest.mark.parametrize("alcance", ["ventana", "ejecucion", "auto"])
def test_where_de_cada_alcance_sin_filtros(alcance):
    _, sql_conteo, p_conteo, sql_pagina, _ = ejecutar(alcance=alcance)
    assert where_de(sql_conteo) == "WHERE " + repo.COND_LOTE[alcance]
    assert where_de(sql_pagina) == "WHERE " + repo.COND_LOTE[alcance]
    assert p_conteo == {"lote": "ingesta_20261119T080000"}
    for sql in (sql_conteo, sql_pagina):
        assert not re.search(r"\b(I?LIKE)\b", sql, re.IGNORECASE)


# ------------------------------------------------------------------ lote intacto

@pytest.mark.parametrize("lote", ["a_b%\\c", "100%", "x_y", "\\", "  con espacios  ", "ñandú",
                                  "ingesta_20261119T080000_002"])
def test_lote_llega_intacto_como_parametro(lote):
    _, sql_conteo, p_conteo, sql_pagina, p_pagina = ejecutar(lote=lote)
    assert p_conteo["lote"] == lote and p_pagina["lote"] == lote
    assert lote not in sql_conteo and lote not in sql_pagina


# ------------------------------------------------------------------ validaciones

@pytest.mark.parametrize("alcance", ["", "AUTO", "Ventana", "otro", None])
def test_alcance_invalido_da_valueerror_sin_ejecutar(alcance):
    c = ConexionFalsa()
    with pytest.raises(ValueError, match="alcance"):
        repo.errores_calidad_por_lote(c, "L", alcance, None, 1, 50)
    assert c.llamadas == []


@pytest.mark.parametrize("filtros", [{"codigo": "ALT-001"}, {"lote": "x"}, {"regla": "r", "estado": "abierta"},
                                     {"IDRUTA": 16}])
def test_filtro_desconocido_da_valueerror_sin_ejecutar(filtros):
    c = ConexionFalsa()
    with pytest.raises(ValueError, match="filtros no permitidos"):
        repo.errores_calidad_por_lote(c, "L", "auto", filtros, 1, 50)
    assert c.llamadas == []


# ------------------------------------------------------------------ filtros

@pytest.mark.parametrize("clave,valor", [("idruta", 16), ("regla", "pasajeros_extremo"),
                                         ("tabla_origen", "progvehiculos"), ("reglas_version", "comun-1.5")])
def test_cada_filtro_permitido_es_condicion_parametrizada(clave, valor):
    _, sql_conteo, p_conteo, _, _ = ejecutar(alcance="ventana", filtros={clave: valor})
    assert where_de(sql_conteo) == f"WHERE {VENTANA} AND {COLUMNA[clave]} = :{clave}"
    assert p_conteo == {"lote": "ingesta_20261119T080000", clave: valor}
    assert str(valor) not in sql_conteo


def test_todos_los_filtros_en_orden_fijo():
    filtros = {"reglas_version": "comun-1.5", "tabla_origen": "progvehiculos", "regla": "fin_tardio",
               "idruta": 2}
    _, sql_conteo, p_conteo, _, _ = ejecutar(alcance="ventana", filtros=filtros)
    assert where_de(sql_conteo) == (f"WHERE {VENTANA} AND d.idruta = :idruta AND e.regla = :regla"
                                    " AND e.tabla_origen = :tabla_origen AND e.reglas_version = :reglas_version")
    assert p_conteo == {"lote": "ingesta_20261119T080000", **filtros}


@pytest.mark.parametrize("filtros", [None, {}, {"idruta": None, "regla": None,
                                                "tabla_origen": None, "reglas_version": None}])
def test_filtros_vacios_o_none_no_agregan_condiciones(filtros):
    _, sql_conteo, p_conteo, _, _ = ejecutar(alcance="auto", filtros=filtros)
    assert where_de(sql_conteo) == "WHERE " + repo.COND_LOTE["auto"]
    assert p_conteo == {"lote": "ingesta_20261119T080000"}


def test_auto_agrupa_el_or_antes_de_los_filtros():
    _, sql_conteo, _, sql_pagina, _ = ejecutar(alcance="auto", filtros={"regla": "pasajeros_extremo",
                                                                       "idruta": 2})
    esperado = "WHERE " + repo.COND_LOTE["auto"] + " AND d.idruta = :idruta AND e.regla = :regla"
    assert where_de(sql_conteo) == esperado and where_de(sql_pagina) == esperado
    w = where_de(sql_conteo)
    ors = [m.start() for m in re.finditer(r" OR ", w)]
    assert ors and all(profundidad(w, i) >= 1 for i in ors), "el OR debe quedar dentro del paréntesis"
    for filtro in (" AND d.idruta = :idruta", " AND e.regla = :regla"):
        assert profundidad(w, w.index(filtro)) == 0, "los filtros deben quedar fuera del OR"


def test_filtros_calidad_son_los_cinco_esperados_en_orden():
    assert repo.FILTROS_CALIDAD == COLUMNA
    assert list(repo.FILTROS_CALIDAD) == ["idruta", "regla", "tabla_origen", "reglas_version", "causa"]


def test_filtro_causa_individual_en_conteo_y_pagina():
    _, sql_conteo, p_conteo, sql_pagina, p_pagina = ejecutar(alcance="ventana", filtros={"causa": CAUSA})
    esperado = f"WHERE {VENTANA} AND d.causa = :causa"
    assert where_de(sql_conteo) == esperado and where_de(sql_pagina) == esperado
    assert p_conteo == {"lote": "ingesta_20261119T080000", "causa": CAUSA}
    assert p_pagina == {**p_conteo, "lim": 50, "off": 0}
    assert CAUSA not in sql_conteo and CAUSA not in sql_pagina


def test_filtro_causa_combinado_con_regla_en_conteo_y_pagina():
    _, sql_conteo, p_conteo, sql_pagina, p_pagina = ejecutar(
        alcance="auto", filtros={"causa": CAUSA, "regla": "pasajeros_extremo"}, pagina=2, tamano=10)
    esperado = "WHERE " + repo.COND_LOTE["auto"] + " AND e.regla = :regla AND d.causa = :causa"
    assert where_de(sql_conteo) == esperado and where_de(sql_pagina) == esperado
    assert p_conteo == {"lote": "ingesta_20261119T080000", "regla": "pasajeros_extremo", "causa": CAUSA}
    assert p_pagina == {**p_conteo, "lim": 10, "off": 10}
    for filtro in (" AND e.regla = :regla", " AND d.causa = :causa"):
        assert profundidad(esperado, esperado.index(filtro)) == 0, "los filtros deben quedar fuera del OR"


# ------------------------------------------------------------------ conteo y página

def test_conteo_y_pagina_comparten_from_join_where_y_parametros():
    _, sql_conteo, p_conteo, sql_pagina, p_pagina = ejecutar(
        alcance="auto", filtros={"regla": "r", "tabla_origen": "progvehiculos"}, pagina=3, tamano=20)
    assert FROM_JOIN in sql_conteo and FROM_JOIN in sql_pagina
    assert where_de(sql_conteo) == where_de(sql_pagina)
    assert "lim" not in p_conteo and "off" not in p_conteo
    assert p_pagina == {**p_conteo, "lim": 20, "off": 40}


def test_pagina_ordena_por_id_descendente_con_limit_y_offset():
    _, sql_conteo, _, sql_pagina, _ = ejecutar()
    assert sql_pagina.endswith(" ORDER BY d.id DESC LIMIT :lim OFFSET :off")
    assert "ORDER BY" not in sql_conteo and "LIMIT" not in sql_conteo


@pytest.mark.parametrize("pagina,tamano,off", [(1, 50, 0), (2, 50, 50), (3, 20, 40), (1, 1, 0),
                                               (10, 500, 4500)])
def test_pagina_y_tamano_a_offset_y_limit(pagina, tamano, off):
    *_, p_pagina = ejecutar(pagina=pagina, tamano=tamano)
    assert p_pagina["lim"] == tamano and p_pagina["off"] == off


def test_valores_por_defecto_de_pagina_y_tamano():
    c = ConexionFalsa()
    repo.errores_calidad_por_lote(c, "L", "ventana", None)
    assert c.llamadas[1][1]["lim"] == 50 and c.llamadas[1][1]["off"] == 0


def test_columnas_son_los_15_alias_de_errorcalidad():
    _, _, _, sql_pagina, _ = ejecutar()
    columnas = sql_pagina[len("SELECT "):sql_pagina.index(" " + FROM_JOIN)]
    alias = re.findall(r" AS (\w+)", columnas)
    assert alias == list(ErrorCalidad.model_fields)
    for oculto in ("id_original", "ultimo_lote", "ultima_deteccion"):
        assert oculto not in sql_pagina


# ------------------------------------------------------------------ retorno

def test_devuelve_lista_de_dicts_y_total_entero():
    filas = [MappingProxyType({"deteccion_id": 2, "lote": "L_002"}),
             MappingProxyType({"deteccion_id": 1, "lote": "L_001"})]
    (resultado, total), *_ = ejecutar(total=7, filas=filas)
    assert resultado == [{"deteccion_id": 2, "lote": "L_002"}, {"deteccion_id": 1, "lote": "L_001"}]
    assert all(type(f) is dict for f in resultado)
    assert total == 7 and type(total) is int


def test_sin_resultados_devuelve_lista_vacia_y_cero():
    (resultado, total), *_ = ejecutar(total=0, filas=())
    assert resultado == [] and total == 0 and type(total) is int
