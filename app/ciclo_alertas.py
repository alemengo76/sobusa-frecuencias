"""Ciclo automático de alertas (v4): ingesta incremental registrada y alertas posteriores. Dos puntos de entrada.

No reemplaza a `app/ciclo.py` (G4), que conserva `correr(desde, hasta)` para un rango manual con diagnóstico ALT-009.
Este módulo sigue la marca de agua y es el que dispara `app/programador.py`.

    python -m app.ciclo_alertas                     # ingesta + alertas posteriores
    python -m app.ciclo_alertas --reintento-de 2026-11-03 --autorizado-por "nombre"   # reintento manual (*)
    python -m app.ciclo_alertas --solo-verificar-retraso                              # sin ingerir

`correr()`:
1. Registra el INTENTO de ingesta en execution_log (tipo 'ingesta', en_curso) en su propia transacción,
   confirmada de inmediato, con la fuente, el alcance (rutas) y, si es un reintento manual, el ciclo
   original y quién lo autorizó. Si el proceso muere, el intento queda registrado.
2. Ejecuta `app.ingesta.correr()` y cierra el intento: 'exitosa' con `consulta_valida`, `completa`,
   `sin_novedades` y la evidencia de la consulta; o 'fallida' con el error.
3. En otra transacción, una ejecución 'alertas' (app/alertas/flujo.py): ALT-009 por retraso, ALT-005/007,
   ALT-010 y ALT-008 (pendiente de fuente), cada una en su SAVEPOINT. Si la ingesta falló, el retraso se
   verifica igual y luego se relanza el error.

`verificar_retraso()` solo verifica ALT-009 por retraso: corre aunque la ingesta nunca haya arrancado.

(*) `--autorizado-por` registra una DECLARACIÓN del operador (`autorizacion_declarada_por`), no una
autorización verificada. Mientras no exista un mecanismo aprobado para verificarla, el reintento queda
registrado para auditoría y no satisface ni recupera ningún ciclo.

`correlation_id` (opcional) identifica la corrida: se propaga a TODAS las ejecuciones que crea el ciclo
(el intento de ingesta y la ejecución 'alertas'), lo que permite auditarlas o limpiarlas con exactitud.

Ejecutarlo dos veces registra dos intentos (los dos quedan vinculados al ciclo en que empezaron) y no
duplica alertas: la deduplicación y el bloqueo por fuente del verificador lo impiden.

La política de calendario del retraso se lee de variables de entorno (retraso_ingesta.ENV). No tiene
valores por defecto: sin ella el retraso es no_evaluable. Este módulo se puede invocar y está probado;
el repositorio NO instala un programador que lo ejecute solo (cron, systemd o la plataforma).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from datetime import date, datetime

from app import ingesta
from app.alertas import flujo
from app.alertas import retraso_ingesta as rt
from app.db import lectura_real, repositorio
from app.db.conexion import consultar_resultados, escribir

log = logging.getLogger(__name__)


def politica(env=None):
    return rt.politica_desde_entorno(os.environ if env is None else env, flujo.FUENTE_INGESTA)


def _marca_vigente():
    with consultar_resultados() as c:
        wm = lectura_real.marca_agua(c)
    return None if wm is None else wm["ultima_actualizacion"]


def parametros_intento(reintento_de: date | None, autorizado_por: str | None) -> dict:
    p = {"fuente": flujo.FUENTE_INGESTA, "alcance": {"rutas": list(ingesta.RUTAS)}}
    if reintento_de is not None:
        p["ciclo_objetivo"] = reintento_de.isoformat()
        # Solo la declaración del operador; no se verifica aquí (ver docstring del módulo).
        p["autorizacion_declarada_por"] = (autorizado_por or "").strip() or None
    return p


def _registrar_inicio(parametros: dict, correlation_id: str) -> str:
    with escribir() as c:
        return repositorio.crear_ejecucion(c, "ingesta", parametros, correlation_id)


def resumen_ingesta(resultado: dict) -> dict:
    """Evidencia que ALT-009 por retraso necesita de una ingesta que terminó sin excepción.

    consulta_valida: la ingesta consultó la fuente en su alcance y recorrió al menos una ventana de la
    marca de agua hasta el máximo de la fuente. Si la fuente no devolvió filas para el alcance
    (`ventanas == 0`), la ausencia de datos es inconcluyente y la consulta NO se acredita como válida.
    completa: no quedaron ventanas pendientes."""
    ventanas = int(resultado.get("ventanas") or 0)
    return {**resultado, "consulta_valida": ventanas > 0, "completa": ventanas > 0 and not resultado.get("pendiente"),
            "sin_novedades": ventanas > 0 and int(resultado.get("viajes") or 0) == 0,
            "alcance_consultado": {"rutas": list(ingesta.RUTAS), "hasta_marca": resultado.get("marca_agua")}}


def _cerrar(eid: str, estado: str, resumen: dict, error: str | None = None) -> None:
    try:
        with escribir() as c:
            repositorio.cerrar_ejecucion(c, eid, estado, resumen, error)
    except Exception:
        log.exception("no se pudo cerrar el registro de la ingesta", extra={"datos": {"execution_id": eid}})


def _alertas(marca_anterior, marca_nueva, cid: str, ahora=None) -> dict:
    pol, motivo = politica()
    try:
        with escribir() as c:
            return flujo.alertas_tras_ingesta(c, marca_anterior, marca_nueva, cid, pol, motivo, ahora)
    except Exception as e:
        log.exception("falló la evaluación de alertas posterior a la ingesta")
        return {"estado": "error", "motivo": f"{type(e).__name__}: {e}"[:300]}


def correr(reintento_de: date | None = None, autorizado_por: str | None = None, ahora=None,
           correlation_id: str | None = None) -> dict:
    cid = correlation_id or f"ciclo-{uuid.uuid4()}"
    marca_anterior = _marca_vigente()
    eid = _registrar_inicio(parametros_intento(reintento_de, autorizado_por), cid)
    try:
        resultado = ingesta.correr()
    except Exception as e:
        _cerrar(eid, "fallida", {"consulta_valida": False, "completa": False}, f"{type(e).__name__}: {e}")
        _alertas(marca_anterior, None, cid, ahora)        # el retraso se verifica igual
        raise
    _cerrar(eid, "exitosa", resumen_ingesta(resultado))
    return {"execution_id_ingesta": eid, "ingesta": resultado,
            "alertas": _alertas(marca_anterior, resultado.get("marca_agua"), cid, ahora)}


def verificar_retraso(ahora: datetime | None = None, correlation_id: str | None = None) -> dict:
    pol, motivo = politica()
    with escribir() as c:
        eid = repositorio.crear_ejecucion(c, "alertas", {"origen": "verificacion_retraso"},
                                          correlation_id or f"retraso-{uuid.uuid4()}")
        r = flujo.en_savepoint(c, "ALT-009 retraso", lambda: flujo.verificar_retraso(c, eid, pol, ahora,
                                                                                   no_evaluable=motivo))
        repositorio.cerrar_ejecucion(c, eid, "exitosa", {"alt_009_retraso": r})
    return {"execution_id": eid, "alt_009_retraso": r}


if __name__ == "__main__":
    from app.core.registro import configurar_logs
    configurar_logs()
    parser = argparse.ArgumentParser(description="Ciclo diario: ingesta registrada y alertas posteriores")
    parser.add_argument("--solo-verificar-retraso", action="store_true",
                        help="No ingiere: solo verifica ALT-009 por retraso")
    parser.add_argument("--reintento-de", type=date.fromisoformat,
                        help="Reintento manual del ciclo de esta fecha operativa (AAAA-MM-DD)")
    parser.add_argument("--autorizado-por",
                        help="Quién declara haber autorizado el reintento. Se registra para auditoría; no es una "
                             "autorización verificada y por sí sola no hace que el reintento cuente")
    a = parser.parse_args()
    salida = verificar_retraso() if a.solo_verificar_retraso else correr(a.reintento_de, a.autorizado_por)
    print(json.dumps(salida, ensure_ascii=False, default=str))
