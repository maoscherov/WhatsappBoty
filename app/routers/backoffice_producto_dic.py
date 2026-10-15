"""
Diccionario de cada producto (15/10): ver y alimentar con qué palabras lo
encuentra el bot, sin esperar a una consulta fallida.

- GET  /bo/sku/{id}/diccionario   nombre del ERP, nombre legible, términos,
                                  abreviaturas que se le aplican, receta y
                                  "siempre derivar".
- PUT  /bo/sku/{id}/diccionario   edita nombre legible / términos / tipo y la
                                  marca "siempre derivar" (tinturas).
- GET  /bo/sku/{id}/probar?q=     en qué puesto aparece ese producto si un
                                  cliente escribe `q`.
Los cambios recargan el catálogo del bot en segundos.
"""

import logging
import re
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.routers.backoffice import _auth
from app.routers.backoffice_receta import _branch, _db, _programar_recarga
from app.services.sku_service import get_sku_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bo")


class DiccionarioIn(BaseModel):
    nombre_legible: Optional[str] = None
    terminos: Optional[Union[list[str], str]] = None
    tipo: Optional[str] = None
    siempre_derivar: Optional[bool] = None
    autor: Optional[str] = None


def _lista(terminos) -> list[str]:
    if terminos is None:
        return []
    partes = terminos if isinstance(terminos, list) else re.split(r"[,|\n]", terminos)
    vistos, out = set(), []
    for t in partes:
        t = " ".join(str(t).split()).strip().lower()
        if t and t not in vistos:
            vistos.add(t)
            out.append(t[:60])
    return out


async def _producto(db, branch: str, external_id: str) -> dict:
    fila = await db.fetchrow(
        """SELECT i.external_id, i.name, i.brand, i.stock, i.price, i.requiere_receta,
                  e.requiere_receta_override, coalesce(e.siempre_derivar, false) AS siempre_derivar,
                  a.nombre_base, a.nombre_legible, a.tipo, a.terminos, a.seguro, a.origen, a.autor,
                  a.updated_at AS alias_updated_at
             FROM catalog_items i
             LEFT JOIN catalog_extras e ON e.branch_id = i.branch_id AND e.external_id = i.external_id
             LEFT JOIN sku_alias a ON a.branch_id = i.branch_id AND a.external_id = i.external_id
            WHERE i.branch_id = $1 AND i.external_id = $2""", branch, external_id)
    if not fila:
        raise HTTPException(status_code=404, detail="No existe ese producto")
    return dict(fila)


def _abreviaturas(nombre: str) -> list[dict]:
    from app.services.catalogo_enriquecido import ABREVIATURAS
    out = []
    for tok in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]+", nombre or ""):
        exp = ABREVIATURAS.get(tok.lower())
        if exp and not any(o["sigla"] == tok.lower() for o in out):
            out.append({"sigla": tok.lower(), "significa": exp})
    return out


def _a_dict(p: dict) -> dict:
    vigente = bool(p.get("nombre_legible")) and \
        (p.get("nombre_base") or "").strip() == (p.get("name") or "").strip()
    return {
        "external_id": p["external_id"], "nombre_erp": p["name"], "marca": p.get("brand"),
        "stock": p.get("stock"), "precio": float(p["price"]) if p.get("price") is not None else None,
        "requiere_receta": p.get("requiere_receta_override") or p.get("requiere_receta"),
        "siempre_derivar": bool(p.get("siempre_derivar")),
        "nombre_legible": p.get("nombre_legible"), "tipo": p.get("tipo"),
        "terminos": _lista(p.get("terminos")),
        "alias_vigente": vigente,
        "alias_origen": p.get("origen"), "alias_autor": p.get("autor"),
        "alias_actualizado": p["alias_updated_at"].isoformat() if p.get("alias_updated_at") else None,
        "abreviaturas": _abreviaturas(p["name"]),
    }


@router.get("/sku/{external_id}/diccionario")
async def bo_sku_diccionario(external_id: str, _=Depends(_auth)):
    db, branch = _db(), await _branch()
    return _a_dict(await _producto(db, branch, external_id))


@router.put("/sku/{external_id}/diccionario")
async def bo_sku_diccionario_guardar(external_id: str, body: DiccionarioIn, _=Depends(_auth)):
    db, branch = _db(), await _branch()
    p = await _producto(db, branch, external_id)
    if any(v is not None for v in (body.nombre_legible, body.terminos, body.tipo)):
        legible = (body.nombre_legible if body.nombre_legible is not None
                   else p.get("nombre_legible") or p["name"]).strip()[:300]
        if not legible:
            raise HTTPException(status_code=422, detail="El nombre legible no puede quedar vacío")
        terminos = _lista(body.terminos) if body.terminos is not None else _lista(p.get("terminos"))
        tipo = (body.tipo if body.tipo is not None else p.get("tipo") or "").strip()[:80] or None
        # El alias vale para el nombre ACTUAL del ERP (si el ERP lo renombra,
        # deja de aplicarse y se ve como "no vigente").
        await db.execute(
            """INSERT INTO sku_alias (branch_id, external_id, nombre_base, nombre_legible, tipo,
                                      terminos, seguro, origen, autor, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, true, 'manual', $7, now())
               ON CONFLICT (branch_id, external_id) DO UPDATE SET
                   nombre_base = EXCLUDED.nombre_base, nombre_legible = EXCLUDED.nombre_legible,
                   tipo = EXCLUDED.tipo, terminos = EXCLUDED.terminos, seguro = true,
                   origen = 'manual', autor = EXCLUDED.autor, updated_at = now()""",
            branch, external_id, p["name"], legible, tipo, ", ".join(terminos)[:500] or None,
            (body.autor or "").strip() or None)
    if body.siempre_derivar is not None:
        await db.execute(
            """INSERT INTO catalog_extras (branch_id, external_id, siempre_derivar, updated_at)
               VALUES ($1, $2, $3, now())
               ON CONFLICT (branch_id, external_id) DO UPDATE SET
                   siempre_derivar = EXCLUDED.siempre_derivar, updated_at = now()""",
            branch, external_id, bool(body.siempre_derivar))
        # En memoria ya, sin esperar la recarga.
        sku = get_sku_service(get_settings().sku_csv_path).get_by_id(external_id)
        if sku is not None:
            try:
                sku.siempre_derivar = bool(body.siempre_derivar)
            except Exception:
                pass
    _programar_recarga(branch, {external_id})
    logger.info(f"Diccionario de {external_id} editado por {body.autor or '?'}")
    return _a_dict(await _producto(db, branch, external_id))


@router.get("/sku/{external_id}/probar")
async def bo_sku_probar(external_id: str, q: str = Query(..., min_length=2), _=Depends(_auth)):
    """Qué devuelve la búsqueda para `q` y en qué puesto queda este producto."""
    sku_svc = get_sku_service(get_settings().sku_csv_path)
    res = sku_svc.buscar(q, top_n=10)
    puesto = next((i + 1 for i, r in enumerate(res) if str(r["sku_id"]) == str(external_id)), None)
    return {"q": q, "puesto": puesto, "encontrado": puesto is not None,
            "resultados": [{"sku_id": r["sku_id"], "nombre": r["nombre"], "estado": r["estado"],
                            "precio": r["precio"]} for r in res]}
