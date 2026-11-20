"""Explicación del modelo de demanda (RF-015) para el tablero: validación y presentación.

Se integró de forma selectiva desde ``dashboard/explicacion.py`` del commit 646ce5b
(rama ``feature/rf015-tablero-explicacion``). Solo se trajo la parte de la explicación del modelo.
La parte de RF-006 (incertidumbre de las predicciones) y la lectura paginada propia de esa rama no
se trajeron, porque el tablero actual ya pagina y valida con ``dashboard/cliente.py``.

Sin Streamlit, sin red y sin ``app.*`` (no carga el ``.env`` de ``app/core/config.py``), así que se
prueba sin interfaz.

Tres explicaciones distintas, que el tablero muestra por separado
-----------------------------------------------------------------
1. **Importancia global.** Hay dos medidas, y no son equivalentes:
   * Por **permutación**: ``metricas.importancia_top10`` de ``GET /models``. Se calculó con el MODELO
     DE EVALUACIÓN del entrenamiento (``scripts/entrenar_gbm_fe31.py``).
   * Por **ganancia**: ``importancia_global_artefacto`` de la explicación individual. Corresponde al
     ARTEFACTO de producción.
2. **Explicación individual TreeSHAP** de una predicción del GBM: ``GET /predictions/{id}/explanation``.
   Los aportes los calcula la API; aquí no se calcula ni se infiere ninguno.
3. **Regla operativa de frecuencias**: explica los buses y el headway con los valores guardados.
   TreeSHAP NO explica la cantidad de buses.

Una explicación incompleta, inconsistente o de otra predicción no se muestra nunca. ``validar_explicacion``
devuelve el motivo del rechazo.
"""
from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

METODO_GBM = "gbm_fe31"                 # app/ml/modelo_gbm.METODO (no se importa app.*)
ESTADO_VERIFICADO = "reconstruida_y_verificada"
N_VARIABLES_MODELO = 29                 # variables de FE 3.1 que recibe el GBM
# La API verifica la identidad con 1e-9 (app/ml/explicacion_gbm.TOLERANCIA). Aquí la tolerancia es más
# holgada: no se repite la verificación de la API, solo se descartan respuestas incoherentes.
TOLERANCIA_RESPUESTA = 1e-6

TEXTO_PERMUTACION = (
    "Importancia por permutación del MODELO DE EVALUACIÓN del entrenamiento, no del artefacto de "
    "producción. Mide cuánto aumenta el error absoluto medio (pasajeros) al desordenar cada variable. "
    "Solo incluye las 10 primeras. Mide influencia global, no el efecto en una predicción.")
TEXTO_GANANCIA = (
    "Importancia por ganancia del ARTEFACTO DE PRODUCCIÓN: proporción de la reducción de la pérdida de "
    "entrenamiento lograda en las divisiones de cada variable. Es global y favorece a las variables con "
    "muchas divisiones. No equivale a la importancia por permutación.")
TEXTO_ESCALA = (
    "Los aportes TreeSHAP están en escala logarítmica (pérdida Poisson): predicción = referencia × "
    "producto de los factores, con factor = exp(aporte). Son factores multiplicativos, no pasajeros que "
    "se suman.")
TEXTO_REFERENCIA = (
    "Referencia = exp(referencia_log): es el valor esperado del modelo en escala logarítmica sobre los "
    "datos de entrenamiento. Es una media geométrica, no la demanda media.")
TEXTO_LINEA_BASE = (
    "Esta predicción es de un modelo de línea base. No tiene explicación individual TreeSHAP: el método "
    "solo aplica al GBM FE 3.1.")
TEXTO_ALCANCE = (
    "TreeSHAP explica por qué el modelo predijo esa DEMANDA de pasajeros. No explica cuántos buses se "
    "recomiendan. Los buses, el headway y la viabilidad salen de la regla operativa (capacidad, "
    "ocupación objetivo, ciclo y flota), que se muestra en «Demanda y frecuencias › 02 · Decisión "
    "operativa».")

# Parámetros que intervienen en la regla operativa pero que la API no devuelve con la recomendación
# (tomado de 646ce5b: NO_DISPONIBLES_RECOMENDACION).
NO_DISPONIBLES_RECOMENDACION = (
    ("Origen del factor de carga (factor_carga_fuente): está en parametro_operativo y ningún endpoint lo "
     "devuelve."),
    "Headway mínimo operable usado para la viabilidad: no se guarda en la recomendación.",
    "Mínimo de buses por hora aplicado por el motor: no se guarda en la recomendación.",
)


# ------------------------------------------------------------------------------- identificadores

