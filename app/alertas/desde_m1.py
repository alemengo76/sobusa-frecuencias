"""ALT-005 y ALT-007 a partir de M1 (predicción contra real), evaluadas DESPUÉS de la ingesta.

Decisiones aprobadas el 19-nov-2026:
1. Se monitorean solo las predicciones de las ejecuciones en que el modelo evaluado era el ACTIVO.
   Cada fila conserva su model_id original: las de respaldo (línea base) se evalúan y se rotulan
   como respaldo, sin mezclarse con las del GBM.
2. Horizonte inicial: 60 minutos.
3. Solo real consolidado: corte = marca de agua − DIAS_REVISION (3 días), igual que M1.
4. Se evalúa después de la ingesta (ver `evaluar_tras_ingesta`; contrato con G4 en
   docs/M1_PREDICCION_VS_REAL.md §9), no al predecir.
5. Trazabilidad sin migración: model_id, prediction_id, ejecución de la predicción, versión de
   umbral y huella de M1 van en `explicacion` (texto) y en `trazabilidad` (estructurado).
   `alert.execution_id` queda para la ejecución de alertas, y `alert.umbral_version` para la versión.
6. ALT-005 y ALT-007 usan la regla y los umbrales actuales (`motor.alt_005_007`, sin cambios).
7. ALT-007 por intervalo (especificación §5.2) es solo una PROPUESTA: se calcula aparte
   (`propuesta_alt_007_intervalo`) y nunca genera alertas.

Estado: SOLO SIMULACIÓN. Nada se inserta ni se notifica. El modo real no está autorizado.
"""
from datetime import datetime

import pandas as pd

from app import evaluacion_real
from app.alertas import motor
from app.core.config import obtener_config
from app.core.errores import Conflicto, SolicitudInvalida
from app.db import lectura_real as lr
from app.db import repositorio as repo
from app.ml import comparacion_real as cr

HORIZONTES = (30, 60, 120)
HORIZONTES_MONITOREO = (60,)
CODIGOS = ("ALT-005", "ALT-007")
MODOS = ("simulacion",)          # "real" requiere aprobación: insertar en alert + execution_log 'alertas'


# ------------------------------------------------------------------ piezas puras

def ventana_tras_ingesta(marca_anterior, marca_nueva, margen_dias: int) -> tuple:
    """Ventanas que pasan a ser consolidadas con esta ingesta: fin_ventana en (corte_anterior,
    corte_nuevo], con corte = marca − margen. Sin marca anterior (primera ingesta) no hay límite
    inferior. Si la marca no avanzó, el intervalo es vacío."""
    nuevo = cr.corte_consolidacion(marca_nueva, margen_dias)
    anterior = cr.corte_consolidacion(marca_anterior, margen_dias)
    return anterior, nuevo


def _trazabilidad(par, origen: str, umbral_version, huella_m1: str) -> dict:
    return {"model_id": int(par.model_id), "model_version": par.model_version,
            "origen_modelo": origen, "prediction_id": int(par.prediction_id),
            "execution_id_prediccion": str(par.execution_id), "umbral_version": umbral_version,
            "reglas_m1": cr.VERSION_REGLAS, "huella_m1": huella_m1}


def _texto_trazabilidad(t: dict) -> str:
    return (f" [Trazabilidad: modelo {t['model_id']} ({t['model_version']}, {t['origen_modelo']}), "
            f"predicción {t['prediction_id']}, ejecución {t['execution_id_prediccion']}, "
            f"umbral v{t['umbral_version']}, M1 {t['reglas_m1']} {t['huella_m1'][:12]}]")


def alertas_de_pares(pares: pd.DataFrame, u: dict, versiones: dict, huella_m1: str,
                     modelo_activo: int, horizontes=HORIZONTES_MONITOREO) -> list[dict]:
    """Aplica motor.alt_005_007 SIN CAMBIOS a los pares válidos de M1, un model_id por vez, y añade
    la trazabilidad. Devuelve las candidatas (antes de deduplicar)."""
    v = pares[pares.estado.eq("valido") & pares.horizonte_min.isin(horizontes)]
    salida = []
    for mid, g in v.groupby("model_id", sort=True):
        obs = cr.para_alt_005_007(g)
        por_clave = {(int(o.idruta), pd.Timestamp(o.franja_inicio), int(o.horizonte_min)): o
                     for o in g.itertuples(index=False)}
        origen = "modelo_activo" if int(mid) == int(modelo_activo) else "respaldo"
        for a in motor.alt_005_007(obs, u):
            par = por_clave[(a["idruta"], a["franja_inicio"], a["horizonte_min"])]
            t = _trazabilidad(par, origen, versiones.get(a["codigo"]), huella_m1)
            a["umbral_version"] = versiones.get(a["codigo"])
            a["trazabilidad"] = t
            a["explicacion"] = a["explicacion"] + _texto_trazabilidad(t)
            salida.append(a)
    return salida


