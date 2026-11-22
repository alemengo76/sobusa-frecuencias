"""RF-017: exportación paginada y serialización CSV/XLSX del tablero (dashboard/exportacion.py).

Sin red, sin base de datos y sin Streamlit: la API se simula en memoria.
"""
import ast
import csv
import io
import math
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from openpyxl import load_workbook

from dashboard import exportacion as ex

RAIZ = Path(__file__).resolve().parents[2]
MODULO = RAIZ / "dashboard" / "exportacion.py"
DDL = RAIZ / "migrations" / "sql" / "0001_esquema.sql"
BOGOTA = timezone(timedelta(hours=-5))


# ------------------------------------------------------------------------------------- utilidades

def alerta(i, **cambios):
    fila = {"alert_id": i, "execution_id": "00000000-0000-0000-0000-000000000001", "codigo": "ALT-001",
            "severidad": "alta", "estado": "abierta", "idruta": 2,
            "franja_inicio": "2026-11-16T05:00:00-05:00", "horizonte_min": 60,
            "valor_observado": 120.5, "valor_esperado": 80.0, "umbral": 100, "umbral_version": 1,
            "puntuacion_anomalia": 1.25, "explicacion": "Demanda prevista alta",
            "recomendacion": "Reforzar la frecuencia", "usuario_asignado": "Persona A",
            "clave_dedup": f"clave-{i}", "creada_en": "2026-11-16T04:00:00-05:00",
            "actualizada_en": "2026-11-16T04:30:00-05:00"}
    fila.update(cambios)
    return fila


