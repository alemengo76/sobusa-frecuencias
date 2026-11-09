"""Lectura incremental diaria con marca de agua (E07).

1. Divide el tiempo de la marca de agua en ventanas consecutivas (A, B].
2. Para cada ventana, busca los días de salida que tienen algún cambio con marca en (A, B]
   y los vuelve a agregar COMPLETOS desde la fuente: así una carga repetida, o un viaje que
   se actualiza, no duplica nada.
3. Aplica las reglas comun-1.5 (las mismas del feature engineering y del modelo) y manda a
   cuarentena lo que no pasa (con lote, regla, causa y fila). Copia los viajes del día a
   ai_frecuencias.viaje_fuente: de ahí el GBM calcula sus variables sin volver a Extreme.
4. Guarda B como marca de agua en la MISMA transacción. Todo lo anterior a B quedó procesado,
   así que si la carga se interrumpe, retoma desde la última ventana guardada sin saltarse nada.
"""
import logging
import re
import time
import tracemalloc
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import text

from app.calidad.cuarentena import REGLAS_VERSION, elegibles, evaluar_programaciones
from app.fe31.viajes import COLUMNAS_FUENTE, limpiar_fuente
from app.core.config import obtener_config
from app.db.conexion import escribir, leer

log = logging.getLogger("sobusa.ingesta")
RUTAS = (2, 16, 21)
COLUMNAS = ("idprogramacion, idruta, idvehiculo, fechasalida, fechainicio, fechafin, "
            "finalizado, cancelada, numeropasajeros, numerobajadas")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

# Un error de calidad = un EVENTO (tabla_origen + idprogramacion + regla + reglas_version + valor)
# con una DETECCIÓN por cada lote en que se vio (migración 0007, DECISIONES.md §8). En una sola
# sentencia: inserta el evento o, si ya existe, solo actualiza su última detección; RETURNING da su
# id en los dos casos (por eso DO UPDATE y no DO NOTHING) y con él se registra la detección del
# lote. Reintentar el mismo lote no la repite (UNIQUE (evento_id, lote)).
# El ON CONFLICT debe repetir EXACTAMENTE las expresiones del índice uq_dq_evento.
SQL_EVENTO_CALIDAD = """
WITH evento AS (
    INSERT INTO data_quality_error AS e
           (lote, tabla_origen, idprogramacion, idruta, regla, causa, valor, reglas_version,
            ultimo_lote, ultima_deteccion)
    VALUES (:lote, :tabla_origen, :idprogramacion, :idruta, :regla, :causa, :valor, :reglas_version,
            :lote, now())
    ON CONFLICT (tabla_origen, idprogramacion, regla, reglas_version, (valor IS NULL), (coalesce(valor, '')))
    DO UPDATE SET ultimo_lote = EXCLUDED.ultimo_lote, ultima_deteccion = EXCLUDED.ultima_deteccion
    RETURNING e.id
)
INSERT INTO data_quality_deteccion (evento_id, lote, detectado_en, idruta, causa, valor_original)
SELECT id, :lote, now(), :idruta, :causa, :valor_original FROM evento
ON CONFLICT (evento_id, lote) DO NOTHING
"""
_FALTANTE = {"NaN", "nan"}


def filas_calidad(cuarentena: pd.DataFrame, reglas_version: str, tabla_origen: str = "progvehiculos") -> list[dict]:
    """Parámetros de SQL_EVENTO_CALIDAD. valor va normalizado (un faltante es NULL, nunca el texto
    'NaN'); valor_original es lo que llegó."""
    filas = []
    for r in cuarentena.astype(object).where(cuarentena.notna(), None).to_dict("records"):
        original = r.get("valor")
        filas.append({**r, "tabla_origen": tabla_origen, "reglas_version": reglas_version,
                      "valor_original": original,
                      "valor": None if original is None or original in _FALTANTE else original})
    return filas


