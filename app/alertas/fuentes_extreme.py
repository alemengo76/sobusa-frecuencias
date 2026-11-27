"""Adaptadores de SOLO LECTURA sobre tablas de Extreme (usuario de lectura, transacciones READ ONLY).

Se usan para comparar lo que tenemos contra referencias de Extreme. Ninguno lee ai_frecuencias:
la referencia y lo evaluado tienen que venir de tablas distintas para que la comparación detecte algo.

- `ConsolidadoTimbradasAnio`: consolidado anual de Extreme (`timbradas_anio`), subidas por ruta y año.
  Es la REFERENCIA de ALT-008. Lo construye Extreme con `fn_insert_timbradas_anio` (agrupado por fecha
  fin: `COALESCE(fecha_fin_liquidacion, fechafin)`, DECISIONES §2).
- `SubidasViajesAnio`: suma de `numeropasajeros` de `progvehiculos` por ruta y año, con el mismo criterio
  de fecha y la marca `apto_estadisticas_historicas`. Es lo EVALUADO en ALT-008. Es la consulta 2 de
  docs/consultas_verificacion.sql.
- `ProgramacionesPorDia`: cuántas programaciones tiene Extreme por ruta y día de salida. Es la referencia
  externa de la completitud (app/calidad/completitud.py).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import text

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
ENV_REFERENCIA_ALT008 = "ALT008_REFERENCIA"          # 'timbradas_anio' habilita la referencia


def _esquema() -> str:
    from app.core.config import obtener_config
    esquema = obtener_config().esquema_fuente
    if not _IDENT.match(esquema):
        raise ValueError(f"esquema de la fuente inválido: {esquema!r}")
    return esquema


def _leer():
    from app.db.conexion import leer
    return leer()


@dataclass
class ConsolidadoTimbradasAnio:
    rutas: tuple = (2, 16, 21)
    granularidad: str = "ruta_anio"
    solo_lectura: bool = True
    autorizada: bool = field(default_factory=lambda: os.environ.get(ENV_REFERENCIA_ALT008) == "timbradas_anio")

    @property
    def origen(self) -> str:
        return f"{_esquema()}.timbradas_anio"

    def leer(self, anios: list[int]) -> pd.DataFrame:
        """idruta, periodo (año), subidas_consolidado y el diagnóstico del hallazgo §9.1 (proporción de
        franjas con nro_viajes = 1: si es casi todo, la acumulación de Extreme no funciona)."""
        with _leer() as c:
            return pd.read_sql(text(f"""
                SELECT id_ruta AS idruta, ano AS periodo, SUM(subidas) AS subidas_consolidado,
                       COUNT(*) AS franjas, AVG((nro_viajes = 1)::int) AS prop_franjas_un_viaje
                FROM {self.origen} WHERE id_ruta = ANY(:r) AND ano = ANY(:a) GROUP BY 1, 2"""),
                c, params={"r": list(self.rutas), "a": list(anios)})


@dataclass
class SubidasViajesAnio:
    rutas: tuple = (2, 16, 21)
    granularidad: str = "ruta_anio"
    solo_lectura: bool = True

    @property
    def origen(self) -> str:
        return f"{_esquema()}.progvehiculos"

    def leer(self, anios: list[int]) -> pd.DataFrame:
        with _leer() as c:
            return pd.read_sql(text(f"""
                SELECT idruta, EXTRACT(YEAR FROM COALESCE(fecha_fin_liquidacion, fechafin))::int AS periodo,
                       SUM(numeropasajeros) AS subidas_viajes, COUNT(*) AS viajes
                FROM {self.origen}
                WHERE idruta = ANY(:r) AND apto_estadisticas_historicas
                  AND EXTRACT(YEAR FROM COALESCE(fecha_fin_liquidacion, fechafin))::int = ANY(:a)
                GROUP BY 1, 2"""), c, params={"r": list(self.rutas), "a": list(anios)})


@dataclass
class ProgramacionesPorDia:
    rutas: tuple = (2, 16, 21)
    solo_lectura: bool = True

    @property
    def origen(self) -> str:
        return f"{_esquema()}.progvehiculos"

    def leer(self, desde, hasta) -> pd.DataFrame:
        """Cantidad e identificadores de las programaciones por ruta y día en [desde, hasta)."""
        with _leer() as c:
            return pd.read_sql(text(f"""
                SELECT idruta, fechasalida::date AS fecha, COUNT(*) AS programaciones,
                       array_agg(idprogramacion) AS ids_programaciones
                FROM {self.origen} WHERE idruta = ANY(:r) AND fechasalida >= :d AND fechasalida < :h
                GROUP BY 1, 2"""), c, params={"r": list(self.rutas), "d": desde, "h": hasta})
