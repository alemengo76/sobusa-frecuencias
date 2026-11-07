"""Configuración: todo sale de variables de entorno (ninguna credencial en el código)."""
import os
from dotenv import load_dotenv
from dataclasses import dataclass, field

load_dotenv()



def _env(nombre: str, defecto: str | None = None) -> str | None:
    return os.environ.get(nombre, defecto)


@dataclass(frozen=True)
class Config:
    database_url_read: str | None = field(default_factory=lambda: _env("DATABASE_URL_READ"))
    database_url_write: str | None = field(default_factory=lambda: _env("DATABASE_URL_WRITE"))
    esquema_fuente: str = field(default_factory=lambda: _env("ESQUEMA_FUENTE", "public"))
    columna_marca_agua: str = field(
        default_factory=lambda: _env("COLUMNA_MARCA_AGUA", "fin_resultado"))
    dias_revision: int = field(default_factory=lambda: int(_env("DIAS_REVISION", "3")))
    tamano_pagina_max: int = field(default_factory=lambda: int(_env("TAMANO_PAGINA_MAX", "500")))
    notificaciones_simuladas: bool = field(
        default_factory=lambda: _env("NOTIFICACIONES_SIMULADAS", "true").lower() == "true")
    correo_destino: str | None = field(default_factory=lambda: _env("ALERTAS_CORREO_DESTINO"))
    whatsapp_destino: str | None = field(default_factory=lambda: _env("ALERTAS_WHATSAPP_DESTINO"))
    nivel_log: str = field(default_factory=lambda: _env("NIVEL_LOG", "INFO"))
    # Carpeta de artefactos de modelos (en Docker es un volumen: ./modelos -> /app/modelos)
    modelos_dir: str = field(default_factory=lambda: _env("MODELOS_DIR", "modelos"))
    # Línea base que se usa si el modelo activo (GBM) no puede predecir
    metodo_respaldo: str = field(default_factory=lambda: _env("METODO_RESPALDO", "dia_hora_4sem"))


def obtener_config() -> Config:
    return Config()