# progvehiculos no tiene columna de última actualización. La marca de agua es el momento
# en que el resultado del viaje quedó disponible: el fin de la liquidación si existe,
# si no, el fin del viaje (es la misma regla que usa fn_insert_timbradas_anio de Extreme).
MARCAS = {
    "fin_resultado": "COALESCE(fecha_fin_liquidacion, fechafin)",
    "fechafin": "fechafin",
    "fecharegistro": "fecharegistro",
}


def expresion_marca(nombre: str) -> str:
    if nombre in MARCAS:
        return MARCAS[nombre]
    return _identificador(nombre)


def _identificador(nombre: str) -> str:
    if not _IDENT.match(nombre):
        raise ValueError(f"Identificador no válido: {nombre}")
    return nombre


def agregar_por_franja(viajes_validos: pd.DataFrame, todos: pd.DataFrame, lote: str) -> pd.DataFrame:
    """Pasajeros y programaciones por ruta y franja de 15 min (por salida programada).

    Misma definición que la etiqueta del FE (comun-1.5): pasajeros = suma de los viajes
    elegibles con conteo válido; la franja queda SIN medición (NULL) si algún viaje elegible no
    tiene conteo usable o está en cuarentena. programaciones = salidas no canceladas (RF-008)."""
    t = limpiar_fuente(todos).assign(franja_inicio=lambda d: d.fechasalida.dt.floor("15min"))
    v = limpiar_fuente(viajes_validos).assign(franja_inicio=lambda d: d.fechasalida.dt.floor("15min"))
    t["elegible"] = elegibles(todos).to_numpy()
    prog = t[t.cancelada.eq("N")].groupby(["idruta", "franja_inicio"]).size().rename("programaciones")
    eleg = t[t.elegible].groupby(["idruta", "franja_inicio"]).size().rename("elegibles")
    val = v.groupby(["idruta", "franja_inicio"]).agg(
        pasajeros=("numeropasajeros", "sum"), programaciones_validas=("idprogramacion", "size"))
    out = pd.concat([prog, eleg, val], axis=1).reset_index()
    for col in ("programaciones", "elegibles", "programaciones_validas"):
        out[col] = out[col].fillna(0).astype(int)
    out["pasajeros"] = out.pasajeros.where(out.programaciones_validas > 0, 0.0)
    incompletas = out.programaciones_validas < out.elegibles
    out["pasajeros"] = out.pasajeros.astype(object).where(~incompletas, None)
    return out.drop(columns="elegibles").assign(lote=lote)


def tramos_contiguos(dias: list) -> list[tuple]:
    """Agrupa días en tramos consecutivos [inicio, fin) para leerlos con pocas consultas."""
    tramos = []
    for d in sorted(set(dias)):
        if tramos and d == tramos[-1][1]:
            tramos[-1] = (tramos[-1][0], d + timedelta(days=1))
        else:
            tramos.append((d, d + timedelta(days=1)))
    return tramos


def ventanas_de_marca(desde: datetime, hasta: datetime, dias_por_ventana: int) -> list[tuple]:
    """Divide (desde, hasta] en ventanas consecutivas de la marca de agua.

    Al terminar cada ventana, TODO cambio con marca <= su fin quedó procesado, así que la
    marca de agua puede avanzar hasta ese fin sin saltarse nada."""
    out, a = [], desde
    paso = timedelta(days=dias_por_ventana)
    while a < hasta:
        b = min(a + paso, hasta)
        out.append((a, b))
        a = b
    return out


def _leer_dias(c, esquema: str, dias: list) -> pd.DataFrame:
    partes = []
    for d, h in tramos_contiguos(dias):
        partes.append(pd.read_sql(text(
            f"SELECT {COLUMNAS} FROM {esquema}.progvehiculos "
            "WHERE idruta = ANY(:r) AND fechasalida >= :d AND fechasalida < :h"),
            c, params={"r": list(RUTAS), "d": d, "h": h}))
    return pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()


