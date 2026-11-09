"""Partes comunes de preparar_wheelhouse.py y verificar_wheelhouse.py (ver docs/WHEELHOUSE.md).

Solo biblioteca estándar + el `packaging` que trae pip (pip._vendor.packaging): es el mismo código que
pip usa para decidir qué rueda es instalable, y no hace falta descargar nada para tenerlo.

Registro: wheelhouse/ORIGEN.jsonl, una línea JSON por archivo; si un archivo tiene varias líneas,
vale la última. Orden de escritura (lo garantiza `instalar_en_su_lugar`):
  1. el archivo se descarga o construye en wheelhouse/.tmp/
  2. se calcula su SHA-256 y se escribe su línea en ORIGEN.jsonl (con fsync)
  3. recién entonces se mueve a su lugar con os.replace (atómico dentro del mismo disco)
Un archivo está COMPLETO solo si existe en su lugar, tiene registro y su SHA-256 es el registrado.
Nada se borra: lo que no cuadra se aparta a .huerfanos/, .corruptos/ o .retirados/.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import tomllib

try:
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import (
        canonicalize_name,
        parse_sdist_filename,
        parse_wheel_filename,
    )
    from pip._vendor.packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover
    sys.exit("Este script necesita pip instalado en el intérprete (usa su copia de packaging).")

LISTA_CONGELADA = Path("docker/requisitos-congelados.txt")
LISTA_CONSTRUCCION = Path("docker/requisitos-construccion.txt")
LISTAS = (LISTA_CONGELADA, LISTA_CONSTRUCCION)
PROYECTO = "sobusa-frecuencias"
# Lo que pip supone cuando un código fuente no declara [build-system] (pip 24, pyproject.py)
BUILD_REQUIRES_POR_DEFECTO = ["setuptools>=40.8.0"]


class ErrorWheelhouse(Exception):
    """Error que detiene el script con un mensaje claro (sin traza)."""


# --- plataforma ---------------------------------------------------------------------------------

def exigir_plataforma() -> None:
    """Las etiquetas de rueda dependen del intérprete: hay que correr en el mismo que la imagen."""
    maquina = platform.machine().lower()
    if sys.version_info[:2] != (3, 11) or sys.platform != "linux" or maquina not in ("x86_64", "amd64"):
        raise ErrorWheelhouse(
            f"Corra este script dentro de python:3.11-slim con --platform linux/amd64 "
            f"(aquí: Python {sys.version_info[0]}.{sys.version_info[1]}, {sys.platform}, {maquina}). "
            "Ver docs/WHEELHOUSE.md.")


def prioridades() -> dict:
    """Etiqueta -> prioridad (0 = la preferida), en el orden en que pip las prefiere."""
    return {t: i for i, t in enumerate(sys_tags())}


def prioridad_rueda(archivo: str, prio: dict) -> int | None:
    """None si la rueda no es instalable en este intérprete."""
    _, _, _, etiquetas = parse_wheel_filename(archivo)
    valores = [prio[t] for t in etiquetas if t in prio]
    return min(valores) if valores else None


# --- listas -------------------------------------------------------------------------------------

def leer_lista(ruta: Path) -> dict[str, tuple[str, str]]:
    """{nombre_canónico: (nombre, versión)}. Solo acepta líneas `nombre==versión` (y comentarios)."""
    if not ruta.exists():
        raise ErrorWheelhouse(f"No existe {ruta}.")
    datos = ruta.read_bytes()
    if b"\x00" in datos:
        raise ErrorWheelhouse(f"{ruta} no está en UTF-8 (¿se guardó con PowerShell '>'?). Ver docs/WHEELHOUSE.md.")
    salida: dict[str, tuple[str, str]] = {}
    for n, linea in enumerate(datos.decode("utf-8-sig").splitlines(), 1):
        limpia = linea.split("#", 1)[0].strip()
        if not limpia:
            continue
        if limpia.count("==") != 1 or any(c in limpia for c in " ;@[<>!~,"):
            raise ErrorWheelhouse(f"{ruta}:{n}: se esperaba 'paquete==versión' exacto, no {linea.strip()!r}")
        nombre, version = (x.strip() for x in limpia.split("=="))
        try:
            Version(version)
        except InvalidVersion as e:
            raise ErrorWheelhouse(f"{ruta}:{n}: versión inválida {version!r}") from e
        canon = canonicalize_name(nombre)
        if canon == PROYECTO:
            raise ErrorWheelhouse(f"{ruta}:{n}: el propio proyecto no va en la lista (quite esa línea).")
        if canon in salida:
            raise ErrorWheelhouse(f"{ruta}:{n}: {nombre} aparece dos veces")
        salida[canon] = (nombre, version)
    return salida


def leer_listas() -> tuple[dict, dict, dict]:
    """(congelada, construcción, todas). Una misma versión en ambas listas vale; dos distintas no."""
    congelada = leer_lista(LISTA_CONGELADA)
    construccion = leer_lista(LISTA_CONSTRUCCION)
    if not congelada:
        raise ErrorWheelhouse(f"{LISTA_CONGELADA} no tiene paquetes: genérela primero (docs/WHEELHOUSE.md, paso 0).")
    if not construccion:
        raise ErrorWheelhouse(f"{LISTA_CONSTRUCCION} no tiene paquetes.")
    todas = dict(congelada)
    for canon, (nombre, version) in construccion.items():
        if canon in todas and Version(todas[canon][1]) != Version(version):
            raise ErrorWheelhouse(
                f"{nombre} tiene versiones distintas en las dos listas ({todas[canon][1]} y {version})")
        todas[canon] = (nombre, version)
    return congelada, construccion, todas


def comprobar_herramientas(requires: list[str], construccion: dict, quien: str) -> list[str]:
    """Cada requisito de construcción debe estar fijado con == en requisitos-construccion.txt."""
    errores = []
    for texto in requires:
        try:
            req = Requirement(texto)
        except InvalidRequirement:
            errores.append(f"{quien}: requisito de construcción ilegible {texto!r}")
            continue
        if req.marker is not None and not req.marker.evaluate():
            continue
        canon = canonicalize_name(req.name)
        if canon not in construccion:
            errores.append(
                f"{quien} necesita '{texto}' para construirse y {req.name} no está fijado en "
                f"{LISTA_CONSTRUCCION}: agréguelo con == y repita la fase A")
        elif not req.specifier.contains(construccion[canon][1], prereleases=True):
            errores.append(
                f"{quien} necesita '{texto}' y en {LISTA_CONSTRUCCION} está "
                f"{req.name}=={construccion[canon][1]}, que no lo cumple")
    return errores


def leer_pyproject_build_requires(texto: str, quien: str) -> list[str]:
    datos = tomllib.loads(texto)
    if "build-system" not in datos:
        return list(BUILD_REQUIRES_POR_DEFECTO)
    if "requires" not in datos["build-system"]:
        raise ErrorWheelhouse(f"{quien}: [build-system] sin 'requires'")
    return list(datos["build-system"]["requires"])


def build_requires_fuente(ruta: Path) -> list[str]:
    """[build-system] requires del pyproject.toml de primer nivel del código fuente."""
    texto = None
    if ruta.name.endswith(".zip"):
        with zipfile.ZipFile(ruta) as z:
            for n in z.namelist():
                if n.count("/") == 1 and n.endswith("/pyproject.toml"):
                    texto = z.read(n).decode("utf-8")
                    break
    else:
        with tarfile.open(ruta) as t:
            for m in t.getmembers():
                if m.isfile() and m.name.count("/") == 1 and m.name.endswith("/pyproject.toml"):
                    texto = t.extractfile(m).read().decode("utf-8")
                    break
    if texto is None:
        return list(BUILD_REQUIRES_POR_DEFECTO)
    return leer_pyproject_build_requires(texto, ruta.name)


# --- archivos y registro ------------------------------------------------------------------------

def sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def ahora() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Carpeta:
    def __init__(self, raiz: Path):
        self.raiz = raiz
        self.tmp = raiz / ".tmp"
        self.fuentes = raiz / "fuentes"
        self.registro = raiz / "ORIGEN.jsonl"
        self.registros_log = raiz / ".registros"

    def crear(self) -> None:
        for d in (self.raiz, self.tmp, self.fuentes, self.registros_log):
            d.mkdir(parents=True, exist_ok=True)

    def ruta_final(self, reg: dict) -> Path:
        return (self.fuentes if reg["tipo"] == "fuente" else self.raiz) / reg["archivo"]

    def leer_registro(self) -> tuple[dict[str, dict], list[str]]:
        """{archivo: último registro}, avisos (líneas ilegibles: p. ej. un corte a mitad de escritura)."""
        registros, avisos = {}, []
        if not self.registro.exists():
            return registros, avisos
        for n, linea in enumerate(self.registro.read_text(encoding="utf-8").splitlines(), 1):
            if not linea.strip():
                continue
            try:
                reg = json.loads(linea)
                registros[reg["archivo"]] = reg
            except (json.JSONDecodeError, KeyError):
                avisos.append(f"ORIGEN.jsonl:{n} ilegible (se ignora): {linea[:80]!r}")
        return registros, avisos

    def anotar(self, reg: dict) -> None:
        reg = {**reg, "registrado": ahora()}
        prefijo = ""
        if self.registro.exists() and self.registro.stat().st_size:
            with self.registro.open("rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    prefijo = "\n"      # la línea anterior quedó a medias: no se pega a la nueva
        with self.registro.open("a", encoding="utf-8") as f:
            f.write(prefijo + json.dumps(reg, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def instalar_en_su_lugar(self, temporal: Path, reg: dict) -> Path:
        """`temporal` ya está en .tmp/ con su nombre final y verificado: registro primero, luego replace."""
        if temporal.parent != self.tmp or temporal.name != reg["archivo"] or sha256(temporal) != reg["sha256"]:
            raise ErrorWheelhouse(f"error interno: {temporal} no corresponde a su registro")
        self.anotar(reg)
        destino = self.ruta_final(reg)
        os.replace(temporal, destino)
        return destino

    def apartar(self, ruta: Path, motivo: str) -> Path:
        """Mueve (no borra) a .huerfanos/, .corruptos/ o .retirados/ con un nombre que no pisa nada."""
        destino_dir = self.raiz / f".{motivo}"
        destino_dir.mkdir(exist_ok=True)
        destino = destino_dir / f"{ruta.name}.{time.strftime('%Y%m%dT%H%M%S')}"
        i = 1
        while destino.exists():
            destino = destino_dir / f"{ruta.name}.{time.strftime('%Y%m%dT%H%M%S')}.{i}"
            i += 1
        shutil.move(str(ruta), str(destino))
        return destino

    def revisar(self, todas: dict, mover: bool) -> tuple[dict, list[str]]:
        """Estado de la carpeta contra las listas.

        Devuelve ({canon: {"rueda": reg|None, "fuente": reg|None}}, problemas). Con mover=True
        (preparar) repara: completa un os.replace interrumpido y aparta lo que no cuadra. Con
        mover=False (verificar) solo informa.
        """
        estado = {canon: {"rueda": None, "fuente": None} for canon in todas}
        registros, problemas = self.leer_registro()
        completos: set[str] = set()
        informados: set[str] = set()   # ya tienen su problema anotado: no se repite como "sin registro"
        for archivo, reg in registros.items():
            final = self.ruta_final(reg)
            pendiente_tmp = self.tmp / archivo
            if not final.exists() and pendiente_tmp.exists() and sha256(pendiente_tmp) == reg["sha256"]:
                if mover:   # se cortó entre el registro y el os.replace: se termina ese paso
                    os.replace(pendiente_tmp, final)
                    print(f"recuperado  {archivo} (se había cortado antes de moverlo a su lugar)")
                else:
                    problemas.append(f"{archivo}: registrado pero sigue en .tmp/ (corra preparar_wheelhouse.py)")
                    informados.add(archivo)
                    continue
            if not final.exists():
                continue    # registrado pero no está: se vuelve a hacer
            if sha256(final) != reg["sha256"]:
                if mover:
                    destino = self.apartar(final, "corruptos")
                    print(f"apartado    {archivo} -> {destino} (SHA-256 distinto al registrado)")
                else:
                    problemas.append(f"{archivo}: SHA-256 distinto al registrado en ORIGEN.jsonl")
                    informados.add(archivo)
                continue
            canon = canonicalize_name(reg["proyecto"])
            if canon not in todas or Version(todas[canon][1]) != Version(reg["version"]):
                if mover:
                    print(f"apartado    {archivo} -> {self.apartar(final, 'retirados')} (ya no está en las listas)")
                else:
                    problemas.append(f"sobra {archivo}: {reg['proyecto']}=={reg['version']} no está en las listas")
                    informados.add(archivo)
                continue
            completos.add(archivo)
            estado[canon]["fuente" if reg["tipo"] == "fuente" else "rueda"] = reg
        for ruta in sorted(list(self.raiz.glob("*.whl")) + [p for p in self.fuentes.glob("*") if p.is_file()]):
            if ruta.name not in completos | informados and ruta.exists():
                if mover:
                    print(f"apartado    {ruta.name} -> {self.apartar(ruta, 'huerfanos')} (sin registro)")
                else:
                    problemas.append(f"{ruta.name}: está en la carpeta sin registro válido en ORIGEN.jsonl")
        return estado, problemas


def abrir_url(url: str, cabeceras: dict | None = None, timeout: float = 120):
    """urlopen solo para https (o http hacia esta misma máquina, para pruebas con un índice local)."""
    p = urllib.parse.urlparse(url)
    if not (p.scheme == "https" or (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost"))):
        raise ErrorWheelhouse(f"URL no permitida (solo https): {url}")
    return urllib.request.urlopen(urllib.request.Request(url, headers=cabeceras or {}), timeout=timeout)  # noqa: S310


def nombre_version_de_archivo(archivo: str) -> tuple[str, str]:
    if archivo.endswith(".whl"):
        n, v, _, _ = parse_wheel_filename(archivo)
    else:
        n, v = parse_sdist_filename(archivo)
    return str(n), str(v)
