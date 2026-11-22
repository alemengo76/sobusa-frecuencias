"""Punto de entrada G4 para ingesta incremental y diagnóstico de ALT-009."""
import argparse
import json
import logging
from datetime import date, datetime, time

import pandas as pd
from sqlalchemy import text

from app import ingesta
from app.alertas import motor
from app.core.config import obtener_config
from app.db import repositorio
from app.db.conexion import consultar_resultados

log = logging.getLogger(__name__)


def _diagnosticar_alt_009(desde: datetime, hasta: datetime, marca) -> dict:
    dias_revision = obtener_config().dias_revision
    with consultar_resultados() as conexion:
        viajes = repositorio.viajes_fuente(conexion, ingesta.RUTAS, desde, hasta)
        detecciones = pd.read_sql(
            text(motor.CONSULTA_DETECCIONES_ALT009),
            conexion,
            params={
                "tabla_origen": motor.TABLA_ORIGEN_ALT009,
                "regla": motor.REGLA_SIN_CONTEO,
                "reglas_version": motor.cuarentena.REGLAS_VERSION,
            },
        )
    cobertura = motor.cobertura_alt_009(viajes, detecciones, marca, dias_revision)
    diagnostico = motor.diagnostico_alt_009(viajes, detecciones, marca, dias_revision)
    return {
        "estado": "diagnosticada",
        "cobertura": cobertura.to_dict(orient="records"),
        "diagnostico": diagnostico,
        "alcance_detecciones": "La correspondencia se comprueba solo en los viajes leídos.",
    }


def correr(desde: date, hasta: date) -> dict:
    """Ejecuta la ingesta y diagnostica ALT-009 en días operativos [desde, hasta)."""
    if isinstance(desde, datetime) or isinstance(hasta, datetime) or desde >= hasta:
        raise ValueError("Se requieren fechas de días completos con desde anterior a hasta.")
    inicio = datetime.combine(desde, time.min)
    fin = datetime.combine(hasta, time.min)

    resultado = ingesta.correr()
    marca = resultado["marca_agua"]
    if marca is None:
        diagnostico = {"estado": "no_evaluable", "motivo": "sin_marca_confirmada"}
    else:
        try:
            diagnostico = _diagnosticar_alt_009(inicio, fin, marca)
        except Exception:
            log.error("Falló el diagnóstico pos-ingesta ALT-009")
            diagnostico = {"estado": "error", "motivo": "fallo_diagnostico_alt_009"}

    return {"ingesta": resultado, "alt_009": diagnostico}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desde", type=date.fromisoformat, required=True, help="Fecha inicial incluida")
    parser.add_argument("--hasta", type=date.fromisoformat, required=True, help="Fecha final excluida")
    args = parser.parse_args()
    print(json.dumps(correr(args.desde, args.hasta), ensure_ascii=False, default=str))
