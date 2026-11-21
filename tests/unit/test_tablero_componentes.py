"""Componentes visuales del tablero (dashboard/estilo.py y dashboard/graficos.py).

Comprueba que el texto de la API se escapa, que los faltantes nunca se convierten en 0 y que las
gráficas usan la hora de Bogotá. Sin Streamlit ni red.
"""
import json
import math

from dashboard import estilo as es
from dashboard import graficos as gr

ATAQUE = '<img src=x onerror="alert(1)">'


def test_todo_texto_de_la_api_se_escapa():
    fragmentos = [
        es.kpis([{"l": ATAQUE, "v": ATAQUE, "u": ATAQUE, "ctx": ATAQUE, "fte": ATAQUE}]),
        es.lista([(ATAQUE, ATAQUE, ATAQUE, None)]),
        es.datos([(ATAQUE, ATAQUE)]),
        es.pasos([(ATAQUE, ATAQUE, ATAQUE, True)]),
        es.encabezado(ATAQUE, ATAQUE, ATAQUE),
        es.conclusion(ATAQUE, "nv"),
        es.chip(ATAQUE),
        es.leyenda([(ATAQUE, "#fff", ATAQUE)]),
        es.pildora(ATAQUE, ATAQUE),
        es.vacio(ATAQUE),
    ]
    for f in fragmentos:
        assert "<img" not in f and "&lt;img" in f


def test_los_faltantes_se_muestran_como_sin_dato_y_nunca_como_cero():
    for faltante in (None, float("nan"), float("inf")):
        assert es.num(faltante) == "Sin dato"
        assert es.pct(faltante) == "Sin dato"
    assert es.num(0) == "0" and es.pct(0) == "0,00 %"
    assert es.chip_viable(None).endswith("Sin dato</span>")
    assert 'class="v sd"' in es.datos([("Headway", None)])


def test_formato_numerico_es_co():
    assert es.num(1234567.891, 2) == "1.234.567,89"
    assert es.pct(0.1385) == "13,85 %"
    assert es.corto("e3f1c0de-0000-4000-8000-00000000a702") == "e3f1…a702"


def test_kpi_sin_dato_y_kpi_malo_tienen_su_clase():
    html = es.kpis([{"l": "a", "v": "Sin dato"}, {"l": "b", "v": "3", "mal": True}])
    assert 'class="kpi sd"' in html and 'class="kpi mal"' in html


def fila_pred(t, pred, inf, sup):
    return {"franja_inicio": t, "demanda_pred": pred, "limite_inferior": inf, "limite_superior": sup}


def test_grafica_de_demanda_en_hora_de_bogota_con_pico_y_valle_sin_inventar_valores():
    filas = [fila_pred("2025-10-01T11:00:00+00:00", 100, 80, 120),       # 06:00 en Bogotá
             fila_pred("2025-10-01T06:15:00-05:00", 300, 250, 360),
             fila_pred("2025-10-01T06:30:00-05:00", None, None, None),
             fila_pred("2025-10-01T06:45:00-05:00", 50, 40, 60)]
    spec = gr.demanda(filas, 60)
    json.dumps(spec)                                     # serializable para st.vega_lite_chart
    valores = spec["data"]["values"]
    assert [v["franja"] for v in valores] == ["06:00", "06:15", "06:30", "06:45"]
    assert valores[2]["pred"] is None and valores[2]["inf"] is None          # el faltante sigue faltante
    marcas = {v["franja"]: v["marca"] for v in valores if v["marca"]}
    assert marcas == {"06:15": "Pico", "06:45": "Valle"}
    assert "60 min" in spec["layer"][0]["encoding"]["y"]["title"]


def test_grafica_de_frecuencia_no_calcula_buses_para_franjas_no_viables():
    filas = [{"recommendation_id": 1, "franja_inicio": "2025-10-01T06:00:00-05:00", "buses_hora_recomendado": 6,
              "buses_hora_actual": 5.0, "es_viable": True, "motivo_no_viable": None},
             {"recommendation_id": 2, "franja_inicio": "2025-10-01T06:15:00-05:00", "buses_hora_recomendado": None,
              "buses_hora_actual": None, "es_viable": False, "motivo_no_viable": "supera el tope"}]
    spec = gr.frecuencia(filas, seleccion=1)
    json.dumps(spec)
    v = {x["id"]: x for x in spec["data"]["values"]}
    assert v[2]["rec"] is None and v[2]["act"] is None and v[2]["viable"] is False
    assert v[1]["sel"] is True and v[2]["sel"] is False
    assert math.isclose(v[1]["techo"], 6 * 1.15)


def test_mapa_de_calor_conserva_celdas_sin_dato():
    filas = [{"idruta": 2, "horizonte_min": 30, "wape": 0.22, "n_validas": 10},
             {"idruta": 2, "horizonte_min": 60, "wape": None, "n_validas": 0},
             {"idruta": 16, "horizonte_min": 30, "wape": 0.15, "n_validas": 8}]
    spec = gr.mapa_calor(filas, "wape", True)
    json.dumps(spec)
    assert [v["valor"] for v in spec["data"]["values"]] == [0.22, None, 0.15]


def test_anillo_omite_grupos_vacios():
    spec = gr.anillo([("Aumenta", 3, "#000"), ("Reduce", 0, "#111")], "3", "franjas")
    assert [v["nombre"] for v in spec["layer"][0]["data"]["values"]] == ["Aumenta"]


def test_md_neutraliza_markdown_sin_alterar_ids():
    assert es.md("![a](http://x)") == r"\!\[a\](http://x)"
    assert es.md("$x$ *y* _z_ <b> `c` | ~ #") == r"\$x\$ \*y\* \_z\_ \<b\> \`c\` \| \~ \#"
    assert es.md("cid-123 e3f1c0de-0000-4000-8000-00000000a702") == "cid-123 e3f1c0de-0000-4000-8000-00000000a702"
    assert es.md(None) == "Sin dato"


def test_na_de_pandas_es_faltante():
    import pandas as pd
    assert es.num(pd.NA) == es.num(pd.NaT) == es.pct(pd.NA) == "Sin dato"


def test_tema_claro_fijo_y_graficas_con_fondo_propio():
    """Con el sistema en modo oscuro, Streamlit pinta etiquetas, campos y gráficas oscuras;
    el tablero fija el tema claro y las gráficas no heredan el tema de Streamlit."""
    import pathlib
    import tomllib
    raiz = pathlib.Path(__file__).resolve().parents[2]
    tema = tomllib.loads((raiz / ".streamlit" / "config.toml").read_text(encoding="utf-8"))["theme"]
    assert tema["base"] == "light"
    from dashboard import graficos
    assert graficos.CONFIG["background"] == "#FFFFFF"
    fuente = (raiz / "dashboard" / "tablero.py").read_text(encoding="utf-8")
    assert fuente.count("st.vega_lite_chart(") == fuente.count("theme=None")


def test_cabecera_fecha_y_origen_tecnico():
    from datetime import date

    from dashboard import estilo
    assert estilo.fecha_larga(date(2026, 11, 20)) == "viernes 20 de noviembre de 2026"
    h = estilo.cabecera("Alertas", [], antetitulo="<x>", bajada="a & b")
    assert "&lt;x&gt;" in h and "a &amp; b" in h and "<h1>Alertas</h1>" in h
    assert ".fte{display:none" in estilo.OCULTAR_FUENTES
    assert estilo.ICONOS_NAV.count("> :nth-child(") == 6 and "{{" not in estilo.CSS