def propuesta_alt_007_intervalo(pares: pd.DataFrame, horizontes=HORIZONTES_MONITOREO,
                                u: dict | None = None) -> dict:
    """PROPUESTA (no activa) para ALT-007 según la especificación §5.2: «la demanda real supera el
    intervalo estimado por el modelo». Variantes:
      A) real > limite_superior            (lectura literal: solo por encima)
      B) real fuera de [inferior, superior] (ambos lados; por debajo se solapa con ALT-005)
    Puntuación: exceso sobre el límite / semiancho del intervalo (0 = justo en el borde)."""
    v = pares[pares.estado.eq("valido") & pares.horizonte_min.isin(horizontes)].copy()
    if v.empty:
        return {"activa": False, "filas_validas": 0, "variante_A_por_encima": 0,
                "variante_B_fuera_del_intervalo": 0, "por_debajo": 0, "actual_35pct": 0,
                "coinciden_A_y_actual": 0, "solo_actual": 0, "solo_A": 0, "ejemplos_A": []}
    semi_sup = (v.limite_superior - v.demanda_pred).clip(lower=1.0)
    semi_inf = (v.demanda_pred - v.limite_inferior).clip(lower=1.0)
    encima = v.demanda_real > v.limite_superior
    debajo = v.demanda_real < v.limite_inferior
    rel = (v.demanda_real - v.demanda_pred) / v.demanda_pred.clip(lower=1.0)
    u = {**motor.umbrales_por_defecto(), **(u or {})}
    actual_007 = (rel.abs() > u["ALT-007"]) & ~(rel <= -u["ALT-005"])   # regla actual, para comparar
    v["puntuacion_A"] = ((v.demanda_real - v.limite_superior) / semi_sup).where(encima)
    v["puntuacion_debajo"] = ((v.limite_inferior - v.demanda_real) / semi_inf).where(debajo)
    ej = v[encima].sort_values("puntuacion_A", ascending=False).head(5)
    return {
        "activa": False,
        "filas_validas": int(len(v)),
        "variante_A_por_encima": int(encima.sum()),
        "variante_B_fuera_del_intervalo": int((encima | debajo).sum()),
        "por_debajo": int(debajo.sum()),
        "actual_35pct": int(actual_007.sum()),
        "coinciden_A_y_actual": int((encima & actual_007).sum()),
        "solo_actual": int((actual_007 & ~encima).sum()),
        "solo_A": int((encima & ~actual_007).sum()),
        "ejemplos_A": [{"prediction_id": int(r.prediction_id), "idruta": int(r.idruta),
                        "franja_inicio": pd.Timestamp(r.franja_inicio).isoformat(),
                        "demanda_pred": float(r.demanda_pred), "limite_superior": float(r.limite_superior),
                        "demanda_real": float(r.demanda_real), "puntuacion": round(float(r.puntuacion_A), 3)}
                       for r in ej.itertuples(index=False)],
    }


# ------------------------------------------------------------------ servicio (solo lectura)

def _comprobar(modo: str, horizontes) -> None:
    if modo not in MODOS:
        raise Conflicto(f"Modo '{modo}' no autorizado: ALT-005/007 desde M1 solo corre en simulación")
    if not set(horizontes) <= set(HORIZONTES):
        raise SolicitudInvalida("horizontes debe ser un subconjunto de (30, 60, 120)")