def _filas_viajes(viajes: pd.DataFrame, lote: str) -> list[dict]:
    v = limpiar_fuente(viajes).drop_duplicates("idprogramacion", keep="last")[COLUMNAS_FUENTE]
    v = v.assign(lote=lote).astype(object)
    return v.where(v.notna(), None).to_dict("records")


def _guardar(w, dias, franjas, cuarentena, fin_ventana, filas, lote, viajes=None):
    """Una sola transacción por ventana: si algo falla, la marca de agua no avanza."""
    if dias:
        tramos = [{"r": list(RUTAS), "d": d, "h": h} for d, h in tramos_contiguos(dias)]
        w.execute(text("DELETE FROM demanda_franja WHERE idruta = ANY(:r) AND franja_inicio >= :d "
                       "AND franja_inicio < :h"), tramos)
        # Los días se releen completos: se reemplazan sus viajes (una carga repetida no duplica)
        w.execute(text("DELETE FROM viaje_fuente WHERE idruta = ANY(:r) AND fechasalida >= :d "
                       "AND fechasalida < :h"), tramos)
    if viajes is not None and not viajes.empty:
        w.execute(text("""INSERT INTO viaje_fuente (idprogramacion, idruta, fechasalida, fechainicio, fechafin,
                          finalizado, cancelada, numeropasajeros, numerobajadas, lote)
                          VALUES (:idprogramacion, :idruta, :fechasalida, :fechainicio, :fechafin, :finalizado,
                          :cancelada, :numeropasajeros, :numerobajadas, :lote)
                          ON CONFLICT (idprogramacion) DO UPDATE SET idruta = EXCLUDED.idruta,
                          fechasalida = EXCLUDED.fechasalida, fechainicio = EXCLUDED.fechainicio,
                          fechafin = EXCLUDED.fechafin, finalizado = EXCLUDED.finalizado,
                          cancelada = EXCLUDED.cancelada, numeropasajeros = EXCLUDED.numeropasajeros,
                          numerobajadas = EXCLUDED.numerobajadas, lote = EXCLUDED.lote, cargado_en = now()"""),
                  _filas_viajes(viajes, lote))
    if franjas is not None and not franjas.empty:
        w.execute(text("""INSERT INTO demanda_franja (idruta, franja_inicio, pasajeros, programaciones,
                          programaciones_validas, lote) VALUES (:idruta, :franja_inicio, :pasajeros,
                          :programaciones, :programaciones_validas, :lote)"""),
                  franjas.astype(object).where(franjas.notna(), None).to_dict("records"))
    if cuarentena is not None and not cuarentena.empty:
        w.execute(text(SQL_EVENTO_CALIDAD), filas_calidad(cuarentena, REGLAS_VERSION))
    w.execute(text("""INSERT INTO watermark (tabla_origen, ultima_actualizacion, filas_ultima_carga, lote)
                      VALUES ('progvehiculos', :m, :n, :l)
                      ON CONFLICT (tabla_origen) DO UPDATE SET ultima_actualizacion = EXCLUDED.ultima_actualizacion,
                      filas_ultima_carga = EXCLUDED.filas_ultima_carga, lote = EXCLUDED.lote,
                      actualizada_en = now()"""),
              {"m": fin_ventana, "n": filas, "l": lote})


