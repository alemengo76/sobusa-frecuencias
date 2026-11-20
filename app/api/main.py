"""API REST /api/v1 con los 10 endpoints mínimos de la especificación v1.5."""
import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import evaluacion_real, servicios
from app.api.esquemas import (ActivacionModelo, CambioEstadoAlerta, ErrorRespuesta,
                              PaginaErroresCalidad, SolicitudFrecuencias, SolicitudPrediccion,
                              SolicitudRecomendacion)
from app.core.config import obtener_config
from app.core.errores import ErrorAPI, NoEncontrado, SolicitudInvalida, cuerpo_error
from app.core.registro import configurar_logs, correlation_id
from app.db import repositorio as repo
from app.db.conexion import consultar_resultados, escribir

configurar_logs(obtener_config().nivel_log)
log = logging.getLogger("sobusa.api")

app = FastAPI(title="Sobusa · Frecuencias de despacho", version="1.0.0",
              description="Predicción de demanda, recomendación de frecuencias y alertas.")
v1 = APIRouter(prefix="/api/v1")


# ------------------------------------------------------------------ Correlation-Id y errores

@app.middleware("http")
async def correlacion(request: Request, call_next):
    cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    token = correlation_id.set(cid)
    try:
        respuesta = await call_next(request)
    finally:
        correlation_id.reset(token)
    respuesta.headers["X-Correlation-Id"] = cid
    return respuesta


@app.exception_handler(ErrorAPI)
async def manejar_error_api(_: Request, e: ErrorAPI):
    return JSONResponse(status_code=e.estado_http,
                        content=cuerpo_error(e.codigo, e.mensaje, correlation_id.get(), e.detalle))


@app.exception_handler(RequestValidationError)
async def manejar_validacion(_: Request, e: RequestValidationError):
    detalle = [{"campo": ".".join(str(x) for x in err["loc"]), "mensaje": err["msg"]}
               for err in e.errors()]
    return JSONResponse(status_code=422, content=cuerpo_error(
        "solicitud_invalida", "La solicitud no cumple el esquema", correlation_id.get(), detalle))


@app.exception_handler(Exception)
async def manejar_inesperado(_: Request, e: Exception):
    log.exception("error no controlado")
    return JSONResponse(status_code=500, content=cuerpo_error(
        "error_interno", "Error interno; use el correlation_id para rastrearlo", correlation_id.get()))


