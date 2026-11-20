"""Entrena un candidato GBM FE 3.1 reproducible (RF-018). No registra, no activa, no toca el activo.

  python -m scripts.entrenar_candidato --config candidato.json --fuente local
  python -m scripts.entrenar_candidato --config candidato.json --csv ../fe_v4_comun15/progvehiculos_limpio.csv

candidato.json (límites exclusivos, hora de Bogotá):
  {"nombre": "gbm_fe31_r1", "entrenamiento_hasta": "2025-01-01",
   "ajuste_hasta": "2025-04-01", "datos_hasta": "2025-07-01", "notas": "..."}

Salida en --salida (por defecto modelos/candidatos/): <version>.pkl, <version>.json (lo que lee la
API) y <version>.manifiesto.json (configuración, huellas de datos y código, versiones, métricas).
Los artefactos de --modelos (el activo) quedan protegidos: no se escriben ni se reemplazan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from app.fe31.viajes import COLUMNAS_FUENTE, RUTAS
from app.ml import candidato as C


def viajes_local(datos_hasta: str) -> tuple[pd.DataFrame, str]:
    """viaje_fuente en solo lectura, con el corte aplicado EN LA CONSULTA (nada posterior se lee)."""
    from sqlalchemy import text

    from app.db.conexion import consultar_resultados
    with consultar_resultados() as c:
        df = pd.read_sql(text(f"""SELECT {", ".join(COLUMNAS_FUENTE)} FROM ai_frecuencias.viaje_fuente
                                  WHERE idruta = ANY(:r) AND fechasalida < :corte"""),
                         c, params={"r": list(RUTAS), "corte": pd.Timestamp(datos_hasta).to_pydatetime()})
        marca = c.execute(text("SELECT ultima_actualizacion FROM ai_frecuencias.watermark "
                               "WHERE tabla_origen = 'progvehiculos'")).scalar()
    if df.empty:
        raise SystemExit("viaje_fuente no tiene viajes anteriores al corte")
    return df, (f"viaje_fuente:marca_{marca:%Y%m%dT%H%M%S}:hasta_{datos_hasta}" if marca
                else f"viaje_fuente:hasta_{datos_hasta}")


def viajes_csv(ruta: Path, datos_hasta: str) -> tuple[pd.DataFrame, str]:
    df = pd.read_csv(ruta, usecols=COLUMNAS_FUENTE, low_memory=False)
    sha = hashlib.sha256(ruta.read_bytes()).hexdigest()
    return C.recortar(df, datos_hasta), f"csv:{ruta.name}:{sha[:16]}:hasta_{datos_hasta}"


def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--config", type=Path, required=True)
    fuente = a.add_mutually_exclusive_group(required=True)
    fuente.add_argument("--fuente", choices=["local"])
    fuente.add_argument("--csv", type=Path)
    a.add_argument("--salida", type=Path, default=C.CARPETA_CANDIDATOS)
    a.add_argument("--modelos", type=Path, default=Path("modelos"), help="carpeta del activo (protegida)")
    args = a.parse_args(argv)

    cfg = C.ConfigCandidato(**json.loads(args.config.read_text(encoding="utf-8")))
    cfg.validar()                                     # antes de leer un solo dato
    protegidos = tuple(args.modelos.glob("*.pkl")) + tuple(args.modelos.glob("*.json"))
    antes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protegidos}
    crudo, version_datos = (viajes_local(cfg.datos_hasta) if args.fuente
                            else viajes_csv(args.csv, cfg.datos_hasta))
    art, manifiesto = C.entrenar(crudo, cfg, version_datos)
    ruta, sha = C.guardar(art, manifiesto, args.salida, protegidos=protegidos)
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != h for p, h in antes.items()):
        raise SystemExit("ERROR: cambió un artefacto protegido")
    print(json.dumps({"version": art.meta["version"], "artefacto": str(ruta), "sha256": sha,
                      "datos_sha256": manifiesto["datos_sha256"],
                      "arboles": art.meta["hiperparametros"]["max_iter"],
                      "filas": manifiesto["particiones"]["filas"], "metricas": manifiesto["metricas"]},
                     indent=2, default=str))


if __name__ == "__main__":
    main()
