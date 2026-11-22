"""E03: factor de carga sintético (param_v2) por ruta y tipo de día."""
import re
from pathlib import Path

import pandas as pd
import pytest

from app.ml.falsas import generar_predicciones_falsas
from app.optimizacion.frecuencias import ParametroInvalido, recomendar
from app.optimizacion.parametros import (TablaParametros, derivar_factor_carga, desde_filas,
                                         tipo_dia_parametros)

RAIZ = Path(__file__).resolve().parents[2]
SQL = RAIZ / "migrations" / "sql" / "0003_param_v2.sql"
ESPERADO = {(2, 'todos'): 0.31, (2, 'laboral'): 0.32, (2, 'sabado'): 0.33, (2, 'domingo_festivo'): 0.34, (16, 'todos'): 0.34, (16, 'laboral'): 0.35, (16, 'sabado'): 0.36, (16, 'domingo_festivo'): 0.37, (21, 'todos'): 0.37, (21, 'laboral'): 0.38, (21, 'sabado'): 0.39, (21, 'domingo_festivo'): 0.4}
CAPACIDAD = {2: 40, 16: 42, 21: 44}


def filas_param_v2(sin=()):
    return [{"idruta": r, "tipo_dia": t, "capacidad_nominal": CAPACIDAD[r], "ocupacion_objetivo": 0.75,
             "ciclo_min": 130, "factor_carga_max": f, "capacidad_fuente": "fixture_sintetico", "flota_tope": None,
             "headway_minimo_min": 1.0, "version": "param_v2"}
            for (r, t), f in ESPERADO.items() if (r, t) not in sin]


def valores_migracion() -> dict:
    texto = SQL.read_text(encoding="utf-8")
    return {(int(r), t): float(f) for r, t, f in
            re.findall(r"\((\d+),\s*'(\w+)',\s*([0-9.]+),", texto)}


FIXTURE = RAIZ / "tests/fixtures/factor_carga_sintetico.csv"


def test_migracion_sale_del_fixture_sintetico():
    d = derivar_factor_carga(FIXTURE)
    derivado = {(r.idruta, r.tipo_dia): r.factor_carga_max for r in d.itertuples()}
    assert derivado == valores_migracion() == ESPERADO


def test_todos_es_la_mediana_exacta_de_la_ruta():
    f = pd.read_csv(FIXTURE)
    exacta = f[f.tipo_dia.eq("todos")].set_index("id_ruta").factor_mediano.round(3).to_dict()
    assert {r: ESPERADO[(r, "todos")] for r in (2, 16, 21)} == exacta


def test_tipo_de_dia_con_calendario_del_fe():
    t = tipo_dia_parametros(pd.to_datetime(["2025-03-04 07:00", "2025-03-08 07:00", "2025-03-09 07:00",
                                            "2025-03-24 07:00", "2025-12-25 10:00"]))
    # martes, sábado, domingo, lunes festivo de San José (Ley Emiliani), Navidad
    assert t.tolist() == ["laboral", "sabado", "domingo_festivo", "domingo_festivo", "domingo_festivo"]


@pytest.mark.parametrize("dia,tipo", [("2025-03-04", "laboral"), ("2025-03-08", "sabado"),
                                      ("2025-03-09", "domingo_festivo"), ("2025-03-24", "domingo_festivo")])
@pytest.mark.parametrize("ruta", [2, 16, 21])
def test_cada_ruta_y_tipo_de_dia_usa_su_factor(ruta, dia, tipo):
    pred = generar_predicciones_falsas(inicio=f"{dia} 05:00", rutas=(ruta,))
    rec = recomendar(pred, desde_filas(filas_param_v2()))
    assert rec.factor_carga_max.eq(ESPERADO[(ruta, tipo)]).all()
    assert rec.parametros_version.eq(f"param_v2:{tipo}").all()
    esperado = rec.demanda_hora.astype(float) * ESPERADO[(ruta, tipo)]
    assert ((rec.carga_max_estimada_hora.astype(float) - esperado).abs() < 0.01).all()


def test_sin_fila_del_tipo_de_dia_usa_todos():
    tabla = desde_filas(filas_param_v2(sin={(16, "sabado")}))
    rec = recomendar(generar_predicciones_falsas(inicio="2025-03-08 05:00", rutas=(16,)), tabla)
    assert rec.factor_carga_max.eq(ESPERADO[(16, "todos")]).all() and rec.parametros_version.eq("param_v2:todos").all()


def test_ruta_sin_parametros_es_error():
    tabla = desde_filas([f for f in filas_param_v2() if f["idruta"] != 21])
    with pytest.raises(ParametroInvalido, match="ruta 21"):
        recomendar(generar_predicciones_falsas(inicio="2025-03-04 05:00", rutas=(21,)), tabla)


def test_formato_anterior_sigue_funcionando():
    t = TablaParametros.desde({16: desde_filas(filas_param_v2()).para(16, "todos")[0]})
    p, usado = t.para(16, "laboral")
    assert usado == "todos" and p.factor_carga_max == ESPERADO[(16, "todos")]


def test_sin_factor_provisional_en_el_codigo():
    for archivo in ["app/optimizacion/frecuencias.py", "app/optimizacion/parametros.py", "app/servicios.py"]:
        assert not re.search(r"factor\w*\s*=\s*0[.,]4\b", (RAIZ / archivo).read_text(encoding="utf-8"))