class ApiFalsa:
    """Pagina como la API real: filtros de igualdad, ORDER BY id DESC, LIMIT/OFFSET.

    ``mutaciones[k]`` se aplica justo antes de responder la llamada k (0 = primera).
    NO aplica ``desde`` ni ``hasta``: el filtrado por fecha lo hace el servidor (franja_inicio en
    app/db/repositorio.py) y estas pruebas unitarias no lo ejercen; solo comprueban qué se envía.
    """

    FILTROS_IGUALDAD = ("idruta", "estado", "severidad", "codigo")

    def __init__(self, filas, mutaciones=None, columna_id="alert_id"):
        self.columna_id = columna_id
        self.filas = {f[columna_id]: dict(f) for f in filas}
        self.mutaciones = mutaciones or {}
        self.llamadas = []

    def __call__(self, ruta, params):
        k = len(self.llamadas)
        self.llamadas.append((ruta, dict(params)))
        if k in self.mutaciones:
            self.mutaciones[k](self)
        seleccion = [f for f in self.filas.values()
                     if all(str(f[c]) == str(params[c]) for c in self.FILTROS_IGUALDAD if c in params)]
        seleccion.sort(key=lambda f: f[self.columna_id], reverse=True)
        p, t = params["pagina"], params["tamano"]
        return {"datos": [dict(f) for f in seleccion[(p - 1) * t:p * t]],
                "paginacion": {"pagina": p, "tamano": t, "total": len(seleccion),
                               "paginas": (len(seleccion) + t - 1) // t}}

    def paginas_pedidas(self):
        return [params["pagina"] for _, params in self.llamadas]


def api_con(n, **kw):
    return ApiFalsa([alerta(i) for i in range(1, n + 1)], **kw)


def respuesta(datos, pagina=1, tamano=500, total=None):
    return {"datos": datos, "paginacion": {"pagina": pagina, "tamano": tamano,
                                           "total": len(datos) if total is None else total}}


def ids(resultado):
    return resultado.datos["alert_id"].tolist()


def leer_csv(contenido: bytes):
    return list(csv.reader(io.StringIO(contenido.decode("utf-8-sig"), newline="")))


def leer_xlsx(contenido: bytes):
    return load_workbook(io.BytesIO(contenido))


# ------------------------------------------------------------------- paginación: casos normales

def test_1201_filas_lee_tres_paginas_y_verifica_la_primera():
    api = api_con(1201)
    r = ex.traer_todo(api, ex.ALERTAS, {"estado": "abierta"})
    assert api.paginas_pedidas() == [1, 2, 3, 1]
    assert r.solicitudes == 4 and r.total == 1201 and len(r.datos) == 1201
    assert ids(r) == list(range(1201, 0, -1))
    for ruta, params in api.llamadas:
        assert ruta == "alerts"
        assert {k: v for k, v in params.items() if k != "pagina"} == {"estado": "abierta", "tamano": 500}


def test_total_multiplo_exacto_no_pide_pagina_extra():
    api = api_con(1000)
    r = ex.traer_todo(api, ex.ALERTAS)
    assert api.paginas_pedidas() == [1, 2, 1]
    assert len(r.datos) == 1000


def test_una_sola_pagina_tambien_hace_verificacion_de_cierre():
    api = api_con(300)
    r = ex.traer_todo(api, ex.ALERTAS, {"estado": "abierta"})
    assert api.paginas_pedidas() == [1, 1]
    assert r.solicitudes == 2 and len(r.datos) == 300
    assert api.llamadas[0] == api.llamadas[1]          # misma ruta, filtros, página y tamaño


def test_una_sola_pagina_detecta_cambio_de_identificadores_con_total_igual():
    def mutar(a):                                     # entra 301 arriba y sale 1 abajo
        a.filas[301] = alerta(301)
        del a.filas[1]
    api = api_con(300, mutaciones={1: mutar})
    with pytest.raises(ex.PaginacionInconsistente, match="página 1"):
        ex.traer_todo(api, ex.ALERTAS)
    assert api.paginas_pedidas() == [1, 1]


def test_una_sola_pagina_detecta_cambio_del_total():
    api = api_con(300, mutaciones={1: lambda a: a.filas.update({301: alerta(301)})})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(api, ex.ALERTAS)


def test_una_sola_pagina_detecta_salida_del_conjunto_filtrado():
    api = api_con(300, mutaciones={1: lambda a: a.filas[150].update(estado="cerrada")})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(api, ex.ALERTAS, {"estado": "abierta"})


def test_una_sola_pagina_detecta_cambio_de_orden_con_mismos_identificadores():
    p1 = [alerta(i) for i in (3, 2, 1)]
    lecturas = iter([respuesta(p1), respuesta(list(reversed(p1)))])
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(lambda ruta, params: next(lecturas), ex.ALERTAS)


def test_total_cero_devuelve_vacio_con_columnas_de_la_lista_blanca():
    api = api_con(0)
    r = ex.traer_todo(api, ex.ALERTAS)
    assert api.paginas_pedidas() == [1, 1]
    assert r.datos.empty and list(r.datos.columns) == list(ex.ALERTAS.columnas)


def test_sin_filas_detecta_una_insercion_antes_del_cierre():
    api = ApiFalsa([], mutaciones={1: lambda a: a.filas.update({1: alerta(1)})})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(api, ex.ALERTAS)


def test_tamano_de_pagina_personalizado():
    api = api_con(25)
    r = ex.traer_todo(api, ex.ALERTAS, tamano=10)
    assert api.paginas_pedidas() == [1, 2, 3, 1]
    assert ids(r) == list(range(25, 0, -1))


def test_valores_se_conservan_tal_como_los_entrega_la_api():
    api = ApiFalsa([alerta(1, horizonte_min=None, valor_observado=None), alerta(2)])
    r = ex.traer_todo(api, ex.ALERTAS)
    fila = r.datos.set_index("alert_id").loc[1]
    assert fila["horizonte_min"] is None and fila["valor_observado"] is None
    assert r.datos.set_index("alert_id").loc[2, "horizonte_min"] == 60  # no se convierte a 60.0
    assert all(r.datos[c].dtype == object for c in r.datos.columns)


# ---------------------------------------------------------------------- parámetros y filtros

@pytest.mark.parametrize("tamano", [0, 501, -1, True, 10.0])
def test_tamano_invalido_falla_sin_llamar(tamano):
    api = api_con(5)
    with pytest.raises(ValueError):
        ex.traer_todo(api, ex.ALERTAS, tamano=tamano)
    assert api.llamadas == []


@pytest.mark.parametrize("limite", [0, -5, True, 1.5])
def test_limite_invalido_falla_sin_llamar(limite):
    api = api_con(5)
    with pytest.raises(ValueError):
        ex.traer_todo(api, ex.ALERTAS, limite_filas=limite)
    assert api.llamadas == []


@pytest.mark.parametrize("filtros", [
    {"estdo": "abierta"},                      # nombre mal escrito: la API lo ignoraría en silencio
    {"usuario_asignado": "Persona A"},
    {"pagina": 2},
    {"tamano": 10},
    {"estado": "abierto"},                     # valor fuera del CHECK
    {"estado": "ABIERTA"},
    {"severidad": "critica"},
    {"idruta": True},
    {"idruta": 2.0},
    {"codigo": ["ALT-001"]},
])
def test_filtros_invalidos_fallan_sin_llamar(filtros):
    api = api_con(5)
    with pytest.raises(ValueError):
        ex.traer_todo(api, ex.ALERTAS, filtros)
    assert api.llamadas == []


@pytest.mark.parametrize("estado", sorted(ex.ESTADOS_ALERTA))
def test_todos_los_estados_del_ddl_se_aceptan(estado):
    ex.traer_todo(api_con(1), ex.ALERTAS, {"estado": estado})


@pytest.mark.parametrize("severidad", sorted(ex.SEVERIDADES_ALERTA))
def test_todas_las_severidades_del_ddl_se_aceptan(severidad):
    ex.traer_todo(api_con(1), ex.ALERTAS, {"severidad": severidad})


def test_filtros_none_no_se_envian_y_los_demas_van_como_texto():
    api = api_con(1)
    r = ex.traer_todo(api, ex.ALERTAS, {"idruta": 16, "estado": None, "codigo": "ALT-002",
                                        "severidad": None})
    assert api.llamadas[0][1] == {"idruta": "16", "codigo": "ALT-002", "pagina": 1, "tamano": 500}
    assert r.filtros == {"idruta": "16", "codigo": "ALT-002"}


# Filtros de fecha: estas pruebas comprueban solo la VALIDACIÓN y el ENVÍO de desde/hasta a la API.
# El filtrado real (franja_inicio >= desde y < hasta) lo hace el servidor y no se prueba aquí.

def test_desde_sin_zona_falla_sin_llamar():
    api = api_con(1)
    with pytest.raises(ValueError):
        ex.traer_todo(api, ex.ALERTAS, {"desde": datetime(2026, 11, 16)})
    with pytest.raises(ValueError):
        ex.traer_todo(api, ex.ALERTAS, {"hasta": date(2026, 11, 17)})
    assert api.llamadas == []


def test_desde_y_hasta_con_zona_se_envian_en_iso_con_desplazamiento():
    """Comprueba el envío de los parámetros, no que el servidor filtre con ellos."""
    api = api_con(1)
    ex.traer_todo(api, ex.ALERTAS, {"desde": datetime(2026, 11, 16, tzinfo=BOGOTA),
                                    "hasta": datetime(2026, 11, 17, tzinfo=BOGOTA)})
    params = api.llamadas[0][1]
    assert params["desde"] == "2026-11-16T00:00:00-05:00"
    assert params["hasta"] == "2026-11-17T00:00:00-05:00"


def test_el_modulo_no_filtra_por_fecha_en_el_cliente():
    """El módulo confía en el filtrado del servidor: no descarta filas por franja_inicio.

    La ApiFalsa ignora desde/hasta, así que llegan filas fuera del rango y con franja nula, y el
    módulo las exporta tal cual. Esto documenta que aquí no se valida el filtrado real.
    """
    filas = [alerta(1, franja_inicio="2020-01-01T00:00:00-05:00"), alerta(2, franja_inicio=None),
             alerta(3)]
    r = ex.traer_todo(ApiFalsa(filas), ex.ALERTAS,
                      {"desde": datetime(2026, 11, 16, tzinfo=BOGOTA),
                       "hasta": datetime(2026, 11, 17, tzinfo=BOGOTA)})
    assert sorted(ids(r)) == [1, 2, 3]


def test_desde_no_anterior_a_hasta_falla():
    api = api_con(1)
    momento = datetime(2026, 11, 16, tzinfo=BOGOTA)
    with pytest.raises(ValueError):
        ex.traer_todo(api, ex.ALERTAS, {"desde": momento, "hasta": momento})
    assert api.llamadas == []


# ---------------------------------------------------------------------------------------- límite

def test_limite_excedido_se_detecta_con_la_primera_pagina():
    api = api_con(10_001)
    with pytest.raises(ex.LimiteExcedido):
        ex.traer_todo(api, ex.ALERTAS)
    assert api.paginas_pedidas() == [1]


def test_limite_por_defecto_es_10000_y_se_admite_exacto():
    assert ex.LIMITE_FILAS_POR_DEFECTO == 10_000
    api = api_con(10_000)
    r = ex.traer_todo(api, ex.ALERTAS)
    assert len(r.datos) == 10_000 and r.solicitudes == 21


def test_limite_configurable():
    with pytest.raises(ex.LimiteExcedido):
        ex.traer_todo(api_con(51), ex.ALERTAS, limite_filas=50)
    assert len(ex.traer_todo(api_con(50), ex.ALERTAS, limite_filas=50).datos) == 50


# ------------------------------------------------------------------- paginación: inconsistencias

def test_total_cambia_entre_paginas():
    api = api_con(1201, mutaciones={1: lambda a: a.filas.update({1202: alerta(1202)})})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(api, ex.ALERTAS)


def test_crecer_por_encima_del_limite_durante_la_lectura_es_inconsistencia():
    def agregar(a):
        a.filas.update({i: alerta(i) for i in range(10_000, 10_002)})
    api = api_con(9_999, mutaciones={1: agregar})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(api, ex.ALERTAS)


def test_insercion_arriba_y_borrado_abajo_produce_duplicado_detectado():
    def mutar(a):
        a.filas[1202] = alerta(1202)
        del a.filas[1]
    api = api_con(1201, mutaciones={1: mutar})
    with pytest.raises(ex.PaginacionInconsistente, match="repetido"):
        ex.traer_todo(api, ex.ALERTAS)


def test_insercion_despues_de_la_ultima_pagina_la_detecta_la_verificacion_de_cierre():
    def mutar(a):
        a.filas[1202] = alerta(1202)
        del a.filas[1]
    api = api_con(1201, mutaciones={3: mutar})
    with pytest.raises(ex.PaginacionInconsistente, match="página 1"):
        ex.traer_todo(api, ex.ALERTAS)
    assert api.paginas_pedidas() == [1, 2, 3, 1]


def test_cambio_de_estado_en_la_primera_pagina_lo_detecta_la_verificacion_de_cierre():
    api = api_con(1201, mutaciones={3: lambda a: a.filas[1201].update(estado="cerrada")})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(api, ex.ALERTAS, {"estado": "abierta"})


def test_limitacion_conocida_salida_y_entrada_compensadas_no_se_detecta():
    """Fija la limitación documentada: no es una instantánea transaccional.

    Con estado=reconocida, antes de leer la página 3 una alerta ya exportada sale del conjunto
    (pasa a asignada) y una alerta vieja entra (pasa a reconocida). El total no cambia, no hay
    duplicados y la página 1 sigue igual: la exportación pasa, pero omite una fila que sí pertenece
    al conjunto e incluye otra que ya no.
    """
    filas = [alerta(i, estado="reconocida") for i in range(10, 12_001, 10)]  # 1200 filas
    filas.append(alerta(15, estado="abierta"))

    def mutar(a):
        a.filas[5000]["estado"] = "asignada"   # estaba en la página 2, ya leída
        a.filas[15]["estado"] = "reconocida"   # id bajo: entra en el rango de la página 3
    api = ApiFalsa(filas, mutaciones={2: mutar})
    r = ex.traer_todo(api, ex.ALERTAS, {"estado": "reconocida"})
    exportados = set(ids(r))
    assert len(exportados) == 1200
    assert 2000 not in exportados              # sigue siendo reconocida y se omitió
    assert 5000 in exportados                  # ya no es reconocida y se incluyó
    assert ex.AVISO_INSTANTANEA in ex.metadatos(r).values()


def falsa_con_paginas(paginas):
    """get que devuelve respuestas fijas por número de página (incluida la verificación)."""
    llamadas = []

    def get(ruta, params):
        llamadas.append(params["pagina"])
        return paginas[params["pagina"]]
    get.llamadas = llamadas
    return get


def test_pagina_intermedia_corta_es_inconsistencia():
    p1 = [alerta(i) for i in range(10, 5, -1)]
    p2 = [alerta(i) for i in range(5, 2, -1)]          # 3 filas; se esperaban 5
    get = falsa_con_paginas({1: respuesta(p1, 1, 5, 12), 2: respuesta(p2, 2, 5, 12)})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(get, ex.ALERTAS, tamano=5)


def test_pagina_mas_larga_de_lo_pedido_es_inconsistencia():
    p1 = [alerta(i) for i in range(7, 0, -1)]          # 7 filas con tamano 5
    get = falsa_con_paginas({1: respuesta(p1, 1, 5, 12)})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(get, ex.ALERTAS, tamano=5)


def test_total_menor_que_las_filas_de_una_sola_pagina():
    get = falsa_con_paginas({1: respuesta([alerta(2), alerta(1)], total=1)})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(get, ex.ALERTAS)


def test_ids_repetidos_dentro_de_la_primera_pagina():
    get = falsa_con_paginas({1: respuesta([alerta(1), alerta(1)])})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(get, ex.ALERTAS)


# ------------------------------------------------------------------- errores y respuestas inválidas

def test_error_http_en_la_pagina_2_conserva_la_causa_y_no_entrega_nada():
    api = api_con(1201)
    causa = RuntimeError("503 Service Unavailable")

    def get(ruta, params):
        if params["pagina"] == 2:
            raise causa
        return api(ruta, params)
    with pytest.raises(ex.ErrorConsulta) as info:
        ex.traer_todo(get, ex.ALERTAS)
    assert info.value.pagina == 2 and info.value.__cause__ is causa
    assert isinstance(info.value, ex.ErrorExportacion)


@pytest.mark.parametrize("cuerpo", [
    None,
    [],
    {"datos": []},
    {"paginacion": {"pagina": 1, "tamano": 500, "total": 0}},
    {"datos": {}, "paginacion": {"pagina": 1, "tamano": 500, "total": 0}},
    {"datos": [], "paginacion": {"pagina": 1, "tamano": 500, "total": -1}},
    {"datos": [], "paginacion": {"pagina": 1, "tamano": 500, "total": "0"}},
    {"datos": [], "paginacion": {"pagina": 1, "tamano": 500, "total": True}},
    {"datos": [], "paginacion": {"pagina": 2, "tamano": 500, "total": 0}},
    {"datos": [], "paginacion": {"pagina": 1, "tamano": 50, "total": 0}},
    {"datos": ["fila"], "paginacion": {"pagina": 1, "tamano": 500, "total": 1}},
    {"datos": [{"codigo": "ALT-001"}], "paginacion": {"pagina": 1, "tamano": 500, "total": 1}},
    {"datos": [alerta(None)], "paginacion": {"pagina": 1, "tamano": 500, "total": 1}},
    {"datos": [alerta(True)], "paginacion": {"pagina": 1, "tamano": 500, "total": 1}},
])
def test_respuestas_mal_formadas(cuerpo):
    with pytest.raises(ex.RespuestaInvalida):
        ex.traer_todo(lambda ruta, params: cuerpo, ex.ALERTAS)


def test_falta_una_columna_de_la_lista_blanca():
    fila = alerta(1)
    del fila["explicacion"]
    with pytest.raises(ex.RespuestaInvalida, match="explicacion"):
        ex.traer_todo(lambda ruta, params: respuesta([fila]), ex.ALERTAS)


def test_columnas_extra_se_descartan_y_se_informan_solo_por_nombre():
    fila = alerta(1, columna_nueva="valor-secreto")
    r = ex.traer_todo(lambda ruta, params: respuesta([fila]), ex.ALERTAS)
    assert "columna_nueva" not in r.datos.columns
    aviso = [a for a in r.advertencias if a.startswith("Columnas no exportadas")]
    assert aviso == ["Columnas no exportadas: clave_dedup, columna_nueva, usuario_asignado"]
    assert "valor-secreto" not in " ".join(r.advertencias)


def test_usuario_asignado_y_comentario_nunca_se_exportan():
    fila = alerta(1, comentario="Llamar a Persona B")
    r = ex.traer_todo(lambda ruta, params: respuesta([fila]), ex.ALERTAS)
    assert not ex.COLUMNAS_EXCLUIDAS & set(r.datos.columns)
    texto_csv = ex.a_csv(r.datos).decode("utf-8-sig")
    assert "Persona A" not in texto_csv and "Persona B" not in texto_csv
    libro = leer_xlsx(ex.a_xlsx(r.datos, ex.metadatos(r)))
    celdas = [c.value for hoja in libro for fila_x in hoja.iter_rows() for c in fila_x]
    assert not any(isinstance(v, str) and ("Persona A" in v or "Persona B" in v) for v in celdas)


def test_conjunto_rechaza_lista_blanca_con_columnas_excluidas():
    assert not ex.COLUMNAS_EXCLUIDAS & set(ex.ALERTAS.columnas)
    with pytest.raises(ValueError):
        ex.Conjunto("x", "x", "id", ("id", "usuario_asignado"), frozenset())
    with pytest.raises(ValueError):
        ex.Conjunto("x", "x", "id", ("otro",), frozenset())
    with pytest.raises(ValueError):
        ex.Conjunto("x", "x", "id", ("id",), frozenset({"pagina"}))


# ---------------------------------------------------------------- advertencia de franja nula

@pytest.mark.parametrize("filtro", ["desde", "hasta"])
def test_filtro_de_fecha_agrega_la_advertencia_de_franja_nula(filtro):
    momento = datetime(2026, 11, 16, tzinfo=BOGOTA)
    r = ex.traer_todo(api_con(3), ex.ALERTAS, {filtro: momento})
    assert ex.ADVERTENCIA_FRANJA_NULA in r.advertencias
    assert ex.ADVERTENCIA_FRANJA_NULA in ex.metadatos(r).values()


def test_sin_filtro_de_fecha_no_hay_advertencia_de_franja_nula():
    r = ex.traer_todo(api_con(3), ex.ALERTAS, {"estado": "abierta"})
    assert ex.ADVERTENCIA_FRANJA_NULA not in r.advertencias


def test_la_advertencia_de_franja_nula_no_informa_una_cantidad():
    momento = datetime(2026, 11, 16, tzinfo=BOGOTA)
    sin_nulas = ex.traer_todo(api_con(3), ex.ALERTAS, {"desde": momento})
    con_nulas = ex.traer_todo(ApiFalsa([alerta(i, franja_inicio=None) for i in range(1, 6)]),
                              ex.ALERTAS, {"desde": momento})
    texto = lambda r: [a for a in r.advertencias if "franja_inicio" in a]  # noqa: E731
    assert texto(sin_nulas) == texto(con_nulas) == [ex.ADVERTENCIA_FRANJA_NULA]
    assert not re.search(r"\d", ex.ADVERTENCIA_FRANJA_NULA)


def test_metadatos_incluyen_filtros_total_y_limitacion():
    reloj = iter([datetime(2026, 11, 16, 22, 0, tzinfo=timezone.utc),
                  datetime(2026, 11, 16, 22, 1, tzinfo=timezone.utc)])
    r = ex.traer_todo(api_con(3), ex.ALERTAS, {"estado": "abierta", "idruta": 2},
                      reloj=lambda: next(reloj))
    meta = ex.metadatos(r)
    assert meta["conjunto"] == "alertas"
    assert meta["filtros"] == "estado=abierta; idruta=2"
    assert meta["total_filas"] == "3" and meta["solicitudes"] == "2"
    assert meta["iniciado_en"] == "2026-11-16T22:00:00+00:00"
    assert meta["terminado_en"] == "2026-11-16T22:01:00+00:00"
    assert meta["limitacion"] == ex.AVISO_INSTANTANEA


# ---------------------------------------------------------------- DDL: valores y columnas reales

def bloque_alert():
    sql = DDL.read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE alert \((.*?)\n\);", sql, re.S)
    assert m, "No se encontró CREATE TABLE alert en 0001_esquema.sql"
    return m.group(1)


def valores_check(bloque, columna):
    m = re.search(rf"CHECK \({columna} IN \(([^)]*)\)\)", bloque)
    assert m, f"No se encontró el CHECK de {columna}"
    return frozenset(re.findall(r"'([^']*)'", m.group(1)))


def test_estados_y_severidades_coinciden_con_los_check_del_ddl():
    bloque = bloque_alert()
    assert ex.ESTADOS_ALERTA == valores_check(bloque, "estado")
    assert ex.SEVERIDADES_ALERTA == valores_check(bloque, "severidad")


def test_lista_blanca_cubre_el_ddl_salvo_las_columnas_omitidas_a_proposito():
    columnas_ddl = re.findall(r"^\s{4}([a-z_]+)\s+(?:bigserial|uuid|text|integer|smallint|numeric|"
                              r"timestamptz|boolean)\b", bloque_alert(), re.M)
    assert set(ex.ALERTAS.columnas) <= set(columnas_ddl)
    assert set(columnas_ddl) - set(ex.ALERTAS.columnas) == {"usuario_asignado", "clave_dedup"}


# ---------------------------------------------------------------------- fechas e ISO 8601

def test_textos_iso_se_conservan_exactos_en_csv_y_xlsx():
    df = pd.DataFrame({"franja_inicio": ["2026-11-16T05:00:00-05:00", "2026-11-16T10:00:00+00:00"]},
                      dtype=object)
    filas = leer_csv(ex.a_csv(df))
    assert [f[0] for f in filas[1:]] == ["2026-11-16T05:00:00-05:00", "2026-11-16T10:00:00+00:00"]
    hoja = leer_xlsx(ex.a_xlsx(df))["datos"]
    assert [(c.value, c.data_type) for c in hoja["A"][1:]] == [
        ("2026-11-16T05:00:00-05:00", "s"), ("2026-11-16T10:00:00+00:00", "s")]


def test_columna_convertida_con_zona_pasa_a_iso_con_desplazamiento():
    df = pd.DataFrame({"f": pd.to_datetime(["2026-11-16T05:00:00-05:00"]),
                       "u": pd.to_datetime(["2026-11-16T10:00:00Z"])})
    preparado = ex.preparar(df)
    assert preparado.loc[0, "f"] == "2026-11-16T05:00:00-05:00"
    assert preparado.loc[0, "u"] == "2026-11-16T10:00:00+00:00"


def test_fechas_sin_zona_fallan():
    with pytest.raises(ValueError):
        ex.preparar(pd.DataFrame({"f": pd.to_datetime(["2026-11-16 05:00:00"])}))
    with pytest.raises(ValueError):
        ex.preparar(pd.DataFrame({"f": [datetime(2026, 11, 16, 5)]}, dtype=object))


def test_date_pasa_a_iso_y_datetime_python_con_zona_tambien():
    df = pd.DataFrame({"d": [date(2026, 11, 16)], "t": [datetime(2026, 11, 16, 5, tzinfo=BOGOTA)]},
                      dtype=object)
    preparado = ex.preparar(df)
    assert preparado.loc[0, "d"] == "2026-11-16"
    assert preparado.loc[0, "t"] == "2026-11-16T05:00:00-05:00"


def test_xlsx_no_tiene_celdas_de_fecha_ni_de_formula():
    r = ex.traer_todo(ApiFalsa([alerta(1, explicacion="=HYPERLINK(\"http://x\")"), alerta(2)]),
                      ex.ALERTAS)
    df = r.datos.copy()
    df["convertida"] = pd.to_datetime(df["franja_inicio"])
    libro = leer_xlsx(ex.a_xlsx(df, ex.metadatos(r), columna_id="alert_id"))
    tipos = {c.data_type for hoja in libro for fila in hoja.iter_rows() for c in fila}
    assert not tipos & {"d", "f"}


# ---------------------------------------------------------------- neutralización de fórmulas

FORMULAS = ["=1+1", "+57 300 000 0000", "-2+3", "@SUMA(A1)", "\t=1+1", "\tvalor", "\r=1+1", "\rvalor",
            "=", "+", "-", "@", "-5 pasajeros", "+3x", "\t5", "\r-1", "-1\n", "-٣"]
NO_FORMULAS = ["1=1", "texto", "", "'=x", " =1", "a@b.com", "ALT-001", "texto -", "2026-11-16T05:00:00-05:00"]
NUMEROS_TEXTO = ["-12.5", "+3", "-1e3", "+1E+5", ".5", "-.5", "-0", "12."]


@pytest.mark.parametrize("valor", FORMULAS)
def test_textos_con_prefijo_peligroso_se_neutralizan(valor):
    assert ex.neutralizar_formula(valor) == "'" + valor


@pytest.mark.parametrize("valor", NO_FORMULAS + NUMEROS_TEXTO)
def test_textos_seguros_y_numeros_completos_no_cambian(valor):
    assert ex.neutralizar_formula(valor) == valor


@pytest.mark.parametrize("valor", [-3.5, -2, 0, Decimal("-1.5"), np.int64(-2), np.float64(-0.25), True,
                                   None, float("nan")])
def test_valores_no_textuales_no_cambian(valor):
    resultado = ex.neutralizar_formula(valor)
    assert resultado is valor or (isinstance(valor, float) and math.isnan(resultado))


def test_tabulador_y_retorno_de_carro_estan_en_los_prefijos():
    assert set(ex.PREFIJOS_FORMULA) == {"=", "+", "-", "@", "\t", "\r"}


def test_neutralizacion_en_csv_incluye_tabulador_y_retorno_de_carro():
    df = pd.DataFrame({"t": ["=1+1", "\t=2", "\r=3", "-12.5", "@x"], "n": [-3.5, -2, 0, 1, 2]},
                      dtype=object)
    filas = leer_csv(ex.a_csv(df))
    assert [f[0] for f in filas[1:]] == ["'=1+1", "'\t=2", "'\r=3", "-12.5", "'@x"]
    assert [f[1] for f in filas[1:]] == ["-3.5", "-2", "0", "1", "2"]


def test_neutralizacion_en_xlsx_y_numeros_reales_quedan_numericos():
    # "\r=3" se neutraliza sobre el valor original y luego el \r pasa a \n: XML 1.0 §2.11 obliga a
    # cualquier lector a normalizar \r a \n, y sin lxml openpyxl no lo escapa (ver la docstring del
    # módulo). Lo que importa para la seguridad es el apóstrofo inicial, que se conserva.
    df = pd.DataFrame({"t": ["=1+1", "\t=2", "\r=3", "-12.5", "+57 300"],
                       "n": [-3.5, -2, Decimal("-1.5"), np.int64(-7), 0]}, dtype=object)
    hoja = leer_xlsx(ex.a_xlsx(df))["datos"]
    assert [c.value for c in hoja["A"][1:]] == ["'=1+1", "'\t=2", "'\n=3", "-12.5", "'+57 300"]
    assert all(c.data_type == "s" for c in hoja["A"][1:])
    assert [c.value for c in hoja["B"][1:]] == [-3.5, -2, -1.5, -7, 0]
    assert all(c.data_type == "n" for c in hoja["B"][1:])


# Retornos de carro en XLSX: el resultado debe ser el mismo con o sin lxml (el entorno del proyecto
# no tiene lxml; con lxml openpyxl escapa \r como &#13;, sin lxml lo escribe literal y se lee \n).

@pytest.mark.parametrize("original, esperado", [
    ("\r=3", "'\n=3"),           # se neutraliza ANTES de normalizar; al revés quedaría sin apóstrofo
    ("\rvalor", "'\nvalor"),
    ("\r\n=1", "'\n=1"),
    ("a\r\nb", "a\nb"),
    ("a\rb\rc", "a\nb\nc"),
    ("a\nb", "a\nb"),
])
def test_xlsx_neutraliza_y_despues_normaliza_retornos_de_carro(original, esperado):
    df = pd.DataFrame({"t": [original]}, dtype=object)
    celda = leer_xlsx(ex.a_xlsx(df))["datos"]["A2"]
    assert celda.value == esperado and celda.data_type == "s"


def test_normalizar_fin_de_linea():
    assert ex.normalizar_fin_de_linea("a\r\nb\rc\nd") == "a\nb\nc\nd"
    assert ex.normalizar_fin_de_linea("sin cambios") == "sin cambios"


def test_xml_interno_del_xlsx_no_tiene_retornos_de_carro(monkeypatch):
    import zipfile
    df = pd.DataFrame({"c\rol": ["\r=3", "a\r\nb"]}, dtype=object)
    contenido = ex.a_xlsx(df, {"clave": "\rvalor", "filtros": "x\r\ny"})
    with zipfile.ZipFile(io.BytesIO(contenido)) as z:
        hojas = [n for n in z.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml")]
        assert len(hojas) == 2
        for nombre in hojas:
            xml = z.read(nombre)
            assert b"\r" not in xml and b"&#13;" not in xml, nombre
    # El XML base ya se interpreta con LF. El ajuste preserva valores y tipos.
    from openpyxl import load_workbook
    with monkeypatch.context() as m:
        m.setattr(ex, "_normalizar_xml_xlsx", lambda datos: datos)
        base = ex.a_xlsx(df, {"clave": "\rvalor", "filtros": "x\r\ny"})
    hojas = lambda datos: [[[(celda.value, celda.data_type) for celda in fila] for fila in hoja]
                            for hoja in load_workbook(io.BytesIO(datos)).worksheets]
    assert hojas(contenido) == hojas(base)


def test_csv_conserva_los_retornos_de_carro():
    df = pd.DataFrame({"t": ["a\r\nb", "\r=3"]}, dtype=object)
    assert [f[0] for f in leer_csv(ex.a_csv(df))[1:]] == ["a\r\nb", "'\r=3"]


def test_xlsx_es_igual_con_y_sin_lxml():
    """Emula el entorno real (sin lxml) con OPENPYXL_LXML=False y lo compara con el valor por defecto."""
    codigo = (
        "import io, sys\n"
        f"sys.path.insert(0, {str(RAIZ)!r})\n"
        "import pandas as pd\n"
        "from openpyxl import load_workbook\n"
        "import openpyxl.xml\n"
        "import dashboard.exportacion as m\n"
        "df = pd.DataFrame({'t': ['\\r=3', 'a\\r\\nb', '\\rvalor']}, dtype=object)\n"
        "hoja = load_workbook(io.BytesIO(m.a_xlsx(df)))['datos']\n"
        "print(repr([c.value for c in hoja['A'][1:]]))\n")
    salidas = {}
    for lxml in ("False", "True"):
        entorno = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        entorno.update(PYTHONDONTWRITEBYTECODE="1", OPENPYXL_LXML=lxml)
        r = subprocess.run([sys.executable, "-I", "-c", codigo], capture_output=True, text=True,
                           env=entorno, cwd=str(RAIZ / "tests"), timeout=120, check=True)
        salidas[lxml] = r.stdout.strip()
    assert salidas["False"] == salidas["True"] == repr(["'\n=3", "a\nb", "'\nvalor"])


def test_encabezados_y_metadatos_tambien_se_neutralizan():
    df = pd.DataFrame({"=columna": ["a"]}, dtype=object)
    assert leer_csv(ex.a_csv(df))[0] == ["'=columna"]
    libro = leer_xlsx(ex.a_xlsx(df, {"filtros": "codigo==cmd", "=clave": "+valor"}))
    assert libro["datos"]["A1"].value == "'=columna"
    meta = {f[0].value: f[1].value for f in libro["exportacion"].iter_rows(min_row=2)}
    assert meta == {"filtros": "codigo==cmd", "'=clave": "'+valor"}


@pytest.mark.parametrize("dtype", [object, "str"])
def test_neutralizacion_con_tipo_object_y_str_de_pandas(dtype):
    df = pd.DataFrame({"t": ["=1", "a", None]}, dtype=dtype)
    preparado = ex.preparar(df)
    assert preparado["t"].tolist() == ["'=1", "a", None]


# ----------------------------------------------------------------------------------- CSV y XLSX

def test_csv_ida_y_vuelta():
    df = pd.DataFrame({"alert_id": [1, 2], "explicacion": ["Señalización, \"Calle 72\"\ny más", None],
                       "valor": [120.5, None], "es_viable": [True, False]}, dtype=object)
    contenido = ex.a_csv(df)
    assert contenido.startswith(b"\xef\xbb\xbf")
    assert contenido.endswith(b"\r\n") and b"False\r\n" in contenido
    assert leer_csv(contenido) == [["alert_id", "explicacion", "valor", "es_viable"],
                                   ["1", "Señalización, \"Calle 72\"\ny más", "120.5", "True"],
                                   ["2", "", "", "False"]]


def test_csv_es_determinista():
    df = ex.traer_todo(api_con(30), ex.ALERTAS).datos
    assert ex.a_csv(df) == ex.a_csv(df)


def test_xlsx_ida_y_vuelta():
    r = ex.traer_todo(ApiFalsa([alerta(1, horizonte_min=None), alerta(2)]), ex.ALERTAS)
    libro = leer_xlsx(ex.a_xlsx(r.datos, ex.metadatos(r), columna_id="alert_id"))
    assert libro.sheetnames == ["datos", "exportacion"]
    hoja = libro["datos"]
    filas = [[c.value for c in f] for f in hoja.iter_rows()]
    assert filas[0] == list(ex.ALERTAS.columnas)
    assert [f[0] for f in filas[1:]] == [2, 1]
    por_id = {f[0]: dict(zip(filas[0], f)) for f in filas[1:]}
    assert por_id[1]["horizonte_min"] is None and por_id[2]["horizonte_min"] == 60
    assert por_id[2]["valor_observado"] == 120.5 and hoja["H2"].data_type == "n"
    meta = {f[0].value: f[1].value for f in libro["exportacion"].iter_rows(min_row=2)}
    assert meta["limitacion"] == ex.AVISO_INSTANTANEA


def test_xlsx_sin_metadatos_tiene_solo_la_hoja_datos():
    assert leer_xlsx(ex.a_xlsx(pd.DataFrame({"a": [1]}))).sheetnames == ["datos"]


def test_xlsx_booleanos_y_vacio():
    hoja = leer_xlsx(ex.a_xlsx(pd.DataFrame({"b": [True, False]}, dtype=object)))["datos"]
    assert [(c.value, c.data_type) for c in hoja["A"][1:]] == [(True, "b"), (False, "b")]
    vacio = leer_xlsx(ex.a_xlsx(pd.DataFrame(columns=list(ex.ALERTAS.columnas))))["datos"]
    assert [c.value for c in vacio[1]] == list(ex.ALERTAS.columnas) and vacio.max_row == 1


def test_xlsx_caracter_de_control_falla_con_columna_e_id():
    df = pd.DataFrame({"alert_id": [7], "explicacion": ["texto\x01oculto"]}, dtype=object)
    with pytest.raises(ex.ErrorExportacion, match=r"'explicacion'.*id 7"):
        ex.a_xlsx(df, columna_id="alert_id")
    assert "\x01" in leer_csv(ex.a_csv(df))[1][1]  # el CSV lo conserva


def test_xlsx_texto_demasiado_largo_falla():
    df = pd.DataFrame({"t": ["x" * (ex.MAX_CARACTERES_CELDA_XLSX + 1)]}, dtype=object)
    with pytest.raises(ex.ErrorExportacion):
        ex.a_xlsx(df)


# Política de no finitos (igual en CSV y XLSX): los nulos de Python/pandas (None, NaN de float o
# numpy, pd.NA, pd.NaT) se exportan como celda vacía; los infinitos y cualquier Decimal no finito,
# incluido Decimal("NaN"), se rechazan con ErrorExportacion.

NO_FINITOS = [float("inf"), float("-inf"), np.float64("inf"), np.float32("-inf"), Decimal("Infinity"),
              Decimal("-Infinity"), Decimal("NaN"), Decimal("-NaN"), Decimal("sNaN")]
NULOS = [None, float("nan"), np.nan, np.float32("nan"), pd.NA, pd.NaT]
EXPORTAR = {"csv": lambda df: ex.a_csv(df, columna_id="alert_id"),
            "xlsx": lambda df: ex.a_xlsx(df, columna_id="alert_id")}


@pytest.mark.parametrize("formato", sorted(EXPORTAR))
@pytest.mark.parametrize("valor", NO_FINITOS, ids=repr)
def test_valores_no_finitos_se_rechazan(formato, valor):
    df = pd.DataFrame({"alert_id": [1, 9], "valor_observado": [1.5, valor]}, dtype=object)
    with pytest.raises(ex.ErrorExportacion, match=r"'valor_observado'.*fila 2, id 9"):
        EXPORTAR[formato](df)


@pytest.mark.parametrize("formato", sorted(EXPORTAR))
def test_infinito_en_columna_float64_se_rechaza(formato):
    df = pd.DataFrame({"alert_id": [1, 2], "v": [1.0, -np.inf]})
    with pytest.raises(ex.ErrorExportacion, match="infinito"):
        EXPORTAR[formato](df)


@pytest.mark.parametrize("valor", NULOS, ids=repr)
def test_nulos_se_exportan_como_celda_vacia_en_csv(valor):
    df = pd.DataFrame({"alert_id": [1], "v": [valor]}, dtype=object)
    assert leer_csv(ex.a_csv(df))[1] == ["1", ""]


@pytest.mark.parametrize("valor", NULOS, ids=repr)
def test_nulos_se_exportan_como_celda_vacia_en_xlsx(valor):
    df = pd.DataFrame({"alert_id": [1], "v": [valor]}, dtype=object)
    hoja = leer_xlsx(ex.a_xlsx(df))["datos"]
    assert hoja["A2"].value == 1 and hoja["B2"].value is None


def test_nan_en_columna_float64_es_nulo_en_ambos_formatos():
    df = pd.DataFrame({"alert_id": [1, 2], "v": [np.nan, -2.5]})
    assert [f[1] for f in leer_csv(ex.a_csv(df))[1:]] == ["", "-2.5"]
    assert [c.value for c in leer_xlsx(ex.a_xlsx(df))["datos"]["B"][1:]] == [None, -2.5]


@pytest.mark.parametrize("valor", [Decimal("-1.50"), Decimal("0"), Decimal("1E+3")], ids=repr)
def test_decimal_finito_se_acepta_en_ambos_formatos(valor):
    df = pd.DataFrame({"v": [valor]}, dtype=object)
    assert leer_csv(ex.a_csv(df))[1] == [str(valor)]
    celda = leer_xlsx(ex.a_xlsx(df))["datos"]["A2"]
    assert celda.data_type == "n" and Decimal(str(celda.value)) == valor


def test_las_funciones_no_modifican_el_dataframe_de_entrada():
    df = pd.DataFrame({"t": ["=1", "a"], "f": pd.to_datetime(["2026-11-16T05:00:00-05:00"] * 2)})
    copia = df.copy(deep=True)
    ex.preparar(df)
    ex.a_csv(df)
    ex.a_xlsx(df)
    pd.testing.assert_frame_equal(df, copia)


# --------------------------------------------------------------------------------- aislamiento

PROHIBIDOS = ("streamlit", "requests", "app")


def test_imports_estaticos_no_incluyen_streamlit_requests_ni_app():
    arbol = ast.parse(MODULO.read_text(encoding="utf-8"))
    nombres = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres.update(a.name.split(".")[0] for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres.add(nodo.module.split(".")[0])
    assert not nombres & set(PROHIBIDOS)
    assert "os" not in nombres  # no lee variables de entorno


def test_el_modulo_se_importa_como_paquete_de_espacio_de_nombres():
    import dashboard
    assert getattr(dashboard, "__file__", None) is None          # sin __init__.py
    assert Path(ex.__file__).resolve() == MODULO.resolve()


def test_importar_el_modulo_no_carga_streamlit_requests_ni_app():
    codigo = (
        "import sys\n"
        "sys.modules['streamlit'] = None\n"
        "sys.modules['requests'] = None\n"
        f"sys.path.insert(0, {str(RAIZ)!r})\n"
        "import dashboard.exportacion as m\n"
        "cargados = sorted(n for n in sys.modules if n.split('.')[0] in ('streamlit', 'requests', 'app')"
        " and sys.modules[n] is not None)\n"
        "print(repr(cargados))\n"
        "print(m.__file__)\n")
    entorno = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    entorno["PYTHONDONTWRITEBYTECODE"] = "1"
    salida = subprocess.run([sys.executable, "-I", "-c", codigo], capture_output=True, text=True,
                            env=entorno, cwd=str(RAIZ / "tests"), timeout=120, check=True)
    lineas = salida.stdout.strip().splitlines()
    assert lineas[0] == "[]"
    assert Path(lineas[1]).resolve() == MODULO.resolve()


# =========================================================================================
# Conjuntos nuevos: predicciones, recomendaciones, calidad G3, pares M1 y explicación RF-015
# =========================================================================================

ESQUEMAS = RAIZ / "app" / "api" / "esquemas.py"
EVALUACION = RAIZ / "app" / "evaluacion_real.py"
COMPARACION = RAIZ / "app" / "ml" / "comparacion_real.py"
SENSIBLES = {"usuario_asignado", "comentario", "artefacto_ruta", "clave_dedup"}


def columnas_tabla(nombre):
    sql = DDL.read_text(encoding="utf-8")
    m = re.search(rf"CREATE TABLE {nombre} \((.*?)\n\);", sql, re.S)
    assert m, f"No se encontró CREATE TABLE {nombre}"
    return re.findall(r"^\s{4}([a-z_0-9]+)\s+(?:bigserial|bigint|uuid|text|integer|smallint|numeric|"
                      r"timestamptz|boolean)\b", m.group(1), re.M)


def constante_ast(ruta, nombre):
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign) and any(getattr(t, "id", None) == nombre for t in nodo.targets):
            return ast.literal_eval(nodo.value)
    raise AssertionError(f"No se encontró {nombre} en {ruta}")


def campos_modelo(ruta, clase):
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    for nodo in arbol.body:
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            return [n.target.id for n in nodo.body if isinstance(n, ast.AnnAssign)]
    raise AssertionError(f"No se encontró la clase {clase}")


def prediccion(i, **cambios):
    fila = {"prediction_id": i, "execution_id": "00000000-0000-0000-0000-00000000000a", "idruta": 16,
            "sentido": "CICLO", "segmento_id": "TOTAL", "franja_inicio": "2025-10-01T06:00:00-05:00",
            "franja_minutos": 15, "horizonte_min": 60, "demanda_pred": 512.25, "limite_inferior": 410.0,
            "limite_superior": 640.5, "nivel_intervalo": 0.8, "definicion_tiempo": "salida_programada",
            "model_id": 2, "model_version": "gbm_fe31_fixture", "feature_version": "sobusa-fe-3.1",
            "data_cutoff": "2025-10-01T00:00:00-05:00", "generated_at": "2026-11-19T08:12:00-05:00",
            "latency_ms": 900}
    fila.update(cambios)
    return fila


def recomendacion(i, **cambios):
    fila = {"recommendation_id": i, "execution_id": "00000000-0000-0000-0000-00000000000b",
            "prediction_id": 100 + i, "idruta": 16, "sentido": "CICLO", "segmento_id": "TOTAL",
            "franja_inicio": "2025-10-01T06:00:00-05:00", "horizonte_min": 60, "demanda_hora": 512.25,
            "base_demanda": "puntual", "factor_carga_max": 0.37, "carga_max_estimada_hora": 189.55,
            "capacidad_nominal": 55.0, "capacidad_fuente": "reporte", "ocupacion_objetivo": 0.75,
            "capacidad_efectiva": 41.25, "buses_hora_recomendado": None, "headway_recomendado_min": None,
            "ciclo_min": 150.0, "vehiculos_requeridos": 30, "flota_tope": 24, "buses_hora_actual": 8.0,
            "headway_actual_min": 7.5, "delta_buses_hora": None, "es_viable": False,
            "motivo_no_viable": "requiere 30 vehículos y el tope de flota es 24",
            "parametros_version": "param_v2:laboral", "generated_at": "2026-11-19T08:12:00-05:00"}
    fila.update(cambios)
    return fila


def deteccion(i, **cambios):
    fila = {"deteccion_id": i, "evento_id": 7, "lote": "ingesta_20261119_001",
            "detectado_en": "2026-11-19T02:00:00-05:00", "idruta": 2, "causa": "numeropasajeros nulo",
            "valor_original": None, "tabla_origen": "progvehiculos", "idprogramacion": 900 + i,
            "regla": "pasajeros_nulos", "reglas_version": "comun-1.5", "valor_normalizado": None,
            "primer_lote": "ingesta_20261118_001", "primera_deteccion": "2026-11-18T02:00:00-05:00",
            "nuevo_en_lote": False}
    fila.update(cambios)
    return fila


def par_m1(i, **cambios):
    fila = {"prediction_id": i, "execution_id": "00000000-0000-0000-0000-00000000000a", "model_id": 2,
            "model_version": "gbm_fe31_fixture", "feature_version": "sobusa-fe-3.1", "idruta": 16,
            "franja_inicio": "2025-10-01T06:00:00-05:00", "fin_ventana": "2025-10-01T07:00:00-05:00",
            "horizonte_min": 60, "data_cutoff": "2025-10-01T00:00:00-05:00",
            "generated_at": "2026-11-19T08:12:00-05:00", "demanda_pred": 512.25, "limite_inferior": 410.0,
            "limite_superior": 640.5, "demanda_real": None, "error": None, "dentro_intervalo": None,
            "demanda_ingenuo_7d": 480.0, "programaciones_ventana": 12, "estado": "no_disponible",
            "motivo": "sin_consolidar", "detalle_motivo": "la ventana termina después del corte de consolidación"}
    fila.update(cambios)
    return fila


class ApiM1(ApiFalsa):
    """Como ApiFalsa, pero cada respuesta trae la huella de la evaluación (claves_constantes)."""

    def __init__(self, filas, huellas=None, **kw):
        super().__init__(filas, columna_id="prediction_id", **kw)
        self.huellas = huellas or {}

    def __call__(self, ruta, params):
        k = len(self.llamadas)
        cuerpo = super().__call__(ruta, params)
        cuerpo["parametros"] = {"reglas": "m1-1.1"}
        if self.huellas.get(k, "h1") is not None:
            cuerpo["huella"] = self.huellas.get(k, "h1")
        return cuerpo


GENERADORES = {"predicciones": prediccion, "recomendaciones": recomendacion,
               "errores_calidad": deteccion, "pares_m1": par_m1}
FILTROS_MINIMOS = {"errores_calidad": {"lote": "ingesta_20261119"}}


def test_las_listas_blancas_de_prediction_y_recommendation_son_las_columnas_del_ddl():
    assert list(ex.PREDICCIONES.columnas) == columnas_tabla("prediction")
    assert list(ex.RECOMENDACIONES.columnas) == columnas_tabla("recommendation")


def test_la_lista_blanca_de_calidad_son_los_campos_de_ErrorCalidad():
    assert list(ex.ERRORES_CALIDAD.columnas) == campos_modelo(ESQUEMAS, "ErrorCalidad")
    assert ex.ERRORES_CALIDAD.columna_id == "deteccion_id"            # no evento_id: entidades distintas


def test_la_lista_blanca_de_pares_es_COLUMNAS_PAR_mas_el_detalle_y_los_estados_coinciden():
    assert list(ex.PARES_M1.columnas) == constante_ast(EVALUACION, "COLUMNAS_PAR") + ["detalle_motivo"]
    assert ex.ESTADOS_M1 == frozenset(constante_ast(COMPARACION, "ESTADOS"))
    assert {"estado", "motivo"} <= set(ex.PARES_M1.columnas)


def test_ningun_conjunto_exporta_campos_sensibles_o_internos():
    for conjunto in ex.CONJUNTOS.values():
        assert not SENSIBLES & set(conjunto.columnas), conjunto.nombre
    assert set(ex.CONJUNTOS) == {"predicciones", "recomendaciones", "alertas", "errores_calidad", "pares_m1"}


def test_los_filtros_de_cada_conjunto_son_los_parametros_de_su_endpoint():
    assert ex.PREDICCIONES.filtros == {"idruta", "horizonte_min", "execution_id", "desde", "hasta"}
    assert ex.RECOMENDACIONES.filtros == {"idruta", "horizonte_min", "es_viable", "execution_id", "desde", "hasta"}
    assert ex.ERRORES_CALIDAD.filtros == {"lote", "alcance", "idruta", "regla", "tabla_origen",
                                          "reglas_version", "causa"}
    assert ex.PARES_M1.filtros == {"model_id", "execution_id", "idruta", "horizonte_min", "desde", "hasta",
                                   "estado"}


@pytest.mark.parametrize("nombre", ["predicciones", "recomendaciones", "errores_calidad", "pares_m1"])
def test_cada_conjunto_lee_todas_las_paginas_y_exporta_solo_su_lista_blanca(nombre):
    conjunto = ex.CONJUNTOS[nombre]
    filas = [{**GENERADORES[nombre](i), "columna_extra_interna": "x"} for i in range(1, 6)]
    api = (ApiM1(filas) if nombre == "pares_m1" else ApiFalsa(filas, columna_id=conjunto.columna_id))
    res = ex.traer_todo(api, conjunto, FILTROS_MINIMOS.get(nombre), tamano=2)
    assert api.paginas_pedidas() == [1, 2, 3, 1]
    assert {r for r, _ in api.llamadas} == {conjunto.ruta}
    assert list(res.datos.columns) == list(conjunto.columnas)
    assert res.datos[conjunto.columna_id].tolist() == [5, 4, 3, 2, 1]
    assert any("columna_extra_interna" in a for a in res.advertencias)
    contenido = leer_csv(ex.a_csv(res.datos, columna_id=conjunto.columna_id))
    assert contenido[0] == list(conjunto.columnas) and len(contenido) == 6
    hoja = leer_xlsx(ex.a_xlsx(res.datos, ex.metadatos(res), columna_id=conjunto.columna_id))
    assert [c.value for c in hoja["datos"][1]] == list(conjunto.columnas)
    assert hoja["exportacion"]["B2"].value == conjunto.nombre


def test_calidad_exige_el_lote_antes_de_llamar_a_la_api():
    api = ApiFalsa([deteccion(1)], columna_id="deteccion_id")
    for filtros in (None, {"alcance": "auto"}, {"lote": ""}, {"lote": None}):
        with pytest.raises(ValueError, match="obligatorio"):
            ex.traer_todo(api, ex.ERRORES_CALIDAD, filtros)
    assert api.llamadas == []


def test_calidad_valida_el_alcance_y_envia_la_causa_exacta():
    api = ApiFalsa([deteccion(1)], columna_id="deteccion_id")
    with pytest.raises(ValueError, match="alcance"):
        ex.traer_todo(api, ex.ERRORES_CALIDAD, {"lote": "L", "alcance": "todo"})
    ex.traer_todo(api, ex.ERRORES_CALIDAD, {"lote": "L", "alcance": "ventana", "causa": "numeropasajeros nulo"})
    assert api.llamadas[0][1] == {"lote": "L", "alcance": "ventana", "causa": "numeropasajeros nulo",
                                  "pagina": 1, "tamano": 500}


def test_es_viable_viaja_como_texto_y_no_se_acepta_un_booleano():
    api = ApiFalsa([recomendacion(1)], columna_id="recommendation_id")
    with pytest.raises(ValueError, match="es_viable"):
        ex.traer_todo(api, ex.RECOMENDACIONES, {"es_viable": False})
    ex.traer_todo(api, ex.RECOMENDACIONES, {"es_viable": "false", "idruta": 16})
    assert api.llamadas[0][1]["es_viable"] == "false"


def test_pares_m1_detecta_que_la_huella_cambia_entre_paginas():
    filas = [par_m1(i) for i in range(1, 6)]
    with pytest.raises(ex.PaginacionInconsistente, match="huella"):
        ex.traer_todo(ApiM1(filas, huellas={1: "h2"}), ex.PARES_M1, tamano=2)
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(ApiM1(filas, huellas={3: "h2"}), ex.PARES_M1, tamano=2)     # verificación de cierre
    with pytest.raises(ex.RespuestaInvalida, match="huella"):
        ex.traer_todo(ApiM1(filas, huellas={0: None}), ex.PARES_M1, tamano=2)


def test_pares_m1_conserva_estado_motivo_y_faltantes_sin_convertirlos_en_cero():
    res = ex.traer_todo(ApiM1([par_m1(1), par_m1(2, estado="valido", motivo="ok", detalle_motivo="",
                                                  demanda_real=0.0, error=-512.25, dentro_intervalo=False)]),
                        ex.PARES_M1)
    filas = leer_csv(ex.a_csv(res.datos))
    encabezado = filas[0]
    por_id = {f[0]: dict(zip(encabezado, f, strict=True)) for f in filas[1:]}
    assert por_id["1"]["demanda_real"] == "" and por_id["1"]["motivo"] == "sin_consolidar"
    assert por_id["2"]["demanda_real"] == "0.0" and por_id["2"]["estado"] == "valido"
    with pytest.raises(ValueError, match="estado"):
        ex.traer_todo(ApiM1([par_m1(1)]), ex.PARES_M1, {"estado": "otro"})
    api = ApiM1([par_m1(1), par_m1(2, estado="valido")])
    res = ex.traer_todo(api, ex.PARES_M1, {"estado": "valido", "model_id": 2})
    assert api.llamadas[0][1] == {"estado": "valido", "model_id": "2", "pagina": 1, "tamano": 500}
    assert res.datos["prediction_id"].tolist() == [2]


def test_formulas_en_campos_de_texto_de_calidad_se_neutralizan():
    res = ex.traer_todo(ApiFalsa([deteccion(1, causa="=HIPERVINCULO(\"x\")", valor_original="+57 300")],
                                 columna_id="deteccion_id"), ex.ERRORES_CALIDAD, {"lote": "L"})
    fila = dict(zip(*leer_csv(ex.a_csv(res.datos))[:2], strict=True))
    assert fila["causa"].startswith("'=") and fila["valor_original"] == "'+57 300"
    hoja = leer_xlsx(ex.a_xlsx(res.datos))["datos"]
    assert all(c.data_type != "f" for fila in hoja.iter_rows() for c in fila)


def test_un_valor_no_finito_en_un_conjunto_nuevo_impide_el_archivo():
    res = ex.traer_todo(ApiFalsa([prediccion(1, demanda_pred=float("inf"))], columna_id="prediction_id"),
                        ex.PREDICCIONES)
    with pytest.raises(ex.ErrorExportacion, match="infinito"):
        ex.a_csv(res.datos, columna_id="prediction_id")
    with pytest.raises(ex.ErrorExportacion):
        ex.a_xlsx(res.datos, columna_id="prediction_id")


def test_limite_de_filas_tambien_aplica_a_los_conjuntos_nuevos():
    assert ex.LIMITE_FILAS_POR_DEFECTO == 10_000

    def api(ruta, params):
        return {"datos": [prediccion(i) for i in range(1, 501)],
                "paginacion": {"pagina": 1, "tamano": 500, "total": 10_001, "paginas": 21}}
    with pytest.raises(ex.LimiteExcedido):
        ex.traer_todo(api, ex.PREDICCIONES)


def test_paginacion_inconsistente_en_recomendaciones_no_entrega_archivo():
    api = ApiFalsa([recomendacion(i) for i in range(1, 6)], columna_id="recommendation_id",
                   mutaciones={1: lambda a: a.filas.update({99: recomendacion(99)})})
    with pytest.raises(ex.PaginacionInconsistente):
        ex.traer_todo(api, ex.RECOMENDACIONES, tamano=2)


def test_un_conjunto_con_filtro_obligatorio_desconocido_no_se_puede_declarar():
    with pytest.raises(ValueError, match="obligatorio"):
        ex.Conjunto("x", "x", "id", ("id",), frozenset({"a"}), obligatorios=frozenset({"b"}))


# ---------------------------------------------------------------------- explicación (RF-015)

def explicacion(**cambios):
    x = {"prediction_id": 771201, "execution_id": "00000000-0000-0000-0000-00000000000a", "model_id": 2,
         "model_version": "gbm_fe31_fixture", "feature_version": "sobusa-fe-3.1",
         "artefacto_sha256": "f" * 64, "scikit_learn": "1.9.1", "idruta": 16,
         "franja_inicio": "2025-10-01T06:00:00-05:00", "horizonte_min": 60,
         "data_cutoff": "2025-10-01T00:00:00-05:00", "demanda_pred": 512.25, "estado": "reconstruida_y_verificada",
         "advertencia": "Explicación reconstruida, no garantizada", "metodo": "TreeSHAP", "escala": "log",
         "referencia_log": 5.1, "referencia_pasajeros": 164.0, "prediccion_log": 6.24,
         "prediccion_pasajeros": 512.25,
         "aportes": [{"variable": "y_media_4_semanas", "valor": 498.0, "valor_modelo": 498.0, "aporte_log": 0.91,
                      "factor": 2.48},
                     {"variable": "tipo_dia", "valor": "laboral", "valor_modelo": 0.0, "aporte_log": 0.05,
                      "factor": 1.05},
                     {"variable": "marca_calendario", "valor": None, "valor_modelo": None, "aporte_log": -0.02,
                      "factor": 0.98}],
         "verificaciones": {}, "importancia_global_artefacto": {}}
    x.update(cambios)
    return x


def reloj_fijo():
    return datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)


def test_la_explicacion_valida_se_exporta_una_fila_por_variable_con_metadatos():
    df, meta = ex.tabla_explicacion(explicacion(), reloj=reloj_fijo)
    assert list(df.columns) == list(ex.COLUMNAS_APORTES)
    assert df["variable"].tolist() == ["y_media_4_semanas", "tipo_dia", "marca_calendario"]
    assert meta["prediction_id"] == "771201" and meta["model_version"] == "gbm_fe31_fixture"
    assert meta["variables"] == "3" and meta["generado_en"] == "2026-11-20T20:00:00+00:00"
    filas = leer_csv(ex.a_csv(df))
    assert filas[3] == ["marca_calendario", "", "", "-0.02", "0.98"]        # faltante como celda vacía
    libro = leer_xlsx(ex.a_xlsx(df, meta))
    assert {fila[0].value for fila in libro["exportacion"].iter_rows(min_row=2)} >= {"prediction_id", "model_id"}


@pytest.mark.parametrize("cambios, error", [
    ({"estado": "parcial"}, ex.ErrorExportacion),
    ({"aportes": []}, ex.RespuestaInvalida),
    ({"aportes": [{"variable": "a", "valor": 1}]}, ex.RespuestaInvalida),
    ({"aportes": [{"variable": "a", "valor": 1, "valor_modelo": 1, "aporte_log": 0.1, "factor": 1.1}] * 2},
     ex.RespuestaInvalida),
], ids=["estado_no_verificado", "sin_aportes", "aporte_incompleto", "variable_repetida"])
def test_una_explicacion_no_valida_no_se_exporta(cambios, error):
    with pytest.raises(error):
        ex.tabla_explicacion(explicacion(**cambios))


def test_la_explicacion_sin_campos_de_identificacion_no_se_exporta():
    x = explicacion()
    del x["model_version"]
    with pytest.raises(ex.RespuestaInvalida, match="model_version"):
        ex.tabla_explicacion(x)


def test_la_explicacion_neutraliza_formulas_y_rechaza_no_finitos():
    df, _ = ex.tabla_explicacion(explicacion(aportes=[
        {"variable": "=x", "valor": "@SUMA(A1)", "valor_modelo": 1.0, "aporte_log": 0.1, "factor": 1.1}]))
    assert leer_csv(ex.a_csv(df))[1][:2] == ["'=x", "'@SUMA(A1)"]
    df, _ = ex.tabla_explicacion(explicacion(aportes=[
        {"variable": "a", "valor": 1.0, "valor_modelo": 1.0, "aporte_log": float("inf"), "factor": 1.1}]))
    with pytest.raises(ex.ErrorExportacion):
        ex.a_csv(df)
