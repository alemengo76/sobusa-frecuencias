"""Canal de inferencia aislado y simulación de despacho sin persistencia empresarial."""
from collections import OrderedDict
import json
import logging
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import uuid4

from app.core.errores import Conflicto, ErrorAPI, NoEncontrado, SolicitudInvalida
from app.optimizacion.frecuencias import ParametrosRuta, recomendar_fila

VERSION = "sobusa-v1.0-p25-d3-r01"
METODO = "sobusa_v1"
RAIZ = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).parent/"ml/sobusa_v1_worker.py"
log = logging.getLogger(__name__)


def inferir(solicitud):
    py = RAIZ / ".venv-worker" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    entorno = {k:v for k,v in os.environ.items() if k.lower() in
               {"systemroot","windir","temp","tmp","path","systemdrive","comspec"}}
    for clave in ("SOBUSA_HISTORICO_DIR", "SOBUSA_ARTEFACTOS_DIR", "SOBUSA_DATOS_SINTETICOS"):
        if clave in os.environ:
            entorno[clave] = os.environ[clave]
    try:
        with TemporaryDirectory(prefix="sobusa-mpl-") as cache:
            entorno["MPLCONFIGDIR"] = cache
            proc = subprocess.run([str(py),"-I","-B","-X","utf8",str(WORKER)],
                                  input=json.dumps(solicitud),capture_output=True,encoding="utf8",
                                  cwd=RAIZ,env=entorno,timeout=60,check=True)
        respuesta = json.loads(proc.stdout)
    except (subprocess.SubprocessError,OSError,ValueError) as e:
        log.error("Fallo del proveedor candidato",extra={"datos":{"tipo":type(e).__name__, "diagnostico":getattr(e,"stderr","")[-4000:]}})
        raise ErrorAPI(503,"candidato_no_disponible","Proveedor candidato no disponible") from e
    if respuesta.get("error")=="integridad_o_paridad":
        log.error("Candidato bloqueado por integridad o paridad: %s",respuesta["detalle"])
        raise Conflicto("Candidato bloqueado por integridad o paridad")
    if respuesta.get("error")=="solicitud_invalida":
        raise SolicitudInvalida(respuesta["detalle"])
    return respuesta


def simulaciones(filas):
    sintetico = os.getenv("SOBUSA_DATOS_SINTETICOS") == "1"
    ruta_parametros = os.getenv("SOBUSA_PARAMETROS_FRECUENCIA")
    if not sintetico and not ruta_parametros:
        return []
    try:
        parametros = None if sintetico else json.loads((RAIZ / ruta_parametros).read_text(encoding="utf8"))
        salida = []
        for f in filas:
            if f["horizonte_min"] != 60:
                continue
            ruta = f["idruta"]
            if sintetico:
                p = ParametrosRuta(ruta, 40, 0.75, 120, 0.5, parametros_version="sintetico_v1")
            else:
                p = ParametrosRuta(ruta, **parametros[str(ruta)][f["tipo_dia"]])
            rec = recomendar_fila(f["demanda_pred"], 60, p)
            salida.append(dict(idruta=ruta, franja_inicio=f["franja_inicio"], horizonte_min=60,
                               base_demanda="puntual", tipo="simulacion_frecuencia", **rec))
        return salida
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ErrorAPI(503, "parametros_no_disponibles", "Parámetros de frecuencia inválidos") from exc


class CorridasCandidatas:
    """Acotado y por proceso; la API empresarial conserva su repositorio original."""
    def __init__(self):
        self.corridas = OrderedDict()
        self.lock = Lock()
        self.bloqueado = False

    def ejecutar(self, solicitud):
        with self.lock:
            if self.bloqueado:
                raise Conflicto("Candidato bloqueado hasta revisión")
            try:
                resultado = inferir(solicitud)
            except Conflicto:
                self.bloqueado = True
                raise
            id_ = uuid4()
            metadata = dict(resultado["metadata"],modelo_solicitado=METODO,modelo_efectivo=METODO,
                            version_solicitada=VERSION,canal="candidato_local")
            pred = [dict(r,fecha=metadata["fecha"],model_version=metadata["version"],
                         feature_version=metadata["feature_version"],snapshot=metadata["snapshot"],
                         data_cutoff_nominal=metadata["corte_nominal"],sentido="CICLO",segmento_id="TOTAL",
                         franja_minutos=15) for r in resultado["filas"]]
            rec = simulaciones(pred)
            self.corridas[id_] = (metadata,pred,rec)
            if len(self.corridas)>16:
                self.corridas.popitem(last=False)
            return dict(run_id=str(id_),metadata=metadata,predicciones=len(pred),simulaciones_60=len(rec))

    def obtener(self,id_):
        with self.lock:
            try:
                return self.corridas[id_]
            except KeyError as e:
                raise NoEncontrado("Corrida candidata",id_) from e
