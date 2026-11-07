"""Dos conexiones separadas, como exige la especificación:

- lectura: usuario de solo SELECT; cada transacción se abre READ ONLY.
- escritura: usuario con permisos solo sobre el esquema ai_frecuencias.
"""
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from app.core.config import obtener_config

# Toda la aplicación trabaja en hora local de Barranquilla, sin importar el servidor
OPCIONES = {"options": "-c timezone=America/Bogota"}


@lru_cache
def motor_lectura() -> Engine:
    url = obtener_config().database_url_read
    if not url:
        raise RuntimeError("Defina DATABASE_URL_READ (ver .env.example)")
    motor = create_engine(url, pool_pre_ping=True, pool_size=5, connect_args=OPCIONES)

    @event.listens_for(motor, "begin")
    def _solo_lectura(conexion):
        conexion.exec_driver_sql("SET TRANSACTION READ ONLY")

    return motor


@lru_cache
def motor_escritura() -> Engine:
    url = obtener_config().database_url_write
    if not url:
        raise RuntimeError("Defina DATABASE_URL_WRITE (ver .env.example)")
    return create_engine(url, pool_pre_ping=True, pool_size=5, connect_args=OPCIONES)


@contextmanager
def leer():
    with motor_lectura().connect() as c:
        with c.begin():
            yield c


@contextmanager
def escribir():
    with motor_escritura().begin() as c:
        c.execute(text("SET LOCAL search_path TO ai_frecuencias"))
        yield c


@contextmanager
def consultar_resultados():
    """Lee los resultados del prototipo (ai_frecuencias) de NUESTRA base, en modo solo lectura.

    La conexión de lectura (DATABASE_URL_READ) apunta a Extreme y allí no existe ai_frecuencias.
    """
    with motor_escritura().connect() as c:
        with c.begin():
            c.exec_driver_sql("SET TRANSACTION READ ONLY")
            c.execute(text("SET LOCAL search_path TO ai_frecuencias"))
            yield c
