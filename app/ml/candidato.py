"""Entrenamiento REPRODUCIBLE de un modelo candidato GBM FE 3.1 (RF-018), separado del activo.

Misma receta que `scripts/entrenar_gbm_fe31.py` (mismas variables, hiperparámetros y semilla de
`app.ml.modelo_gbm`; parada temprana e intervalos en la partición de ajuste; modelo final con todo
lo anterior al corte), con dos diferencias:

1. **Corte duro de datos.** Solo se usan viajes con salida programada < `datos_hasta`, y el
   calendario, la cuarentena, la clasificación de días y el HORARIO de operación se calculan solo
   con esos viajes. El activo calculó horario y días anómalos con todo el histórico cargado (hasta
   2025-12), es decir, con información posterior a su entrenamiento (revisión de A6): aquí no.
2. **Reproducible por construcción.** La versión sale de la huella de la configuración y de los
   datos (no de la hora); el artefacto no guarda marcas de tiempo, así que la misma configuración
   con los mismos datos da el mismo SHA-256.

No escribe en la base, no registra en model_registry y no activa nada. El artefacto va a una
carpeta propia (por defecto `modelos/candidatos/`) y nunca reemplaza un archivo existente distinto.
"""
from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn

from app.fe31 import FEATURE_VERSION, REGLAS_VERSION, ce, rc
from app.ml import modelo_gbm as G
from app.ml.evaluacion import metricas

RAIZ = Path(__file__).resolve().parents[2]
ARCHIVOS_RECETA = ["app/ml/candidato.py", "app/ml/modelo_gbm.py", "app/ml/evaluacion.py",
                   "app/fe31/__init__.py", "app/fe31/conjunto_entrenamiento.py",
                   "app/fe31/reglas_comunes.py", "app/fe31/viajes.py"]
CARPETA_CANDIDATOS = Path("modelos") / "candidatos"
# Datos autorizados para ENTRENAR: los mismos del activo (anteriores a la prueba del FE). Pasar de
# aquí (jul-sep 2025, bloque de monitoreo de B1) requiere confirmar B1.
DATOS_AUTORIZADOS_HASTA = "2025-07-01"


class CandidatoInvalido(ValueError):
    """La configuración o los datos no permiten entrenar un candidato válido."""