def _simular(c, modelo_activo: dict, desde, hasta, filtro_fin, horizontes, contexto: dict) -> dict:
    activo = int(modelo_activo["model_id"])
    ejecuciones = lr.ejecuciones_prediccion_de_modelo(c, activo)
    base = {"simulacion": True, "escrito": False, "notificado": False, "modelo_activo": activo,
            "horizontes": list(horizontes), **contexto,
            "ejecuciones_monitoreadas": len(ejecuciones),
            "ejecuciones_con_respaldo": sum(bool((e["resumen"] or {}).get("respaldo"))
                                            for e in ejecuciones)}
    sin_pares = cr.emparejar(pd.DataFrame(columns=cr.COLUMNAS_PREDICCION), pd.DataFrame(), None, 0)
    vacio = {"conteo_estados_m1": cr.conteo_estados(sin_pares), "observaciones_validas": 0,
             "candidatas": 0, "por_codigo": dict.fromkeys(CODIGOS, 0), "por_origen": {},
             "nuevas_tras_deduplicar": 0, "alertas": [],
             "propuesta_alt_007": propuesta_alt_007_intervalo(sin_pares)}
    if not ejecuciones:
        return {**base, **vacio, "motivo_vacio": "no hay ejecuciones de predicción del modelo activo"}
    if desde is not None and hasta is not None and desde >= hasta:
        return {**base, **vacio, "motivo_vacio": "no hay ventanas nuevas consolidadas"}

    r = evaluacion_real.evaluar(c, desde=desde, hasta=hasta,
                                execution_ids=[e["execution_id"] for e in ejecuciones])
    pares = r["pares"]
    if not pares.empty and filtro_fin is not None:
        pares = pares[filtro_fin(pares.fin_ventana)]
    umbrales, versiones = repo.umbrales_vigentes(c)
    u = {**motor.umbrales_por_defecto(), **umbrales}
    candidatas = (alertas_de_pares(pares, u, versiones, r["huella"], activo, horizontes)
                  if not pares.empty else [])
    nuevas = motor.deduplicar(candidatas, repo.claves_alertas_abiertas(c))
    validas = (pares[pares.estado.eq("valido") & pares.horizonte_min.isin(horizontes)]
               if not pares.empty else pares)
    return {
        **base,
        "parametros_m1": r["parametros"], "huella_m1": r["huella"],
        "conteo_estados_m1": cr.conteo_estados(pares),
        "observaciones_validas": int(len(validas)),
        "umbrales": {k: u[k] for k in CODIGOS},
        "umbral_version": {k: versiones.get(k) for k in CODIGOS},
        "candidatas": len(candidatas),
        "por_codigo": {k: sum(a["codigo"] == k for a in candidatas) for k in CODIGOS},
        "por_origen": {o: sum(a["trazabilidad"]["origen_modelo"] == o for a in candidatas)
                       for o in ("modelo_activo", "respaldo")},
        "nuevas_tras_deduplicar": len(nuevas),
        "alertas": nuevas,
        "propuesta_alt_007": propuesta_alt_007_intervalo(pares, horizontes, u),
    }


def evaluar_tras_ingesta(c, marca_anterior, marca_nueva, modo: str = "simulacion",
                         horizontes=HORIZONTES_MONITOREO) -> dict:
    """Punto de entrada para G4, después de `ingesta.correr()`. Evalúa solo las ventanas que esta
    ingesta volvió consolidadas: fin_ventana en (marca_anterior − 3 d, marca_nueva − 3 d].
    Idempotente: con las mismas marcas y datos devuelve lo mismo (misma huella de M1), y la
    deduplicación contra alertas abiertas impide repetir una alerta. Solo lectura."""
    _comprobar(modo, horizontes)
    activo = repo.modelo_activo(c)
    if activo is None:
        raise Conflicto("No hay modelo activo que monitorear")
    margen = obtener_config().dias_revision
    wm = lr.marca_agua(c)
    vigente = None if wm is None else wm["ultima_actualizacion"]
    if marca_nueva is None or vigente is None or pd.Timestamp(marca_nueva) != pd.Timestamp(vigente):
        raise Conflicto("La marca de agua recibida no es la vigente: evalúe con la marca que dejó la "
                        f"ingesta (recibida {marca_nueva}, vigente {vigente})")
    corte_ant, corte_nuevo = ventana_tras_ingesta(marca_anterior, marca_nueva, margen)
    contexto = {"modo": modo,
                "marca_anterior": (None if marca_anterior is None
                                   else pd.Timestamp(marca_anterior).isoformat()),
                "marca_nueva": pd.Timestamp(marca_nueva).isoformat(),
                "corte_anterior": None if corte_ant is None else corte_ant.isoformat(),
                "corte_nuevo": corte_nuevo.isoformat()}
    if corte_ant is not None and corte_ant >= corte_nuevo:
        return _simular(c, activo, corte_nuevo, corte_nuevo, None, horizontes, contexto)
    desde = None if corte_ant is None else (corte_ant - pd.Timedelta(minutes=max(HORIZONTES))).to_pydatetime()
    hasta = corte_nuevo.to_pydatetime()

    def filtro(fin):
        sel = fin <= corte_nuevo
        return sel if corte_ant is None else sel & (fin > corte_ant)
    return _simular(c, activo, desde, hasta, filtro, horizontes, contexto)


def simular_alt_005_007(c, desde: datetime | None = None, hasta: datetime | None = None,
                        horizontes=HORIZONTES_MONITOREO, modo: str = "simulacion") -> dict:
    """Simulación sobre un rango de franja_inicio [desde, hasta) del modelo activo (para revisar a
    mano o desde el tablero). Mismas reglas que `evaluar_tras_ingesta`."""
    _comprobar(modo, horizontes)
    activo = repo.modelo_activo(c)
    if activo is None:
        raise SolicitudInvalida("No hay modelo activo; no hay nada que monitorear")
    return _simular(c, activo, desde, hasta, None, horizontes, {"modo": modo})
