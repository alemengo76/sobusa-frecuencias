"""Esquemas de entrada de la API (validados por Pydantic y publicados en OpenAPI 3)."""
from datetime import date, datetime
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


# ------------------------------------------------------------------ explicación de una predicción (RF-015)


class AporteVariable(BaseModel):
    variable: str
    valor: float | int | str | None = Field(description="Valor de la variable de FE 3.1 (null si falta)")
    valor_modelo: float | None = Field(description="Valor que recibe el modelo (código fijo en categóricas; "
                                                   "null si falta o la categoría es desconocida)")
    aporte_log: float = Field(description="Aporte TreeSHAP en escala log (pérdida Poisson)")
    factor: float = Field(description="exp(aporte_log): factor que multiplica la predicción")


class ImportanciaArtefacto(BaseModel):
    metodo: str
    model_version: str
    nota: str
    variables: list[dict] = Field(description="variable, ganancia, proporcion, divisiones")


class ExplicacionPrediccion(BaseModel):
    """Explicación individual TreeSHAP, RECONSTRUIDA Y VERIFICADA (no garantizada: ver advertencia)."""
    prediction_id: int
    execution_id: str
    model_id: int
    model_version: str
    feature_version: str
    artefacto_sha256: str
    scikit_learn: str
    idruta: int
    franja_inicio: datetime
    horizonte_min: int
    data_cutoff: datetime
    demanda_pred: float
    estado: Literal["reconstruida_y_verificada"]
    advertencia: str
    metodo: str
    escala: str
    referencia_log: float
    referencia_pasajeros: float
    prediccion_log: float
    prediccion_pasajeros: float
    aportes: list[AporteVariable]
    verificaciones: dict
    importancia_global_artefacto: ImportanciaArtefacto


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
