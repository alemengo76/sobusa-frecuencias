"""Carga en el Postgres LOCAL las tablas fuente desde la extracción del equipo (parquet).

Simula las tablas de Extreme en el esquema public para desarrollar sin depender de su
servidor. Se conecta como administrador (POSTGRES_*), nunca con los usuarios de la app.

Uso:
  python -m scripts.cargar_extracto_local --base "../sobusa-book-main" \
      --extracciones salidas_extraccion/ruta_2/20261012T024407_20362e05 \
                     salidas_ruta16/20261012T224023_6ecc8784 \
                     salidas_ruta21/20261013T024459_762b3a10
"""
import argparse
import os
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

TABLAS = ["progvehiculos", "vehiculos", "rutas"]
# Columnas mínimas que usa el prototipo (la fuente real tiene más)
COLUMNAS = {
    "progvehiculos": ["idprogramacion", "idvehiculo", "idruta", "proceso", "fechasalida",
                      "fecharegistro", "fechainicio", "fechafin", "finalizado", "cancelada",
                      "numeropasajeros", "numerobajadas", "apto_estadisticas_historicas",
                      "fecha_fin_liquidacion"],
    "vehiculos": ["idvehiculo", "placa", "modelo", "marca", "capacidad", "n_sentados", "n_depie"],
    "rutas": ["idruta", "nombre", "proceso", "tiempo_global", "flotadisp_prog"],
}


def leer(base: Path, tabla: str) -> pd.DataFrame:
    archivos = sorted((base / "raw" / tabla).glob("*.parquet"))
    if not archivos:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f) for f in archivos], ignore_index=True)
    return df[[c for c in COLUMNAS[tabla] if c in df.columns]]


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--base", required=True, type=Path)
    a.add_argument("--extracciones", nargs="+", required=True)
    a.add_argument("--host", default="localhost")
    args = a.parse_args()
    url = (f"postgresql+psycopg://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
           f"@{args.host}:5432/{os.environ['POSTGRES_DB']}")
    motor = create_engine(url)
    for tabla in TABLAS:
        df = pd.concat([leer(args.base / e, tabla) for e in args.extracciones], ignore_index=True)
        clave = COLUMNAS[tabla][0]
        df = df.drop_duplicates(clave)
        if "ultimaactualizacion" not in df.columns and tabla == "progvehiculos":
            # Marca de agua local simulada con fechafin; confirme la columna real con Extreme
            df["ultimaactualizacion"] = pd.to_datetime(df["fechafin"]).fillna(
                pd.to_datetime(df["fecharegistro"]))
        df.to_sql(tabla, motor, schema="public", if_exists="replace", index=False, chunksize=10_000)
        with motor.begin() as c:
            c.execute(text(f"ALTER TABLE public.{tabla} ADD PRIMARY KEY ({clave})"))
            c.execute(text(f"GRANT SELECT ON public.{tabla} TO sobusa_lector, sobusa_escritor"))
        print(f"{tabla}: {len(df):,} filas")
    with motor.begin() as c:
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_pv_ruta_salida ON public.progvehiculos (idruta, fechasalida)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_pv_marca ON public.progvehiculos (ultimaactualizacion)"))


if __name__ == "__main__":
    main()
