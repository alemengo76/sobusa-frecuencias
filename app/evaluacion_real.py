"""M1: servicio de comparación predicción-real (solo lectura).

evaluar(c, ...) lee las predicciones guardadas, la demanda real consolidada y la marca de agua,
empareja con app.ml.comparacion_real y devuelve pares + métricas por modelo. No escribe nada.
"""
from datetime import datetime

import pandas as pd

from app.core.config import obtener_config
from app.core.errores import NoEncontrado, SolicitudInvalida
from app.db import lectura_real as lr
from app.db import repositorio as repo
from app.ml import comparacion_real as cr
from app.ml import modelo_gbm


def _iso(x):
    if x is None or (not isinstance(x, str) and pd.isna(x)):
        return None
    return pd.Timestamp(x).isoformat()


def _horario_de(m: dict) -> tuple[pd.DataFrame, dict]:
    """Horario de operación guardado en el artefacto GBM del modelo m (hash verificado; solo
    lectura). Lanza ModeloNoDisponible si no hay artefacto utilizable."""
    if (m.get("hiperparametros") or {}).get("metodo") != modelo_gbm.METODO or not m.get("artefacto_ruta"):
        raise modelo_gbm.ModeloNoDisponible("el modelo no tiene artefacto GBM")
    art = modelo_gbm.cargar(modelo_gbm.ruta_artefacto(m["artefacto_ruta"], obtener_config().modelos_dir),
                            m.get("artefacto_sha256") or "")
    return art.horario, {"version": m.get("version"), "artefacto_sha256": m.get("artefacto_sha256")}


def horarios_por_modelo(c, model_ids) -> tuple[pd.DataFrame | None, dict, list]:
    """Horario de operación para decidir el 0 operativo, por modelo evaluado:
    1) el de su propio artefacto GBM; 2) si no tiene (línea base), el del GBM activo.
    El horario es una propiedad de la ruta (comun-1.5), no del modelo: usar el del GBM activo para
    la línea base aplica la misma definición de ventana operativa que la etiqueta del FE."""
    registro = {int(m["model_id"]): m for m in repo.listar_modelos(c)}
    activo = next((m for m in registro.values() if m.get("estado") == "activo"), None)
    partes, fuente, avisos = [], {}, []
    for mid in sorted({int(x) for x in model_ids}):
        for origen, m in (("propio", registro.get(mid)), ("modelo_activo", activo)):
            if m is None:
                continue
            try:
                h, info = _horario_de(m)
            except modelo_gbm.ModeloNoDisponible:
                continue
            partes.append(h.assign(model_id=mid))
            fuente[mid] = {"origen": origen, **info}
            break
        else:
            avisos.append(f"modelo {mid}: sin horario de operación; las ventanas sin salidas quedan "
                          "sin_correspondencia (no se puede decidir si son 0)")
    return (pd.concat(partes, ignore_index=True) if partes else None), fuente, avisos


