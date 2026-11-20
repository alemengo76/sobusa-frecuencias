"""Candidato desde viaje_fuente en PostgreSQL real: el corte se aplica en la consulta (nada posterior
se lee), la lectura es READ ONLY y el resultado es igual al de los mismos viajes en memoria.
Solo en base desechable: siembra viajes sintéticos con un lote propio y los borra al terminar."""
import importlib.util
import json
import os

import pandas as pd
import pytest

if importlib.util.find_spec("sqlalchemy") is None or not os.environ.get("DATABASE_URL_WRITE"):
    pytest.skip("requiere sqlalchemy y DATABASE_URL_WRITE", allow_module_level=True)
if os.environ.get("SOBUSA_BD_DESECHABLE") != "1":
    pytest.skip("requiere una base desechable (SOBUSA_BD_DESECHABLE=1)", allow_module_level=True)

from sqlalchemy import text  # noqa: E402

from app.db.conexion import motor_escritura  # noqa: E402
from app.ml import candidato as C  # noqa: E402
from app.ml import modelo_gbm as G  # noqa: E402
from scripts import entrenar_candidato as S  # noqa: E402
from tests.unit import sinteticos  # noqa: E402

LOTE = "prueba_candidato"
CFG = {"nombre": "pg", "entrenamiento_hasta": "2025-03-03", "datos_hasta": "2025-03-17"}


@pytest.fixture()
def viajes_sembrados(monkeypatch):
    monkeypatch.setitem(G.HIPERPARAMETROS, "max_iter", 25)
    crudo = sinteticos.viajes(dias=98)
    with motor_escritura().begin() as c:
        if not c.execute(text("SELECT current_database()")).scalar().startswith("sobusa_desechable"):
            pytest.skip("la base no es desechable")
        if c.execute(text("SELECT count(*) FROM ai_frecuencias.viaje_fuente")).scalar():
            pytest.skip("viaje_fuente no está vacía: no se siembra sobre datos existentes")
        filas = crudo.drop(columns=["idvehiculo"]).assign(lote=LOTE).to_dict("records")
        c.execute(text("""INSERT INTO ai_frecuencias.viaje_fuente (idprogramacion, idruta, fechasalida,
            fechainicio, fechafin, finalizado, cancelada, numeropasajeros, numerobajadas, lote)
            VALUES (:idprogramacion, :idruta, :fechasalida, :fechainicio, :fechafin, :finalizado,
                    :cancelada, :numeropasajeros, :numerobajadas, :lote)"""), filas)
    try:
        yield crudo
    finally:
        with motor_escritura().begin() as c:
            c.execute(text("DELETE FROM ai_frecuencias.viaje_fuente WHERE lote = :l"), {"l": LOTE})


def test_corte_en_la_consulta_y_mismo_resultado_que_en_memoria(viajes_sembrados, tmp_path):
    df, version_datos = S.viajes_local(CFG["datos_hasta"])
    assert pd.to_datetime(df.fechasalida).max() < pd.Timestamp(CFG["datos_hasta"])
    antes = pd.to_datetime(viajes_sembrados.fechasalida) < pd.Timestamp(CFG["datos_hasta"])
    assert len(df) == int(antes.sum())
    assert version_datos.endswith("hasta_2025-03-17")
    cfg = C.ConfigCandidato(**CFG)
    a_bd, m_bd = C.entrenar(df, cfg, version_datos)
    a_mem, m_mem = C.entrenar(viajes_sembrados, cfg, version_datos)
    assert m_bd["datos_sha256"] == m_mem["datos_sha256"] and m_bd["version"] == m_mem["version"]
    _, s1 = C.guardar(a_bd, m_bd, tmp_path / "bd")
    _, s2 = C.guardar(a_mem, m_mem, tmp_path / "mem")
    assert s1 == s2


def test_script_con_fuente_local(viajes_sembrados, tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps(CFG))
    S.main(["--config", str(cfg), "--fuente", "local", "--salida", str(tmp_path / "cand"),
            "--modelos", str(tmp_path / "vacia")])
    man = next((tmp_path / "cand").glob("*.manifiesto.json"))
    assert json.loads(man.read_text())["datos_hasta"] == CFG["datos_hasta"]
