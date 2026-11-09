#!/bin/sh
# Instala el proyecto SIN red desde un wheelhouse verificado (lo llama el Dockerfile con
# PIP_MODO=wheelhouse). Uso: instalar_wheelhouse.sh <carpeta wheelhouse> <carpeta con listas y scripts>
# Ver docs/WHEELHOUSE.md.
set -eu
W="$1"
L="$2"
export PIP_NO_INDEX=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

echo "[wheelhouse 1/5] integridad: SHA256SUMS contra los archivos"
if [ ! -s "$W/SHA256SUMS" ]; then
  echo "Falta $W/SHA256SUMS: corra scripts/verificar_wheelhouse.py (docs/WHEELHOUSE.md)." >&2
  exit 1
fi
(cd "$W" && sha256sum --check --strict --quiet SHA256SUMS)
(cd "$W" && ls -1 -- *.whl | sort) > /tmp/wh_hay
awk '{print $2}' "$W/SHA256SUMS" | sort > /tmp/wh_esperado
if ! diff /tmp/wh_esperado /tmp/wh_hay; then
  echo "Las ruedas de la carpeta no son exactamente las de SHA256SUMS (< falta, > sobra)." >&2
  exit 1
fi

echo "[wheelhouse 2/5] herramientas de construcción fijadas (requisitos-construccion.txt)"
python -m pip install --no-index --find-links="$W" --only-binary=:all: -r "$L/requisitos-construccion.txt"

echo "[wheelhouse 3/5] comprobando que las herramientas quedaron instaladas en la versión fijada"
python "$L/verificar_wheelhouse.py" --instalados "$L/requisitos-construccion.txt"

echo "[wheelhouse 4/5] proyecto, sin aislamiento de construcción ni índice"
python -m pip install --no-index --find-links="$W" --only-binary=:all: \
  --no-build-isolation --check-build-dependencies --use-pep517 \
  -c "$L/requisitos-congelados.txt" -c "$L/requisitos-construccion.txt" ".[dev]"

echo "[wheelhouse 5/5] pip check y versiones exactas"
python -m pip check
python "$L/verificar_wheelhouse.py" --instalados "$L/requisitos-congelados.txt" "$L/requisitos-construccion.txt"
