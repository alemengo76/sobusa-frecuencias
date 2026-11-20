"""ALT-010 «Modelo degradado» a partir de M1 (predicción contra real). Regla `alt010-v1`.

Contrato aprobado el 20-nov-2026 (docs del proyecto: propuesta técnica ALT-010):
- Modelo monitoreado: el ACTIVO, con su model_id y su model_version. Otras versiones del mismo
  model_id y otros modelos (respaldo) se excluyen y se cuentan en la evidencia.
- Horizonte 60 min, evaluación independiente por ruta.
- Ventanas por franja_inicio, contiguas y sin solapamiento, ancladas en C = corte de consolidación de
  M1 (marca de agua − DIAS_REVISION) a medianoche:
      reciente   = [C − 7 d, C)      referencia = [C − 35 d, C − 7 d)
- Métrica: WAPE de M1 (`comparacion_real.agregar`) sobre pares en estado `valido`.
- Mínimos por ruta: ≥ 30 observaciones válidas en cada ventana y ≥ 4 días (reciente) / ≥ 14 días
  (referencia) con observaciones válidas. Si no se cumplen, o el WAPE de referencia es 0 o no está
  definido, NO se evalúa ni se genera alerta (estado «sin_evidencia_suficiente» o «wape_referencia_cero»).
- Disparo (`motor.alt_010`, sin cambios en la condición): WAPE_rec > WAPE_ref × (1 + umbral vigente),
  estricto. Umbral: `ai_frecuencias.umbral` (degradacion_wape), con su versión en la evidencia.
- Modo real (B1 v2.2): se excluyen las franjas anteriores al 2026-01-01 (oct-dic 2025 no evalúa el
  modelo operativo). Hoy no hay 35 días de predicciones guardadas en 2026: la regla queda silenciosa y
  se demuestra con datos sintéticos.

No reimplementa métricas: usa `agregar` y `para_alt_010` de M1. Solo simulación: no escribe ni notifica.
"""
import pandas as pd

from app.alertas import motor
from app.ml import comparacion_real as cr

REGLA_ALT010 = "alt010-v1"
HORIZONTE_ALT010 = 60
DIAS_RECIENTE = 7
DIAS_REFERENCIA = 28
MIN_OBSERVACIONES = 30
MIN_DIAS_RECIENTE = 4
MIN_DIAS_REFERENCIA = 14
INICIO_MODO_REAL = pd.Timestamp("2026-01-01")      # B1 v2.2: oct-dic 2025 no evalúa el modelo operativo
MODOS = ("simulacion",)


def ventanas(marca_agua, dias_revision: int) -> dict | None:
    """{'reciente': (desde, hasta), 'referencia': (desde, hasta), 'corte': C} o None sin marca."""
    corte = cr.corte_consolidacion(marca_agua, dias_revision)
    if corte is None:
        return None
    c = pd.Timestamp(corte).normalize()
    r0 = c - pd.Timedelta(days=DIAS_RECIENTE)
    return {"corte": c, "reciente": (r0, c), "referencia": (r0 - pd.Timedelta(days=DIAS_REFERENCIA), r0)}


def _por_ruta(filas: list[dict]) -> dict:
    return {int(f["idruta"]): f for f in filas}


def _dias_con_datos(pares_ventana: pd.DataFrame) -> dict:
    """Días con al menos una observación válida, por ruta, contados con agregar(..., 'dia') de M1."""
    dias = {}
    for f in cr.agregar(pares_ventana, ("idruta", "horizonte_min", "dia")):
        if (f.get("n_validas") or 0) > 0:
            dias[int(f["idruta"])] = dias.get(int(f["idruta"]), 0) + 1
    return dias


