"""Candidato GBM FE 3.1 reproducible (RF-018) y comparación justa con el activo (RF-019).
Datos sintéticos; no lee la base ni el artefacto activo."""
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from app.ml import candidato as C
from app.ml import comparacion_modelos as K
from app.ml import modelo_gbm as G
from tests.unit import sinteticos

INICIO = sinteticos.INICIO                       # 2025-01-06
FIN_DATOS = "2025-03-17"                         # 70 días
CFG = dict(nombre="prueba", entrenamiento_hasta="2025-03-03", datos_hasta=FIN_DATOS)


@pytest.fixture(autouse=True)
def pocos_arboles(monkeypatch):
    monkeypatch.setitem(G.HIPERPARAMETROS, "max_iter", 25)


@pytest.fixture(scope="module")
def crudo():
    return sinteticos.viajes(dias=98)            # hasta 2025-04-13: hay datos posteriores al corte


def _cfg(**kw):
    return C.ConfigCandidato(**{**CFG, "hiperparametros": dict(G.HIPERPARAMETROS), **kw})


def test_reproducible_mismo_sha_y_misma_version(crudo, tmp_path):
    a1, m1 = C.entrenar(crudo, _cfg(), "sint")
    a2, m2 = C.entrenar(crudo.sample(frac=1, random_state=3), _cfg(), "sint")   # otro orden de filas
    r1, s1 = C.guardar(a1, m1, tmp_path / "a")
    r2, s2 = C.guardar(a2, m2, tmp_path / "b")
    assert s1 == s2 and r1.name == r2.name and m1["datos_sha256"] == m2["datos_sha256"]
    assert m1["version"].startswith("cand_prueba_")
    man = json.loads((tmp_path / "a" / f"{a1.meta['version']}.manifiesto.json").read_text())
    assert man["sha256"] == s1 == hashlib.sha256(r1.read_bytes()).hexdigest()
    assert set(man["receta_sha256"]) == set(C.ARCHIVOS_RECETA) and man["semilla"] == G.SEMILLA
    assert man["config"]["datos_hasta"] == FIN_DATOS and man["sklearn"]


def test_no_usa_ningun_dato_posterior_al_corte(crudo):
    _, m = C.entrenar(crudo, _cfg(), "sint")
    corte = pd.Timestamp(FIN_DATOS)
    assert pd.Timestamp(m["entrenado_hasta"]) < corte
    _, solo_antes = C.entrenar(crudo[pd.to_datetime(crudo.fechasalida) < corte], _cfg(), "sint")
    # los viajes posteriores no cambian NADA: mismos datos, misma versión, mismo horario
    assert m["datos_sha256"] == solo_antes["datos_sha256"] and m["version"] == solo_antes["version"]
    otro, _ = C.entrenar(crudo, _cfg(datos_hasta="2025-03-24"), "sint")
    assert otro.meta["version"] != m["version"]


def test_particiones_cronologicas_sin_solape(crudo):
    from app.fe31.viajes import a_formato_fe
    P = a_formato_fe(C.recortar(crudo, FIN_DATOS))
    _, _, part = C.construir(P, _cfg())
    fin = lambda X: X.origen + pd.to_timedelta(X.horizonte_min.astype(int), unit="min")  # noqa: E731
    assert fin(part["E"]).max() <= pd.Timestamp(CFG["entrenamiento_hasta"])
    assert part["A"].origen.min() >= pd.Timestamp(CFG["entrenamiento_hasta"])
    assert fin(part["H"]).max() <= pd.Timestamp(FIN_DATOS)
    assert part["E"].index.intersection(part["A"].index).empty
    with pytest.raises(C.CandidatoInvalido):
        C.construir(a_formato_fe(crudo), _cfg())          # viajes posteriores sin recortar


def test_configuracion_fuera_de_receta_se_rechaza():
    with pytest.raises(C.CandidatoInvalido):
        _cfg(entrenamiento_hasta="2025-03-17").validar()
    with pytest.raises(C.CandidatoInvalido):
        _cfg(semilla=1).validar()
    with pytest.raises(C.CandidatoInvalido):
        _cfg(hiperparametros={**G.HIPERPARAMETROS, "learning_rate": 0.1}).validar()


