"""
Relevamiento de SOLO LECTURA de la API REST Mercurio v1 (Mascotas del Oeste).

Responde empíricamente lo que la documentación no detalla: esquema del
artículo, precios, forma del stock por depósito y catálogos. No crea nada.

Uso:  MERCURIO_API_KEY=mrc_... python scripts/mercurio_explorar.py [--out DIR]
      (o con la clave en .env). Guarda cada respuesta cruda como JSON en DIR
      (default: scratch/mercurio/) y resume el esquema por consola.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = "https://api.mercurio.com.ar/v1"
CATALOGOS = ["rubros", "marcas", "materiales", "grupos", "subgrupos",
             "tamanios-mascota", "edades-mascota"]


def _clave() -> str:
    k = os.environ.get("MERCURIO_API_KEY", "").strip()
    if not k and Path(".env").exists():
        for line in Path(".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("MERCURIO_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not k:
        sys.exit("Falta MERCURIO_API_KEY (entorno o .env)")
    return k


def _get(client: httpx.Client, path: str, out: Path, nombre: str):
    t = time.perf_counter()
    r = client.get(f"{BASE}{path}")
    ms = int((time.perf_counter() - t) * 1000)
    body = None
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:2000]}
    (out / f"{nombre}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"GET {path:45} → {r.status_code} en {ms} ms  (guardado {nombre}.json)")
    if r.status_code != 200:
        print("   cuerpo:", json.dumps(body, ensure_ascii=False)[:300])
    return r.status_code, body


def _esquema(obj, prefijo="", nivel=0):
    """Imprime claves y tipos (recursivo, acotado) de un dict de ejemplo."""
    if not isinstance(obj, dict) or nivel > 3:
        return
    for k, v in obj.items():
        tipo = type(v).__name__
        ejemplo = "" if isinstance(v, (dict, list)) else f" = {json.dumps(v, ensure_ascii=False)[:60]}"
        print(f"   {'  ' * nivel}{prefijo}{k}: {tipo}{ejemplo}")
        if isinstance(v, dict):
            _esquema(v, "", nivel + 1)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            print(f"   {'  ' * nivel}  [0] →")
            _esquema(v[0], "", nivel + 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="scratch/mercurio")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    client = httpx.Client(headers={"Authorization": f"Bearer {_clave()}",
                                   "Accept": "application/json"}, timeout=60)

    print("== Estado (sin auth)")
    print("  ", httpx.get(f"{BASE}/estado", timeout=15).json())

    print("\n== Catálogos")
    for c in CATALOGOS:
        st, body = _get(client, f"/{c}", out, f"catalogo_{c}")
        if st == 200 and isinstance(body, dict):
            items = body.get("items") or []
            print(f"   cantidad={body.get('cantidad')}  ejemplo={json.dumps(items[0], ensure_ascii=False) if items else '-'}")

    print("\n== Artículos: cantidad de páginas")
    st, body = _get(client, "/articulos/paginas", out, "articulos_paginas")
    print("  ", body)

    print("\n== Artículos: página 1 (esquema del primer artículo)")
    st, body = _get(client, "/articulos?pagina=1", out, "articulos_pagina1")
    arts = (body or {}).get("articulos") or []
    print(f"   cantidad en página: {len(arts)}")
    if arts:
        _esquema(arts[0])
        print("\n   Tres ejemplos completos:")
        for a in arts[:3]:
            print("   ", json.dumps(a, ensure_ascii=False)[:400])
        # ¿qué campos parecen precio/stock/código de barras?
        claves = set()
        for a in arts:
            claves |= set(a.keys())
        pistas = [k for k in sorted(claves) if any(s in k.lower() for s in
                  ("prec", "pric", "stock", "barra", "ean", "codigo", "cod", "activo", "baja", "imagen", "foto", "iva"))]
        print("\n   Claves con pinta de precio/stock/código/imagen:", pistas)

        print("\n== Stock del primer artículo")
        cand = arts[0]
        id_art = cand.get("id_articulo_mercurio") or cand.get("id_articulo") or cand.get("id")
        if id_art is not None:
            st, body = _get(client, f"/articulos/{id_art}/stock", out, "stock_primer_articulo")
            print("  ", json.dumps(body, ensure_ascii=False)[:600])
        else:
            print("   no encontré el id del artículo en las claves:", sorted(cand.keys()))

    print(f"\nRespuestas crudas en {out.resolve()}")


if __name__ == "__main__":
    main()
