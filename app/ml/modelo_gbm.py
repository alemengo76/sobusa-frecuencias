"""Gradient boosting sobre FE 3.1 (comun-1.5), servido por la API (E01).

Qué cambia frente a app/ml/gbm.py (que se conserva para las pruebas antiguas):
  - Las variables son EXACTAMENTE las de FE 3.1 (conjunto_entrenamiento.VARIABLES) y se calculan
    con el mismo código al entrenar y al predecir (app/fe31 es copia exacta del FE).
  - Objetivo de producción: y_pasajeros (pasajeros de salidas elegibles en [t, t+H)).
  - Un solo modelo con el horizonte como variable (como el conjunto del FE), pérdida Poisson.
  - El modelo se entrena fuera de la API (scripts/entrenar_gbm_fe31.py), se guarda con su hash y
    la API solo lo carga (una vez, en caché) y predice. Nunca se entrena en una solicitud.

Predicción en línea para el día D (contrato D+1, igual que en el FE):
  1. Viajes de [D - 35 días, D) desde ai_frecuencias.viaje_fuente (la ingesta los copia).
  2. reglas_comunes: cuarentena, calendario hasta D + 1 (víspera de festivo), clasificación de días.
     El día D no tiene viajes todavía: se marca 'completo' para que tenga filas.
  3. Horario de operación: el guardado en el artefacto (el del entrenamiento), no uno recalculado.
  4. construir_conjunto() del FE y se toman las filas de D. Con 35 días alcanzan todos los rezagos
     (máximo 28 días) y el nivel de 28 días. La prueba test_variables_en_linea_iguales_a_entrenamiento
     verifica que las variables de D son idénticas a las del conjunto completo.
"""
from __future__ import annotations

import hashlib
import io
import json
import pickle
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor

from app.fe31 import FEATURE_VERSION, REGLAS_VERSION, ce, rc

METODO = "gbm_fe31"
FORMATO = "sobusa-gbm-fe31/1"
OBJETIVO = "y_pasajeros"
HISTORIA_DIAS = 35
SEMILLA = 2026
VARIABLES = list(ce.VARIABLES)
CATEGORIAS = {
    "id_ruta": ["2", "16", "21"],
    "tipo_dia": ["laboral", "sabado", "dom_festivo"],
    "marca_calendario": ["normal", "fin_de_anio", "sabado_santo", "festivo_comun",
                         "festivo_profundo", "carnaval_lun_mar"],
}
HIPERPARAMETROS = {"loss": "poisson", "learning_rate": 0.05, "max_iter": 600, "max_leaf_nodes": 63,
                   "min_samples_leaf": 40, "l2_regularization": 1.0, "early_stopping": False,
                   "random_state": SEMILLA}


class ModeloNoDisponible(RuntimeError):
    """El GBM no puede predecir (artefacto ausente, alterado, incompatible o sin historia)."""


# ------------------------------------------------------------------ variables

def matriz(X: pd.DataFrame) -> pd.DataFrame:
    """Variables del FE -> matriz numérica. Las categóricas usan códigos FIJOS (CATEGORIAS):
    una categoría desconocida queda como faltante, nunca como otra categoría."""
    M = pd.DataFrame(index=X.index)
    for v in VARIABLES:
        if v in CATEGORIAS:
            codigos = {c: float(i) for i, c in enumerate(CATEGORIAS[v])}
            M[v] = X[v].astype(str).map(codigos).astype("float64")
        else:
            M[v] = pd.to_numeric(X[v], errors="coerce").astype("float64")
    return M


def _mascara_categoricas() -> list[bool]:
    return [v in CATEGORIAS for v in VARIABLES]


