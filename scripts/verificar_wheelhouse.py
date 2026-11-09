"""Valida wheelhouse/ antes de construir la imagen sin conexión. Ver docs/WHEELHOUSE.md.

Modo carpeta (después de las fases A y B; sin red, dentro de python:3.11-slim --platform linux/amd64):

  python scripts/verificar_wheelhouse.py            # solo con lo registrado
  python scripts/verificar_wheelhouse.py --pypi     # además vuelve a preguntar los SHA-256 a PyPI (con red)

  Comprueba, sin modificar nada:
    1. Cada versión de las dos listas tiene exactamente una rueda, y no sobra ninguna.
    2. Cada rueda es instalable en este intérprete (packaging.tags.sys_tags(), como pip).
    3. Integridad según ORIGEN.jsonl:
         rueda de PyPI    : SHA-256 del archivo = registrado = el que publicó el índice al bajarla.
         rueda construida : SHA-256 del archivo = registrado; su código fuente está en fuentes/ y su
                            SHA-256 = registrado = el que publicó el índice; fue construida con las
                            mismas herramientas fijadas que hay hoy en requisitos-construccion.txt.
    4. Las herramientas de construcción del proyecto (pyproject.toml) están fijadas con ==.
  Si todo está bien escribe wheelhouse/SHA256SUMS (el Dockerfile lo comprueba con sha256sum -c).
  Si algo falla no lo escribe, y uno anterior pasa a SHA256SUMS.invalido.

Modo instalados (lo usa el Dockerfile, después de pip install):

  python verificar_wheelhouse.py --instalados LISTA [LISTA ...]

  Cada paquete de las listas está instalado y en la versión exacta fijada.

Sale con código 1 si hay cualquier problema.
"""
from __future__ import annotations

import json
import os
import sys
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wheelhouse_comun import (  # noqa: E402
    Carpeta,
    ErrorWheelhouse,
    Version,
    abrir_url,
    comprobar_herramientas,
    exigir_plataforma,
    leer_lista,
    leer_listas,
    leer_pyproject_build_requires,
    prioridad_rueda,
    prioridades,
    sha256,
)

CARPETA = Carpeta(Path(os.environ.get("WHEELHOUSE_DIR", "wheelhouse")))
INDICE = os.environ.get("WHEELHOUSE_INDICE", "https://pypi.org/pypi").rstrip("/")


def publicados(nombre: str, version: str) -> dict[str, str]:
    with abrir_url(f"{INDICE}/{nombre}/{version}/json") as r:
        return {u["filename"]: u["digests"]["sha256"] for u in json.load(r)["urls"]}


def verificar_carpeta(consultar_pypi: bool) -> int:
    exigir_plataforma()
    _, construccion, todas = leer_listas()
    errores = comprobar_herramientas(
        leer_pyproject_build_requires(Path("pyproject.toml").read_text(encoding="utf-8"), "pyproject.toml"),
        construccion, "el proyecto (pyproject.toml)")
    if not CARPETA.raiz.is_dir():
        raise ErrorWheelhouse(f"No existe {CARPETA.raiz}/: corra primero preparar_wheelhouse.py descargar.")
    estado, problemas = CARPETA.revisar(todas, mover=False)
    errores += problemas
    prio = prioridades()
    herramientas_hoy = {c: v for c, (_, v) in sorted(construccion.items())}
    ruedas, n_pypi, n_construidas = [], 0, 0
    for canon, (nombre, version) in sorted(todas.items()):
        e = estado[canon]
        rueda = e["rueda"]
        if rueda is None:
            motivo = "hay código fuente sin construir (fase B)" if e["fuente"] else "corra la fase A"
            errores.append(f"falta la rueda de {nombre}=={version}: {motivo}")
            continue
        ruedas.append(rueda["archivo"])
        if prioridad_rueda(rueda["archivo"], prio) is None:
            errores.append(f"{rueda['archivo']} no es instalable en Python 3.11 / Linux x86_64")
        if rueda["origen"] == "pypi":
            n_pypi += 1
            if rueda["sha256"] != rueda.get("sha256_indice"):
                errores.append(f"{rueda['archivo']}: el SHA-256 registrado no es el que publicó el índice")
            oficiales = [(rueda["archivo"], rueda["sha256"])]
        else:
            n_construidas += 1
            fuente = e["fuente"]
            if fuente is None or fuente["archivo"] != rueda.get("fuente"):
                errores.append(f"{rueda['archivo']}: falta su código fuente {rueda.get('fuente')} en fuentes/")
                continue
            if not (fuente["sha256"] == fuente.get("sha256_indice") == rueda.get("sha256_fuente")):
                errores.append(f"{rueda['archivo']}: el SHA-256 de su fuente {fuente['archivo']} no coincide "
                               "entre el archivo, el registro y el índice")
            if rueda.get("herramientas") != herramientas_hoy:
                errores.append(f"{rueda['archivo']}: se construyó con otras herramientas que las fijadas hoy; "
                               "apártela (muévala fuera de la carpeta) y repita la fase B")
            oficiales = [(fuente["archivo"], fuente["sha256"])]   # la rueda construida no existe en PyPI
        if consultar_pypi:
            try:
                pub = publicados(nombre, version)
            except Exception as ex:  # noqa: BLE001 - cualquier falla de red es un error de verificación
                errores.append(f"no se pudo consultar el índice para {nombre}=={version}: {ex}")
                continue
            for archivo, h in oficiales:
                if pub.get(archivo) != h:
                    errores.append(f"{archivo}: el índice publica hoy otro SHA-256 ({pub.get(archivo)})")

    sumas = CARPETA.raiz / "SHA256SUMS"
    print(f"{len(ruedas)} ruedas ({n_pypi} de PyPI, {n_construidas} construidas desde su fuente); "
          f"{len(errores)} problemas")
    for e in errores:
        print("  ERROR:", e)
    if errores:
        if sumas.exists():
            os.replace(sumas, CARPETA.raiz / "SHA256SUMS.invalido")
            print("SHA256SUMS anterior -> SHA256SUMS.invalido (la imagen no se construye hasta corregir)")
        return 1
    temporal = CARPETA.raiz / "SHA256SUMS.tmp"
    temporal.write_text("".join(f"{sha256(CARPETA.raiz / a)}  {a}\n" for a in sorted(ruedas)), encoding="utf-8")
    os.replace(temporal, sumas)
    print(f"Listo: {sumas} escrito. Siguiente: docker compose build app con PIP_MODO=wheelhouse.")
    return 0


def verificar_instalados(listas: list[str]) -> int:
    pedidos: dict[str, tuple[str, str]] = {}
    for lista in listas:
        pedidos.update(leer_lista(Path(lista)))
    errores = []
    for nombre, version in sorted(pedidos.values()):
        try:
            hay = metadata.version(nombre)
        except metadata.PackageNotFoundError:
            errores.append(f"{nombre}=={version} no quedó instalado")
            continue
        if Version(hay) != Version(version):
            errores.append(f"{nombre}: instalado {hay}, fijado {version}")
    print(f"{len(pedidos)} paquetes fijados comprobados; {len(errores)} problemas")
    for e in errores:
        print("  ERROR:", e)
    return 1 if errores else 0


def main(argv: list[str]) -> int:
    try:
        if argv and argv[0] == "--instalados":
            if len(argv) < 2:
                print(__doc__)
                return 1
            return verificar_instalados(argv[1:])
        if argv not in ([], ["--pypi"]):
            print(__doc__)
            return 1
        return verificar_carpeta("--pypi" in argv)
    except ErrorWheelhouse as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
