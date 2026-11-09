"""Prepara wheelhouse/ para construir la imagen sin conexión. Ver docs/WHEELHOUSE.md.

Se corre DENTRO de python:3.11-slim --platform linux/amd64 (el mismo intérprete de la imagen), desde
la raíz del repositorio:

  fase A (con red):   python scripts/preparar_wheelhouse.py descargar
  fase B (sin red):   python scripts/preparar_wheelhouse.py construir      (docker run --network none)
  en cualquier momento: python scripts/preparar_wheelhouse.py estado

Fase A, por cada versión de docker/requisitos-congelados.txt y docker/requisitos-construccion.txt:
  1. Consulta qué archivos publica el índice para esa versión (API JSON de PyPI) y elige con
     packaging.tags.sys_tags(), igual que pip.
  2. Si hay una rueda compatible, baja SOLO esa. Si falla (red, corte, SHA-256 distinto), queda
     PENDIENTE: nunca se pasa al código fuente por un error.
  3. Si no hay rueda compatible y hay código fuente, baja el fuente a wheelhouse/fuentes/ para la fase B.
  4. Si la consulta falla, o no hay ni rueda ni fuente, queda PENDIENTE con el motivo.
  Las descargas son reanudables: lo bajado queda en wheelhouse/.tmp/<archivo>.part y se continúa
  desde ahí. El SHA-256 se compara con el que publica el índice antes de aceptar el archivo.

Fase B: en un entorno virtual nuevo instala SOLO requisitos-construccion.txt desde la carpeta
(--no-index) y construye cada fuente con pip wheel --no-deps --no-build-isolation --no-index.
Antes comprueba que cada requisito de construcción del fuente esté fijado con ==. Se niega a correr
si hay red.

Códigos de salida: 0 = terminado; 2 = quedan pendientes (repita el mismo comando); 1 = error.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wheelhouse_comun import (  # noqa: E402
    Carpeta,
    ErrorWheelhouse,
    Version,
    abrir_url,
    build_requires_fuente,
    canonicalize_name,
    comprobar_herramientas,
    exigir_plataforma,
    leer_listas,
    leer_pyproject_build_requires,
    nombre_version_de_archivo,
    parse_sdist_filename,
    prioridad_rueda,
    prioridades,
    sha256,
)

CARPETA = Carpeta(Path(os.environ.get("WHEELHOUSE_DIR", "wheelhouse")))
INDICE = os.environ.get("WHEELHOUSE_INDICE", "https://pypi.org/pypi").rstrip("/")
TIEMPO_ESPERA = float(os.environ.get("WHEELHOUSE_TIMEOUT", "120"))
REINTENTOS = int(os.environ.get("WHEELHOUSE_REINTENTOS", "5"))


class Pendiente(Exception):
    """Esta versión no se pudo completar ahora; se reintenta al repetir el comando."""


# --- fase A -------------------------------------------------------------------------------------

def consultar_indice(nombre: str, version: str) -> list[dict]:
    url = f"{INDICE}/{nombre}/{version}/json"
    try:
        with abrir_url(url, timeout=TIEMPO_ESPERA) as r:
            datos = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Pendiente(f"no se pudo consultar el índice ({url}): {e}") from e
    return datos.get("urls", [])


def version_fuente(archivo: str) -> Version | None:
    try:
        return Version(str(parse_sdist_filename(archivo)[1]))
    except ValueError:      # InvalidSdistFilename / InvalidVersion: nombre que pip tampoco reconocería
        return None


def elegir(nombre: str, version: str, publicados: list[dict], prio: dict) -> tuple[str, dict | None]:
    """('rueda', archivo) | ('fuente', archivo) | ('ninguno', None)."""
    ruedas = []
    for u in publicados:
        if u.get("packagetype") == "bdist_wheel" and u["filename"].endswith(".whl"):
            n, v = nombre_version_de_archivo(u["filename"])
            if canonicalize_name(n) == canonicalize_name(nombre) and Version(v) == Version(version):
                p = prioridad_rueda(u["filename"], prio)
                if p is not None:
                    ruedas.append((p, u["filename"], u))
    if ruedas:
        return "rueda", min(ruedas, key=lambda x: (x[0], x[1]))[2]
    fuentes = [u for u in publicados
               if u.get("packagetype") == "sdist" and version_fuente(u["filename"]) == Version(version)]
    if fuentes:   # .tar.gz antes que .zip, como pip
        return "fuente", sorted(fuentes, key=lambda u: (not u["filename"].endswith(".tar.gz"), u["filename"]))[0]
    return "ninguno", None


def bajar(u: dict) -> Path:
    """Descarga reanudable a .tmp/<archivo>.part; devuelve .tmp/<archivo> con el SHA-256 del índice."""
    archivo, esperado = u["filename"], u["digests"]["sha256"]
    tamano = u.get("size")
    parcial = CARPETA.tmp / f"{archivo}.part"
    listo = CARPETA.tmp / archivo
    ultimo_error = None
    for intento in range(1, REINTENTOS + 1):
        ya = parcial.stat().st_size if parcial.exists() else 0
        if tamano is not None and ya > tamano:
            print(f"  {archivo}: el parcial es más grande que el archivo publicado -> "
                  f"{CARPETA.apartar(parcial, 'corruptos')}")
            ya = 0
        try:
            if tamano is None or ya < tamano:
                rango = {"Range": f"bytes={ya}-"} if ya else {}
                with abrir_url(u["url"], rango, timeout=TIEMPO_ESPERA) as r:
                    modo = "ab" if ya and r.status == 206 else "wb"
                    if ya and modo == "wb":
                        print(f"  {archivo}: el servidor no permite reanudar; se descarga desde el inicio")
                    with parcial.open(modo) as f:
                        for bloque in iter(lambda: r.read(1 << 16), b""):
                            f.write(bloque)
                        f.flush()
                        os.fsync(f.fileno())
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            ultimo_error = e
            tiene = parcial.stat().st_size if parcial.exists() else 0
            print(f"  {archivo}: intento {intento}/{REINTENTOS} cortado ({e}); van {tiene} bytes, se reanuda",
                  flush=True)
            time.sleep(min(30, 2 ** intento))
            continue
        if not parcial.exists():
            raise Pendiente(f"{parcial} desapareció durante la descarga")
        actual = parcial.stat().st_size
        if tamano is not None and actual < tamano:
            ultimo_error = f"llegaron {actual} de {tamano} bytes"
            print(f"  {archivo}: intento {intento}/{REINTENTOS} incompleto ({ultimo_error}), se reanuda", flush=True)
            time.sleep(min(30, 2 ** intento))
            continue
        obtenido = sha256(parcial)
        if obtenido != esperado:
            apartado = CARPETA.apartar(parcial, "corruptos")
            raise Pendiente(f"SHA-256 distinto al publicado (esperado {esperado[:12]}…, obtenido "
                            f"{obtenido[:12]}…); el archivo quedó en {apartado}")
        os.replace(parcial, listo)
        return listo
    raise Pendiente(f"red: {ultimo_error}; lo descargado se conserva en {parcial}")


def descargar(canon: str, nombre: str, version: str, estado: dict, prio: dict) -> str:
    if estado["rueda"]:
        return f"ya está     {estado['rueda']['archivo']}"
    publicados = consultar_indice(nombre, version)
    tipo, u = elegir(nombre, version, publicados, prio)
    if tipo == "ninguno":
        raise Pendiente("sin distribución: el índice no publica una rueda compatible ni código fuente")
    if tipo == "fuente" and estado["fuente"]:
        return f"fuente lista {estado['fuente']['archivo']} (falta construir: fase B)"
    listo = bajar(u)
    reg = {"tipo": tipo, "origen": "pypi", "proyecto": nombre, "version": version, "archivo": u["filename"],
           "sha256": sha256(listo), "sha256_indice": u["digests"]["sha256"], "url": u["url"]}
    CARPETA.instalar_en_su_lugar(listo, reg)
    if tipo == "rueda":
        return f"descargado  {u['filename']}"
    return f"fuente      {u['filename']} (no hay rueda compatible: se construye en la fase B)"


def fase_a() -> int:
    exigir_plataforma()
    congelada, construccion, todas = leer_listas()
    errores = comprobar_herramientas(
        leer_pyproject_build_requires(Path("pyproject.toml").read_text(encoding="utf-8"), "pyproject.toml"),
        construccion, "el proyecto (pyproject.toml)")
    if errores:
        raise ErrorWheelhouse("\n".join(errores))
    CARPETA.crear()
    estado, avisos = CARPETA.revisar(todas, mover=True)
    for a in avisos:
        print("aviso:", a)
    prio = prioridades()
    pendientes = []
    for canon, (nombre, version) in sorted(todas.items()):
        try:
            print(descargar(canon, nombre, version, estado[canon], prio), flush=True)
        except Pendiente as e:
            print(f"PENDIENTE   {nombre}=={version}: {e}", flush=True)
            pendientes.append(f"{nombre}=={version}")
    estado, _ = CARPETA.revisar(todas, mover=True)
    por_construir = [c for c, e in estado.items() if e["fuente"] and not e["rueda"]]
    for c in por_construir:   # se avisa ya, no recién en la fase B
        for error in comprobar_herramientas(build_requires_fuente(CARPETA.ruta_final(estado[c]["fuente"])),
                                            construccion, estado[c]["fuente"]["archivo"]):
            print(f"PENDIENTE   {todas[c][0]}=={todas[c][1]}: {error}", flush=True)
            pendientes.append(f"{todas[c][0]}=={todas[c][1]}")
    print(f"\nfase A: {sum(1 for e in estado.values() if e['rueda'])} ruedas, {len(por_construir)} por construir, "
          f"{len(pendientes)} pendientes de {len(todas)}.")
    if pendientes:
        print("Repita el mismo comando: lo completo se conserva y las descargas cortadas se reanudan.")
        return 2
    if por_construir:
        print("Siguiente: fase B (preparar_wheelhouse.py construir, con --network none).")
    else:
        print("No hay nada que construir. Siguiente: verificar_wheelhouse.py.")
    return 0


# --- fase B -------------------------------------------------------------------------------------

def exigir_sin_red() -> None:
    hosts = {("pypi.org", 443), ("files.pythonhosted.org", 443)}
    p = urlparse(INDICE)
    if p.hostname:
        hosts.add((p.hostname, p.port or (443 if p.scheme == "https" else 80)))
    for host, puerto in sorted(hosts):
        try:
            socket.create_connection((host, puerto), timeout=3).close()
        except OSError:
            continue
        raise ErrorWheelhouse(f"Hay red (se pudo conectar a {host}:{puerto}). La fase B se corre con "
                              "docker run --network none: así ninguna construcción puede descargar nada.")


def correr(args: list[str], log: Path) -> subprocess.CompletedProcess:
    entorno = {**os.environ, "PIP_NO_INDEX": "1", "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    r = subprocess.run(args, capture_output=True, text=True, env=entorno)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"$ {' '.join(args)}\n{r.stdout}{r.stderr}\n[código {r.returncode}]\n")
    return r


def resumen_error(texto: str) -> str:
    """La línea que explica la falla: la última excepción de Python, o el último mensaje de pip."""
    excepciones = [x.strip() for x in texto.splitlines() if re.match(r"\s*[A-Za-z_.]*(Error|Exception): ", x)]
    if excepciones:
        return excepciones[-1]
    for linea in texto.splitlines():
        if "No matching distribution" in linea or "Could not find" in linea:
            return linea.strip()
    lineas = [x for x in texto.strip().splitlines() if x.strip()]
    return lineas[-1].strip() if lineas else "sin mensaje"


def preparar_entorno(construccion: dict, base: Path, log: Path) -> Path:
    """Entorno virtual nuevo con SOLO las herramientas fijadas, instaladas desde la carpeta."""
    venv = base / "venv-construccion"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / "bin" / "python"
    r = correr([str(py), "-m", "pip", "install", "--no-index", f"--find-links={CARPETA.raiz}", "--only-binary=:all:",
                "-r", "docker/requisitos-construccion.txt"], log)
    if r.returncode:
        raise ErrorWheelhouse(f"no se pudieron instalar las herramientas de construcción desde la carpeta: "
                              f"{resumen_error(r.stdout + r.stderr)} (detalle en {log})")
    lista = json.loads(subprocess.run([str(py), "-m", "pip", "list", "--format=json"], capture_output=True,
                                      text=True, check=True).stdout)
    instaladas = {canonicalize_name(p["name"]): p["version"] for p in lista}
    for canon, (nombre, version) in construccion.items():
        if canon not in instaladas or Version(instaladas[canon]) != Version(version):
            raise ErrorWheelhouse(f"en el entorno de construcción quedó {nombre} "
                                  f"{instaladas.get(canon, 'sin instalar')} y se fijó {version}")
    if correr([str(py), "-m", "pip", "check"], log).returncode:
        raise ErrorWheelhouse(f"pip check falló en el entorno de construcción (detalle en {log})")
    return py


def construir_uno(py: Path, nombre: str, version: str, fuente_reg: dict, construccion: dict, prio: dict,
                  base: Path) -> str:
    fuente = CARPETA.ruta_final(fuente_reg)
    if sha256(fuente) != fuente_reg["sha256"] or fuente_reg["sha256"] != fuente_reg["sha256_indice"]:
        raise Pendiente(f"{fuente.name}: su SHA-256 no es el registrado; repita la fase A")
    errores = comprobar_herramientas(build_requires_fuente(fuente), construccion, fuente.name)
    if errores:
        raise Pendiente("; ".join(errores))
    log = CARPETA.registros_log / f"{canonicalize_name(nombre)}-{version}.log"
    salida = base / f"construida-{canonicalize_name(nombre)}"
    salida.mkdir()
    r = correr([str(py), "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index",
                "--use-pep517", "--check-build-dependencies", "-w", str(salida), str(fuente)], log)
    if r.returncode:
        raise Pendiente(f"no se pudo construir sin red: {resumen_error(r.stdout + r.stderr)} (detalle en {log})")
    ruedas = list(salida.glob("*.whl"))
    if len(ruedas) != 1:
        raise Pendiente(f"se esperaba 1 rueda y salieron {len(ruedas)} (detalle en {log})")
    rueda = ruedas[0]
    n, v = nombre_version_de_archivo(rueda.name)
    if canonicalize_name(n) != canonicalize_name(nombre) or Version(v) != Version(version):
        raise Pendiente(f"la rueda construida es {rueda.name}, no {nombre}=={version}")
    if prioridad_rueda(rueda.name, prio) is None:
        raise Pendiente(f"la rueda construida {rueda.name} no es instalable en este intérprete")
    temporal = CARPETA.tmp / rueda.name
    shutil.copyfile(rueda, temporal)     # el directorio temporal puede estar en otro disco
    reg = {"tipo": "rueda", "origen": "construida", "proyecto": nombre, "version": version,
           "archivo": rueda.name, "sha256": sha256(temporal), "fuente": fuente.name,
           "sha256_fuente": fuente_reg["sha256"],
           "herramientas": {c: v for c, (_, v) in sorted(construccion.items())}}
    CARPETA.instalar_en_su_lugar(temporal, reg)
    return f"construida  {rueda.name} (desde {fuente.name})"


def fase_b() -> int:
    exigir_plataforma()
    exigir_sin_red()
    _, construccion, todas = leer_listas()
    CARPETA.crear()
    estado, avisos = CARPETA.revisar(todas, mover=True)
    for a in avisos:
        print("aviso:", a)
    sin_fuente = [f"{todas[c][0]}=={todas[c][1]}" for c, e in estado.items() if not e["rueda"] and not e["fuente"]]
    if sin_fuente:
        print(f"Aviso: {len(sin_fuente)} versiones no tienen ni rueda ni fuente (falta la fase A): "
              + ", ".join(sin_fuente[:10]) + (" …" if len(sin_fuente) > 10 else ""))
    por_construir = sorted(c for c, e in estado.items() if e["fuente"] and not e["rueda"])
    if not por_construir:
        print("No hay fuentes por construir.")
        return 2 if sin_fuente else 0
    prio = prioridades()
    pendientes = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        py = preparar_entorno(construccion, base, CARPETA.registros_log / "entorno-construccion.log")
        for canon in por_construir:
            nombre, version = todas[canon]
            try:
                print(construir_uno(py, nombre, version, estado[canon]["fuente"], construccion, prio, base),
                      flush=True)
            except Pendiente as e:
                print(f"PENDIENTE   {nombre}=={version}: {e}", flush=True)
                pendientes.append(f"{nombre}=={version}")
    print(f"\nfase B: {len(por_construir) - len(pendientes)} construidas, {len(pendientes)} pendientes.")
    if pendientes or sin_fuente:
        return 2
    print("Siguiente: verificar_wheelhouse.py.")
    return 0


# --- estado -------------------------------------------------------------------------------------

def estado_cmd() -> int:
    _, _, todas = leer_listas()
    CARPETA.crear()
    estado, avisos = CARPETA.revisar(todas, mover=False)
    for a in avisos:
        print("aviso:", a)
    cuenta = {"rueda de PyPI": 0, "rueda construida": 0, "fuente sin construir (fase B)": 0, "falta (fase A)": 0}
    for canon, e in sorted(estado.items()):
        if e["rueda"]:
            clave = "rueda de PyPI" if e["rueda"]["origen"] == "pypi" else "rueda construida"
        elif e["fuente"]:
            clave = "fuente sin construir (fase B)"
        else:
            clave = "falta (fase A)"
            print(f"falta       {todas[canon][0]}=={todas[canon][1]}")
        cuenta[clave] += 1
    for k, v in cuenta.items():
        print(f"{v:5d}  {k}")
    parciales = list(CARPETA.tmp.glob("*.part"))
    if parciales:
        print(f"{len(parciales)} descargas a medias en .tmp/ (se reanudan en la fase A)")
    return 0 if cuenta["falta (fase A)"] == cuenta["fuente sin construir (fase B)"] == 0 else 2


@contextlib.contextmanager
def bloqueo():
    """Una sola ejecución a la vez sobre la carpeta (dos a la vez se pisarían las descargas)."""
    CARPETA.crear()
    ruta = CARPETA.tmp / "BLOQUEO"
    try:
        fd = os.open(ruta, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise ErrorWheelhouse(
            f"Hay otra ejecución trabajando sobre {CARPETA.raiz}/ ({ruta.read_text(errors='replace').strip()}). "
            f"Si no la hay (p. ej. se apagó el equipo), mueva {ruta} fuera de la carpeta y repita.") from None
    with os.fdopen(fd, "w") as f:
        f.write(f"iniciada {time.strftime('%Y-%m-%d %H:%M:%S')} en {socket.gethostname()}\n")
    try:
        yield
    finally:
        ruta.unlink(missing_ok=True)   # solo el archivo de bloqueo que esta misma ejecución creó


def main(argv: list[str]) -> int:
    comandos = {"descargar": fase_a, "construir": fase_b, "estado": estado_cmd}
    if len(argv) != 1 or argv[0] not in comandos:
        print(__doc__)
        return 1
    try:
        if argv[0] == "estado":
            return estado_cmd()
        with bloqueo():
            return comandos[argv[0]]()
    except ErrorWheelhouse as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
