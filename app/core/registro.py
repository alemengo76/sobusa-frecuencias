"""Logs en JSON con Correlation-Id (requisito no funcional)."""
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")


class FormatoJSON(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        datos = {
            "momento": datetime.now(timezone.utc).isoformat(),
            "nivel": record.levelname,
            "modulo": record.name,
            "mensaje": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        extra = getattr(record, "datos", None)
        if isinstance(extra, dict):
            datos.update(extra)
        if record.exc_info:
            datos["excepcion"] = self.formatException(record.exc_info)
        return json.dumps(datos, ensure_ascii=False, default=str)


def configurar_logs(nivel: str = "INFO") -> None:
    manejador = logging.StreamHandler(sys.stdout)
    manejador.setFormatter(FormatoJSON())
    raiz = logging.getLogger()
    raiz.handlers = [manejador]
    raiz.setLevel(nivel)