def correr(lote: str | None = None, dias_por_ventana: int = 31, max_ventanas: int | None = None,
           reiniciar: bool = False) -> dict:
    """Procesa los cambios en ventanas de la marca de agua, de la más antigua a la más nueva.

    dias_por_ventana: tamaño de cada ventana (en días de la marca de agua).
    max_ventanas: para pruebas; procesa solo esas ventanas y deja la marca en un punto seguro.
    reiniciar: ignora la marca guardada y vuelve a cargar todo el histórico (idempotente).
    """
    cfg = obtener_config()
    esquema = _identificador(cfg.esquema_fuente)
    marca_col = expresion_marca(cfg.columna_marca_agua)
    lote = lote or datetime.now().strftime("ingesta_%Y%m%dT%H%M%S")
    tracemalloc.start()
    t0 = time.perf_counter()

    with escribir() as w:
        if reiniciar:
            w.execute(text("DELETE FROM watermark WHERE tabla_origen = 'progvehiculos'"))
        marca = w.execute(text("SELECT ultima_actualizacion FROM watermark "
                               "WHERE tabla_origen = 'progvehiculos'")).scalar()

    with leer() as c:
        rango = c.execute(text(
            f"SELECT min({marca_col}), max({marca_col}) FROM {esquema}.progvehiculos "
            "WHERE idruta = ANY(:r)"), {"r": list(RUTAS)}).one()
    minimo, maximo = rango
    if maximo is None:
        return {"lote": lote, "ventanas": 0, "viajes": 0, "marca_agua": marca}

    if marca is None:
        desde = minimo - timedelta(seconds=1)          # carga inicial completa
    else:
        # Ventana de revisión: se releen los últimos días por si hubo correcciones
        desde = marca - timedelta(days=cfg.dias_revision)

    ventanas = ventanas_de_marca(desde, maximo, dias_por_ventana)
    if max_ventanas:
        ventanas = ventanas[:max_ventanas]

    totales = {"dias": 0, "viajes": 0, "franjas": 0, "cuarentena": 0}
    for i, (a, b) in enumerate(ventanas, start=1):
        lote_i = f"{lote}_{i:03d}"
        with leer() as c:
            cambios = pd.read_sql(text(
                f"SELECT DISTINCT date_trunc('day', fechasalida) AS dia FROM {esquema}.progvehiculos "
                f"WHERE idruta = ANY(:r) AND {marca_col} > :a AND {marca_col} <= :b"),
                c, params={"r": list(RUTAS), "a": a, "b": b})
            dias = [pd.Timestamp(d).to_pydatetime() for d in cambios.dia]
            viajes = _leer_dias(c, esquema, dias)

        franjas = cuarentena = None
        if not viajes.empty:
            validas, cuarentena = evaluar_programaciones(viajes, lote_i)
            franjas = agregar_por_franja(validas, viajes, lote_i)
        with escribir() as w:
            _guardar(w, dias, franjas, cuarentena, b, len(viajes), lote_i, viajes)

        totales["dias"] += len(dias)
        totales["viajes"] += len(viajes)
        totales["franjas"] += 0 if franjas is None else len(franjas)
        totales["cuarentena"] += 0 if cuarentena is None else len(cuarentena)
        log.info("ventana procesada", extra={"datos": {
            "lote": lote_i, "ventana": f"{a} -> {b}", "dias": len(dias), "viajes": len(viajes)}})

    _, pico = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    resumen = {"lote": lote, "ventanas": len(ventanas), **totales,
               "marca_agua": ventanas[-1][1] if ventanas else marca,
               "pendiente": bool(ventanas) and ventanas[-1][1] < maximo,
               "segundos": round(time.perf_counter() - t0, 2),
               "memoria_pico_mb": round(pico / 2**20, 1)}
    log.info("ingesta terminada", extra={"datos": resumen})
    return resumen


if __name__ == "__main__":
    import argparse

    from app.core.registro import configurar_logs
    configurar_logs()
    p = argparse.ArgumentParser(description="Lectura incremental de progvehiculos")
    p.add_argument("--dias-por-ventana", type=int, default=31)
    p.add_argument("--max-ventanas", type=int, default=None)
    p.add_argument("--reiniciar", action="store_true",
                   help="Ignora la marca guardada y recarga todo el histórico")
    a = p.parse_args()
    print(correr(dias_por_ventana=a.dias_por_ventana, max_ventanas=a.max_ventanas,
                 reiniciar=a.reiniciar))
