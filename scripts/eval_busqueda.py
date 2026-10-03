"""
Mide la búsqueda de productos contra un set de casos reales (2/10).

Uso:
    python scripts/eval_busqueda.py CATALOGO.pkl [--diccionario base|tabla|todo]
        [--casos scripts/busqueda_casos.json] [--detalle]

CATALOGO.pkl: copia del catálogo de la sucursal ({"rows", "extras",
"diccionario"}), la que arma un dump de solo lectura de catalog_items.

--diccionario:
    base   solo la lista del código (como estaba antes de la tabla)
    tabla  la tabla tal cual (activas)
    todo   la tabla con las PROPUESTAS también activas (qué ganaríamos)

Acierto@1: el primer resultado es correcto. Acierto@3: alguno de los tres
primeros (el bot muestra hasta 3).
"""

import argparse
import json
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import diccionario_service as dic  # noqa: E402
from app.services.sku_service import SKUService  # noqa: E402


def _semilla() -> list[dict]:
    """Sin tabla en la copia (migración no aplicada): la semilla de la 0014."""
    import importlib.util
    ruta = os.path.join(os.path.dirname(__file__), "..", "migrations", "versions",
                        "0014_catalogo_diccionario.py")
    spec = importlib.util.spec_from_file_location("m0014", ruta)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    filas = [{"tipo": "abreviatura", "termino": t, "equivale": e, "estado": "activa"}
             for t, e in m._BASE_ABREV.items()]
    filas += [{"tipo": "sinonimo", "termino": t, "equivale": e, "estado": "activa"}
              for t, e in m._BASE_SINON.items()]
    filas += [{"tipo": "abreviatura", "termino": t, "equivale": e, "estado": "propuesta"}
              for t, e, _ in m._PROPUESTAS_ABREV]
    filas += [{"tipo": "sinonimo", "termino": t, "equivale": e, "estado": "propuesta"}
              for t, e, _ in m._PROPUESTAS_SINON]
    return filas


def correcto(nombre: str, debe: list[list[str]]) -> bool:
    n = (nombre or "").lower()
    return any(all(f.lower() in n for f in alternativa) for alternativa in debe)


def evaluar(svc: SKUService, casos: list[dict]) -> dict:
    res = []
    for c in casos:
        top = svc.buscar(c["q"], top_n=3)
        nombres = [r["nombre"] for r in top]
        a1 = bool(nombres) and correcto(nombres[0], c["debe"])
        a3 = any(correcto(n, c["debe"]) for n in nombres)
        res.append({**c, "top": nombres, "a1": a1, "a3": a3})
    n = len(res) or 1
    return {"casos": res, "acierto1": sum(r["a1"] for r in res) / n,
            "acierto3": sum(r["a3"] for r in res) / n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("catalogo")
    ap.add_argument("--diccionario", default="tabla", choices=["base", "tabla", "todo"])
    ap.add_argument("--casos", default=os.path.join(os.path.dirname(__file__), "busqueda_casos.json"))
    ap.add_argument("--detalle", action="store_true")
    a = ap.parse_args()

    with open(a.catalogo, "rb") as f:
        data = pickle.load(f)
    filas = data.get("diccionario") or [] or _semilla()
    if a.diccionario == "base":
        filas = []
    elif a.diccionario == "todo":
        filas = [dict(f, estado="activa") if f["estado"] == "propuesta" else f for f in filas]
    dic.aplicar(filas)
    svc = SKUService.from_rows(data["rows"], data.get("extras"))
    casos = json.load(open(a.casos, encoding="utf-8"))["casos"]
    r = evaluar(svc, casos)

    print(f"Diccionario: {a.diccionario} | {len(casos)} casos | "
          f"acierto@1 {r['acierto1']:.0%} | acierto@3 {r['acierto3']:.0%}")
    for origen in sorted({c["origen"] for c in r["casos"]}):
        sub = [c for c in r["casos"] if c["origen"] == origen]
        print(f"  {origen:10s} {len(sub):3d} casos  @1 {sum(c['a1'] for c in sub)/len(sub):.0%}"
              f"  @3 {sum(c['a3'] for c in sub)/len(sub):.0%}")
    if a.detalle:
        for c in r["casos"]:
            if not c["a3"]:
                print(f"  ✗ {c['q']!r}: {c['top']}")


if __name__ == "__main__":
    main()
