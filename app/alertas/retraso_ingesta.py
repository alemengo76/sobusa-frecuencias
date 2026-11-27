"""ALT-009, condición «retraso de ingesta» (DECISIONES §16.2): reglas puras, sin base de datos.

Modelo
------
- Un CICLO se identifica por fuente, alcance y fecha operativa: ``<fuente>|ruta:<idruta>|<AAAA-MM-DD>``.
  El alcance es una ruta: una ingesta de las rutas 2, 16 y 21 aporta evidencia a los tres alcances.
- La política de calendario (apertura, vencimiento, tolerancia) es explícita y versionada. En hora de
  Bogotá, el ciclo del día d abre en ``d + apertura`` y vence en ``d + vencimiento + tolerancia``.
  Sin una política completa la verificación no es evaluable: no hay valores por defecto.
- Cada INTENTO de ingesta se vincula con UN SOLO ciclo:
  * un reintento manual (``ciclo_objetivo`` en los parámetros) va a ese ciclo solo si un verificador de
    autorización APROBADO lo confirma. El nombre que el operador escribe (``autorizacion_declarada_por``)
    solo acredita su declaración: se conserva para auditoría, pero por sí solo no satisface ni recupera
    nada. Hoy no existe un mecanismo aprobado (quién autoriza y cómo se verifica está pendiente), así que
    en operación ningún reintento manual cuenta;
  * los demás, por su INICIO: pertenecen al ciclo d si ``apertura(d) <= inicio < apertura(d + 1)``.
  Así una ingesta que termina tarde puede recuperar su ciclo, pero su hora de fin no satisface el
  ciclo siguiente.
- Un ciclo está CUMPLIDO si uno de sus intentos terminó ``exitosa``, completo, con consulta válida
  del alcance, a más tardar en el vencimiento. Una ingesta sin viajes nuevos cuenta si acredita esa
  consulta válida (``sin_novedades``). PENDIENTE si todavía no vence. VENCIDO en otro caso, con causa:
  no_iniciada · fallida · parcial · sin_consulta_valida · en_curso · tardia.
- Un EPISODIO es una racha de ciclos vencidos consecutivos. Desde ``CICLOS_MINIMOS`` (2, DECISIONES
  §16.2) genera incumplimiento. Se RECUPERA con la primera ingesta satisfactoria que termina después del
  vencimiento de su último ciclo vencido; ahí termina. Un vencimiento posterior a la recuperación abre
  una racha NUEVA (aunque no haya un ciclo cumplido entre medio) y solo genera incumplimiento cuando
  vuelve a sumar dos ciclos vencidos.

Este módulo no certifica que se haya recibido el universo completo de viajes y no atribuye la causa
a la fuente: solo acredita si cada ciclo definido se cumplió en su plazo.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONA = ZoneInfo("America/Bogota")
REGLA = "alt009-retraso-v1"
CICLOS_MINIMOS = 2
SEVERIDAD_FUNCIONAL = "Crítica"
SEVERIDAD_PERSISTIDA = "alta"          # equivalencia técnica PENDIENTE de aprobación (DECISIONES §16.5)
PREFIJO_CLAVE = "ALT-009|retraso|"
MARCA_NOTA = "[alt009-retraso]"

ENV = {
    "apertura": "ALT009_RETRASO_APERTURA",          # HH:MM, hora de Bogotá
    "vencimiento": "ALT009_RETRASO_VENCIMIENTO",    # HH:MM, hora de Bogotá
    "tolerancia_min": "ALT009_RETRASO_TOLERANCIA_MIN",
    "version": "ALT009_RETRASO_POLITICA_VERSION",   # identifica la política validada
    "inicio_monitoreo": "ALT009_RETRASO_INICIO_MONITOREO",  # AAAA-MM-DD: primer día operativo vigilado
}
CAUSAS = ("no_iniciada", "fallida", "parcial", "sin_consulta_valida", "en_curso", "tardia")


@dataclass(frozen=True)
class Politica:
    fuente: str
    apertura: time
    vencimiento: time
    tolerancia_min: int
    version: str
    inicio_monitoreo: date

    def validar(self) -> None:
        if not self.fuente or not self.version:
            raise ValueError("la política requiere fuente y versión")
        if self.tolerancia_min < 0:
            raise ValueError("la tolerancia no puede ser negativa")
        if self.apertura >= self.vencimiento:
            raise ValueError("la apertura debe ser anterior al vencimiento")
        if self.limite(date(2000, 1, 1)) > self.abre(date(2000, 1, 2)):
            raise ValueError("vencimiento + tolerancia no puede pasar la apertura del ciclo siguiente")

    def abre(self, d: date) -> datetime:
        return datetime.combine(d, self.apertura)

    def vence(self, d: date) -> datetime:
        return datetime.combine(d, self.vencimiento)

    def limite(self, d: date) -> datetime:
        return self.vence(d) + timedelta(minutes=self.tolerancia_min)

    def describir(self) -> dict:
        return {"fuente": self.fuente, "apertura": self.apertura.strftime("%H:%M"),
                "vencimiento": self.vencimiento.strftime("%H:%M"), "tolerancia_min": self.tolerancia_min,
                "version": self.version, "inicio_monitoreo": self.inicio_monitoreo.isoformat(),
                "zona": "America/Bogota", "ciclos_minimos": CICLOS_MINIMOS}


def _hhmm(valor: str) -> time:
    if not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", valor or ""):
        raise ValueError(f"hora inválida '{valor}' (se espera HH:MM)")
    return time.fromisoformat(valor)


def politica_desde_entorno(env, fuente: str) -> tuple[Politica | None, dict | None]:
    """(política, None) o (None, {"estado": "no_evaluable", "motivo": ...}). Nunca inventa valores."""
    faltan = [v for v in ENV.values() if not (env.get(v) or "").strip()]
    if faltan:
        return None, {"estado": "no_evaluable", "motivo": "politica_operativa_no_configurada",
                      "faltan": faltan,
                      "detalle": "La apertura, el vencimiento y la tolerancia están pendientes de validación "
                                 "operativa (DECISIONES §16.2); sin ellos no se evalúa el retraso."}
    try:
        p = Politica(fuente=fuente, apertura=_hhmm(env[ENV["apertura"]].strip()),
                     vencimiento=_hhmm(env[ENV["vencimiento"]].strip()),
                     tolerancia_min=int(env[ENV["tolerancia_min"]].strip()),
                     version=env[ENV["version"]].strip(),
                     inicio_monitoreo=date.fromisoformat(env[ENV["inicio_monitoreo"]].strip()))
        p.validar()
    except ValueError as e:
        return None, {"estado": "no_evaluable", "motivo": "politica_operativa_invalida", "detalle": str(e)}
    return p, None


def local(ts) -> datetime | None:
    """Hora local de Bogotá sin zona. Los valores sin zona se toman como hora de Bogotá."""
    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is not None:
        return ts.astimezone(ZONA).replace(tzinfo=None)
    return ts


# ------------------------------------------------------------------ intentos

@dataclass
class Intento:
    execution_id: str
    inicio: datetime
    fin: datetime | None
    estado: str
    fuente: str | None
    rutas: tuple
    ciclo_objetivo: date | None
    autorizacion_declarada_por: str | None
    consulta_valida: bool
    completa: bool
    viajes: int | None
    evidencia_autorizacion: str | None = None
    autorizacion_verificada: bool = False

    @property
    def satisfactorio(self) -> bool:
        return (self.estado == "exitosa" and self.fin is not None and self.completa
                and self.consulta_valida)

    def resultado(self) -> str:
        if self.estado == "exitosa" and self.fin is not None:
            if not self.consulta_valida:
                return "sin_consulta_valida"
            if not self.completa:
                return "parcial"
            return "exitosa_sin_novedades" if self.viajes == 0 else "exitosa_completa"
        if self.estado == "fallida":
            return "fallida"
        return "en_curso"

    def resumen(self) -> dict:
        return {"execution_id": self.execution_id, "inicio": self.inicio.isoformat(sep=" "),
                "fin": None if self.fin is None else self.fin.isoformat(sep=" "),
                "estado": self.estado, "resultado": self.resultado(),
                "ciclo_objetivo": None if self.ciclo_objetivo is None else self.ciclo_objetivo.isoformat(),
                "autorizacion_declarada_por": self.autorizacion_declarada_por,
                "autorizacion_verificada": self.autorizacion_verificada}

    @property
    def utilizable(self) -> bool:
        """Un reintento manual solo cuenta con autorización verificada; los demás intentos, siempre."""
        return self.ciclo_objetivo is None or self.autorizacion_verificada


def intento_desde_fila(f: dict) -> Intento:
    """Fila de execution_log (tipo 'ingesta') → Intento. Los campos que faltan cuentan en contra."""
    par = f.get("parametros") or {}
    res = f.get("resumen") or {}
    if isinstance(par, str):
        par = json.loads(par)
    if isinstance(res, str):
        res = json.loads(res)
    objetivo = par.get("ciclo_objetivo")
    return Intento(
        execution_id=str(f["execution_id"]), inicio=local(f["iniciada_en"]), fin=local(f.get("finalizada_en")),
        estado=f["estado"], fuente=par.get("fuente"),
        rutas=tuple(int(r) for r in (par.get("alcance") or {}).get("rutas", ())),
        ciclo_objetivo=date.fromisoformat(objetivo) if objetivo else None,
        autorizacion_declarada_por=(par.get("autorizacion_declarada_por") or None),
        evidencia_autorizacion=(par.get("evidencia_autorizacion") or None),
        consulta_valida=res.get("consulta_valida") is True, completa=res.get("completa") is True,
        viajes=None if res.get("viajes") is None else int(res["viajes"]))


# ------------------------------------------------------------------ ciclos

@dataclass
class Ciclo:
    ciclo_id: str
    fecha: date
    apertura: datetime
    vencimiento: datetime
    limite: datetime
    estado: str = "pendiente"
    causa: str | None = None
    sin_novedades: bool = False
    satisfecho_por: str | None = None
    recuperado_en: datetime | None = None
    recuperado_por: str | None = None
    intentos: list = field(default_factory=list)

    def resumen(self) -> dict:
        d = asdict(self)
        for k in ("apertura", "vencimiento", "limite", "recuperado_en"):
            d[k] = None if d[k] is None else d[k].isoformat(sep=" ")
        d["fecha"] = self.fecha.isoformat()
        d["intentos"] = [i.resumen() for i in self.intentos]
        return d


def ciclo_id(fuente: str, idruta: int, d: date) -> str:
    return f"{fuente}|ruta:{int(idruta)}|{d.isoformat()}"


def marcar_autorizaciones(intentos: list[Intento], verificar_autorizacion=None) -> None:
    """Marca los reintentos cuya autorización confirma `verificar_autorizacion(intento) -> bool`.
    Sin verificador aprobado (None, lo que hay hoy) ningún reintento queda verificado."""
    for i in intentos:
        i.autorizacion_verificada = bool(i.ciclo_objetivo is not None and verificar_autorizacion is not None
                                         and verificar_autorizacion(i) is True)


def ciclos_de_ruta(politica: Politica, idruta: int, intentos: list[Intento], ahora: datetime,
                   desde: date | None = None) -> tuple[list[Ciclo], list[dict]]:
    """Ciclos de la ruta desde `desde` (o el inicio del monitoreo) hasta el último ya abierto.
    Devuelve también los intentos que no se usan, con el motivo (se conservan para auditoría).
    La verificación de autorizaciones se marca antes con `marcar_autorizaciones`."""
    ahora = local(ahora)
    primero = max(desde or politica.inicio_monitoreo, politica.inicio_monitoreo)
    ciclos, d = {}, primero
    while politica.abre(d) <= ahora:
        ciclos[d] = Ciclo(ciclo_id(politica.fuente, idruta, d), d, politica.abre(d), politica.vence(d),
                          politica.limite(d))
        d += timedelta(days=1)
    no_usados = []
    for i in sorted(intentos, key=lambda x: (x.inicio, x.execution_id)):
        if i.fuente != politica.fuente or int(idruta) not in i.rutas:
            continue
        if i.ciclo_objetivo is not None:
            if not i.autorizacion_verificada:
                motivo = ("autorizacion_declarada_no_verificada" if i.autorizacion_declarada_por
                          else "reintento_sin_autorizacion")
                no_usados.append({**i.resumen(), "motivo": motivo})
                continue
            destino = i.ciclo_objetivo
        else:
            destino = i.inicio.date() if i.inicio >= politica.abre(i.inicio.date()) \
                else i.inicio.date() - timedelta(days=1)
        if destino in ciclos:
            ciclos[destino].intentos.append(i)
        elif destino >= primero:
            no_usados.append({**i.resumen(), "motivo": "ciclo_aun_no_abierto"})
    for c in ciclos.values():
        _clasificar(c, ahora)
    return [ciclos[k] for k in sorted(ciclos)], no_usados


def _clasificar(c: Ciclo, ahora: datetime) -> None:
    a_tiempo = [i for i in c.intentos if i.satisfactorio and i.fin <= c.limite]
    if a_tiempo:
        i = min(a_tiempo, key=lambda x: x.fin)
        c.estado, c.satisfecho_por, c.sin_novedades = "cumplido", i.execution_id, i.viajes == 0
        return
    if ahora < c.limite:
        c.estado = "pendiente"
        return
    c.estado = "vencido"
    previos = [i for i in c.intentos if i.inicio <= c.limite]
    if not previos:
        c.causa = "no_iniciada"
    else:
        ultimo = max(previos, key=lambda x: (x.inicio, x.execution_id))
        # satisfactorio aquí implica que terminó después del vencimiento
        c.causa = "tardia" if ultimo.satisfactorio else ultimo.resultado()
    tardios = [i for i in c.intentos if i.satisfactorio and i.fin > c.limite]
    if tardios:
        i = min(tardios, key=lambda x: x.fin)
        c.recuperado_en, c.recuperado_por = i.fin, i.execution_id


# ------------------------------------------------------------------ episodios

@dataclass
class Episodio:
    idruta: int
    ciclos: list
    truncado: bool                      # la racha empieza en el primer ciclo observado
    recuperado_en: datetime | None = None
    recuperado_por: str | None = None

    @property
    def primero(self) -> Ciclo:
        return self.ciclos[0]

    @property
    def ultimo(self) -> Ciclo:
        return self.ciclos[-1]

    @property
    def incumplimiento(self) -> bool:
        return len(self.ciclos) >= CICLOS_MINIMOS


def _primera_recuperacion(intentos: list[Intento], fuente: str, idruta: int, despues_de: datetime,
                          hasta: datetime | None):
    """Primer intento satisfactorio y utilizable de la misma fuente y alcance que termina en
    (despues_de, hasta]; `hasta=None` sin tope."""
    candidatos = [i for i in intentos if i.fuente == fuente and int(idruta) in i.rutas and i.utilizable
                  and i.satisfactorio and i.fin > despues_de and (hasta is None or i.fin <= hasta)]
    return min(candidatos, key=lambda x: (x.fin, x.execution_id)) if candidatos else None


def episodios(idruta: int, ciclos: list[Ciclo], intentos: list[Intento], fuente: str) -> list[Episodio]:
    """Rachas de ciclos vencidos consecutivos. Una racha termina con un ciclo cumplido o pendiente, o
    con una RECUPERACIÓN: un intento satisfactorio que termina después del vencimiento de su último ciclo.
    Si esa recuperación ocurre antes de que venza el ciclo siguiente y ese ciclo también vence, el
    siguiente empieza una racha nueva."""
    out: list[Episodio] = []
    racha: list[Ciclo] = []
    inicio_racha = 0

    def cerrar(rec):
        ep = Episodio(idruta, list(racha), truncado=inicio_racha == 0)
        if rec is not None:
            ep.recuperado_en, ep.recuperado_por = rec.fin, rec.execution_id
        out.append(ep)

    for k, c in enumerate(ciclos):
        if c.estado == "vencido":
            if racha:
                rec = _primera_recuperacion(intentos, fuente, idruta, racha[-1].limite, c.limite)
                if rec is not None:
                    cerrar(rec)
                    racha = []
            if not racha:
                inicio_racha = k
            racha.append(c)
        elif racha:
            cerrar(_primera_recuperacion(intentos, fuente, idruta, racha[-1].limite, None))
            racha = []
    if racha:
        cerrar(_primera_recuperacion(intentos, fuente, idruta, racha[-1].limite, None))
    return out


def prefijo_ruta(fuente: str, idruta: int) -> str:
    return f"{PREFIJO_CLAVE}{fuente}|ruta:{int(idruta)}|"


def clave(fuente: str, idruta: int, primer_ciclo: date) -> str:
    """Identidad del episodio: espacio propio, legible y distinto de la incompletitud (motor.clave_dedup,
    un hash de 16 caracteres). La fecha es la del PRIMER ciclo vencido del episodio."""
    return f"{prefijo_ruta(fuente, idruta)}{primer_ciclo.isoformat()}"


def primer_ciclo_de_clave(c: str) -> date:
    return date.fromisoformat(c.rsplit("|", 1)[1])


def explicacion(ep: Episodio, politica: Politica) -> str:
    detalle = "; ".join(
        f"{c.ciclo_id} vencido {c.limite:%Y-%m-%d %H:%M} ({c.causa}"
        + (f"; intentos {', '.join(i.execution_id[:8] for i in c.intentos)}" if c.intentos else "") + ")"
        for c in ep.ciclos)
    p = politica.describir()
    return (f"Información retrasada (ALT-009, regla {REGLA}): {len(ep.ciclos)} ciclos diarios consecutivos "
            f"sin una ingesta completa y exitosa dentro de su plazo para la fuente {politica.fuente}, ruta "
            f"{ep.idruta}. Ciclos: {detalle}. Política {p['version']}: apertura {p['apertura']}, vencimiento "
            f"{p['vencimiento']}, tolerancia {p['tolerancia_min']} min (hora de Bogotá). La causa no está "
            "determinada y no se atribuye a la fuente; una ingesta exitosa no certifica que el universo de "
            f"viajes esté completo. Severidad funcional {SEVERIDAD_FUNCIONAL}; se guarda como "
            f"'{SEVERIDAD_PERSISTIDA}' (equivalencia pendiente de aprobación). Sin notificaciones.")


def alerta(ep: Episodio, politica: Politica, clave_episodio: str) -> dict:
    return {"codigo": "ALT-009", "severidad": SEVERIDAD_PERSISTIDA, "idruta": int(ep.idruta),
            "franja_inicio": ep.primero.limite.replace(tzinfo=ZONA), "horizonte_min": None,
            "valor_observado": float(len(ep.ciclos)), "valor_esperado": 0.0,
            "umbral": float(CICLOS_MINIMOS), "puntuacion_anomalia": None,
            "explicacion": explicacion(ep, politica),
            "recomendacion": "Revisar con el responsable de la carga diaria qué impidió completar la ingesta. "
                             "Un reintento manual queda registrado, pero solo cuenta cuando exista un mecanismo "
                             "aprobado para verificar su autorización.",
            "clave_dedup": clave_episodio}


def nota_crecimiento(ep: Episodio) -> str:
    return (f"{MARCA_NOTA} ciclos_vencidos={len(ep.ciclos)} hasta {ep.ultimo.ciclo_id} "
            f"(vencido {ep.ultimo.limite:%Y-%m-%d %H:%M}, causa {ep.ultimo.causa}). "
            "Los vencimientos anteriores se conservan.")


def nota_recuperacion(ep: Episodio) -> str:
    return (f"{MARCA_NOTA} recuperado en {ep.recuperado_en:%Y-%m-%d %H:%M} (hora de Bogotá) por la ingesta "
            f"{ep.recuperado_por}. Se conservan los {len(ep.ciclos)} ciclos vencidos según su plazo original "
            f"({ep.primero.ciclo_id} a {ep.ultimo.ciclo_id}). La alerta no cambia de estado por esta nota.")
