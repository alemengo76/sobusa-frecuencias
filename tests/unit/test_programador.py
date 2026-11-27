"""Programador del ciclo diario (app/programador.py) con reloj simulado: horarios de PRUEBA explícitos."""
# ruff: noqa: DTZ001  -- hora local de Bogotá a propósito
import threading
from datetime import datetime, time

import pytest

from app import programador as pg


class Reloj:
    def __init__(self, inicio):
        self.t = inicio
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.t

    def dormir(self, s):
        from datetime import timedelta
        with self.lock:
            self.t += timedelta(seconds=s)


def test_sin_horario_explicito_no_programa_nada():
    horarios, error = pg.configuracion({})
    assert horarios is None and error["estado"] == "no_configurado"
    assert set(error["faltan"]) == {pg.ENV["ciclo"], pg.ENV["verificador"]}
    _, error = pg.configuracion({pg.ENV["ciclo"]: "5h", pg.ENV["verificador"]: "06:00"})
    assert "hora inválida" in error["motivo"]


def test_horario_de_prueba_y_proximo_disparo():
    horarios, error = pg.configuracion({pg.ENV["ciclo"]: "05:30, 04:00", pg.ENV["verificador"]: "06:00:30"})
    assert error is None and horarios == {"ciclo": [time(4), time(5, 30)], "verificador": [time(6, 0, 30)]}
    assert pg.proximo(datetime(2026, 11, 26, 5), horarios["ciclo"]) == datetime(2026, 11, 26, 5, 30)
    assert pg.proximo(datetime(2026, 11, 26, 5, 30), horarios["ciclo"]) == datetime(2026, 11, 27, 4)
    assert pg.proximo(datetime(2026, 11, 26, 23), horarios["verificador"]) == datetime(2026, 11, 27, 6, 0, 30)


def test_dispara_cada_trabajo_a_su_hora_sin_intervencion():
    reloj = Reloj(datetime(2026, 11, 26, 4, 59, 50))
    llamadas = []
    trabajos = {"ciclo": lambda t: llamadas.append(("ciclo", t, reloj())),
                "verificador": lambda t: llamadas.append(("verificador", t, reloj()))}
    p = pg.Programador({"ciclo": [time(5)], "verificador": [time(5, 0, 20)]}, trabajos, reloj, reloj.dormir)
    p.correr(max_disparos=4)
    p.esperar()
    assert [(n, t) for n, t, _ in llamadas] == [
        ("ciclo", datetime(2026, 11, 26, 5)), ("verificador", datetime(2026, 11, 26, 5, 0, 20)),
        ("ciclo", datetime(2026, 11, 27, 5)), ("verificador", datetime(2026, 11, 27, 5, 0, 20))]
    assert all(real >= programado for _, programado, real in llamadas)          # nunca antes de su hora


def test_el_verificador_corre_aunque_la_ingesta_no_termine_ni_arranque():
    reloj = Reloj(datetime(2026, 11, 26, 4, 59, 59))
    colgada, verificado = threading.Event(), threading.Event()

    def ciclo_colgado(t):
        colgada.wait(5)                     # la ingesta no termina

    def verificador(t):
        verificado.set()
    p = pg.Programador({"ciclo": [time(5), time(5, 0, 30)], "verificador": [time(5, 0, 10)]},
                       {"ciclo": ciclo_colgado, "verificador": verificador}, reloj, reloj.dormir)
    p.correr(max_disparos=3)
    assert verificado.wait(5), "el verificador no corrió mientras la ingesta seguía colgada"
    resultados = [(e["trabajo"], e["resultado"]) for e in p.registro]
    assert ("verificador", "ok") in resultados
    assert ("ciclo", "omitido_en_curso") in resultados          # no se superpone consigo mismo
    colgada.set()
    p.esperar()


def test_un_trabajo_que_falla_al_arrancar_no_detiene_al_programador():
    reloj = Reloj(datetime(2026, 11, 26, 4, 59, 59))

    def falla(t):
        raise RuntimeError("no hay conexión con la fuente")
    hechos = []
    p = pg.Programador({"ciclo": [time(5)], "verificador": [time(5, 0, 5)]},
                       {"ciclo": falla, "verificador": lambda t: hechos.append(t)}, reloj, reloj.dormir)
    p.correr(max_disparos=2)
    p.esperar()
    assert hechos == [datetime(2026, 11, 26, 5, 0, 5)]
    (error,) = [e for e in p.registro if e.get("resultado") == "error"]
    assert error["trabajo"] == "ciclo" and "no hay conexión" in error["detalle"]


def test_los_trabajos_reales_propagan_un_correlation_id_por_disparo(monkeypatch):
    from app import ciclo_alertas as ciclo
    vistos = {}
    monkeypatch.setattr(ciclo, "correr", lambda **k: vistos.setdefault("ciclo", k["correlation_id"]))
    monkeypatch.setattr(ciclo, "verificar_retraso", lambda **k: vistos.setdefault("verif", k["correlation_id"]))
    t = pg.trabajos_por_defecto("prueba")
    t["ciclo"](datetime(2026, 11, 26, 5))
    t["verificador"](datetime(2026, 11, 26, 6, 0, 30))
    assert vistos == {"ciclo": "prueba-ciclo-20261126T050000", "verif": "prueba-verificador-20261126T060030"}


def test_main_sin_configuracion_termina_con_codigo_2(monkeypatch, capsys):
    for v in pg.ENV.values():
        monkeypatch.delenv(v, raising=False)
    assert pg.main([]) == 2
    assert "no_configurado" in capsys.readouterr().out


@pytest.mark.parametrize("valor", ["05:00+00:00"])
def test_horas_con_zona_se_rechazan(valor):
    with pytest.raises(ValueError):
        pg.leer_horas(valor)