def test_no_sobrescribe_el_activo_ni_otro_candidato(crudo, tmp_path):
    a, m = C.entrenar(crudo, _cfg(), "sint")
    activo = tmp_path / "modelos" / "gbm_fe31_activo.pkl"
    activo.parent.mkdir()
    activo.write_bytes(b"activo")
    sha_activo = hashlib.sha256(activo.read_bytes()).hexdigest()
    C.guardar(a, m, tmp_path / "modelos" / "candidatos", protegidos=(activo,))
    assert hashlib.sha256(activo.read_bytes()).hexdigest() == sha_activo
    falso = G.Artefacto(a.modelo, a.horario, a.intervalos, {**a.meta, "version": "gbm_fe31_activo"})
    with pytest.raises(C.CandidatoInvalido):
        C.guardar(falso, m, tmp_path / "modelos", protegidos=(activo,))
    assert activo.read_bytes() == b"activo"
    distinto = G.Artefacto(a.modelo, a.horario, a.intervalos.assign(error_superior=0.0), a.meta)
    with pytest.raises(C.CandidatoInvalido):
        C.guardar(distinto, m, tmp_path / "modelos" / "candidatos")


def test_artefacto_candidato_lo_carga_la_api(crudo, tmp_path):
    a, m = C.entrenar(crudo, _cfg(), "sint")
    ruta, sha = C.guardar(a, m, tmp_path)
    G.cargar.cache_clear()
    b = G.cargar(str(ruta), sha)
    assert b.meta["version"] == a.meta["version"] and b.meta["tipo"] == "candidato"


# ------------------------------------------------------------------ comparación

def _artefacto(crudo, hasta, entrenado_hasta):
    a, _ = C.entrenar(crudo, _cfg(datos_hasta=hasta, entrenamiento_hasta="2025-03-03"), "sint")
    return G.Artefacto(a.modelo, a.horario, a.intervalos, {**a.meta, "entrenado_hasta": entrenado_hasta})


def test_periodo_no_aprobado_se_detiene_antes_de_predecir(crudo, monkeypatch):
    llamado = []
    monkeypatch.setattr(K, "predicciones", lambda *a, **k: llamado.append(1))
    monkeypatch.setattr(K, "etiquetas", lambda *a, **k: llamado.append(1))
    arts = {"activo": G.Artefacto(None, None, None, {"entrenado_hasta": "2025-03-16 20:00"}),
            "candidato": G.Artefacto(None, None, None, {"entrenado_hasta": "2025-03-16 20:00"})}
    with pytest.raises(K.PeriodoNoAutorizado):
        K.comparar(arts, crudo, "2025-03-17", "2025-04-07", "activo")       # PERIODOS_APROBADOS vacío
    assert llamado == []


def test_periodo_solapado_con_entrenamiento_es_invalido():
    aprobado = (("2025-03-01", "2025-05-01"),)
    with pytest.raises(K.ComparacionInvalida):
        K.comprobar_periodo("2025-03-10", "2025-04-01", {"activo": "2025-03-16 20:00"}, aprobado)
    with pytest.raises(K.ComparacionInvalida):
        K.comprobar_periodo("2025-03-17", "2025-04-01", {"x": None}, aprobado)
    K.comprobar_periodo("2025-03-17", "2025-04-01", {"activo": "2025-03-16 20:00"}, aprobado)
    with pytest.raises(K.PeriodoNoAutorizado):
        K.comprobar_periodo("2025-03-17", "2025-06-01", {"activo": "2025-03-16 20:00"}, aprobado)


@pytest.fixture(scope="module")
def arts(crudo):
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(G.HIPERPARAMETROS, "max_iter", 25)
        return {"activo": _artefacto(crudo, "2025-03-17", "2025-03-16 20:00"),
                "candidato": _artefacto(crudo, "2025-03-10", "2025-03-09 20:00")}


def test_comparacion_en_las_mismas_ventanas(crudo, arts):
    r = K.comparar(arts, crudo, "2025-03-17", "2025-03-27", "activo",
                   aprobados=(("2025-03-17", "2025-04-14"),))
    assert r["filas_comunes"] > 0 and r["filas_comunes"] <= min(r["filas_por_modelo"].values())
    tot = {f["modelo"]: f for f in r["metricas"] if f["idruta"] == 0}
    assert tot["activo"]["n"] == tot["candidato"]["n"] == r["filas_comunes"]
    grupos = {(f["idruta"], f["horizonte_min"]) for f in r["metricas"] if f["idruta"]}
    assert grupos == {(ru, h) for ru in (2, 16, 21) for h in (30, 60, 120)}
    dif = r["diferencias"]["candidato"]
    assert dif["contra"] == "activo" and dif["dias"] == 10 and dif["grupos_ruta_horizonte"] == 9
    li, ls = dif["ic95_bootstrap_bloques_7d"]
    # tolerancia absoluta: con 10 dias un remuestreo puede ser la muestra completa en otro orden de suma
    assert li - 1e-12 <= dif["dif_mae_diaria_media"] <= ls + 1e-12
    assert r["decision"].startswith("pendiente") and len(r["huella"]) == 64
    # reproducible
    assert K.comparar(arts, crudo, "2025-03-17", "2025-03-27", "activo",
                      aprobados=(("2025-03-17", "2025-04-14"),)) == r