def id_ejecucion(valor) -> str | None:
    """execution_id en forma canónica (UUID en minúsculas), o None si es nulo, vacío o no es un UUID."""
    if valor is None or isinstance(valor, bool):
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        return str(uuid.UUID(texto))
    except ValueError:
        return None


def _entero(valor) -> int | None:
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str) and valor.strip().isdigit():
        return int(valor.strip())
    return None


def _finito(valor) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def _decimal(valor) -> Decimal | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        d = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


# ------------------------------------------------------------------------------- registro de modelos

def modelo_registrado(modelos, model_id, model_version) -> Mapping | None:
    """Fila de GET /models con la misma pareja (model_id, version), o None si no hay exactamente una.

    Un modelo se identifica por la pareja, nunca solo por model_id."""
    if not isinstance(modelos, list):
        return None
    filas = [m for m in modelos if isinstance(m, Mapping)
             and (str(m.get("model_id")), str(m.get("version"))) == (str(model_id), str(model_version))]
    return filas[0] if len(filas) == 1 else None


def es_gbm(registro: Mapping | None) -> bool:
    hiper = registro.get("hiperparametros") if isinstance(registro, Mapping) else None
    return isinstance(hiper, Mapping) and hiper.get("metodo") == METODO_GBM


def importancia_permutacion(registro: Mapping | None) -> list[dict] | None:
    """``metricas.importancia_top10`` tal como la guarda el registro, o None si falta o no es válida."""
    metricas = registro.get("metricas") if isinstance(registro, Mapping) else None
    filas = metricas.get("importancia_top10") if isinstance(metricas, Mapping) else None
    if not isinstance(filas, list) or not filas:
        return None
    if not all(isinstance(f, Mapping) and isinstance(f.get("variable"), str)
               and _decimal(f.get("importancia_mae")) is not None for f in filas):
        return None
    return [{"variable": f["variable"], "importancia_mae": float(f["importancia_mae"])} for f in filas]


# ------------------------------------------------------------------------------- explicación individual

def validar_explicacion(resp, prediction_id: int, fila: Mapping | None = None) -> str | None:
    """Motivo para descartar la respuesta del endpoint, o None si se puede mostrar completa.

    ``prediction_id`` es el id pedido. ``fila`` es la predicción seleccionada en el tablero, con
    execution_id, model_id y model_version. Si no se conoce (id escrito a mano), no se pueden contrastar
    la ejecución ni el modelo, y el tablero lo advierte.

    Se exige:
    * estado «reconstruida_y_verificada»;
    * mismo prediction_id y, si hay fila, mismos model_id, model_version y execution_id;
    * exactamente 29 aportes, con variables únicas y valores finitos;
    * referencia + suma de aportes = predicción (escala log), y los valores en pasajeros y los factores
      coherentes con su escala log;
    * verificaciones e importancia global presentes, y la identidad TreeSHAP dentro de su tolerancia."""
    if not isinstance(resp, Mapping):
        return "la respuesta no es un objeto"
    if resp.get("estado") != ESTADO_VERIFICADO:
        return f"estado {resp.get('estado')!r} distinto de {ESTADO_VERIFICADO!r}"
    if _entero(resp.get("prediction_id")) != _entero(prediction_id):
        return (f"prediction_id {resp.get('prediction_id')!r} no es el de la predicción solicitada "
                f"({prediction_id!r})")
    if fila is not None:
        for campo_r, campo_f in (("model_id", "model_id"), ("model_version", "model_version")):
            if str(resp.get(campo_r)) != str(fila.get(campo_f)):
                return (f"{campo_r} {resp.get(campo_r)!r} no es el de la predicción seleccionada "
                        f"({fila.get(campo_f)!r})")
        eid = id_ejecucion(resp.get("execution_id"))
        if eid is None or eid != id_ejecucion(fila.get("execution_id")):
            return "la ejecución de la explicación no es la de la predicción seleccionada"
    elif id_ejecucion(resp.get("execution_id")) is None:
        return "la explicación no trae un execution_id válido"
    aportes = resp.get("aportes")
    if not isinstance(aportes, list) or len(aportes) != N_VARIABLES_MODELO:
        n = len(aportes) if isinstance(aportes, list) else 0
        return f"se esperaban {N_VARIABLES_MODELO} aportes y llegaron {n}"
    if not all(isinstance(a, Mapping) and isinstance(a.get("variable"), str) and a["variable"].strip()
               and _finito(a.get("aporte_log")) and _finito(a.get("factor")) for a in aportes):
        return "hay aportes incompletos o no numéricos"
    if len({a["variable"] for a in aportes}) != N_VARIABLES_MODELO:
        return "hay variables repetidas en los aportes"
    if any(not math.isclose(a["factor"], math.exp(a["aporte_log"]), rel_tol=TOLERANCIA_RESPUESTA)
           for a in aportes):
        return "algún factor no es exp(aporte_log)"
    for campo in ("referencia_log", "referencia_pasajeros", "prediccion_log", "prediccion_pasajeros"):
        if not _finito(resp.get(campo)):
            return f"falta {campo} o no es numérico"
    suma = resp["referencia_log"] + math.fsum(a["aporte_log"] for a in aportes)
    if abs(suma - resp["prediccion_log"]) > TOLERANCIA_RESPUESTA:
        return "los aportes no suman la predicción (referencia + aportes ≠ predicción en escala log)"
    for pas, log in (("referencia_pasajeros", "referencia_log"), ("prediccion_pasajeros", "prediccion_log")):
        if not math.isclose(resp[pas], math.exp(resp[log]), rel_tol=TOLERANCIA_RESPUESTA):
            return f"{pas} no es exp({log})"
    verif = resp.get("verificaciones")
    if not isinstance(verif, Mapping) or not verif:
        return "faltan las verificaciones"
    identidad = verif.get("identidad_treeshap")
    if isinstance(identidad, Mapping):
        err, tol = identidad.get("error_absoluto"), identidad.get("tolerancia")
        if not (_finito(err) and _finito(tol)) or err > tol:
            return "la verificación de identidad TreeSHAP no está dentro de su tolerancia"
    imp = resp.get("importancia_global_artefacto")
    if not isinstance(imp, Mapping) or not isinstance(imp.get("variables"), list) or not imp["variables"]:
        return "falta la importancia global del artefacto"
    return None


