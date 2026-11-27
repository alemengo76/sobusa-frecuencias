"""API de consulta de alertas para Render."""
import logging
import os
from datetime import datetime
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException, Query
from psycopg.rows import dict_row

app = FastAPI(title="SOBUSA · Consulta de alertas", docs_url=None, redoc_url=None)
log = logging.getLogger(__name__)
DATABASE = "sobusa_alertas_render"
READER = "sobusa_alertas_lector"
COLUMNS = (
    "alert_id, codigo, severidad, estado, idruta, franja_inicio, horizonte_min, "
    "valor_observado, valor_esperado, umbral, puntuacion_anomalia, explicacion, recomendacion"
)


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Falta DATABASE_URL")
    return psycopg.connect(url, connect_timeout=5, row_factory=dict_row,
                           options="-c statement_timeout=5000")


def verify_reader(cur):
    cur.execute("""
        SELECT current_database() AS db, current_user AS role,
               has_table_privilege(current_user, 'public.alertas_publicas', 'SELECT') AS lectura,
               has_table_privilege(current_user, 'public.alertas_publicas',
                                   'INSERT, UPDATE, DELETE, TRUNCATE') AS escritura
    """)
    if cur.fetchone() != {"db": DATABASE, "role": READER, "lectura": True, "escritura": False}:
        raise RuntimeError("La base o el rol no corresponde a la bandeja de consulta")


@app.get("/healthz")
def health():
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            verify_reader(cur)
    except (psycopg.Error, RuntimeError) as exc:
        log.warning("Bandeja no disponible (%s)", type(exc).__name__)
        raise HTTPException(503, "Base de consulta no disponible") from None
    return {"estado": "ok"}


@app.get("/api/v1/alerts")
def alerts(
    idruta: int | None = Query(None, gt=0),
    estado: Literal["abierta", "reconocida", "asignada", "cerrada", "descartada"] | None = None,
    severidad: Literal["baja", "media", "alta"] | None = None,
    codigo: str | None = Query(None, pattern=r"^ALT-[0-9]{3}$"),
    desde: datetime | None = None,
    hasta: datetime | None = None,
    pagina: int = Query(1, ge=1, le=10_000_000),
    tamano: int = Query(50, ge=1, le=500),
):
    if any(d is not None and d.utcoffset() is None for d in (desde, hasta)):
        raise HTTPException(422, "Indique la zona horaria de las fechas")
    if desde is not None and hasta is not None and desde >= hasta:
        raise HTTPException(422, "El rango de fechas es inválido")
    conditions = []
    args = []
    for field, op, value in (
        ("idruta", "=", idruta), ("estado", "=", estado),
        ("severidad", "=", severidad), ("codigo", "=", codigo),
        ("franja_inicio", ">=", desde), ("franja_inicio", "<", hasta),
    ):
        if value is not None:
            conditions.append(f"{field} {op} %s")
            args.append(value)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            verify_reader(cur)
            cur.execute("SELECT count(*) AS total FROM public.alertas_publicas" + where, args)
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT {COLUMNS} FROM public.alertas_publicas{where} "
                "ORDER BY alert_id DESC LIMIT %s OFFSET %s",
                [*args, tamano, (pagina - 1) * tamano],
            )
            records = cur.fetchall()
    except (psycopg.Error, RuntimeError) as exc:
        log.warning("Consulta no disponible (%s)", type(exc).__name__)
        raise HTTPException(503, "Base de consulta no disponible") from None
    return {"datos": records, "paginacion": {"pagina": pagina, "tamano": tamano,
                                            "total": total, "paginas": (total + tamano - 1) // tamano}}
