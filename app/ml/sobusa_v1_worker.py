"""Proceso privado de inferencia candidato, ejecutado con el entorno de cierre."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sobusa_v1_runtime.interfaz import IntegridadError, ParidadError, predecir


def ejecutar(s):
    opcion = os.getenv("SOBUSA_DATOS_SINTETICOS", "0")
    if opcion not in ("0", "1") or (opcion == "1" and os.getenv("SOBUSA_HISTORICO_DIR")):
        raise ValueError("Configuración incompatible de datos sintéticos e históricos")
    modo = "sintetico" if opcion == "1" else "retrospectivo"
    return predecir(s["fecha"], s["rutas"], s["horizontes"], modo=modo)


if __name__=="__main__":
    try:
        out = ejecutar(json.load(sys.stdin))
    except (IntegridadError,ParidadError) as e:
        out = {"error":"integridad_o_paridad","detalle":str(e)}
    except ValueError as e:
        out = {"error":"solicitud_invalida","detalle":str(e)}
    print(json.dumps(out,ensure_ascii=False,allow_nan=False))
