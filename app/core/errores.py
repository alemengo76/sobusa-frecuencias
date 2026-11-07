"""Errores uniformes de la API: siempre {"error": {codigo, mensaje, detalle, correlation_id}}."""


class ErrorAPI(Exception):
    def __init__(self, estado_http: int, codigo: str, mensaje: str, detalle=None):
        super().__init__(mensaje)
        self.estado_http = estado_http
        self.codigo = codigo
        self.mensaje = mensaje
        self.detalle = detalle


class NoEncontrado(ErrorAPI):
    def __init__(self, recurso: str, identificador):
        super().__init__(404, "no_encontrado", f"{recurso} {identificador} no existe")


class SolicitudInvalida(ErrorAPI):
    def __init__(self, mensaje: str, detalle=None):
        super().__init__(422, "solicitud_invalida", mensaje, detalle)


class Conflicto(ErrorAPI):
    def __init__(self, mensaje: str, detalle=None):
        super().__init__(409, "conflicto", mensaje, detalle)


def cuerpo_error(codigo: str, mensaje: str, correlation_id: str, detalle=None) -> dict:
    return {"error": {"codigo": codigo, "mensaje": mensaje, "detalle": detalle,
                      "correlation_id": correlation_id}}
