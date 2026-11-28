"""API de consulta de alertas para Render."""
import csv
import hmac
import io
import logging
import re
import os
from datetime import datetime
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

app = FastAPI(title="SOBUSA · Consulta de alertas", docs_url=None, redoc_url=None, openapi_url=None)
log = logging.getLogger(__name__)
DATABASE = "sobusa_alertas_render"
READER = "sobusa_alertas_lector"
COLUMNS = (
    "alert_id, codigo, severidad, estado, idruta, franja_inicio, horizonte_min, "
    "valor_observado, valor_esperado, umbral, puntuacion_anomalia, explicacion, recomendacion"
)


EXPORT_LIMIT = 8 * 1024 * 1024
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43,128}")


@app.middleware("http")
async def authorize(request: Request, call_next):
    """Health checks are public; every other route is server-to-server only."""
    if request.url.path != "/healthz":
        token = os.environ.get("SOBUSA_API_TOKEN", "")
        if not TOKEN_FORMAT.fullmatch(token):
            return JSONResponse({"detail": "Acceso de consulta sin configurar"}, 503,
                                headers={"Cache-Control": "no-store"})
        authorization = request.headers.getlist("authorization")
        supplied = authorization[0].removeprefix("Bearer ") if len(authorization) == 1 else ""
        if (len(authorization) != 1 or not authorization[0].startswith("Bearer ")
                or not TOKEN_FORMAT.fullmatch(supplied) or not hmac.compare_digest(supplied, token)):
            return JSONResponse({"detail": "Acceso no autorizado"}, 401,
                                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


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

@app.get("/api/v1/alerts/export")
def export_alerts():
    """CSV de la proyección autorizada, sin filtros ni paginación."""
    fields = COLUMNS.split(", ")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(fields)
    output = bytearray.fromhex("efbbbf")

    def flush():
        output.extend(buffer.getvalue().encode("utf-8"))
        if len(output) > EXPORT_LIMIT:
            raise HTTPException(413, "El CSV completo excede el límite de descarga")
        buffer.seek(0)
        buffer.truncate()

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                verify_reader(cur)
            with conn.cursor(name="sobusa_csv") as cur:
                cur.execute(f"SELECT {COLUMNS} FROM public.alertas_publicas ORDER BY alert_id DESC")
                for row in cur:
                    writer.writerow([csv_cell(row[field]) for field in fields])
                    if buffer.tell() >= 8192:
                        flush()
                flush()
    except (psycopg.Error, RuntimeError) as exc:
        log.warning("Exportación no disponible (%s)", type(exc).__name__)
        raise HTTPException(503, "Base de consulta no disponible") from None
    return Response(bytes(output), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="sobusa_alertas_autorizadas.csv"'})