@dataclass(frozen=True)
class ConfigCandidato:
    """Particiones cronológicas (límites exclusivos, hora de Bogotá):
    entrenamiento: origen < entrenamiento_hasta
    ajuste:        entrenamiento_hasta <= origen < ajuste_hasta (parada temprana e intervalos)
    desarrollo:    ajuste_hasta <= origen < datos_hasta (solo si ajuste_hasta < datos_hasta):
                   métricas del modelo de ajuste, como hace el script del activo. Es un diagnóstico
                   de la receta, NO la evaluación del artefacto final ni una comparación con el activo
    final:         todo origen < datos_hasta, con el número de árboles elegido en el ajuste
    En cada partición se purga la fila cuya ventana [t, t+H) termina después de su límite."""
    nombre: str
    entrenamiento_hasta: str
    datos_hasta: str
    ajuste_hasta: str | None = None
    semilla: int = G.SEMILLA
    hiperparametros: dict = field(default_factory=lambda: dict(G.HIPERPARAMETROS))
    notas: str = ""

    def validar(self) -> None:
        e, d = pd.Timestamp(self.entrenamiento_hasta), pd.Timestamp(self.datos_hasta)
        a = pd.Timestamp(self.ajuste_hasta or self.datos_hasta)
        if not e < a <= d:
            raise CandidatoInvalido("se exige entrenamiento_hasta < ajuste_hasta <= datos_hasta")
        if d > pd.Timestamp(DATOS_AUTORIZADOS_HASTA):
            raise CandidatoInvalido(f"datos_hasta {self.datos_hasta} supera los datos autorizados para "
                                    f"entrenar ({DATOS_AUTORIZADOS_HASTA}); requiere confirmar B1")
        # La receta aprobada fija hiperparámetros y semilla; cambiarlos es otro experimento a aprobar.
        if self.hiperparametros != G.HIPERPARAMETROS:
            raise CandidatoInvalido("los hiperparámetros difieren de la receta GBM FE 3.1 aprobada")
        if self.semilla != G.SEMILLA:
            raise CandidatoInvalido(f"la receta fija la semilla {G.SEMILLA}")

    def huella(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------------ huellas

def huella_viajes(crudo: pd.DataFrame) -> str:
    """SHA-256 canónico de los viajes usados (orden y formato fijos)."""
    cols = ["idprogramacion", "idruta", "fechasalida", "fechainicio", "fechafin", "finalizado",
            "cancelada", "numeropasajeros", "numerobajadas"]
    d = crudo[cols].copy().sort_values(["idprogramacion", "idruta"]).reset_index(drop=True)
    for c in ("fechasalida", "fechainicio", "fechafin"):
        d[c] = pd.to_datetime(d[c]).dt.strftime("%Y-%m-%dT%H:%M:%S")
    return hashlib.sha256(d.to_csv(index=False, na_rep="").encode()).hexdigest()


def huella_receta(raiz: Path = RAIZ) -> dict:
    """SHA-256 del código de la receta, con fin de línea normalizado (igual en Windows y Linux)."""
    return {f: hashlib.sha256((raiz / f).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for f in ARCHIVOS_RECETA}


# ------------------------------------------------------------------ datos y particiones

def recortar(crudo: pd.DataFrame, datos_hasta) -> pd.DataFrame:
    """Solo viajes con salida programada anterior al corte. Nada posterior entra al candidato."""
    corte = pd.Timestamp(datos_hasta)
    salida = pd.to_datetime(crudo.fechasalida)
    return crudo[salida < corte].copy()


def _fin(X):
    return X.origen + pd.to_timedelta(X.horizonte_min.astype(int), unit="min")


def construir(P: pd.DataFrame, cfg: ConfigCandidato) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Conjunto FE 3.1 calculado solo con viajes < datos_hasta, y sus particiones."""
    corte = pd.Timestamp(cfg.datos_hasta)
    if len(P) and P.salida_programada.max() >= corte:
        raise CandidatoInvalido("hay viajes posteriores a datos_hasta: use recortar() antes")
    cal = rc.calendario(P.salida_programada.min(), corte - pd.Timedelta(days=1))
    Pq, _ = rc.aplicar_cuarentena(P)
    dias = rc.clasificar_dias(Pq, cal)
    hor = rc.horario_operacion(Pq, cal, dias)
    X = ce.construir_conjunto(Pq, cal, dias, hor)
    X = X[X.etiqueta_completa & (_fin(X) <= corte)]
    e = pd.Timestamp(cfg.entrenamiento_hasta)
    a = pd.Timestamp(cfg.ajuste_hasta or cfg.datos_hasta)
    part = {"E": X[_fin(X) <= e], "A": X[(X.origen >= e) & (_fin(X) <= a)],
            "V": X[(X.origen >= a) & (_fin(X) <= corte)], "H": X}
    if min(len(v) for k, v in part.items() if k != "V") == 0:
        raise CandidatoInvalido(f"partición vacía: { {k: len(v) for k, v in part.items()} }")
    return X, hor, part


# ------------------------------------------------------------------ entrenamiento

def entrenar(crudo: pd.DataFrame, cfg: ConfigCandidato, version_datos: str,
             raiz: Path = RAIZ) -> tuple[G.Artefacto, dict]:
    """Entrena el candidato. Devuelve el artefacto (aún sin guardar) y su manifiesto."""
    from app.fe31.viajes import RUTAS, a_formato_fe
    cfg.validar()
    crudo = recortar(crudo[crudo.idruta.isin(RUTAS)], cfg.datos_hasta)
    if crudo.empty:
        raise CandidatoInvalido("no hay viajes anteriores a datos_hasta")
    sha_datos = huella_viajes(crudo)
    P = a_formato_fe(crudo)
    X, hor, part = construir(P, cfg)
    E, A, V, H = part["E"], part["A"], part["V"], part["H"]

    m = G.entrenar(E)
    n_arboles, curva = G.mejor_iteracion(m, A)
    m = G.entrenar(E, max_iter=n_arboles)
    pa = G.predecir_matriz(m, A)
    intervalos = G.ajustar_intervalos(pd.DataFrame({
        "idruta": A.id_ruta.astype(int), "horizonte_min": A.horizonte_min.astype(int),
        "franja_inicio": A.origen, "demanda_real": A[G.OBJETIVO], "demanda_pred": pa}))
    desarrollo = None
    if len(V):
        pv = G.predecir_matriz(m, V)
        Vp = V.assign(_pred=pv)
        ie = G.aplicar_intervalos(pd.DataFrame({
            "idruta": V.id_ruta.astype(int).to_numpy(), "franja_inicio": V.origen.to_numpy(),
            "horizonte_min": V.horizonte_min.astype(int).to_numpy(), "demanda_pred": pv}), intervalos)
        y = V[G.OBJETIVO].to_numpy()
        dentro = (y >= ie.limite_inferior.to_numpy()) & (y <= ie.limite_superior.to_numpy())
        desarrollo = {
            "advertencia": ("modelo de ajuste (entrenado con origen < entrenamiento_hasta), no el "
                            "artefacto final; filas de días completos del FE; no compara con el activo"),
            "periodo": f"[{cfg.ajuste_hasta}, {cfg.datos_hasta})", **metricas(V[G.OBJETIVO], Vp._pred),
            "cobertura_intervalo_80": float(dentro.mean()),
            "por_ruta_horizonte": [{"idruta": int(r), "horizonte_min": int(h),
                                    **metricas(g[G.OBJETIVO], g._pred)}
                                   for (r, h), g in Vp.groupby(["id_ruta", "horizonte_min"], observed=True)]}
    m_final = G.entrenar(H, max_iter=n_arboles)

    version = f"cand_{cfg.nombre}_{hashlib.sha256((cfg.huella() + sha_datos).encode()).hexdigest()[:12]}"
    Ap = A.assign(_pred=pa)
    ajuste = [{"idruta": int(r), "horizonte_min": int(h), **metricas(g[G.OBJETIVO], g._pred)}
              for (r, h), g in Ap.groupby(["id_ruta", "horizonte_min"], observed=True)]
    meta = {
        "version": version, "tipo": "candidato", "version_datos": version_datos,
        "datos_sha256": sha_datos, "config": asdict(cfg), "config_sha256": cfg.huella(),
        "semilla": cfg.semilla, "hiperparametros": {**G.HIPERPARAMETROS, "max_iter": n_arboles},
        "entrenado_hasta": str(H.origen.max()), "datos_hasta": cfg.datos_hasta,
        "filas_entrenamiento": int(len(H)),
        "particiones": {"entrenamiento": f"fin de ventana <= {cfg.entrenamiento_hasta}",
                        "ajuste": f"[{cfg.entrenamiento_hasta}, {cfg.ajuste_hasta or cfg.datos_hasta})",
                        "desarrollo": (f"[{cfg.ajuste_hasta}, {cfg.datos_hasta})" if len(V) else None),
                        "final": f"< {cfg.datos_hasta}", "filas": {k: int(len(v)) for k, v in part.items()}},
        "horario_calculado_con": f"viajes < {cfg.datos_hasta}",
        "metricas": {"ajuste_seleccion": {
            "advertencia": "partición usada para elegir árboles e intervalos: no es una evaluación",
            "wape_total": round(float(curva[n_arboles - 1]), 4), "por_ruta_horizonte": ajuste},
            "desarrollo": desarrollo},
    }
    manifiesto = {**meta, "receta_sha256": huella_receta(raiz), "feature_version": FEATURE_VERSION,
                  "reglas_version": REGLAS_VERSION, "python": platform.python_version(),
                  "sklearn": sklearn.__version__, "numpy": np.__version__, "pandas": pd.__version__}
    return G.Artefacto(m_final, hor, intervalos, meta), manifiesto


def guardar(art: G.Artefacto, manifiesto: dict, carpeta: Path = CARPETA_CANDIDATOS,
            protegidos: tuple[Path, ...] = ()) -> tuple[Path, str]:
    """Guarda en la carpeta de candidatos. Rechaza escribir sobre un artefacto protegido (el activo)
    o sobre un candidato existente con otro contenido."""
    carpeta = Path(carpeta)
    nombre = art.meta["version"]
    destino = (carpeta / f"{nombre}.pkl").resolve()
    for p in protegidos:
        if Path(p).resolve() in (destino, destino.with_suffix(".json")) or \
                Path(p).resolve().parent == destino.parent and Path(p).stem == nombre:
            raise CandidatoInvalido(f"{destino.name} coincide con un artefacto protegido")
    previo = destino.read_bytes() if destino.exists() else None
    ruta, sha = G.guardar(art, carpeta, nombre)
    if previo is not None and hashlib.sha256(previo).hexdigest() != sha:
        ruta.write_bytes(previo)                       # no se pisa un candidato distinto
        raise CandidatoInvalido(f"ya existe {ruta.name} con otro contenido")
    (carpeta / f"{nombre}.manifiesto.json").write_text(
        json.dumps({**manifiesto, "artefacto": ruta.name, "sha256": sha}, indent=2, sort_keys=True,
                   default=str), encoding="utf-8")
    return ruta, sha
