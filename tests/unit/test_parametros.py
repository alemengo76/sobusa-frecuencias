"""E03: factor de carga medido (param_v2) por ruta y tipo de día."""
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
ESPERADO = {(2, "todos"): 0.400, (2, "laboral"): 0.419, (2, "sabado"): 0.378, (2, "domingo_festivo"): 0.365,
            (16, "todos"): 0.435, (16, "laboral"): 0.459, (16, "sabado"): 0.429, (16, "domingo_festivo"): 0.418,
            (21, "todos"): 0.379, (21, "laboral"): 0.387, (21, "sabado"): 0.381, (21, "domingo_festivo"): 0.372}
CAPACIDAD = {2: 52, 16: 55, 21: 50}


def filas_param_v2(sin=()):
    return [{"idruta": r, "tipo_dia": t, "capacidad_nominal": CAPACIDAD[r], "ocupacion_objetivo": 0.75,
             "ciclo_min": 239, "factor_carga_max": f, "capacidad_fuente": "reporte", "flota_tope": None,
             "headway_minimo_min": 1.0, "version": "param_v2"}
            for (r, t), f in ESPERADO.items() if (r, t) not in sin]


def valores_migracion() -> dict:
    texto = SQL.read_text(encoding="utf-8")
    return {(int(r), t): float(f) for r, t, f in
            re.findall(r"\((\d+),\s*'(\w+)',\s*([0-9.]+),", texto)}


def test_migracion_sale_del_archivo_medido():
    d = derivar_factor_carga()
    derivado = {(r.idruta, r.tipo_dia): r.factor_carga_max for r in d.itertuples()}
    assert derivado == valores_migracion() == ESPERADO


def test_todos_es_la_mediana_exacta_de_la_ruta():
    f = pd.read_csv(RAIZ / "datos" / "referencia" / "factor_carga_sensor11.csv")
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
    assert rec.factor_carga_max.eq(0.435).all() and rec.parametros_version.eq("param_v2:todos").all()


def test_ruta_sin_parametros_es_error():
    tabla = desde_filas([f for f in filas_param_v2() if f["idruta"] != 21])
    with pytest.raises(ParametroInvalido, match="ruta 21"):
        recomendar(generar_predicciones_falsas(inicio="2025-03-04 05:00", rutas=(21,)), tabla)


def test_formato_anterior_sigue_funcionando():
    t = TablaParametros.desde({16: desde_filas(filas_param_v2()).para(16, "todos")[0]})
    p, usado = t.para(16, "laboral")
    assert usado == "todos" and p.factor_carga_max == 0.435


def test_sin_factor_provisional_en_el_codigo():
    for archivo in ["app/optimizacion/frecuencias.py", "app/optimizacion/parametros.py", "app/servicios.py"]:
        assert not re.search(r"factor\w*\s*=\s*0[.,]4\b", (RAIZ / archivo).read_text(encoding="utf-8"))
