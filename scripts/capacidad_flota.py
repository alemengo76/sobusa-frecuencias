"""Capacidad por vehículo y tope de flota por ruta, a partir del reporte de Extreme.

Resuelve dos problemas del reto:
  1. vehiculos.capacidad = 0 en toda la flota  -> plazas = Sentados + Pie del reporte.
  2. flotadisp_prog = 62 no cuadra con lo observado -> tope = p95 de buses activos por tipo de día.

PRIVACIDAD: un reporte autorizado puede contener nombres, cédulas y teléfonos de
conductores y propietarios. Este script lee SOLO las columnas operativas; no suban
el .xls original al repositorio.

Uso:
  # Solo el reporte (capacidad por placa y semilla por ruta):
  python capacidad_flota.py --reporte reporte_autorizado.xls --salida salidas_flota

  # Con la extracción del equipo (plazas por viaje y tope de flota):
  python capacidad_flota.py --reporte reporte_autorizado.xls --salida salidas_flota \
      --vehiculos ruta2/raw/vehiculos ruta16/raw/vehiculos ruta21/raw/vehiculos \
      --programaciones ruta2/raw/progvehiculos ruta16/raw/progvehiculos ruta21/raw/progvehiculos
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

# Nombre de la ruta en el reporte -> idruta del reto
RUTAS_REPORTE = {"SILENCIO": 2, "KRA. 54 UNINORTE": 16, "72": 21}
COLUMNAS_OPERATIVAS = ["Codigo", "Placa", "Ruta", "Modelo", "Marca", "Clase",
                       "Carroceria", "Sentados", "Pie"]
PLACAS_PRUEBA = {"PRUEBA", "XXX000", "-"}
VENTANA_RECIENTE = ("2025-07-01", "2026-01-01")  # para la capacidad típica por ruta


def leer_reporte(ruta_xls: Path) -> pd.DataFrame:
    r = pd.read_excel(ruta_xls, header=2, usecols=COLUMNAS_OPERATIVAS, dtype={"Ruta": str})
    r["placa"] = r["Placa"].astype(str).str.strip().str.upper()
    r["ruta_reporte"] = r["Ruta"].astype(str).str.strip()
    r["idruta_reporte"] = r["ruta_reporte"].map(RUTAS_REPORTE).astype("Int64")
    for c in ["Sentados", "Pie"]:
        r[c] = pd.to_numeric(r[c], errors="coerce")
    r["plazas"] = r["Sentados"] + r["Pie"]
    r["es_prueba"] = r["placa"].isin(PLACAS_PRUEBA)
    return r


def capacidad_por_placa(r: pd.DataFrame) -> pd.DataFrame:
    """Una fila por placa. Plazas = valor más frecuente; 0 o vacío = sin dato (no se imputa)."""
    validos = r[~r.es_prueba]

    def moda(s):
        s = s[s > 0].dropna()
        return s.mode().max() if len(s) else np.nan

    cap = validos.groupby("placa").agg(
        plazas=("plazas", moda),
        sentados=("Sentados", "max"),
        pie=("Pie", "max"),
        valores_plazas=("plazas", lambda s: s[s > 0].nunique()),
        registros=("placa", "size"),
        rutas_reporte=("ruta_reporte", lambda s: "|".join(sorted(set(s)))),
        modelo=("Modelo", "max"),
        marca=("Marca", "first"),
    ).reset_index()
    cap["estado_capacidad"] = np.select(
        [cap.plazas.isna(), cap.valores_plazas > 1],
        ["sin_dato", "inconsistente_entre_registros"], "ok")
    cap["fuente"] = "reporte_operativo_autorizado"
    return cap


def leer_parquets(carpetas, columnas):
    partes = []
    for c in carpetas:
        for f in sorted(Path(c).glob("*.parquet")):
            partes.append(pd.read_parquet(f, columns=columnas))
    if not partes:
        raise FileNotFoundError(f"No hay parquet en {carpetas}")
    return pd.concat(partes, ignore_index=True)


def tipo_dia(fechas: pd.Series) -> pd.Series:
    try:
        import holidays
        festivos = holidays.Colombia(years=range(2023, 2027))
        es_festivo = fechas.dt.date.map(lambda d: d in festivos)
    except ImportError:
        print("AVISO: instala 'holidays' para marcar festivos; se tratan como días normales.")
        es_festivo = pd.Series(False, index=fechas.index)
    dow = fechas.dt.dayofweek
    return pd.Series(np.select([es_festivo | (dow == 6), dow == 5],
                               ["domingo_festivo", "sabado"], "laboral"), index=fechas.index)


def con_extraccion(cap, carpetas_veh, carpetas_prog, salida: Path):
    veh = leer_parquets(carpetas_veh, ["idvehiculo", "placa"]).drop_duplicates("idvehiculo")
    veh["placa"] = veh["placa"].astype(str).str.strip().str.upper()
    prog = leer_parquets(carpetas_prog, ["idprogramacion", "idvehiculo", "idruta",
                                         "fechasalida", "finalizado", "cancelada"])
    prog = prog[(prog.finalizado == "S") & (prog.cancelada == "N")].copy()
    prog["fechasalida"] = pd.to_datetime(prog.fechasalida)

    viajes = (prog.merge(veh, on="idvehiculo", how="left", validate="many_to_one")
                  .merge(cap[["placa", "plazas", "estado_capacidad"]], on="placa", how="left",
                         validate="many_to_one"))
    assert len(viajes) == len(prog)

    # Cobertura: qué parte de los viajes tiene capacidad conocida
    cobertura = viajes.groupby("idruta").agg(
        viajes=("idprogramacion", "size"),
        con_plazas=("plazas", lambda s: s.notna().sum()),
    )
    cobertura["cobertura_pct"] = 100 * cobertura.con_plazas / cobertura.viajes
    cobertura.to_csv(salida / "cobertura_capacidad_por_ruta.csv")

    # Capacidad típica por ruta, ponderada por viajes del último semestre
    reciente = viajes[viajes.fechasalida.between(*VENTANA_RECIENTE, inclusive="left")]
    semilla = reciente.groupby("idruta").plazas.agg(
        capacidad_nominal="median",
        plazas_p25=lambda s: s.quantile(0.25),
        plazas_p75=lambda s: s.quantile(0.75),
        viajes_con_dato="count",
    ).reset_index()

    # Tope de flota: buses distintos por día, p95 por tipo de día
    prog["fecha"] = prog.fechasalida.dt.normalize()
    diario = prog.groupby(["idruta", "fecha"]).idvehiculo.nunique().rename("buses_activos").reset_index()
    diario["tipo_dia"] = tipo_dia(diario.fecha)
    tope = (diario[diario.fecha >= "2025-01-01"]
            .groupby(["idruta", "tipo_dia"]).buses_activos
            .agg(flota_mediana="median", flota_tope=lambda s: int(np.ceil(s.quantile(0.95))), dias="size")
            .reset_index())
    tope.to_csv(salida / "flota_tope_por_tipo_dia.csv", index=False)

    semilla["capacidad_fuente"] = "reporte_operativo_autorizado; mediana por viaje"
    semilla["ocupacion_objetivo"] = 0.75
    semilla["parametros_version"] = "param_v1"
    semilla.to_csv(salida / "parametro_operativo_semilla.csv", index=False)
    return cobertura, semilla, tope


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reporte", required=True, type=Path)
    p.add_argument("--salida", default="salidas_flota", type=Path)
    p.add_argument("--vehiculos", nargs="*")
    p.add_argument("--programaciones", nargs="*")
    a = p.parse_args()
    a.salida.mkdir(parents=True, exist_ok=True)

    r = leer_reporte(a.reporte)
    cap = capacidad_por_placa(r)
    cap.to_csv(a.salida / "vehiculo_capacidad.csv", index=False)

    # Resumen por ruta según el reporte (sin ponderar por viajes)
    por_ruta = (r[r.idruta_reporte.notna() & ~r.es_prueba]
                .drop_duplicates(["placa", "idruta_reporte"])
                .groupby("idruta_reporte").plazas
                .agg(registros="size", plazas_mediana="median",
                     sin_dato=lambda s: int((s.fillna(0) == 0).sum())))
    por_ruta.to_csv(a.salida / "resumen_reporte_por_ruta.csv")
    print(por_ruta.to_string())
    print(cap.estado_capacidad.value_counts().to_string())

    if a.vehiculos and a.programaciones:
        cobertura, semilla, tope = con_extraccion(cap, a.vehiculos, a.programaciones, a.salida)
        print(cobertura.to_string()); print(semilla.to_string()); print(tope.to_string())


if __name__ == "__main__":
    main()