def test_comparacion_no_usa_datos_posteriores_al_periodo(crudo, arts):
    ap = (("2025-03-17", "2025-04-14"),)
    r1 = K.comparar(arts, crudo, "2025-03-17", "2025-03-24", "activo", aprobados=ap)
    corte = pd.Timestamp("2025-03-24")
    r2 = K.comparar(arts, crudo[pd.to_datetime(crudo.fechasalida) < corte], "2025-03-17", "2025-03-24",
                    "activo", aprobados=ap)
    assert r1 == r2


def test_etiquetas_no_convierten_faltantes_en_cero(crudo):
    from app.fe31.viajes import a_formato_fe
    c = crudo.copy()
    sel = pd.to_datetime(c.fechasalida).between("2025-03-18 08:00", "2025-03-18 08:10")
    c.loc[sel, "numeropasajeros"] = np.nan
    Y = K.etiquetas(a_formato_fe(c), "2025-03-17", "2025-03-24")
    v = Y[(Y.idruta == 2) & (Y.franja_inicio == pd.Timestamp("2025-03-18 08:00")) & (Y.horizonte_min == 60)]
    assert v.estado_etiqueta.iloc[0] == "bloqueada_conteo" and np.isnan(v.demanda_real.iloc[0])


def test_datos_mas_alla_de_lo_autorizado_se_rechazan():
    with pytest.raises(C.CandidatoInvalido, match="B1"):
        _cfg(datos_hasta="2025-07-02").validar()
    _cfg(entrenamiento_hasta="2025-01-01", ajuste_hasta="2025-04-01", datos_hasta="2025-07-01").validar()


def test_particion_de_desarrollo_opcional(crudo):
    _, m = C.entrenar(crudo, _cfg(entrenamiento_hasta="2025-02-24", ajuste_hasta="2025-03-06"), "sint")
    d = m["metricas"]["desarrollo"]
    assert d is not None and d["n"] > 0 and "no compara con el activo" in d["advertencia"]
    assert m["particiones"]["filas"]["V"] > 0


# ------------------------------------------------------------------ scripts

def test_script_entrenar_por_csv_protege_el_activo(crudo, tmp_path):
    from scripts import entrenar_candidato as S
    csv = tmp_path / "viajes.csv"
    crudo.to_csv(csv, index=False)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps(CFG))
    modelos = tmp_path / "modelos"
    modelos.mkdir()
    (modelos / "gbm_activo.pkl").write_bytes(b"x")
    (modelos / "gbm_activo.json").write_text("{}")
    S.main(["--config", str(cfg), "--csv", str(csv), "--salida", str(modelos / "candidatos"),
            "--modelos", str(modelos)])
    assert (modelos / "gbm_activo.pkl").read_bytes() == b"x"
    hechos = sorted(p.name for p in (modelos / "candidatos").iterdir())
    assert len(hechos) == 3 and all(n.startswith("cand_prueba_") for n in hechos)


def test_script_comparar_se_detiene_sin_leer_datos(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts import comparar_candidato as S
    activo = tmp_path / "modelo_sintetico.json"
    monkeypatch.setattr(S, "cargar", lambda _: SimpleNamespace(meta={"entrenado_hasta": "2025-06-30"}))
    leidos = []
    monkeypatch.setattr(S, "viajes_local", lambda *a: leidos.append(a))
    with pytest.raises(K.PeriodoNoAutorizado):
        S.main(["--candidato", str(activo), "--activo", str(activo), "--desde", "2025-07-01",
                "--hasta", "2025-10-01", "--fuente", "local", "--salida", str(tmp_path)])
    with pytest.raises(K.ComparacionInvalida):                # el activo se entrenó hasta 2025-06-30
        S.main(["--candidato", str(activo), "--activo", str(activo), "--desde", "2025-04-01",
                "--hasta", "2025-07-01", "--fuente", "local", "--salida", str(tmp_path)])
    assert leidos == []


def test_huella_de_receta_no_depende_del_fin_de_linea(tmp_path):
    for f in C.ARCHIVOS_RECETA:
        (tmp_path / "lf" / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "crlf" / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "lf" / f).write_bytes(b"a = 1\nb = 2\n")
        (tmp_path / "crlf" / f).write_bytes(b"a = 1\r\nb = 2\r\n")
    assert C.huella_receta(tmp_path / "lf") == C.huella_receta(tmp_path / "crlf")
