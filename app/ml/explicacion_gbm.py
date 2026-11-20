"""Explicación individual del GBM FE 3.1 con TreeSHAP dependiente del camino (RF-015, alternativa A1).

Qué calcula
-----------
Para una fila de variables de FE 3.1, el aporte de cada una de las 29 variables a la predicción en la
escala del modelo. La pérdida es Poisson: el modelo suma árboles en escala logarítmica y la predicción
es ``exp(raw)``. Se cumple exactamente

    referencia + suma(aportes) = raw = log(predicción)

donde ``referencia`` es la esperanza de ``raw`` sobre las filas de entrenamiento según la cubierta de
cada nodo (``count``). En pasajeros, cada aporte es un FACTOR multiplicativo ``exp(aporte)`` sobre
``exp(referencia)``; los aportes no son pasajeros que se suman.

Algoritmo: TreeSHAP dependiente del camino (Lundberg, Erion y Lee, 2018, algoritmo 2), con la cubierta
``count`` que guarda cada nodo. No usa la librería shap ni dependencias nuevas.

Dependencia de scikit-learn 1.9.1
---------------------------------
Usa internos de ``HistGradientBoostingRegressor``: ``_preprocessor`` (ColumnTransformer que recodifica
las categóricas con OrdinalEncoder y REORDENA las columnas: primero las categóricas), ``_predictors``,
``_baseline_prediction`` y ``_bin_mapper.make_known_categories_bitsets()``; y de ``TreePredictor``:
``nodes`` y ``raw_left_cat_bitsets``. El recorrido replica ``_predict_one_from_raw_data`` de
``_predictor.pyx`` 1.9.1. Son internos privados: por eso ``modelo_gbm.cargar`` exige la versión exacta,
``validar_estructura`` comprueba la forma del modelo antes de usarlo y ``explicar`` verifica, fila por
fila, la identidad referencia + suma = ``_raw_predict``. Si algo no cuadra se lanza un error y no se
devuelve ninguna explicación.

Importancia por ganancia (``importancia_ganancia``)
--------------------------------------------------
Proporción de la ganancia de entrenamiento (reducción de la pérdida) lograda en las divisiones de cada
variable, en el ARTEFACTO de producción. Es global, favorece a las variables con muchas divisiones y no
mide el efecto en una predicción concreta. No equivale a la importancia por permutación guardada en
``metricas.importancia_top10``, que se calculó con el modelo de evaluación del entrenamiento.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.preprocessing import OrdinalEncoder

from app.ml import modelo_gbm as G

METODO = "treeshap_dependiente_del_camino"
METODO_IMPORTANCIA = "ganancia_de_division"
TOLERANCIA = 1e-9
CAMPOS_NODO = ("value", "count", "feature_idx", "num_threshold", "missing_go_to_left", "left", "right",
               "gain", "is_leaf", "is_categorical", "bitset_idx")
ESTADO = "reconstruida_y_verificada"
ADVERTENCIA = (
    "Explicación reconstruida y verificada, no garantizada: las variables se recalculan con los viajes "
    "que hay hoy en ai_frecuencias.viaje_fuente, que la ingesta puede reescribir. Se exigió que ningún "
    "viaje del período se haya recargado después de la ejecución y que todas las predicciones de la "
    "ejecución y la ruta se reproduzcan. Riesgo residual: un día releído que quedó sin viajes de la ruta "
    "no deja rastro, y un cambio que no altere ninguna predicción redondeada no se detecta.")


class ExplicacionNoValida(RuntimeError):
    """El artefacto no tiene la estructura esperada o la identidad matemática no se cumple."""


class ReproduccionDistinta(RuntimeError):
    """Las predicciones recalculadas no coinciden con las guardadas: no se explica."""


# ------------------------------------------------------------------ estructura del modelo

def validar_estructura(modelo) -> None:
    """Comprueba, antes de calcular nada, que el modelo tiene la forma que el algoritmo supone."""
    if not isinstance(modelo, HistGradientBoostingRegressor):
        raise ExplicacionNoValida(f"Se esperaba HistGradientBoostingRegressor, no {type(modelo).__name__}")
    if modelo.loss != "poisson" or getattr(modelo, "n_trees_per_iteration_", None) != 1:
        raise ExplicacionNoValida("Se esperaba pérdida Poisson con un árbol por iteración")
    if list(getattr(modelo, "feature_names_in_", [])) != G.VARIABLES:
        raise ExplicacionNoValida("Las variables del modelo no son las 29 de FE 3.1 en su orden")
    if list(getattr(modelo, "is_categorical_", [])) != [v in G.CATEGORIAS for v in G.VARIABLES]:
        raise ExplicacionNoValida("Las variables categóricas del modelo no son las esperadas")
    columnas_arbol(modelo)                                   # valida el preprocesador
    base = np.ravel(getattr(modelo, "_baseline_prediction", []))
    if base.shape != (1,) or not np.isfinite(base).all():
        raise ExplicacionNoValida("Predicción base del modelo no válida")
    predictores = getattr(modelo, "_predictors", None)
    if not predictores or any(len(it) != 1 for it in predictores):
        raise ExplicacionNoValida("Árboles del modelo no disponibles")
    n = len(G.VARIABLES)
    for it in predictores:
        nodos = it[0].nodes
        if not set(CAMPOS_NODO) <= set(nodos.dtype.names or ()):
            raise ExplicacionNoValida("Los nodos de los árboles no tienen los campos esperados")
        if not hasattr(it[0], "raw_left_cat_bitsets"):
            raise ExplicacionNoValida("Árbol sin conjuntos de categorías")
        for i, nodo in enumerate(nodos):
            if nodo["is_leaf"]:
                continue
            izq, der = int(nodo["left"]), int(nodo["right"])
            if not (i < izq < len(nodos) and i < der < len(nodos)):
                raise ExplicacionNoValida("Hijos de un nodo fuera de orden")
            if nodo["count"] != nodos[izq]["count"] + nodos[der]["count"] or nodo["count"] <= 0:
                raise ExplicacionNoValida("La cubierta (count) de un nodo no es la suma de sus hijos")
            if not 0 <= int(nodo["feature_idx"]) < n:
                raise ExplicacionNoValida("Índice de variable fuera de rango")


def columnas_arbol(modelo) -> list[str]:
    """Nombres de las columnas en el orden en que las ven los árboles (salida del preprocesador).

    El preprocesador pone primero las categóricas recodificadas (OrdinalEncoder) y luego las numéricas;
    usar ``feature_names_in_`` directamente atribuiría los aportes a variables equivocadas."""
    pre = getattr(modelo, "_preprocessor", None)
    transformadores = getattr(pre, "transformers_", None)
    if transformadores is None:
        raise ExplicacionNoValida("El modelo no tiene el preprocesador esperado")
    nombres, n = [], len(G.VARIABLES)
    for nombre, transformador, mascara in transformadores:
        if isinstance(mascara, str):                 # 'remainder' sin columnas
            if mascara == "drop" or transformador == "drop":
                continue
            raise ExplicacionNoValida("Preprocesador con columnas no reconocidas")
        mascara = np.asarray(mascara)
        if mascara.dtype != bool or mascara.shape != (n,):
            raise ExplicacionNoValida("Máscara del preprocesador no reconocida")
        if nombre == "encoder":
            if not isinstance(transformador, OrdinalEncoder):
                raise ExplicacionNoValida("Las categóricas no usan OrdinalEncoder")
            categoricas = [G.VARIABLES[j] for j in np.flatnonzero(mascara)]
            if categoricas != [v for v in G.VARIABLES if v in G.CATEGORIAS]:
                raise ExplicacionNoValida("Columnas categóricas del preprocesador inesperadas")
            for v, cats in zip(categoricas, transformador.categories_):
                # Las aprendidas deben ser códigos fijos (modelo_gbm.CATEGORIAS), sin repetir y en orden; una
                # categoría que no se vio al entrenar se recodifica como faltante, igual que al predecir.
                aprendidas = [float(c) for c in cats]
                fijos = {float(i) for i in range(len(G.CATEGORIAS[v]))}
                if not aprendidas or not set(aprendidas) <= fijos or aprendidas != sorted(set(aprendidas)):
                    raise ExplicacionNoValida(f"Categorías de {v} distintas de los códigos fijos")
        nombres += [G.VARIABLES[j] for j in np.flatnonzero(mascara)]
    if sorted(nombres) != sorted(G.VARIABLES) or len(nombres) != n:
        raise ExplicacionNoValida("El preprocesador no cubre las 29 variables exactamente una vez")
    return nombres


def esperanzas(nodos) -> np.ndarray:
    """Esperanza de cada nodo con los valores de las hojas y la cubierta ``count``.

    Los ``value`` de los nodos internos no sirven: están en otra escala (sin la tasa de aprendizaje).
    Requiere que cada hijo tenga un índice mayor que su padre (lo comprueba validar_estructura)."""
    E = np.zeros(len(nodos), dtype="float64")
    for i in range(len(nodos) - 1, -1, -1):
        n = nodos[i]
        if n["is_leaf"]:
            E[i] = n["value"]
        else:
            izq, der = nodos[n["left"]], nodos[n["right"]]
            E[i] = (E[n["left"]] * izq["count"] + E[n["right"]] * der["count"]) / n["count"]
    return E


# ------------------------------------------------------------------ recorrido

def _en_bitset(bits, fila: int, valor: float) -> bool:
    v = int(valor) & 0xFF                     # el predictor lo convierte a uint8
    return bool((int(bits[fila, v // 32]) >> (v % 32)) & 1)


def va_a_la_izquierda(nodo, x: np.ndarray, raw_bits, known_bits, f_idx_map) -> bool:
    """Réplica de _predict_one_from_raw_data (scikit-learn 1.9.1)."""
    valor = x[nodo["feature_idx"]]
    if math.isnan(valor):
        return bool(nodo["missing_go_to_left"])
    if nodo["is_categorical"]:
        if valor < 0:                          # fuera de rango: como faltante
            return bool(nodo["missing_go_to_left"])
        if _en_bitset(raw_bits, int(nodo["bitset_idx"]), valor):
            return True
        if _en_bitset(known_bits, int(f_idx_map[nodo["feature_idx"]]), valor):
            return False
        return bool(nodo["missing_go_to_left"])   # categoría desconocida: como faltante
    return valor <= nodo["num_threshold"]


@dataclass
class _Contexto:
    arboles: list
    known_bits: np.ndarray
    f_idx_map: np.ndarray
    referencia: float


def _contexto(modelo) -> _Contexto:
    known_bits, f_idx_map = modelo._bin_mapper.make_known_categories_bitsets()
    arboles = [it[0] for it in modelo._predictors]
    base = float(np.ravel(modelo._baseline_prediction)[0])
    referencia = base + float(sum(esperanzas(t.nodes)[0] for t in arboles))
    return _Contexto(arboles, known_bits, f_idx_map, referencia)


def hoja(arbol, x: np.ndarray, known_bits, f_idx_map) -> int:
    """Índice de la hoja a la que llega ``x`` (para comprobar el recorrido contra scikit-learn)."""
    nodos, i = arbol.nodes, 0
    while not nodos[i]["is_leaf"]:
        n = nodos[i]
        i = int(n["left"] if va_a_la_izquierda(n, x, arbol.raw_left_cat_bitsets, known_bits, f_idx_map)
                else n["right"])
    return i


# ------------------------------------------------------------------ TreeSHAP

def _shap_arbol(arbol, x, phi, known_bits, f_idx_map) -> None:
    nodos, raw_bits = arbol.nodes, arbol.raw_left_cat_bitsets

    def extender(camino, pz, po, pi):
        camino = [list(e) for e in camino]
        lon = len(camino)
        camino.append([pi, pz, po, 1.0 if lon == 0 else 0.0])
        for i in range(lon - 1, -1, -1):
            camino[i + 1][3] += po * camino[i][3] * (i + 1) / (lon + 1)
            camino[i][3] = pz * camino[i][3] * (lon - i) / (lon + 1)
        return camino

    def deshacer(camino, i):
        camino = [list(e) for e in camino]
        lon = len(camino) - 1
        n, po, pz = camino[lon][3], camino[i][2], camino[i][1]
        for j in range(lon - 1, -1, -1):
            if po != 0:
                t = camino[j][3]
                camino[j][3] = n * (lon + 1) / ((j + 1) * po)
                n = t - camino[j][3] * pz * (lon - j) / (lon + 1)
            else:
                camino[j][3] = camino[j][3] * (lon + 1) / (pz * (lon - j))
        for j in range(i, lon):
            camino[j][0], camino[j][1], camino[j][2] = camino[j + 1][0], camino[j + 1][1], camino[j + 1][2]
        return camino[:lon]

    def suma_deshecha(camino, i):
        lon = len(camino) - 1
        po, pz, n, total = camino[i][2], camino[i][1], camino[lon][3], 0.0
        for j in range(lon - 1, -1, -1):
            if po != 0:
                t = n * (lon + 1) / ((j + 1) * po)
                total += t
                n = camino[j][3] - t * pz * (lon - j) / (lon + 1)
            else:
                total += camino[j][3] / (pz * (lon - j) / (lon + 1))
        return total

    def recorrer(j, camino, pz, po, pi):
        camino = extender(camino, pz, po, pi)
        nodo = nodos[j]
        if nodo["is_leaf"]:
            for i in range(1, len(camino)):
                w = suma_deshecha(camino, i)
                phi[camino[i][0]] += w * (camino[i][2] - camino[i][1]) * nodo["value"]
            return
        if va_a_la_izquierda(nodo, x, raw_bits, known_bits, f_idx_map):
            caliente, fria = int(nodo["left"]), int(nodo["right"])
        else:
            caliente, fria = int(nodo["right"]), int(nodo["left"])
        iz, io = 1.0, 1.0
        d = int(nodo["feature_idx"])
        k = next((i for i in range(1, len(camino)) if camino[i][0] == d), None)
        if k is not None:
            iz, io = camino[k][1], camino[k][2]
            camino = deshacer(camino, k)
        cubierta = nodo["count"]
        recorrer(caliente, camino, iz * nodos[caliente]["count"] / cubierta, io, d)
        recorrer(fria, camino, iz * nodos[fria]["count"] / cubierta, 0.0, d)

    recorrer(0, [], 1.0, 1.0, -1)


def treeshap_fila(modelo, x: np.ndarray, contexto: _Contexto | None = None) -> np.ndarray:
    """Aportes (escala log) de una fila YA preprocesada, en el orden de ``columnas_arbol(modelo)``."""
    ctx = contexto or _contexto(modelo)
    x = np.asarray(x, dtype="float64")
    if x.shape != (len(G.VARIABLES),):
        raise ExplicacionNoValida("La fila no tiene las 29 variables")
    phi = np.zeros(len(G.VARIABLES), dtype="float64")
    for arbol in ctx.arboles:
        _shap_arbol(arbol, x, phi, ctx.known_bits, ctx.f_idx_map)
    return phi


def _json(valor):
    if valor is None or (isinstance(valor, float) and math.isnan(valor)) or valor is pd.NA:
        return None
    if isinstance(valor, np.generic):
        valor = valor.item()
    return None if isinstance(valor, float) and math.isnan(valor) else valor


def explicar(art: G.Artefacto, X: pd.DataFrame) -> list[dict]:
    """Explicación TreeSHAP de cada fila de variables de FE 3.1 ``X`` con el artefacto ``art``.

    Devuelve por fila: referencia y predicción en escala log y en pasajeros, y los 29 aportes
    (variable, valor de FE, valor que ve el modelo, aporte log, factor). Lanza ExplicacionNoValida si
    la estructura no es la esperada o si referencia + suma(aportes) difiere de ``_raw_predict`` en
    más de TOLERANCIA."""
    modelo = art.modelo
    validar_estructura(modelo)
    nombres = columnas_arbol(modelo)
    M = G.matriz(X)[G.VARIABLES]
    Xp = np.asarray(modelo._preprocess_X(M, reset=False), dtype="float64")
    raw = np.asarray(modelo._raw_predict(M), dtype="float64").ravel()
    ctx = _contexto(modelo)
    resultados = []
    for i in range(len(X)):
        phi = treeshap_fila(modelo, Xp[i], ctx)
        error = abs(ctx.referencia + float(phi.sum()) - float(raw[i]))
        if not error <= TOLERANCIA:
            raise ExplicacionNoValida(f"La suma de aportes no reproduce la predicción (error {error:.3e})")
        por_nombre = dict(zip(nombres, phi))
        aportes = [{"variable": v, "valor": _json(X[v].iloc[i]), "valor_modelo": _json(float(M[v].iloc[i])),
                    "aporte_log": float(por_nombre[v]), "factor": float(math.exp(por_nombre[v]))}
                   for v in G.VARIABLES]
        aportes.sort(key=lambda a: abs(a["aporte_log"]), reverse=True)
        resultados.append({"referencia_log": ctx.referencia, "referencia_pasajeros": math.exp(ctx.referencia),
                           "prediccion_log": float(raw[i]), "prediccion_pasajeros": math.exp(float(raw[i])),
                           "error_identidad": error, "aportes": aportes})
    return resultados


def importancia_ganancia(modelo) -> pd.DataFrame:
    """Ganancia de entrenamiento por variable en el artefacto (global; ver docstring del módulo)."""
    validar_estructura(modelo)
    nombres = columnas_arbol(modelo)
    ganancia = np.zeros(len(nombres))
    divisiones = np.zeros(len(nombres), dtype="int64")
    for it in modelo._predictors:
        for n in it[0].nodes:
            if not n["is_leaf"]:
                ganancia[n["feature_idx"]] += float(n["gain"])
                divisiones[n["feature_idx"]] += 1
    total = ganancia.sum()
    return (pd.DataFrame({"variable": nombres, "ganancia": ganancia,
                          "proporcion": ganancia / total if total > 0 else np.nan, "divisiones": divisiones})
              .sort_values("ganancia", ascending=False).reset_index(drop=True))


# ------------------------------------------------------------------ verificaciones del servicio

CENTAVO = Decimal("0.01")


def a_precision_bd(valor) -> Decimal:
    """Valor tal como queda en numeric(10, 2): float8 con 15 dígitos significativos, redondeo a 2
    decimales con la mitad hacia afuera (lo que hace PostgreSQL al convertir float8 a numeric)."""
    if isinstance(valor, Decimal):
        return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)
    return Decimal(format(float(valor), ".15g")).quantize(CENTAVO, rounding=ROUND_HALF_UP)


def instante_local(valor) -> pd.Timestamp:
    """Instante de la base (timestamptz) como hora de Bogotá sin zona, el formato con que se predice."""
    t = pd.Timestamp(valor)
    return t.tz_convert("America/Bogota").tz_localize(None) if t.tzinfo is not None else t


def ventana_viajes(dia: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    """[D - HISTORIA_DIAS, D): los viajes que usa el GBM para el día D (contrato D+1)."""
    return dia - timedelta(days=G.HISTORIA_DIAS), dia


def verificar_reproduccion(guardadas: list[dict], recalculadas: pd.DataFrame) -> int:
    """Exige el mismo conjunto de franjas y horizontes, y la misma predicción y límites a la precisión
    de la base en TODAS las filas. Devuelve cuántas se compararon; si algo difiere, ReproduccionDistinta."""
    columnas = ("demanda_pred", "limite_inferior", "limite_superior")
    g = {(instante_local(f["franja_inicio"]), int(f["horizonte_min"])): f for f in guardadas}
    r = {(pd.Timestamp(f.franja_inicio), int(f.horizonte_min)): f for f in recalculadas.itertuples()}
    if len(g) != len(guardadas):
        raise ReproduccionDistinta("La ejecución tiene franjas repetidas para la ruta")
    if set(g) != set(r):
        faltan, sobran = len(set(g) - set(r)), len(set(r) - set(g))
        raise ReproduccionDistinta(f"Las franjas recalculadas no son las guardadas ({faltan} faltan, "
                                   f"{sobran} sobran)")
    distintas = [k for k in g for c in columnas
                 if a_precision_bd(g[k][c]) != a_precision_bd(getattr(r[k], c))]
    if distintas:
        raise ReproduccionDistinta(f"{len(set(distintas))} de {len(g)} predicciones recalculadas no "
                                   "coinciden con las guardadas (valor o límites)")
    return len(g)