def evaluar(pares: pd.DataFrame, modelo: dict, marca_agua, dias_revision: int, u: dict,
            versiones: dict | None = None, excluir_antes=INICIO_MODO_REAL) -> dict:
    """Pura y determinista. `pares`: salida de M1 (`emparejar`). `modelo`: {'model_id', 'version'}.
    Devuelve las alertas candidatas (antes de deduplicar) y la evaluación de cada ruta."""
    versiones = versiones or {}
    umbral = float(u["ALT-010"])
    v = ventanas(marca_agua, dias_revision)
    base = {"regla": REGLA_ALT010, "modelo": {"model_id": int(modelo["model_id"]),
                                              "model_version": modelo["version"]},
            "horizonte_min": HORIZONTE_ALT010, "umbral": umbral, "umbral_version": versiones.get("ALT-010"),
            "minimos": {"observaciones": MIN_OBSERVACIONES, "dias_reciente": MIN_DIAS_RECIENTE,
                        "dias_referencia": MIN_DIAS_REFERENCIA},
            "excluir_antes": None if excluir_antes is None else str(pd.Timestamp(excluir_antes)),
            "marca_agua": None if marca_agua is None else str(pd.Timestamp(marca_agua))}
    if v is None:
        return {**base, "ventanas": None, "excluidos": {}, "rutas": [], "alertas": [],
                "motivo": "sin marca de agua: no hay real consolidado"}
    base["ventanas"] = {k: [str(a), str(b)] for k, (a, b) in ((k, v[k]) for k in ("reciente", "referencia"))}
    base["corte_consolidacion"] = str(v["corte"])

    p = pares.copy()
    if not p.empty:
        p["franja_inicio"] = pd.to_datetime(p.franja_inicio)
    excl = {"otro_modelo": 0, "otra_version": 0, "otro_horizonte": 0, "antes_de_inicio": 0}
    if not p.empty:
        otro = p.model_id.astype(int) != int(modelo["model_id"])
        excl["otro_modelo"] = int(otro.sum())
        p = p[~otro]
        otra = p.model_version.astype(str) != str(modelo["version"])
        excl["otra_version"] = int(otra.sum())
        p = p[~otra]
        oh = p.horizonte_min.astype(int) != HORIZONTE_ALT010
        excl["otro_horizonte"] = int(oh.sum())
        p = p[~oh]
        if excluir_antes is not None:
            antes = p.franja_inicio < pd.Timestamp(excluir_antes)
            excl["antes_de_inicio"] = int(antes.sum())
            p = p[~antes]

    def en(ventana):
        a, b = v[ventana]
        return p[(p.franja_inicio >= a) & (p.franja_inicio < b)] if not p.empty else p

    rec, ref = en("reciente"), en("referencia")
    ar, af = _por_ruta(cr.agregar(rec)), _por_ruta(cr.agregar(ref))
    dr, df = _dias_con_datos(rec), _dias_con_datos(ref)

    rutas, suficientes = [], []
    for ruta in sorted(set(ar) | set(af)):
        fr, ff = ar.get(ruta, {}), af.get(ruta, {})
        fila = {"idruta": ruta,
                "n_validas_reciente": int(fr.get("n_validas") or 0),
                "n_validas_referencia": int(ff.get("n_validas") or 0),
                "dias_reciente": dr.get(ruta, 0), "dias_referencia": df.get(ruta, 0),
                "wape_reciente": fr.get("wape"), "wape_referencia": ff.get("wape")}
        if (fila["n_validas_reciente"] < MIN_OBSERVACIONES or fila["n_validas_referencia"] < MIN_OBSERVACIONES
                or fila["dias_reciente"] < MIN_DIAS_RECIENTE or fila["dias_referencia"] < MIN_DIAS_REFERENCIA
                or fila["wape_reciente"] is None):
            fila["estado"] = "sin_evidencia_suficiente"
        elif fila["wape_referencia"] is None or fila["wape_referencia"] <= 0:
            fila["estado"] = "wape_referencia_cero"
        else:
            fila["estado"] = "evaluada"
            suficientes.append(ruta)
        rutas.append(fila)

    entrada = cr.para_alt_010([ar[r] for r in suficientes], [af[r] for r in suficientes])
    if not entrada.empty:
        evid = pd.DataFrame([f for f in rutas if f["idruta"] in suficientes])[
            ["idruta", "n_validas_reciente", "n_validas_referencia", "dias_reciente", "dias_referencia"]]
        entrada = entrada.merge(evid, on="idruta").assign(
            model_id=int(modelo["model_id"]), model_version=str(modelo["version"]),
            reciente_desde=v["reciente"][0], reciente_hasta=v["reciente"][1],
            referencia_desde=v["referencia"][0], referencia_hasta=v["referencia"][1],
            umbral_version=versiones.get("ALT-010"), marca_agua=pd.Timestamp(marca_agua),
            corte=v["corte"], regla=REGLA_ALT010)
    alertas = motor.alt_010(entrada, u) if not entrada.empty else []
    for f in rutas:
        f["alerta"] = any(a["idruta"] == f["idruta"] for a in alertas)
        if f["estado"] == "evaluada" and f["wape_referencia"]:
            f["degradacion"] = f["wape_reciente"] / f["wape_referencia"] - 1
    for a in alertas:
        a["umbral_version"] = versiones.get("ALT-010")
    return {**base, "excluidos": excl, "rutas": rutas, "alertas": alertas}


def simular(c, modo: str = "simulacion") -> dict:
    """Lectura de solo lectura de nuestra base (M1) y evaluación. No inserta ni notifica."""
    from app import evaluacion_real
    from app.core.config import obtener_config
    from app.core.errores import Conflicto
    from app.db import lectura_real as lr
    from app.db import repositorio as repo
    if modo not in MODOS:
        raise Conflicto(f"Modo '{modo}' no autorizado: ALT-010 solo corre en simulación")
    activo = repo.modelo_activo(c)
    if activo is None:
        raise Conflicto("No hay modelo activo que monitorear")
    margen = obtener_config().dias_revision
    wm = lr.marca_agua(c)
    marca = None if wm is None else wm["ultima_actualizacion"]
    umbrales, versiones = repo.umbrales_vigentes(c)
    u = {**motor.umbrales_por_defecto(), **umbrales}
    v = ventanas(marca, margen)
    if v is None:
        r = evaluar(pd.DataFrame(), activo, None, margen, u, versiones)
    else:
        desde = max(v["referencia"][0], INICIO_MODO_REAL)
        if desde >= v["reciente"][1]:
            r = evaluar(pd.DataFrame(), activo, marca, margen, u, versiones)
        else:
            m1 = evaluacion_real.evaluar(c, model_id=int(activo["model_id"]), horizonte_min=HORIZONTE_ALT010,
                                         desde=desde.to_pydatetime(), hasta=v["reciente"][1].to_pydatetime())
            r = evaluar(m1["pares"], activo, marca, margen, u, versiones)
            r["huella_m1"] = m1["huella"]
    nuevas = motor.deduplicar(r["alertas"], repo.claves_alertas_abiertas(c))
    return {**r, "simulacion": True, "escrito": False, "notificado": False, "modo": modo,
            "candidatas": len(r["alertas"]), "nuevas_tras_deduplicar": len(nuevas), "alertas": nuevas}
