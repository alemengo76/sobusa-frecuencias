import os
from hmac import compare_digest

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errores import ErrorAPI

bearer = HTTPBearer(auto_error=False)


def _autorizar(credenciales, permitidos):
    token = credenciales.credentials.encode() if credenciales else b""
    roles = []
    for rol in ("lector", "operador", "admin"):
        secreto = os.environ.get(f"SOBUSA_{rol.upper()}_TOKEN", "").encode()
        if secreto and token and compare_digest(secreto, token):
            roles.append(rol)
    # Una credencial compartida entre roles es ambigua y falla en cerrado.
    if len(roles) != 1:
        raise ErrorAPI(401, "no_autenticado", "Se requiere una credencial Bearer válida")
    if roles[0] not in permitidos:
        raise ErrorAPI(403, "sin_permiso", "La credencial no autoriza esta operación")


def requerir_operador(credenciales: HTTPAuthorizationCredentials | None = Depends(bearer)):
    _autorizar(credenciales, {"operador", "admin"})


def requerir_admin(credenciales: HTTPAuthorizationCredentials | None = Depends(bearer)):
    _autorizar(credenciales, {"admin"})
