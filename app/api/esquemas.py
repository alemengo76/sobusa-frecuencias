"""Esquemas de entrada de la API (validados por Pydantic y publicados en OpenAPI 3)."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Ruta = Literal[2, 16, 21]
Horizonte = Literal[30, 60, 120]
EstadoAlerta = Literal["abierta", "reconocida", "asignada", "cerrada", "descartada"]


class SolicitudPrediccion(BaseModel):
    idrutas: list[Ruta] = Field(default=[2, 16, 21], min_length=1)
    dia: date = Field(description="Día a predecir; se usan datos hasta las 00:00 de ese día (D+1)")
    horizontes: list[Horizonte] = Field(default=[30, 60, 120], min_length=1)


class SolicitudFrecuencias(SolicitudPrediccion):
    base_demanda: Literal["puntual", "superior"] = "puntual"


class SolicitudRecomendacion(BaseModel):
    execution_id_prediccion: str = Field(description="Ejecución de POST /predictions/run")
    base_demanda: Literal["puntual", "superior"] = "puntual"


class CambioEstadoAlerta(BaseModel):
    estado: EstadoAlerta
    usuario: str = Field(min_length=1, max_length=100)
    comentario: str | None = Field(default=None, max_length=2000)
    usuario_asignado: str | None = Field(default=None, max_length=100)


class ActivacionModelo(BaseModel):
    usuario: str = Field(min_length=1, max_length=100)
    motivo: str = Field(min_length=3, max_length=500)


# ------------------------------------------------------------------ errores (solo documentación OpenAPI)
# Reflejan app/core/errores.py: cuerpo_error() siempre escribe las cuatro claves.


class DetalleCampo(BaseModel):
    campo: str = Field(description="Ubicación del dato inválido, p. ej. 'body.idrutas.0'")
    mensaje: str = Field(description="Motivo informado por la validación")


class CuerpoError(BaseModel):
    codigo: str = Field(description="solicitud_invalida (422), no_encontrado (404), conflicto (409) "
                                    "o error_interno (500)")
    mensaje: str
    detalle: list[DetalleCampo] | None = Field(
        description="Campos inválidos en los errores de validación; null en los demás")
    correlation_id: str = Field(description="Identificador de correlación asociado a la solicitud")


class ErrorRespuesta(BaseModel):
    """Formato uniforme de error de la API (app/core/errores.py: cuerpo_error)."""
    error: CuerpoError