def pagina(filas: list, total: int, p: int, t: int) -> dict:
    return {"datos": filas, "paginacion": {"pagina": p, "tamano": t, "total": total,
                                           "paginas": (total + t - 1) // t if t else 0}}


def _dia(d) -> datetime:
    return datetime(d.year, d.month, d.day)


Pagina = Query(1, ge=1, alias="pagina")
Tamano = Query(50, ge=1, le=500)
AgruparPor = Query(["idruta", "horizonte_min"])

# Respuestas de error documentadas en OpenAPI (no cambian el comportamiento). Cada ruta declara
# solo los códigos que su código puede producir; todas usan el formato uniforme ErrorRespuesta.
DESCRIPCION_ERRORES = {
    404: "Recurso no encontrado",
    409: "Conflicto con el estado actual",
    422: "Solicitud inválida: no cumple el esquema o una regla de validación",
    500: "Error interno no controlado (por ejemplo, un fallo de acceso a la base de datos)",
}


def errores(*codigos: int) -> dict:
    return {c: {"model": ErrorRespuesta, "description": DESCRIPCION_ERRORES[c]} for c in codigos}


@app.get("/salud", tags=["sistema"])
def salud():
    return {"estado": "ok"}


# ------------------------------------------------------------------ 1-2. frecuencias

@v1.post("/frequency/executions", status_code=201, tags=["frecuencias"],
         responses=errores(409, 422, 500))
def crear_ejecucion_frecuencias(s: SolicitudFrecuencias):
    """Predicción + recomendación + alertas en una sola ejecución trazable."""
    return servicios.ejecutar_frecuencias(s.idrutas, _dia(s.dia), s.horizontes, s.base_demanda,
                                          correlation_id.get())


@v1.get("/frequency/executions/{execution_id}", tags=["frecuencias"],
        responses=errores(404, 422, 500))
def obtener_ejecucion(execution_id: uuid.UUID):
    with consultar_resultados() as c:
        e = repo.obtener_ejecucion(c, str(execution_id))
    if e is None:
        raise NoEncontrado("Ejecución", execution_id)
    return e


# ------------------------------------------------------------------ 3-4. predicciones

@v1.post("/predictions/run", status_code=201, tags=["predicciones"],
         responses=errores(409, 422, 500))
def correr_predicciones(s: SolicitudPrediccion):
    r = servicios.ejecutar_predicciones(s.idrutas, _dia(s.dia), s.horizontes, correlation_id.get())
    return {k: v for k, v in r.items() if not k.startswith("_")}


@v1.get("/predictions", tags=["predicciones"],
        responses=errores(422, 500))
def listar_predicciones(idruta: int | None = None, horizonte_min: int | None = None,
                        execution_id: str | None = None, desde: datetime | None = None,
                        hasta: datetime | None = None, pagina_n: int = Pagina,
                        tamano: int = Tamano):
    with consultar_resultados() as c:
        filas, total = repo.consultar(c, "prediction", {"idruta": idruta, "horizonte_min": horizonte_min,
                                                        "execution_id": execution_id},
                                      desde, hasta, pagina_n, tamano)
    return pagina(filas, total, pagina_n, tamano)


# ------------------------------------------------------------------ M1. predicción contra real

def _validar_filtros_precision(idruta, horizonte_min):
    if idruta is not None and idruta not in (2, 16, 21):
        raise SolicitudInvalida("idruta debe ser 2, 16 o 21")
    if horizonte_min is not None and horizonte_min not in (30, 60, 120):
        raise SolicitudInvalida("horizonte_min debe ser 30, 60 o 120")


@v1.get("/predictions/accuracy", tags=["predicciones"])
def precision_predicciones(model_id: int | None = None, execution_id: uuid.UUID | None = None,
                           idruta: int | None = None, horizonte_min: int | None = None,
                           desde: datetime | None = None, hasta: datetime | None = None,
                           agrupar_por: list[str] = AgruparPor):
    """Métricas predicción-real (MAE, RMSE, WAPE, sMAPE, sesgo, cobertura del intervalo y MAE
    relativo al ingenuo de 7 días) por modelo. Solo usa reales consolidados y medibles; el resto se
    cuenta por estado. desde/hasta filtran franja_inicio [desde, hasta)."""
    _validar_filtros_precision(idruta, horizonte_min)
    invalidas = [g for g in agrupar_por if g not in evaluacion_real.cr.AGRUPACIONES]
    if invalidas:
        raise SolicitudInvalida(f"agrupar_por no soportado: {invalidas}",
                                {"permitidos": list(evaluacion_real.cr.AGRUPACIONES)})
    with consultar_resultados() as c:
        r = evaluacion_real.evaluar(c, model_id, None if execution_id is None else str(execution_id),
                                    idruta, horizonte_min, desde, hasta)
    return evaluacion_real.resumen_metricas(r, tuple(dict.fromkeys(agrupar_por)))


@v1.get("/predictions/accuracy/pairs", tags=["predicciones"])
def pares_prediccion_real(model_id: int | None = None, execution_id: uuid.UUID | None = None,
                          idruta: int | None = None, horizonte_min: int | None = None,
                          desde: datetime | None = None, hasta: datetime | None = None,
                          estado: str | None = None, pagina_n: int = Pagina,
                          tamano: int = Tamano):
    """Cada predicción con su real, estado y motivo de exclusión (paginado)."""
    _validar_filtros_precision(idruta, horizonte_min)
    if estado is not None and estado not in evaluacion_real.cr.ESTADOS:
        raise SolicitudInvalida(f"estado no válido: {estado}",
                                {"permitidos": list(evaluacion_real.cr.ESTADOS)})
    with consultar_resultados() as c:
        r = evaluacion_real.evaluar(c, model_id, None if execution_id is None else str(execution_id),
                                    idruta, horizonte_min, desde, hasta)
    filas, total = evaluacion_real.pares_json(r["pares"], estado, pagina_n, tamano)
    return {**pagina(filas, total, pagina_n, tamano), "parametros": r["parametros"],
            "huella": r["huella"]}


# ------------------------------------------------------------------ 5-6. recomendaciones

@v1.post("/recommendations/calculate", status_code=201, tags=["recomendaciones"],
         responses=errores(404, 422, 500))
def calcular_recomendaciones(s: SolicitudRecomendacion):
    with escribir() as c:
        pred = servicios.predicciones_de_ejecucion(c, s.execution_id_prediccion)
        eid = repo.crear_ejecucion(c, "recomendacion", s.model_dump(), correlation_id.get())
        rec = servicios.calcular_recomendaciones(c, eid, pred, s.base_demanda)
        resumen = {"recomendaciones": len(rec), "no_viables": int((~rec.es_viable).sum())}
        repo.cerrar_ejecucion(c, eid, "exitosa", resumen)
    return {"execution_id": eid, **resumen}


@v1.get("/recommendations", tags=["recomendaciones"],
        responses=errores(422, 500))
def listar_recomendaciones(idruta: int | None = None, horizonte_min: int | None = None,
                           es_viable: bool | None = None, execution_id: str | None = None,
                           desde: datetime | None = None, hasta: datetime | None = None,
                           pagina_n: int = Pagina, tamano: int = Tamano):
    with consultar_resultados() as c:
        filas, total = repo.consultar(c, "recommendation",
                                      {"idruta": idruta, "horizonte_min": horizonte_min,
                                       "es_viable": es_viable, "execution_id": execution_id},
                                      desde, hasta, pagina_n, tamano)
    return pagina(filas, total, pagina_n, tamano)


# ------------------------------------------------------------------ 7-8. alertas

@v1.get("/alerts", tags=["alertas"],
        responses=errores(422, 500))
def listar_alertas(idruta: int | None = None, estado: str | None = None,
                   severidad: str | None = None, codigo: str | None = None,
                   desde: datetime | None = None, hasta: datetime | None = None,
                   pagina_n: int = Pagina, tamano: int = Tamano):
    with consultar_resultados() as c:
        filas, total = repo.consultar(c, "alert", {"idruta": idruta, "estado": estado,
                                                   "severidad": severidad, "codigo": codigo},
                                      desde, hasta, pagina_n, tamano)
    return pagina(filas, total, pagina_n, tamano)


@v1.patch("/alerts/{alert_id}/status", tags=["alertas"],
          responses=errores(404, 409, 422, 500))
def cambiar_estado_alerta(alert_id: int, s: CambioEstadoAlerta):
    """Reconocer, asignar, comentar (mismo estado + comentario), cerrar o descartar."""
    return servicios.cambiar_estado_alerta(alert_id, s.estado, s.usuario, s.comentario,
                                           s.usuario_asignado)


# ------------------------------------------------------------------ 9-10. modelos

@v1.get("/models", tags=["modelos"],
        responses=errores(500))
def listar_modelos():
    with consultar_resultados() as c:
        return {"datos": repo.listar_modelos(c)}


@v1.post("/models/{model_id}/activate", tags=["modelos"],
         responses=errores(404, 409, 422, 500))
def activar_modelo(model_id: int, s: ActivacionModelo):
    """Activa un modelo y retira el anterior. Para revertir, active de nuevo el anterior."""
    with escribir() as c:
        servicios.verificar_activable(c, model_id)
        r = repo.activar_modelo(c, model_id, s.usuario, s.motivo)
    if r is None:
        raise NoEncontrado("Modelo", model_id)
    return r


# ------------------------------------------------------------------ 11. calidad de datos (G3)

@v1.get("/quality/errors", tags=["calidad"], response_model=PaginaErroresCalidad,
        responses=errores(422, 500))
def listar_errores_calidad(lote: str = Query(..., min_length=1, max_length=120),
                           alcance: Literal["auto", "ejecucion", "ventana"] = "auto",
                           idruta: int | None = None,
                           regla: str | None = Query(None, min_length=1, max_length=100),
                           tabla_origen: str | None = Query(None, min_length=1, max_length=100),
                           reglas_version: str | None = Query(None, min_length=1, max_length=50),
                           causa: str | None = Query(None, min_length=1),
                           pagina_n: int = Pagina, tamano: int = Tamano):
    """Errores de calidad de un lote, con el evento deduplicado al que pertenece cada detección.

    alcance: 'ventana' = lote exacto; 'ejecucion' = sus ventanas <lote>_NNN; 'auto' = ambos.
    """
    with consultar_resultados() as c:
        filas, total = repo.errores_calidad_por_lote(
            c, lote, alcance, {"idruta": idruta, "regla": regla, "tabla_origen": tabla_origen,
                               "reglas_version": reglas_version, "causa": causa},
            pagina_n, tamano)
    return pagina(filas, total, pagina_n, tamano)


app.include_router(v1)
