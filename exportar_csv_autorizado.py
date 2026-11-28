"""Exporta todas las filas de un CSV expresamente autorizado, sin leer la base original."""
import argparse
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from pathlib import Path
import tempfile

FIELDS = (
    "alert_id", "codigo", "severidad", "estado", "idruta", "franja_inicio",
    "horizonte_min", "valor_observado", "valor_esperado", "umbral",
    "puntuacion_anomalia", "explicacion", "recomendacion",
)
TEXT = {"codigo", "severidad", "estado", "explicacion", "recomendacion"}


def safe_cell(field, value):
    if field in TEXT and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--csv-autorizado", required=True, type=Path)
    cli.add_argument("--salida", required=True, type=Path)
    args = cli.parse_args()
    if args.salida.exists():
        cli.error("La salida debe ser un archivo nuevo.")
    written = 0
    ids = set()
    with args.csv_autorizado.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != FIELDS:
            cli.error("La cabecera debe coincidir exactamente con la plantilla.")
        temp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                           dir=args.salida.parent, delete=False)
        try:
            with temp:
                writer = csv.writer(temp)
                writer.writerow(FIELDS)
                for row in reader:
                    if None in row or any(row[field] is None for field in FIELDS):
                        cli.error(f"Fila {reader.line_num}: columnas incompletas o adicionales.")
                    try:
                        ident = int(row["alert_id"])
                        if ident <= 0 or int(row["idruta"]) <= 0:
                            raise ValueError
                        if row["horizonte_min"]:
                            int(row["horizonte_min"])
                        for field in ("valor_observado", "valor_esperado", "umbral", "puntuacion_anomalia"):
                            if row[field] and not Decimal(row[field]).is_finite():
                                raise ValueError
                        if row["franja_inicio"] and datetime.fromisoformat(row["franja_inicio"]).utcoffset() is None:
                            raise ValueError
                        if (not re.fullmatch(r"ALT-[0-9]{3}", row["codigo"])
                                or row["estado"] not in {"abierta", "reconocida", "asignada", "cerrada", "descartada"}
                                or row["severidad"] not in {"baja", "media", "alta"}):
                            raise ValueError
                    except (ValueError, InvalidOperation):
                        cli.error(f"Fila {reader.line_num}: ID, fecha, número o categoría inválidos.")
                    if ident in ids:
                        cli.error(f"Fila {reader.line_num}: ID duplicado.")
                    ids.add(ident)
                    writer.writerow([safe_cell(field, row[field]) for field in FIELDS])
                    written += 1
            if not written:
                cli.error("El archivo autorizado no contiene filas.")
            with args.salida.open("xb") as out, Path(temp.name).open("rb") as src:
                while chunk := src.read(65536):
                    out.write(chunk)
        finally:
            Path(temp.name).unlink(missing_ok=True)
    print(f"{written} filas autorizadas exportadas a {args.salida.resolve()}")


if __name__ == "__main__":
    main()
