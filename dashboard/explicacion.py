"""Incertidumbre de las predicciones (RF-006) y explicación de las recomendaciones (RF-015).

Lógica del tablero sin Streamlit, sin red y sin ``app.*`` (así no carga el ``.env`` que lee
``app/core/config.py``). La función HTTP se recibe como parámetro (``get``) para probarla sin API.

Principios
----------
* Nada se recalcula: se muestran los valores tal como los devuelve la API. Las únicas comparaciones
  numéricas son las de ``clasificar_intervalo``, que solo etiquetan una fila y no reemplazan valores.
* No se mezclan ejecuciones, días, rutas, horizontes ni modelos: las filas se agrupan y el tablero
  muestra un solo grupo.
* Lo que la API no expone se declara como no disponible; no se infiere.
* Fechas: nunca se compara una fecha con zona horaria con otra sin zona (no hay conversión implícita
  que pueda desplazar una franja). El día se pide a la API en hora de Bogotá con su desplazamiento.
* Un modelo se identifica por la pareja (model_id, model_version), nunca solo por model_id.
* Una fila sin execution_id válido no pertenece a ninguna ejecución: se excluye y se cuenta.
* Si dos filas ocupan la misma franja del gráfico, no se grafica ni se elige una: se muestran ambas.

Alcance de RF-015
-----------------
Se explica la regla operativa de frecuencias (regla aplicada, variables de la fórmula y valores
guardados como evidencia cuantitativa). NO se ofrece explicación local de las variables que influyeron
en cada pronóstico: la API no la expone. Es una limitación de cumplimiento de RF-015 pendiente; aquí
no se simula ni se infiere importancia de variables.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import pandas as pd

ZONA_DIA = "America/Bogota"         # la API trabaja en esta zona (app/db/conexion.py: OPCIONES)
TAMANO_PAGINA = 500                 # igual al le=500 de la API (app/api/main.py: Tamano)
MAX_FILAS = 5_000                   # por consulta de un día, una ruta y un horizonte

# Banda usada por app/servicios.py (BANDA_POR_DEFECTO) cuando el modelo no tiene cuantiles de error.
# Se copia aquí para no importar app.*; si cambia allí, esta clasificación deja de reconocerla.
BANDA_PROVISIONAL = (Decimal("-0.25"), Decimal("0.30"))
# Los tres valores se guardan como numeric(10, 2) a partir de la predicción sin redondear: la
# diferencia máxima por redondeo es 1,30 × 0,005 + 0,005 = 0,0115.
TOLERANCIA_BANDA = Decimal("0.0125")

COLAPSADO = "colapsado"
PARCIAL = "parcial"
BANDA = "compatible_banda_provisional"
NO_DETERMINADO = "origen_no_determinado"
SIN_DATO = "sin_dato"
ETIQUETA_INTERVALO = {
    COLAPSADO: "Colapsado",
    PARCIAL: "Un límite igual a la predicción",
    BANDA: "Compatible numéricamente con la banda provisional",
    NO_DETERMINADO: "Origen no determinado",
    SIN_DATO: "Sin valores numéricos",
}
DESCRIPCION_INTERVALO = {
    COLAPSADO: "Límite inferior = predicción = límite superior: el intervalo no informa incertidumbre. "
               "El código de la API lo produce, por ejemplo, con predicción 0 o cuando no hay error "
               "histórico para el grupo de la franja; la API no registra la causa.",
    PARCIAL: "Solo uno de los límites es igual a la predicción. La API no registra la causa.",
    BANDA: "Los límites coinciden, dentro del redondeo a 2 decimales, con −25 % y +30 % de la "
           "predicción, la banda fija que la API usa cuando el modelo no tiene errores históricos. "
           "Es una coincidencia numérica: no confirma que ese haya sido el método.",
    NO_DETERMINADO: "No se puede determinar el origen del intervalo desde el tablero: no coincide con "
                    "la banda provisional, o la predicción es tan pequeña que, con 2 decimales, la "
                    "comparación no distingue la banda de otros intervalos. La API no registra el "
                    "método por fila.",
    SIN_DATO: "Falta alguno de los tres valores o no es numérico.",
}

ALCANCE_RF015 = (
    "Alcance de RF-015: se explica la regla operativa de frecuencias con los valores guardados como "
    "evidencia cuantitativa. La explicación local de las variables que influyeron en cada pronóstico "
    "no está disponible en la API: es una limitación pendiente de RF-015.")

LIMITACIONES_PREDICCION = (
    "nivel_intervalo es el nivel nominal guardado con la predicción. Según el código de la API, el "
    "intervalo sale de errores históricos del modelo o de una banda fija provisional; no es un "
    "intervalo de confianza estadístico y la API no guarda por fila qué método se usó.",
    "La API no ofrece explicación local de las variables del modelo para cada predicción: este "
    "tablero no la muestra.",
)

# Parámetros que intervienen en la regla pero que la API no devuelve con la recomendación.
NO_DISPONIBLES_RECOMENDACION = (
    "factor_carga_fuente (origen del factor de carga): está en parametro_operativo, sin endpoint.",
    "headway_minimo_min (headway mínimo operable usado para la viabilidad): no se guarda en la "
    "recomendación.",
    "Mínimo de buses por hora aplicado por el motor: no se guarda en la recomendación.",
)

# Claves del resumen de una ejecución de predicción que describen el modelo y el respaldo
# (app/servicios.py: predecir). Solo se muestran las que la API devuelva.
CLAVES_RESPALDO = ("modelo_activo", "metodo_activo", "respaldo", "rutas_respaldo", "modelo_respaldo",
                   "motivo_respaldo", "filas_por_modelo")


class ErrorLectura(Exception):
    """La lectura paginada no se pudo completar de forma coherente: no se devuelve nada."""


# ------------------------------------------------------------------------------------- lectura

def rango_dia(dia) -> tuple[str, str]:
    """(desde, hasta) del día calendario de Bogotá como ISO-8601 con desplazamiento (-05:00).

    desde es inclusivo y hasta exclusivo, como filtra la API (franja_inicio >= desde y < hasta). Un
    ``dia`` con zona horaria se lleva primero a Bogotá; uno sin zona se toma como fecha de Bogotá."""
    t = pd.Timestamp(dia)
    fecha = (t.tz_convert(ZONA_DIA) if t.tzinfo is not None else t).date()
    inicio = pd.Timestamp(fecha).tz_localize(ZONA_DIA)
    fin = pd.Timestamp(fecha + pd.Timedelta(days=1)).tz_localize(ZONA_DIA)
    return inicio.isoformat(), fin.isoformat()


def _pagina(get, ruta: str, params: Mapping, pagina: int, tamano: int,
            columna_id: str) -> tuple[list[dict], int]:
    respuesta = get(ruta, {**params, "pagina": pagina, "tamano": tamano})
    try:
        datos = list(respuesta["datos"])
        total = int(respuesta["paginacion"]["total"])
    except (KeyError, TypeError, ValueError) as e:
        raise ErrorLectura(f"Respuesta de {ruta} sin 'datos' o 'paginacion.total'") from e
    for f in datos:
        if not isinstance(f, Mapping) or f.get(columna_id) is None:
            raise ErrorLectura(f"{ruta}: una fila no tiene '{columna_id}'")
    return datos, total


def leer_paginas(get: Callable[[str, Mapping[str, object]], Mapping], ruta: str,
                 params: Mapping[str, object], columna_id: str, *, tamano: int = TAMANO_PAGINA,
                 max_filas: int = MAX_FILAS) -> list[dict]:
    """Todas las filas de ``ruta`` con ``params``, o ``ErrorLectura`` (nunca filas parciales).

    La API pagina con ORDER BY ``columna_id`` DESC y LIMIT/OFFSET, y cada página es una consulta
    distinta. Se exige:
    * el mismo total en todas las páginas y el número de filas leídas igual a ese total;
    * identificadores sin repetir y estrictamente decrecientes en todo el recorrido (orden estable);
    * al terminar, la página 1 se vuelve a leer y debe traer el mismo total y los mismos
      identificadores en el mismo orden (detecta inserciones durante la lectura, incluida la que cae
      entre el conteo y la consulta de la última página).
    Las predicciones y recomendaciones no se modifican ni se borran (la API solo inserta)."""
    filas: list[dict] = []
    datos, total = _pagina(get, ruta, params, 1, tamano, columna_id)
    if total > max_filas:
        raise ErrorLectura(f"{ruta}: {total} filas para este filtro; el límite es {max_filas}")
    primera = [f[columna_id] for f in datos]
    filas.extend(datos)
    pagina = 1
    while len(filas) < total:
        if not datos:
            break
        pagina += 1
        datos, total_p = _pagina(get, ruta, params, pagina, tamano, columna_id)
        if total_p != total:
            raise ErrorLectura(f"{ruta}: el total cambió durante la lectura ({total} → {total_p}); "
                               "vuelva a cargar la página")
        filas.extend(datos)
    if len(filas) != total:
        raise ErrorLectura(f"{ruta}: se leyeron {len(filas)} filas y la API informó {total}")
    ids = [f[columna_id] for f in filas]
    if len(set(ids)) != len(ids):
        raise ErrorLectura(f"{ruta}: identificadores repetidos entre páginas; los datos cambiaron "
                           "durante la lectura")
    if any(not a > b for a, b in zip(ids, ids[1:])):
        raise ErrorLectura(f"{ruta}: el orden por {columna_id} no es estable entre páginas")
    datos_v, total_v = _pagina(get, ruta, params, 1, tamano, columna_id)
    if total_v != total or [f[columna_id] for f in datos_v] != primera:
        raise ErrorLectura(f"{ruta}: los datos cambiaron durante la lectura (la página 1 ya no "
                           "coincide); vuelva a cargar la página")
    return filas


# ------------------------------------------------------------------------------------- fechas

def instante(valor) -> pd.Timestamp | None:
    """Timestamp tal como llegó (con o sin zona), sin convertirlo. None si no es legible."""
    if valor is None:
        return None
    try:
        t = pd.Timestamp(valor)
    except (TypeError, ValueError):
        return None
    return None if t is pd.NaT else t


def con_zona(t: pd.Timestamp) -> bool:
    return t.tzinfo is not None


def comparables(a: pd.Timestamp | None, b: pd.Timestamp | None) -> bool:
    """Solo se comparan dos instantes si ambos tienen zona horaria o ninguno la tiene."""
    return a is not None and b is not None and con_zona(a) == con_zona(b)


# ---------------------------------------------------------------------------------- agrupación

@dataclass(frozen=True)
class Ejecucion:
    execution_id: str
    filas: int
    generada_hasta: str | None         # máximo generated_at de sus filas, tal como llegó


@dataclass(frozen=True)
class Ejecuciones:
    lista: list[Ejecucion]
    por_fecha: bool                    # False: no se pudo ordenar por generated_at (ver motivo)
    motivo: str | None = None
    excluidas: int = 0                 # filas sin execution_id válido (no están en ninguna ejecución)


def id_ejecucion(valor) -> str | None:
    """execution_id utilizable, o None si es nulo, vacío o no es un UUID.

    La API lo guarda como ``uuid NOT NULL`` y ``/frequency/executions/{id}`` exige un UUID, así que
    cualquier otro valor no identifica una ejecución. Se devuelve el texto recibido, sin espacios."""
    if valor is None or isinstance(valor, bool):
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        uuid.UUID(texto)
    except ValueError:
        return None
    return texto


def separar_ejecuciones(filas: list[Mapping]) -> Ejecuciones:
    """Ejecuciones presentes en ``filas``, de la más reciente a la más antigua por generated_at.

    Las filas sin execution_id válido (nulo, ausente, vacío o no UUID) no se agrupan: se cuentan en
    ``excluidas``. Si algún generated_at falta, no es legible o se mezclan valores con y sin zona
    horaria, no se ordena por fecha (orden por identificador) y se informa el motivo."""
    grupos: dict[str, list[Mapping]] = {}
    excluidas = 0
    for f in filas:
        eid = id_ejecucion(f.get("execution_id"))
        if eid is None:
            excluidas += 1
        else:
            grupos.setdefault(eid, []).append(f)
    marcas = {eid: [(instante(f.get("generated_at")), f.get("generated_at")) for f in fs]
              for eid, fs in grupos.items()}
    todas = [t for ms in marcas.values() for t, _ in ms]
    motivo = None
    if any(t is None for t in todas):
        motivo = "hay filas sin generated_at legible"
    elif len({con_zona(t) for t in todas}) > 1:
        motivo = "generated_at mezcla valores con y sin zona horaria"
    lista = []
    for eid, fs in grupos.items():
        validas = [(t, txt) for t, txt in marcas[eid] if t is not None]
        texto = max(validas, key=lambda m: m[0])[1] if validas and motivo is None else None
        lista.append(Ejecucion(eid, len(fs), texto))
    if motivo is None:
        ultima = {eid: max(t for t, _ in marcas[eid]) for eid in grupos}
        lista.sort(key=lambda e: (ultima[e.execution_id], e.execution_id), reverse=True)
    else:
        lista.sort(key=lambda e: e.execution_id)
    return Ejecuciones(lista, motivo is None, motivo, excluidas)


def de_ejecucion(filas: list[Mapping], execution_id: str) -> list[Mapping]:
    """Filas de una ejecución válida. Con un id inválido no devuelve nada (nunca agrupa nulos)."""
    if id_ejecucion(execution_id) is None:
        return []
    return [f for f in filas if id_ejecucion(f.get("execution_id")) == execution_id]


def fuera_de_filtro(filas: list[Mapping], idruta: int, horizonte_min: int) -> int:
    """Filas que no corresponden a la ruta y el horizonte pedidos (no debería haber ninguna)."""
    return sum(1 for f in filas
               if str(f.get("idruta")) != str(idruta) or str(f.get("horizonte_min")) != str(horizonte_min))


def clave_modelo(fila: Mapping) -> tuple[str, str]:
    """Identidad de un modelo: (model_id, model_version). model_id solo no basta."""
    return str(fila.get("model_id")), str(fila.get("model_version"))


def modelos(filas: list[Mapping]) -> list[tuple[tuple[str, str], int]]:
    """((model_id, model_version), filas) por modelo: primero el de más filas; a igualdad, por clave."""
    conteo: dict[tuple[str, str], int] = {}
    for f in filas:
        clave = clave_modelo(f)
        conteo[clave] = conteo.get(clave, 0) + 1
    return sorted(conteo.items(), key=lambda t: (-t[1], t[0]))


def de_modelo(filas: list[Mapping], modelo: tuple[str, str]) -> list[Mapping]:
    return [f for f in filas if clave_modelo(f) == tuple(modelo)]


def ocultas_por_modelo(filas: list[Mapping], modelo: tuple[str, str]) -> list[dict]:
    """Franjas de otros modelos (otra pareja id-versión) que quedan fuera al mostrar ``modelo``."""
    return [{"franja_inicio": f.get("franja_inicio"), "model_id": f.get("model_id"),
             "model_version": f.get("model_version")}
            for f in filas if clave_modelo(f) != tuple(modelo)]


# Franja del gráfico: el eje es solo el tiempo, así que dos filas de la misma ruta, horizonte (y, en
# predicciones, modelo) en el mismo instante se superponen aunque difieran en segmento_id o
# data_cutoff. Esas columnas no forman parte de la clave: se muestran para explicar el conflicto.
CLAVE_FRANJA_PREDICCION = ("idruta", "horizonte_min", "model_id", "model_version")
CLAVE_FRANJA_RECOMENDACION = ("idruta", "horizonte_min")
COLUMNAS_DUPLICADO_PREDICCION = ("prediction_id", "execution_id", "franja_inicio", "idruta",
                                 "horizonte_min", "segmento_id", "data_cutoff", "model_id",
                                 "model_version")
COLUMNAS_DUPLICADO_RECOMENDACION = ("recommendation_id", "execution_id", "franja_inicio", "idruta",
                                    "horizonte_min", "segmento_id", "base_demanda", "parametros_version")


def clave_instante(valor) -> tuple | None:
    """Clave comparable de una fecha, normalizada antes de comparar.

    Con zona: el instante expresado en America/Bogota (06:00-05:00 = 11:00+00:00). Sin zona: el valor
    tal cual (no se le asigna una zona). Una fecha con zona y otra sin zona nunca tienen la misma
    clave. None si no es legible."""
    t = instante(valor)
    if t is None:
        return None
    return ("con_zona", t.tz_convert(ZONA_DIA)) if con_zona(t) else ("sin_zona", t)


def franjas_duplicadas(filas: list[Mapping], campos: tuple[str, ...],
                       columnas: tuple[str, ...]) -> list[dict]:
    """Todas las filas (sin elegir ninguna) que comparten ``campos`` y el instante de franja_inicio,
    con ``columnas`` tal como llegaron. Las franjas ilegibles no se agrupan (el gráfico ya las
    rechaza)."""
    grupos: dict[tuple, list[Mapping]] = {}
    for f in filas:
        k = clave_instante(f.get("franja_inicio"))
        if k is not None:
            grupos.setdefault((tuple(str(f.get(c)) for c in campos), k), []).append(f)
    return [{c: f.get(c) for c in columnas} for fs in grupos.values() if len(fs) > 1 for f in fs]


def valores_unicos(filas: list[Mapping], columna: str) -> list:
    vistos = []
    for f in filas:
        if f.get(columna) not in vistos:
            vistos.append(f.get(columna))
    return vistos


def respaldo_registrado(ejecucion: Mapping | None) -> dict | None:
    """Claves de modelo y respaldo que el resumen de la ejecución trae de verdad. None si no trae
    ninguna (no se completa nada que falte)."""
    if not isinstance(ejecucion, Mapping) or not isinstance(ejecucion.get("resumen"), Mapping):
        return None
    resumen = ejecucion["resumen"]
    datos = {k: resumen[k] for k in CLAVES_RESPALDO if k in resumen}
    return datos or None


# ----------------------------------------------------------------------------------- intervalos

def _decimal(valor) -> Decimal | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        d = Decimal(str(valor))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def clasificar_intervalo(fila: Mapping) -> str:
    """Etiqueta el intervalo de una predicción sin modificar sus valores.

    La comparación con la banda provisional solo se hace cuando puede distinguirla: los límites de
    la banda se separan de la predicción en 25 % y 30 %; si el 25 % de la predicción no supera la
    tolerancia del redondeo a 2 decimales (TOLERANCIA_BANDA), cualquier intervalo estrecho
    coincidiría y el resultado es NO_DETERMINADO. El límite sale de la resolución con que la base
    guarda los valores, no de un umbral de negocio."""
    li, p, ls = (_decimal(fila.get(c)) for c in ("limite_inferior", "demanda_pred", "limite_superior"))
    if li is None or p is None or ls is None:
        return SIN_DATO
    if li == p == ls:
        return COLAPSADO
    if li == p or ls == p:
        return PARCIAL
    bi, bs = BANDA_PROVISIONAL
    distinguible = p * min(-bi, bs) > TOLERANCIA_BANDA
    if (distinguible and abs(p * (1 + bi) - li) <= TOLERANCIA_BANDA
            and abs(p * (1 + bs) - ls) <= TOLERANCIA_BANDA):
        return BANDA
    return NO_DETERMINADO


def resumen_intervalos(filas: list[Mapping]) -> dict[str, int]:
    """Franjas por clase, solo las clases presentes, en orden fijo."""
    conteo = {c: 0 for c in ETIQUETA_INTERVALO}
    for f in filas:
        conteo[clasificar_intervalo(f)] += 1
    return {c: n for c, n in conteo.items() if n}


# ------------------------------------------------------------------- recomendación ↔ predicción

def ejecucion_prediccion_asociada(ejecucion: Mapping | None) -> str | None:
    """execution_id de las predicciones que usó una ejecución de recomendación.

    'frecuencias' lo guarda en resumen.execution_id_prediccion (y en parametros.prediccion);
    'recomendacion' (POST /recommendations/calculate) en parametros.execution_id_prediccion."""
    if not isinstance(ejecucion, Mapping):
        return None
    resumen = ejecucion.get("resumen") if isinstance(ejecucion.get("resumen"), Mapping) else {}
    parametros = ejecucion.get("parametros") if isinstance(ejecucion.get("parametros"), Mapping) else {}
    for valor in (resumen.get("execution_id_prediccion"), parametros.get("execution_id_prediccion"),
                  parametros.get("prediccion")):
        if valor:
            return id_ejecucion(valor)      # el primero informado decide; inválido -> None
    return None


SIN_PREDICCIONES = "sin_predicciones"
ZONA_MIXTA = "zona_horaria_mixta"
FRANJA_ILEGIBLE = "franja_ilegible"
NO_ENCONTRADA = "no_encontrada"
AMBIGUA = "ambigua"
MOTIVO_NO_ASOCIADA = {
    SIN_PREDICCIONES: "la ejecución asociada no tiene predicciones para esta ruta, horizonte y día",
    ZONA_MIXTA: "la franja de la recomendación y la de las predicciones no tienen ambas zona horaria "
                "(o ambas no la tienen); no se comparan para no desplazar la franja",
    FRANJA_ILEGIBLE: "alguna franja (de la recomendación o de las predicciones) no es una fecha legible",
    NO_ENCONTRADA: "ninguna predicción de la ejecución asociada corresponde a esta franja",
    AMBIGUA: "más de una predicción de la ejecución asociada corresponde a esta franja",
}


@dataclass(frozen=True)
class Asociacion:
    prediccion: Mapping | None
    motivo: str | None               # clave de MOTIVO_NO_ASOCIADA cuando prediccion es None


def prediccion_para(recomendacion: Mapping, predicciones: list[Mapping]) -> Asociacion:
    """Predicción de la que salió ``recomendacion``, solo dentro de ``predicciones`` (que deben ser de
    la ejecución asociada). Por prediction_id si existe; si no (las recomendaciones de
    /frequency/executions lo guardan nulo), por ruta, horizonte y franja. Nunca elige entre varias y
    nunca compara una franja con zona horaria con otra sin zona."""
    if not predicciones:
        return Asociacion(None, SIN_PREDICCIONES)
    pid = recomendacion.get("prediction_id")
    if pid is not None:
        candidatas = [p for p in predicciones if p.get("prediction_id") == pid]
    else:
        franja = instante(recomendacion.get("franja_inicio"))
        if franja is None:
            return Asociacion(None, FRANJA_ILEGIBLE)
        mismas = [p for p in predicciones
                  if str(p.get("idruta")) == str(recomendacion.get("idruta"))
                  and str(p.get("horizonte_min")) == str(recomendacion.get("horizonte_min"))]
        instantes = [(p, instante(p.get("franja_inicio"))) for p in mismas]
        if any(t is None for _, t in instantes):
            return Asociacion(None, FRANJA_ILEGIBLE)
        if any(not comparables(franja, t) for _, t in instantes):
            return Asociacion(None, ZONA_MIXTA)
        candidatas = [p for p, t in instantes if t == franja]
    if len(candidatas) > 1:
        return Asociacion(None, AMBIGUA)
    if not candidatas:
        return Asociacion(None, NO_ENCONTRADA)
    return Asociacion(candidatas[0], None)


# -------------------------------------------------------------------------- explicación (RF-015)

NO_DISPONIBLE = "no disponible"


def _texto(valor) -> str:
    """Representación del valor tal como llegó (sin redondear ni convertir unidades)."""
    if valor is None:
        return NO_DISPONIBLE
    if isinstance(valor, bool):
        return "sí" if valor else "no"
    return str(valor)


def regla_parametros(parametros_version) -> tuple[str | None, str | None]:
    """('param_v2', 'laboral') a partir de 'param_v2:laboral'."""
    if not isinstance(parametros_version, str) or ":" not in parametros_version:
        return (parametros_version if isinstance(parametros_version, str) else None), None
    version, tipo = parametros_version.rsplit(":", 1)
    return version, tipo


def explicar_recomendacion(rec: Mapping, prediccion: Mapping | None,
                           motivo_sin_prediccion: str | None = None) -> dict:
    """Explicación de una recomendación con los valores guardados.

    Devuelve {"regla": [...], "pasos": [{"concepto", "regla", "valor", "origen"}], "viable": bool|None,
    "motivo": str|None, "no_disponibles": [...]}. "valor" es el texto del valor tal como lo devolvió
    la API; ninguna cifra se calcula aquí. ``motivo_sin_prediccion`` explica por qué falta la
    predicción de entrada (texto libre o clave de MOTIVO_NO_ASOCIADA)."""
    base = rec.get("base_demanda")
    version, tipo_dia = regla_parametros(rec.get("parametros_version"))
    nombre_base = {"puntual": "predicción puntual", "superior": "límite superior"}.get(base, "desconocida")
    regla = [f"Demanda base: {_texto(base)} ({nombre_base}).",
             f"Parámetros operativos: versión {_texto(version)}, tipo de día {_texto(tipo_dia)}."]
    if tipo_dia == "todos":
        regla.append("Se usaron los parámetros 'todos' de la ruta: no había fila vigente para el tipo "
                     "de día de la franja.")

    no_disponibles = list(NO_DISPONIBLES_RECOMENDACION)
    if prediccion is None:
        demanda, origen_demanda = None, "sin predicción asociada"
        causa = MOTIVO_NO_ASOCIADA.get(motivo_sin_prediccion, motivo_sin_prediccion) or "sin detalle"
        no_disponibles.insert(0, f"Demanda de entrada (predicción usada): {causa}.")
    else:
        columna = "limite_superior" if base == "superior" else "demanda_pred"
        demanda, origen_demanda = prediccion.get(columna), f"prediction.{columna}"

    viable = rec.get("es_viable")
    pasos = [
        ("Demanda del horizonte", "predicción usada como base", demanda, origen_demanda),
        ("Horizonte (min)", "ventana de la predicción", rec.get("horizonte_min"), "recommendation"),
        ("Demanda por hora", "demanda × 60 / horizonte", rec.get("demanda_hora"), "recommendation"),
        ("Factor de carga máximo", "parámetro operativo", rec.get("factor_carga_max"), "recommendation"),
        ("Carga máxima por hora", "demanda por hora × factor de carga",
         rec.get("carga_max_estimada_hora"), "recommendation"),
        ("Capacidad nominal", f"fuente: {_texto(rec.get('capacidad_fuente'))}",
         rec.get("capacidad_nominal"), "recommendation"),
        ("Ocupación objetivo", "parámetro operativo", rec.get("ocupacion_objetivo"), "recommendation"),
        ("Capacidad efectiva", "capacidad nominal × ocupación objetivo",
         rec.get("capacidad_efectiva"), "recommendation"),
        ("Buses por hora recomendados", "techo(carga máxima / capacidad efectiva), con un mínimo",
         rec.get("buses_hora_recomendado"), "recommendation"),
        ("Headway recomendado (min)", "60 / buses por hora", rec.get("headway_recomendado_min"),
         "recommendation"),
        ("Ciclo (min)", "parámetro operativo", rec.get("ciclo_min"), "recommendation"),
        ("Vehículos requeridos", "techo(ciclo / headway)", rec.get("vehiculos_requeridos"),
         "recommendation"),
        ("Tope de flota", "parámetro operativo", rec.get("flota_tope"), "recommendation"),
        ("Buses por hora actuales", "salidas programadas en la hora (RF-008)",
         rec.get("buses_hora_actual"), "recommendation"),
        ("Headway actual (min)", "60 / buses actuales", rec.get("headway_actual_min"), "recommendation"),
        ("Diferencia buses por hora", "recomendado − actual", rec.get("delta_buses_hora"),
         "recommendation"),
        ("Viable", "headway ≥ mínimo y vehículos ≤ tope de flota", viable, "recommendation"),
    ]
    if viable is False:
        regla.append("Recomendación no viable: no se publica una cifra de buses ni de headway.")
    return {
        "regla": regla,
        "pasos": [{"concepto": c, "regla": r, "valor": _texto(v), "origen": o} for c, r, v, o in pasos],
        "viable": viable if isinstance(viable, bool) else None,
        "motivo": rec.get("motivo_no_viable"),
        "no_disponibles": no_disponibles,
    }