def interpretacion_aporte(aporte: Mapping) -> str:
    if aporte["aporte_log"] > 0:
        sentido = "la aumenta"
    elif aporte["aporte_log"] < 0:
        sentido = "la disminuye"
    else:
        sentido = "no la cambia"
    return f"multiplica la predicción por {aporte['factor']:.4f} ({sentido})"


def filas_aportes(resp: Mapping) -> list[dict]:
    """Aportes de mayor a menor magnitud, con signo e interpretación (solo de una respuesta validada)."""
    ordenados = sorted(resp["aportes"], key=lambda a: -abs(a["aporte_log"]))
    return [{"variable": a["variable"], "valor": a.get("valor"), "valor_modelo": a.get("valor_modelo"),
             "aporte_log": a["aporte_log"], "factor": a["factor"],
             "efecto": "aumenta" if a["aporte_log"] > 0 else ("disminuye" if a["aporte_log"] < 0 else "neutro"),
             "interpretacion": interpretacion_aporte(a)} for a in ordenados]


def resumen_aportes(resp: Mapping) -> dict:
    """Cuántas variables suben y bajan la predicción y cuál pesa más en cada sentido."""
    aportes = resp["aportes"]
    pos = [a for a in aportes if a["aporte_log"] > 0]
    neg = [a for a in aportes if a["aporte_log"] < 0]
    return {"suben": len(pos), "bajan": len(neg),
            "mayor_sube": max(pos, key=lambda a: a["aporte_log"])["variable"] if pos else None,
            "mayor_baja": min(neg, key=lambda a: a["aporte_log"])["variable"] if neg else None}


def filas_importancia_ganancia(resp: Mapping) -> list[dict]:
    return [{"variable": v.get("variable"), "proporcion": v.get("proporcion"), "ganancia": v.get("ganancia"),
             "divisiones": v.get("divisiones")}
            for v in resp["importancia_global_artefacto"]["variables"] if isinstance(v, Mapping)]


def resumen_verificaciones(resp: Mapping) -> list[tuple[str, object]]:
    """Pares (etiqueta, valor) de las verificaciones que devuelve la API, sin interpretarlas."""
    v = resp.get("verificaciones") or {}
    ident = v.get("identidad_treeshap") if isinstance(v.get("identidad_treeshap"), Mapping) else {}
    rep = v.get("reproduccion") if isinstance(v.get("reproduccion"), Mapping) else {}
    proc = v.get("procedencia") if isinstance(v.get("procedencia"), Mapping) else {}
    return [("Error de la identidad TreeSHAP", ident.get("error_absoluto")),
            ("Tolerancia de la API", ident.get("tolerancia")),
            ("Predicciones recalculadas y comparadas", rep.get("predicciones_comparadas")),
            ("Horizontes comparados", rep.get("horizontes")),
            ("Última carga de viajes", proc.get("ultima_carga_viajes")),
            ("Inicio de la ejecución", proc.get("inicio_ejecucion"))]