def preparar(P: pd.DataFrame, desde, hasta, horario: pd.DataFrame | None = None,
             forzar_completo=()) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Pipeline del FE: (X, horario, dias). P en formato FE (app.fe31.viajes.a_formato_fe)."""
    cal = rc.calendario(desde, hasta)
    P, _ = rc.aplicar_cuarentena(P)
    dias = rc.clasificar_dias(P, cal)
    if len(forzar_completo):
        dias.loc[dias.fecha.isin(pd.to_datetime(list(forzar_completo))), "clase_dia"] = "completo"
    hor = rc.horario_operacion(P, cal, dias) if horario is None else horario
    return ce.construir_conjunto(P, cal, dias, hor), hor, dias


def variables_dia(P: pd.DataFrame, dia, horario: pd.DataFrame) -> pd.DataFrame:
    """Filas de FE 3.1 del día `dia` con solo los viajes anteriores a ese día (contrato D+1)."""
    D = pd.Timestamp(dia).normalize()
    P = P[(P.salida_programada >= D - timedelta(days=HISTORIA_DIAS)) & (P.salida_programada < D)]
    if P.empty:
        raise ModeloNoDisponible(f"No hay viajes ingeridos en los {HISTORIA_DIAS} días anteriores a {D:%Y-%m-%d}")
    X, _, _ = preparar(P, D - timedelta(days=HISTORIA_DIAS), D + timedelta(days=1), horario,
                       forzar_completo=[D])
    return X[X.fecha.eq(D)].reset_index(drop=True)


# ------------------------------------------------------------------ entrenamiento

def entrenar(X: pd.DataFrame, max_iter: int | None = None) -> HistGradientBoostingRegressor:
    hp = {**HIPERPARAMETROS, **({"max_iter": int(max_iter)} if max_iter else {})}
    m = HistGradientBoostingRegressor(categorical_features=_mascara_categoricas(), **hp)
    m.fit(matriz(X), X[OBJETIVO].to_numpy(dtype="float64"))
    return m


def mejor_iteracion(m: HistGradientBoostingRegressor, V: pd.DataFrame) -> tuple[int, list[float]]:
    """Número de árboles con menor WAPE en validación (parada temprana CRONOLÓGICA, sin mezclar)."""
    y = V[OBJETIVO].to_numpy(dtype="float64")
    wape = [float(np.abs(y - p).sum() / np.abs(y).sum()) for p in m.staged_predict(matriz(V))]
    return int(np.argmin(wape)) + 1, wape


def predecir_matriz(m, X: pd.DataFrame) -> np.ndarray:
    return np.clip(m.predict(matriz(X)), 0, None)


# ------------------------------------------------------------------ artefacto

@dataclass
class Artefacto:
    modelo: HistGradientBoostingRegressor
    horario: pd.DataFrame
    intervalos: pd.DataFrame          # idruta, horizonte_min, hora, error_inferior, error_superior
    meta: dict = field(default_factory=dict)


def guardar(a: Artefacto, carpeta: Path, nombre: str) -> tuple[Path, str]:
    carpeta.mkdir(parents=True, exist_ok=True)
    meta = {"formato": FORMATO, "metodo": METODO, "feature_version": FEATURE_VERSION,
            "reglas_version": REGLAS_VERSION, "objetivo": OBJETIVO, "variables": VARIABLES,
            "categorias": CATEGORIAS, "sklearn": sklearn.__version__, "numpy": np.__version__,
            "pandas": pd.__version__, **a.meta}
    paquete = {"modelo": a.modelo, "horario": a.horario[["id_ruta", "tipo_dia", "hhmm", "operativa"]],
               "intervalos": a.intervalos, "meta": meta}
    ruta = carpeta / f"{nombre}.pkl"
    temporal = ruta.with_suffix(".tmp")
    temporal.write_bytes(pickle.dumps(paquete, protocol=pickle.HIGHEST_PROTOCOL))
    temporal.replace(ruta)
    sha = hashlib.sha256(ruta.read_bytes()).hexdigest()
    (carpeta / f"{nombre}.json").write_text(json.dumps({**meta, "artefacto": ruta.name, "sha256": sha},
                                                       indent=2, default=str), encoding="utf-8")
    return ruta, sha


@lru_cache(maxsize=4)
def cargar(ruta: str, sha_esperado: str) -> Artefacto:
    """Carga UNA vez por (ruta, hash). Verifica el hash ANTES de deserializar: un archivo
    cambiado o reemplazado no se abre."""
    p = Path(ruta)
    if not p.is_file():
        raise ModeloNoDisponible(f"No existe el artefacto {p}")
    datos = p.read_bytes()
    sha = hashlib.sha256(datos).hexdigest()
    if not sha_esperado or sha != sha_esperado.strip():
        raise ModeloNoDisponible(f"El hash del artefacto {p.name} no coincide con model_registry")
    paquete = pickle.load(io.BytesIO(datos))  # noqa: S301 - archivo propio verificado por hash
    meta = paquete.get("meta", {})
    if meta.get("formato") != FORMATO:
        raise ModeloNoDisponible(f"Formato de artefacto desconocido: {meta.get('formato')}")
    if meta.get("sklearn") != sklearn.__version__:
        raise ModeloNoDisponible(
            f"El artefacto se entrenó con scikit-learn {meta.get('sklearn')} y aquí hay "
            f"{sklearn.__version__}: reentrene dentro del contenedor (ver README)")
    if meta.get("feature_version") != FEATURE_VERSION or meta.get("reglas_version") != REGLAS_VERSION:
        raise ModeloNoDisponible("El artefacto no corresponde a FE 3.1 / comun-1.5")
    return Artefacto(paquete["modelo"], paquete["horario"], paquete["intervalos"], meta)


def ruta_artefacto(artefacto_ruta: str, carpeta_modelos: str) -> str:
    """En model_registry se guarda el nombre del archivo; la carpeta viene de MODELOS_DIR."""
    p = Path(artefacto_ruta)
    return str(p if p.is_absolute() else Path(carpeta_modelos) / p.name)


# ------------------------------------------------------------------ predicción

def predecir_dia(a: Artefacto, P: pd.DataFrame, dia, idrutas, horizontes) -> pd.DataFrame:
    """Predicción de y_pasajeros para cada franja operativa del día y horizonte pedido.

    Devuelve idruta, franja_inicio, horizonte_min, demanda_pred, limite_inferior, limite_superior.
    Falla (ModeloNoDisponible) si no hay historia suficiente o el resultado no es válido; quien
    llama decide usar la línea base."""
    X = variables_dia(P, dia, a.horario)
    X = X[X.id_ruta.isin([str(r) for r in idrutas]) & X.horizonte_min.isin(list(horizontes))]
    if X.empty:
        raise ModeloNoDisponible("El día pedido no tiene franjas operativas para esas rutas")
    # Un rezago faltante es normal (p. ej., hace 7 días fue Nochebuena y quedó de baja calidad):
    # el modelo se entrenó con esos huecos. Solo se rechaza la ruta si no tiene NINGÚN rezago
    # semanal ni nivel de 28 días, es decir, si no hay historia reciente.
    rezagos = ["y_rezago_7d", "y_rezago_14d", "y_rezago_21d", "y_media_4_semanas"]
    sin_historia = X.groupby("id_ruta").apply(
        lambda g: g[rezagos].isna().all().all() or g.nivel_diario_28d.isna().all(), include_groups=False)
    if sin_historia.any():
        rutas = sorted(sin_historia.index[sin_historia].tolist())
        raise ModeloNoDisponible(f"Historia insuficiente (sin rezagos semanales ni nivel de 28 días) para rutas {rutas}")
    pred = predecir_matriz(a.modelo, X)
    if not np.isfinite(pred).all():
        raise ModeloNoDisponible("El modelo produjo valores no finitos")
    out = pd.DataFrame({"idruta": X.id_ruta.astype(int).to_numpy(), "franja_inicio": X.origen.to_numpy(),
                        "horizonte_min": X.horizonte_min.astype("int64").to_numpy(),
                        "demanda_pred": pred.astype("float64")})
    return aplicar_intervalos(out, a.intervalos)


def ajustar_intervalos(v: pd.DataFrame, cuantiles=(0.10, 0.90)) -> pd.DataFrame:
    """Cuantiles del error (real - pred) en validación por ruta, horizonte y hora."""
    d = v.assign(error=v.demanda_real - v.demanda_pred, hora=pd.DatetimeIndex(v.franja_inicio).hour)
    q = d.groupby(["idruta", "horizonte_min", "hora"]).error.quantile(list(cuantiles)).unstack()
    q.columns = ["error_inferior", "error_superior"]
    return q.reset_index()


def aplicar_intervalos(pred: pd.DataFrame, intervalos: pd.DataFrame) -> pd.DataFrame:
    p = pred.assign(hora=pd.DatetimeIndex(pred.franja_inicio).hour)
    p = p.merge(intervalos, on=["idruta", "horizonte_min", "hora"], how="left")
    p["limite_inferior"] = np.minimum((p.demanda_pred + p.error_inferior.fillna(0)).clip(lower=0), p.demanda_pred)
    p["limite_superior"] = np.maximum(p.demanda_pred + p.error_superior.fillna(0), p.demanda_pred)
    return p.drop(columns=["hora", "error_inferior", "error_superior"])