def evaluar(c, model_id: int | None = None, execution_id: str | None = None,
            idruta: int | None = None, horizonte_min: int | None = None,
            desde: datetime | None = None, hasta: datetime | None = None,
            execution_ids: list[str] | None = None) -> dict:
    """Devuelve {'pares': DataFrame, 'parametros': dict, 'huella': str, 'modelos': dict}."""
    if desde is not None and hasta is not None and desde >= hasta:
        raise SolicitudInvalida("desde debe ser anterior a hasta")
    eid = None
    if execution_id is not None:
        eid = repo.ejecucion_duena(c, str(execution_id))
        if eid is None:
            raise NoEncontrado("Ejecución", execution_id)

    eids = None
    if execution_ids is not None:
        # cada ejecución que reutilizó predicciones se resuelve a la dueña de las filas (como en P1)
        eids = sorted({d for d in (repo.ejecucion_duena(c, str(e)) for e in execution_ids) if d})
    filtros = dict(model_id=model_id, execution_id=eid, idruta=idruta,
                   horizonte_min=horizonte_min, desde=desde, hasta=hasta)
    if eids is not None:
        filtros["execution_ids"] = eids
    n = lr.contar_predicciones(c, **filtros)
    if n > lr.LIMITE_PREDICCIONES:
        raise SolicitudInvalida(
            f"La consulta abarca {n} predicciones (máximo {lr.LIMITE_PREDICCIONES}); "
            "acote por modelo, ejecución, ruta, horizonte o periodo")

    margen = obtener_config().dias_revision
    wm = lr.marca_agua(c)
    marca = None if wm is None else wm["ultima_actualizacion"]
    parametros = {
        "reglas": cr.VERSION_REGLAS,
        "filtros": {"model_id": model_id, "execution_id": execution_id,
                    "execution_id_datos": eid, "execution_ids_datos": eids,
                    "idruta": idruta, "horizonte_min": horizonte_min,
                    "desde": _iso(desde), "hasta": _iso(hasta)},
        "marca_agua": _iso(marca),
        "lote_marca_agua": None if wm is None else wm.get("lote"),
        "margen_consolidacion_dias": margen,
        "corte_consolidacion": _iso(cr.corte_consolidacion(marca, margen)),
        "fuente_real": "ai_frecuencias.demanda_franja (comun-1.5, salida programada)",
    }

    pred = lr.predicciones(c, **filtros) if n else pd.DataFrame(columns=cr.COLUMNAS_PREDICCION)
    modelos = {}
    if not pred.empty:
        p = cr._normalizar(pred)
        ini = p.franja_inicio.min() - pd.Timedelta(days=cr.DIAS_INGENUO)
        fin = p.franja_inicio.max() + pd.Timedelta(minutes=max(cr.HORIZONTES))
        franjas = lr.franjas_reales(c, sorted(p.idruta.dropna().astype(int).unique()),
                                    ini.to_pydatetime(), fin.to_pydatetime())
        for _, f in pred.drop_duplicates("model_id").iterrows():
            modelos[int(f.model_id)] = {"nombre": f.get("modelo_nombre"),
                                        "version_registro": f.get("registro_version"),
                                        "estado": f.get("modelo_estado")}
        horario, parametros["horario_operacion"], avisos = horarios_por_modelo(c, modelos)
        if avisos:
            parametros["advertencias"] = avisos
        pares = cr.emparejar(pred, franjas, marca, margen, horario)
    else:
        pares = cr.emparejar(pred, pd.DataFrame(), marca, margen)
    return {"pares": pares, "parametros": parametros, "modelos": modelos,
            "huella": cr.huella(pares, {k: v for k, v in parametros.items() if k != "filtros"})}


def resumen_metricas(r: dict, agrupar_por=("idruta", "horizonte_min")) -> dict:
    pares = r["pares"]
    return {
        "parametros": {**r["parametros"], "agrupar_por": list(agrupar_por)},
        "huella": r["huella"],
        "modelos": r["modelos"],
        "conteo_estados": cr.conteo_estados(pares),
        "conteo_motivos": cr.conteo_motivos(pares),
        "total": cr.agregar(pares, ()) if not pares.empty else [],
        "datos": cr.agregar(pares, agrupar_por),
    }


COLUMNAS_PAR = ["prediction_id", "execution_id", "model_id", "model_version", "feature_version",
                "idruta", "franja_inicio", "fin_ventana", "horizonte_min", "data_cutoff",
                "generated_at", "demanda_pred", "limite_inferior", "limite_superior",
                "demanda_real", "error", "dentro_intervalo", "demanda_ingenuo_7d",
                "programaciones_ventana", "estado", "motivo"]


def pares_json(pares: pd.DataFrame, estado: str | None, pagina: int, tamano: int) -> tuple[list, int]:
    d = pares if estado is None else pares[pares.estado.eq(estado)]
    total = len(d)
    d = d.iloc[(pagina - 1) * tamano: pagina * tamano]
    filas = []
    for r in d[COLUMNAS_PAR].astype(object).to_dict("records"):
        out = {}
        for k, v in r.items():
            if k in ("franja_inicio", "fin_ventana", "data_cutoff", "generated_at"):
                out[k] = _iso(v)
            elif k in ("execution_id", "model_version", "feature_version", "estado", "motivo"):
                out[k] = v
            elif k == "dentro_intervalo":
                out[k] = None if v is None or pd.isna(v) else bool(v)
            else:
                out[k] = cr._num(v)
        out["detalle_motivo"] = cr.MOTIVOS.get(out["motivo"], "")
        filas.append(out)
    return filas, total
