"""Programador del ciclo diario: dispara los trabajos a sus horas, en hora de Bogotá, sin intervención.

    python -m app.programador                       # usa los horarios de las variables de entorno
    python -m app.programador --max-disparos 3      # termina después de 3 disparos (pruebas)

Trabajos (cada uno con su lista de horas HH:MM o HH:MM:SS, separadas por coma):
- PROGRAMADOR_CICLO_HORAS        → app.ciclo_alertas.correr(): ingesta registrada + alertas posteriores.
- PROGRAMADOR_VERIFICADOR_HORAS  → app.ciclo_alertas.verificar_retraso(): ALT-009 por retraso, SIN ingerir.
- PROGRAMADOR_FRECUENCIAS_HORAS  → servicios.ejecutar_frecuencias() para el día siguiente
                                   (ALT-001 a ALT-004 e incompletitud de ALT-009). Opcional.

No hay horarios por defecto: sin PROGRAMADOR_CICLO_HORAS y PROGRAMADOR_VERIFICADOR_HORAS el programador no
arranca (el horario real depende de la política operativa de ALT-009, pendiente de validación).

Cada trabajo corre en su propio hilo: si la ingesta se queda colgada o falla al arrancar, el verificador
de retraso igual se dispara a su hora. Un trabajo que todavía está corriendo no se superpone consigo
mismo: el disparo se omite y queda en el registro. Cada disparo usa un correlation_id propio
(`<PROGRAMADOR_PREFIJO>-<trabajo>-<AAAAMMDDTHHMMSS>`) que se propaga a todas sus ejecuciones.

Este módulo NO se instala solo. La configuración para activarlo está en deploy/programador/.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time as reloj
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from app.alertas.retraso_ingesta import ZONA

log = logging.getLogger(__name__)

ENV = {"ciclo": "PROGRAMADOR_CICLO_HORAS", "verificador": "PROGRAMADOR_VERIFICADOR_HORAS",
       "frecuencias": "PROGRAMADOR_FRECUENCIAS_HORAS"}
ENV_PREFIJO = "PROGRAMADOR_PREFIJO"
OBLIGATORIOS = ("ciclo", "verificador")


def ahora_bogota() -> datetime:
    return datetime.now(ZONA).replace(tzinfo=None)


def leer_horas(valor: str | None) -> list[time]:
    horas = []
    for parte in (valor or "").split(","):
        parte = parte.strip()
        if not parte:
            continue
        h = time.fromisoformat(parte)
        if h.tzinfo is not None:
            raise ValueError(f"la hora '{parte}' no debe llevar zona: se interpreta en hora de Bogotá")
        horas.append(h)
    return sorted(set(horas))


def proximo(despues_de: datetime, horas: list[time]) -> datetime:
    """Primer instante programado estrictamente posterior a `despues_de`."""
    for dias in (0, 1):
        d = despues_de.date() + timedelta(days=dias)
        for h in horas:
            t = datetime.combine(d, h)
            if t > despues_de:
                return t
    raise ValueError("sin horas configuradas")


def configuracion(env=None) -> tuple[dict[str, list[time]] | None, dict | None]:
    env = os.environ if env is None else env
    try:
        horarios = {k: leer_horas(env.get(v)) for k, v in ENV.items()}
    except ValueError as e:
        return None, {"estado": "no_configurado", "motivo": f"hora inválida: {e}"}
    faltan = [ENV[k] for k in OBLIGATORIOS if not horarios[k]]
    if faltan:
        return None, {"estado": "no_configurado", "faltan": faltan,
                      "motivo": "Sin horario explícito no se programa nada (no hay valores por defecto)."}
    return {k: v for k, v in horarios.items() if v}, None


def trabajos_por_defecto(prefijo: str) -> dict:
    """Funciones reales. Se importan aquí para que el módulo cargue sin base de datos."""
    def cid(nombre, cuando):
        return f"{prefijo}-{nombre}-{cuando:%Y%m%dT%H%M%S}"

    def ciclo_diario(cuando):
        from app import ciclo_alertas as ciclo
        return ciclo.correr(correlation_id=cid("ciclo", cuando))

    def verificador(cuando):
        from app import ciclo_alertas as ciclo
        return ciclo.verificar_retraso(correlation_id=cid("verificador", cuando))

    def frecuencias(cuando):
        from app import ingesta, servicios
        dia = datetime.combine(cuando.date() + timedelta(days=1), time.min)
        return servicios.ejecutar_frecuencias(list(ingesta.RUTAS), dia, [30, 60, 120], "puntual",
                                              cid("frecuencias", cuando))
    return {"ciclo": ciclo_diario, "verificador": verificador, "frecuencias": frecuencias}


@dataclass
class Programador:
    horarios: dict[str, list[time]]
    trabajos: dict
    reloj_fn: object = ahora_bogota
    dormir: object = reloj.sleep
    max_espera_s: float = 30.0
    detener: threading.Event = field(default_factory=threading.Event)
    registro: list = field(default_factory=list)
    _hilos: dict = field(default_factory=dict)
    _bloqueo: threading.Lock = field(default_factory=threading.Lock)

    def _anotar(self, **e):
        with self._bloqueo:
            self.registro.append(e)
        log.info("programador", extra={"datos": e})

    def _correr(self, nombre: str, cuando: datetime):
        inicio = self.reloj_fn()
        try:
            r = self.trabajos[nombre](cuando)
            self._anotar(trabajo=nombre, programado=cuando, inicio=inicio, fin=self.reloj_fn(), resultado="ok",
                         detalle=_resumen(r))
        except Exception as e:                         # el programador sigue aunque falle un trabajo
            log.exception(f"falló el trabajo {nombre}")
            self._anotar(trabajo=nombre, programado=cuando, inicio=inicio, fin=self.reloj_fn(), resultado="error",
                         detalle=f"{type(e).__name__}: {e}"[:300])

    def disparar(self, nombre: str, cuando: datetime) -> bool:
        h = self._hilos.get(nombre)
        if h is not None and h.is_alive():
            self._anotar(trabajo=nombre, programado=cuando, resultado="omitido_en_curso")
            return False
        h = threading.Thread(target=self._correr, args=(nombre, cuando), name=f"programador-{nombre}", daemon=True)
        self._hilos[nombre] = h
        self._anotar(trabajo=nombre, programado=cuando, resultado="disparado")
        h.start()
        return True

    def correr(self, max_disparos: int | None = None, desde: datetime | None = None) -> list:
        base = desde or self.reloj_fn()
        siguientes = {n: proximo(base, h) for n, h in self.horarios.items()}
        disparos = 0
        while not self.detener.is_set() and (max_disparos is None or disparos < max_disparos):
            nombre, cuando = min(siguientes.items(), key=lambda kv: (kv[1], kv[0]))
            espera = (cuando - self.reloj_fn()).total_seconds()
            if espera > 0:
                self.dormir(min(espera, self.max_espera_s))
                continue
            self.disparar(nombre, cuando)
            disparos += 1
            siguientes[nombre] = proximo(cuando, self.horarios[nombre])
        return self.registro

    def esperar(self, timeout: float = 60.0):
        for h in list(self._hilos.values()):
            h.join(timeout)


def _resumen(r):
    if not isinstance(r, dict):
        return None
    out = {k: r.get(k) for k in ("execution_id", "execution_id_ingesta", "estado") if k in r}
    if isinstance(r.get("alertas"), dict):
        out["alertas_insertadas"] = r["alertas"].get("insertadas")
    if isinstance(r.get("alt_009_retraso"), dict):
        out["retraso"] = {k: r["alt_009_retraso"].get(k) for k in ("estado", "insertadas", "motivo")}
    return out


def main(argv=None) -> int:
    from app.core.registro import configurar_logs
    configurar_logs()
    p = argparse.ArgumentParser(description="Programador del ciclo diario (hora de Bogotá)")
    p.add_argument("--max-disparos", type=int, default=None, help="Termina después de N disparos (pruebas)")
    p.add_argument("--comprobar", action="store_true", help="Solo valida la configuración y muestra los próximos disparos")
    a = p.parse_args(argv)
    horarios, error = configuracion()
    if error:
        print(json.dumps(error, ensure_ascii=False))
        return 2
    prog = Programador(horarios, trabajos_por_defecto(os.environ.get(ENV_PREFIJO, "programador")))
    ahora = ahora_bogota()
    proximos = {n: proximo(ahora, h).isoformat(sep=" ") for n, h in horarios.items()}
    print(json.dumps({"estado": "configurado", "zona": "America/Bogota", "proximos": proximos}, ensure_ascii=False))
    if a.comprobar:
        return 0
    signal.signal(signal.SIGTERM, lambda *_: prog.detener.set())
    prog.correr(a.max_disparos)
    prog.esperar()
    print(json.dumps({"estado": "terminado", "registro": prog.registro}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

