"""Parámetros operativos por ruta y tipo de día (E03).

Los valores viven en ai_frecuencias.parametro_operativo (versionados). Aquí no hay cifras de
negocio: solo cómo se elige la fila que corresponde a cada franja y cómo se deriva param_v2 del
archivo medido (para que la migración 0003 sea reproducible y verificable con una prueba).

Elección: (ruta, tipo_dia del día de la franja); si esa combinación no tiene fila vigente, la
fila 'todos' de la ruta; si tampoco existe, error (no se inventa un parámetro).
El tipo de día sale del calendario del FE (reglas_comunes.calendario, festivos de Colombia con
Ley Emiliani): el mismo con el que se entrena el modelo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from app.fe31 import TIPO_DIA_PARAMETROS, rc
from app.optimizacion.frecuencias import ParametroInvalido, ParametrosRuta

TODOS = "todos"
TIPOS_DIA = ("laboral", "sabado", "domingo_festivo")
ARCHIVO_FACTOR = Path(__file__).resolve().parents[2] / "datos" / "referencia" / "factor_carga_sensor11.csv"
FUENTE_FACTOR = "sensor-1.1 factor_carga.csv"


def tipo_dia_parametros(franjas) -> pd.Series:
    """laboral / sabado / domingo_festivo para cada instante, con el calendario del FE."""
    t = pd.DatetimeIndex(pd.to_datetime(franjas))
    if len(t) == 0:
        return pd.Series([], dtype=object)
    cal = rc.calendario(t.min(), t.max()).set_index("fecha").tipo_dia
    return pd.Series(cal.reindex(t.normalize()).map(TIPO_DIA_PARAMETROS).to_numpy(), index=range(len(t)))


@dataclass
class TablaParametros:
    """Parámetros vigentes indexados por (idruta, tipo_dia)."""
    filas: dict[tuple[int, str], ParametrosRuta] = field(default_factory=dict)

    @classmethod
    def desde(cls, parametros) -> "TablaParametros":
        """Acepta una TablaParametros o el formato anterior {idruta: ParametrosRuta} (= 'todos')."""
        if isinstance(parametros, cls):
            return parametros
        return cls({(int(k), TODOS): v for k, v in parametros.items()})

    def agregar(self, idruta: int, tipo_dia: str, p: ParametrosRuta) -> None:
        if tipo_dia not in (TODOS, *TIPOS_DIA):
            raise ParametroInvalido(f"tipo_dia desconocido: {tipo_dia}")
        self.filas[(int(idruta), tipo_dia)] = p

    def rutas(self) -> set[int]:
        return {r for r, _ in self.filas}

    def para(self, idruta: int, tipo_dia: str) -> tuple[ParametrosRuta, str]:
        """Devuelve (parámetros, tipo_dia usado). El segundo dice si se usó el respaldo 'todos'."""
        clave = (int(idruta), tipo_dia)
        if clave in self.filas:
            return self.filas[clave], tipo_dia
        if (int(idruta), TODOS) in self.filas:
            return self.filas[(int(idruta), TODOS)], TODOS
        raise ParametroInvalido(f"No hay parámetros operativos vigentes para la ruta {idruta} "
                                f"({tipo_dia} ni '{TODOS}')")


def desde_filas(filas: list[dict]) -> TablaParametros:
    """Filas de parametro_operativo (vigentes) -> TablaParametros."""
    t = TablaParametros()
    for p in filas:
        t.agregar(p["idruta"], p["tipo_dia"], ParametrosRuta(
            idruta=p["idruta"], capacidad_nominal=float(p["capacidad_nominal"]),
            ocupacion_objetivo=float(p["ocupacion_objetivo"]), ciclo_min=float(p["ciclo_min"]),
            factor_carga_max=float(p["factor_carga_max"]), capacidad_fuente=p["capacidad_fuente"],
            flota_tope=p["flota_tope"], headway_minimo_min=float(p["headway_minimo_min"]),
            parametros_version=p["version"]))
    return t


def derivar_factor_carga(archivo: Path = ARCHIVO_FACTOR, decimales: int = 3) -> pd.DataFrame:
    """factor_carga_max de param_v2 a partir de factor_carga.csv (sensor-1.1).

    - 'todos': la fila 'todos' del archivo (mediana exacta por viaje de la ruta).
    - laboral / sabado / domingo_festivo: el archivo trae la mediana por (tipo de día, hora) y el
      número de viajes; se toma la mediana ponderada por viajes de esas medianas. Es una
      aproximación (la mediana exacta por tipo de día requiere los datos del sensor): aplicada a
      la ruta completa da 0,398 / 0,436 / 0,387 frente a las exactas 0,400 / 0,435 / 0,379.
    No se usa el factor por hora (queda para la migración de factores horarios, no autorizada)."""
    f = pd.read_csv(archivo)
    f["id_ruta"] = f.id_ruta.astype(int)
    filas = [{"idruta": int(r.id_ruta), "tipo_dia": TODOS, "factor_carga_max": r.factor_mediano,
              "viajes": int(r.viajes)} for r in f[f.tipo_dia.eq(TODOS)].itertuples()]
    horas = f[f.tipo_dia.ne(TODOS)]
    for (ruta, td), g in horas.groupby(["id_ruta", "tipo_dia"]):
        g = g.sort_values("factor_mediano")
        acumulado = g.viajes.cumsum()
        mediana = g.factor_mediano[acumulado >= g.viajes.sum() / 2].iloc[0]
        filas.append({"idruta": int(ruta), "tipo_dia": TIPO_DIA_PARAMETROS[td],
                      "factor_carga_max": mediana, "viajes": int(g.viajes.sum())})
    out = pd.DataFrame(filas)
    out["factor_carga_max"] = out.factor_carga_max.round(decimales)
    return out.sort_values(["idruta", "tipo_dia"]).reset_index(drop=True)
