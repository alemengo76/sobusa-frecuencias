"""Compara un candidato con el GBM activo en un periodo de validación temporal (RF-019).

  python -m scripts.comparar_candidato --candidato modelos/candidatos/<version>.json \\
      --activo modelos/gbm_fe31_20261107T065452.json --desde 2025-07-01 --hasta 2025-10-01 --fuente local

Solo lectura. Se detiene ANTES de leer datos si el periodo no está en
`app.ml.comparacion_modelos.PERIODOS_APROBADOS` (hoy vacío: B1 sin confirmar) o si algún modelo se
entrenó con datos del periodo. Aprobar un periodo es un cambio revisado de esa constante, no un
parámetro de línea de comandos. No activa ni decide: deja las métricas para RF-019 / C2.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from app.fe31.viajes import COLUMNAS_FUENTE, RUTAS
from app.ml import comparacion_modelos as K
from app.ml import modelo_gbm as G


def cargar(json_artefacto: Path) -> G.Artefacto:
    meta = json.loads(json_artefacto.read_text(encoding="utf-8"))
    return G.cargar(str(json_artefacto.with_name(meta["artefacto"])), meta["sha256"])


def viajes_local(desde, hasta) -> pd.DataFrame:
    from sqlalchemy import text

    from app.db.conexion import consultar_resultados
    inicio = pd.Timestamp(desde) - pd.Timedelta(days=G.HISTORIA_DIAS)     # historia para el día 1
    with consultar_resultados() as c:
        return pd.read_sql(text(f"""SELECT {", ".join(COLUMNAS_FUENTE)} FROM ai_frecuencias.viaje_fuente
                                    WHERE idruta = ANY(:r) AND fechasalida >= :i AND fechasalida < :h"""),
                           c, params={"r": list(RUTAS), "i": inicio.to_pydatetime(),
                                      "h": pd.Timestamp(hasta).to_pydatetime()})


def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--candidato", type=Path, required=True)
    a.add_argument("--activo", type=Path, required=True)
    a.add_argument("--desde", required=True)
    a.add_argument("--hasta", required=True)
    a.add_argument("--fuente", choices=["local"], required=True)
    a.add_argument("--salida", type=Path, default=Path("salidas_comparacion"))
    args = a.parse_args(argv)
    arts = {"activo": cargar(args.activo), "candidato": cargar(args.candidato)}
    # Se valida ANTES de leer un solo viaje del periodo
    K.comprobar_periodo(args.desde, args.hasta, {n: x.meta.get("entrenado_hasta") for n, x in arts.items()})
    r = K.comparar(arts, viajes_local(args.desde, args.hasta), args.desde, args.hasta, "activo")
    args.salida.mkdir(parents=True, exist_ok=True)
    destino = args.salida / f"comparacion_{arts['candidato'].meta['version']}_{args.desde}_{args.hasta}.json"
    destino.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    print(destino)


if __name__ == "__main__":
    main()
