"""Entorno de Alembic.

- La URL se toma de DATABASE_URL_WRITE: las credenciales nunca van en el código.
- La tabla alembic_version vive en ai_frecuencias, porque el usuario de escritura de
  Extreme solo podrá escribir en ese esquema (por defecto Alembic la crea en public).
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from alembic import context
from sqlalchemy import create_engine, pool

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

ESQUEMA = "ai_frecuencias"


def _url() -> str:
    url = os.environ.get("DATABASE_URL_WRITE")
    if not url:
        raise RuntimeError("Defina DATABASE_URL_WRITE (ver .env.example)")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, version_table_schema=ESQUEMA)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, version_table_schema=ESQUEMA)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
