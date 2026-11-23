"""Ejecuta el ejemplo en un kernel Jupyter real del entorno activo."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nbclient import NotebookClient
import nbformat


raiz = Path(__file__).resolve().parents[1]
with TemporaryDirectory(prefix="sobusa-jupyter-") as temporal:
    kernel = Path(temporal) / "kernels/sobusa-v1-worker"
    kernel.mkdir(parents=True)
    (kernel / "kernel.json").write_text(json.dumps(dict(
        argv=[sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        display_name="SOBUSA v1.0 (worker)", language="python")), encoding="utf8")
    notebook = nbformat.read(raiz / "ejemplos/inferencia_minima.ipynb", as_version=4)
    notebook.cells.append(nbformat.v4.new_code_cell(
        "print(json.dumps(resultado, ensure_ascii=True, allow_nan=False))"))
    with patch.dict(os.environ, {"JUPYTER_PATH": temporal}):
        NotebookClient(notebook, timeout=120).execute(cwd=str(raiz))
    texto = "".join(o.text for o in notebook.cells[-1].outputs
                    if o.output_type == "stream" and o.name == "stdout")
    print(json.dumps(json.loads(texto), ensure_ascii=True, allow_nan=False))
