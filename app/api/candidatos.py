"""API versionada del candidato histórico. Sin identificadores de PostgreSQL."""
from datetime import date
import os
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.api.autorizacion import requerir_operador
from app.core.errores import SolicitudInvalida
from app.servicios_candidatos import CorridasCandidatas, METODO, VERSION

router = APIRouter(prefix="/api/v1",tags=["candidatos"],dependencies=[Depends(requerir_operador)])
corridas = CorridasCandidatas()


class SolicitudCandidata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modelo: Literal["sobusa_v1"]
    version: Literal["sobusa-v1.0-p25-d3-r01"]
    fecha: date
    rutas: Annotated[list[StrictInt],Field(min_length=1,max_length=3)]
    horizontes: Annotated[list[StrictInt],Field(min_length=1,max_length=3)]

    @field_validator("rutas","horizontes")
    @classmethod
    def valores(cls,v,info):
        permitidos = (2,16,21) if info.field_name=="rutas" else (30,60,120)
        if len(v)!=len(set(v)) or set(v)-set(permitidos):
            raise ValueError(f"Valores únicos de {permitidos}")
        return v

    @field_validator("fecha")
    @classmethod
    def periodo(cls,v):
        if not date(2025,4,1)<=v<=date(2025,6,30):
            raise ValueError("Fecha fuera del bloque retrospectivo p25")
        return v


@router.get("/candidate-models")
def catalogo():
    modo = "sintetico" if os.getenv("SOBUSA_DATOS_SINTETICOS") == "1" else "retrospectivo"
    return {"datos":[dict(modelo=METODO,version=VERSION,modo=modo,intervalos="no_calibrados",
                           horizontes=[30,60,120],horizonte_frecuencia=60,activable=False)]}


@router.post("/candidate-runs",status_code=201)
def ejecutar(s: SolicitudCandidata):
    return corridas.ejecutar(s.model_dump(mode="json"))


Pagina = Annotated[int, Query(ge=1,alias="pagina")]
Tamano = Annotated[int, Query(ge=1,le=500)]


def pagina(filas,meta,r,h,p,t):
    filtradas = [f for f in filas if (r is None or f["idruta"]==r) and f["horizonte_min"]==h]
    n = len(filtradas)
    return dict(datos=filtradas[(p-1)*t:p*t],paginacion=dict(pagina=p,tamano=t,total=n,paginas=(n+t-1)//t),
                metadata=meta)


@router.get("/candidate-runs/{run_id}/predictions")
def predicciones(run_id: UUID,idruta: int | None=None,
                 horizonte_min: int=60,pagina_n: Pagina=1,tamano: Tamano=50):
    if idruta not in (None,2,16,21) or horizonte_min not in (30,60,120):
        raise SolicitudInvalida("Ruta u horizonte no permitido")
    meta,pred,_ = corridas.obtener(run_id)
    return pagina(pred,meta,idruta,horizonte_min,pagina_n,tamano)


@router.get("/candidate-runs/{run_id}/recommendations")
def recomendaciones(run_id: UUID,idruta: int | None=None,horizonte_min: int=60,
                    base_demanda: Literal["puntual","superior"]="puntual",pagina_n: Pagina=1,tamano: Tamano=50):
    if idruta not in (None,2,16,21) or horizonte_min!=60:
        raise SolicitudInvalida("Simulación disponible a 60 minutos")
    if base_demanda=="superior":
        raise SolicitudInvalida("El candidato no tiene intervalos calibrados")
    meta,_,rec = corridas.obtener(run_id)
    return pagina(rec,dict(meta,tipo="simulacion_frecuencia",parametros=(rec[0]["parametros_version"] if rec else None)),idruta,horizonte_min,pagina_n,tamano)
