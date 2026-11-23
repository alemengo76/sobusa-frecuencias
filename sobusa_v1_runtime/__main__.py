"""Ejecuta una predicción puntual y devuelve el contrato común en JSON."""
import argparse
import json

from .interfaz import predecir


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--fecha", required=True)
    cli.add_argument("--rutas", nargs="+", type=int, default=[2, 16, 21])
    cli.add_argument("--horizontes", nargs="+", type=int, default=[30, 60, 120])
    cli.add_argument("--modo", required=True, choices=["sintetico", "retrospectivo"])
    args = cli.parse_args()
    resultado = predecir(args.fecha, args.rutas, args.horizontes, modo=args.modo)
    print(json.dumps(resultado, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
