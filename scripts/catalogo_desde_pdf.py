"""
CLI del conversor de PDFs de existencias → CSV de catálogo.

    python -m scripts.catalogo_desde_pdf a.pdf [b.pdf ...] -o data/catalogo_pdf.csv

La lógica vive en app/services/catalogo_pdf.py (la usa también el endpoint
POST /bo/sku/import-pdf del backoffice).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.catalogo_pdf import convertir  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("-o", "--salida", default="data/catalogo_pdf.csv")
    args = ap.parse_args()
    for nombre, n in convertir(args.pdfs, args.salida).items():
        print(f"{nombre}: {n}")
    print(f"-> {args.salida}")
