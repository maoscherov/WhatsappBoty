# Catálogo Enriquecido + Oferta Determinista — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminar de raíz las equivocaciones de producto: (Fase 1) enriquecer el catálogo con nombres expandidos y sinónimos generados por LLM en batch, y (Fase 2) redactar las ofertas de producto desde el estado de la conversación (código) en vez de inferirlas de la redacción del modelo.

**Architecture:** La Fase 1 agrega un archivo `data/catalogo_enriquecido.csv` (generado offline por `scripts/enriquecer_catalogo.py`, cacheado por hash de nombre) que `SKUService` fusiona al índice de búsqueda; si el archivo no existe, todo funciona como hoy. La Fase 2 invierte el flujo de oferta en `webhook.py`: cuando hay resultados de catálogo y la intención es de compra, la respuesta se construye con plantillas desde los registros del catálogo (`oferta_helper.py`) y el pending se setea en el mismo acto — el texto y el estado no pueden divergir porque salen del mismo dato.

**Tech Stack:** Python 3.11+, FastAPI, rapidfuzz, Anthropic/OpenAI SDK (batch de enriquecimiento), pytest (los tests de lógica corren sin DB: `pytest tests/test_logic.py`).

**Spec:** No hay documento de spec separado; la especificación es la conversación con Mariano (2026-08-19): causa raíz 1 = catálogo con nombres crípticos ("SEDAL CERAMICAS SHA"), causa raíz 3 = el LLM redacta la oferta y el sistema infiere el producto desde el texto (familia de bugs: Framintrol→Gillette, tintura ceniza→L'Oréal, "perfecto rubio oscuro"→opción 1). Orden acordado: primero enriquecer catálogo, después oferta desde estado.

## Global Constraints

- Repo: `D:\Dev\WhatsappBOTy`, branch `develop` (Railway auto-deploya `develop`; **no** tocar `master`).
- Los tests existentes (178 en verde al momento del plan) deben seguir pasando; los que asserten el comportamiento viejo de oferta se ACTUALIZAN, no se borran.
- Invariante del negocio: **el precio que el bot dice es el que cobra**. La Fase 2 lo garantiza por construcción; los guards existentes (`producto_respaldado`, `_espera_eleccion`) quedan como red de seguridad, no se eliminan.
- Sin el archivo de enriquecimiento el bot debe funcionar EXACTAMENTE como hoy (deploy seguro antes de correr el batch).
- El script de enriquecimiento nunca corre en el request path: es batch offline (local o manual), su output se commitea como CSV.
- Mensajes al cliente: español rioplatense, voseo, tono actual del bot (emojis moderados).
- Commits chicos y frecuentes; mensajes en español como los del repo (`git log`: "Fix: la lista de opciones ya no deja la primera como pendiente").

---

## Fase 1 — Catálogo enriquecido

### Task 1: Módulo de enriquecimiento con expansión determinista de abreviaturas

Las abreviaturas de góndola más comunes se expanden SIN LLM (gratis, sin riesgo). El LLM (Task 2) sólo agrega lo que un diccionario no puede (tipo de producto, variante, sinónimos coloquiales).

**Files:**
- Create: `app/services/catalogo_enriquecido.py`
- Test: `tests/test_logic.py` (agregar clase `TestCatalogoEnriquecido` al final del archivo)

**Interfaces:**
- Produces: `expandir_abreviaturas(nombre: str) -> str`; clase `CatalogoEnriquecido(path: str)` con `.get(sku_id: str) -> Optional[dict]` (claves: `nombre_expandido`, `tipo`, `variante`, `sinonimos` — sinonimos es `list[str]`), `.texto_busqueda(sku_id: str) -> str` (string listo para concatenar al índice, `""` si el sku no está), `.total: int`. Constructor con path inexistente → objeto vacío sin error.
- Consumes: nada (módulo hoja).

- [ ] **Step 1: Escribir los tests que fallan**

En `tests/test_logic.py`, al final:

```python
class TestCatalogoEnriquecido:
    def test_expandir_abreviaturas(self):
        from app.services.catalogo_enriquecido import expandir_abreviaturas
        assert "shampoo" in expandir_abreviaturas("SEDAL CERAMICAS SHA").lower()
        assert "acondicionador" in expandir_abreviaturas("SEDAL RIZOS ACO x 340").lower()
        assert "jabon" in expandir_abreviaturas("DOVE JAB BLANCO x90").lower()
        assert "desodorante" in expandir_abreviaturas("REXONA DES AER MEN").lower()
        assert "comprimidos" in expandir_abreviaturas("TAFIROL 1G COMP X16").lower()
        # El nombre original se conserva (se AGREGA la expansión, no se reemplaza)
        assert "SEDAL" in expandir_abreviaturas("SEDAL CERAMICAS SHA")

    def test_expandir_sin_abreviaturas_devuelve_igual(self):
        from app.services.catalogo_enriquecido import expandir_abreviaturas
        assert expandir_abreviaturas("IBUPIRAC 400") == "IBUPIRAC 400"

    def test_catalogo_enriquecido_carga_csv(self, tmp_path):
        from app.services.catalogo_enriquecido import CatalogoEnriquecido
        p = tmp_path / "enriquecido.csv"
        p.write_text(
            "sku_id,hash_nombre,nombre_expandido,tipo,variante,sinonimos\n"
            '123,abc,"Shampoo Sedal Ceramidas 340ml",shampoo,ceramidas,"crema de enjuague|pelo"\n',
            encoding="utf-8",
        )
        ce = CatalogoEnriquecido(str(p))
        assert ce.total == 1
        info = ce.get("123")
        assert info["tipo"] == "shampoo"
        assert info["sinonimos"] == ["crema de enjuague", "pelo"]
        texto = ce.texto_busqueda("123")
        assert "shampoo" in texto and "ceramidas" in texto and "crema de enjuague" in texto

    def test_catalogo_enriquecido_path_inexistente(self):
        from app.services.catalogo_enriquecido import CatalogoEnriquecido
        ce = CatalogoEnriquecido("data/no_existe_123.csv")
        assert ce.total == 0
        assert ce.get("123") is None
        assert ce.texto_busqueda("123") == ""
```

- [ ] **Step 2: Correr los tests y verificar que fallan**

Run: `cd D:\Dev\WhatsappBOTy && python -m pytest tests/test_logic.py::TestCatalogoEnriquecido -v`
Expected: FAIL con `ModuleNotFoundError: app.services.catalogo_enriquecido`

- [ ] **Step 3: Implementar el módulo**

Crear `app/services/catalogo_enriquecido.py`:

```python
"""
Enriquecimiento del catálogo: nombres de góndola → texto buscable.

Dos capas:
  1. expandir_abreviaturas(): diccionario determinista (SHA→shampoo, ACO→
     acondicionador...). Corre siempre, gratis, sin LLM.
  2. CatalogoEnriquecido: carga data/catalogo_enriquecido.csv generado por
     scripts/enriquecer_catalogo.py (batch LLM offline). Si el archivo no
     existe, el servicio queda vacío y el bot funciona como siempre.
"""

import csv
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Abreviaturas de góndola → palabra completa. La clave se matchea como token
# aislado (case-insensitive). La expansión se AGREGA al final del nombre para
# que el índice contenga ambas formas.
ABREVIATURAS: dict[str, str] = {
    "sha": "shampoo",
    "shamp": "shampoo",
    "aco": "acondicionador",
    "acond": "acondicionador",
    "jab": "jabon",
    "des": "desodorante",
    "som": "sombra",
    "comp": "comprimidos",
    "cap": "capsulas",
    "caps": "capsulas",
    "sol": "solucion",
    "susp": "suspension",
    "iny": "inyectable",
    "aer": "aerosol",
    "loc": "locion",
    "cr": "crema",
    "ung": "unguento",
    "past": "pastillas",
    "grag": "grageas",
}

_TOKEN_RE = re.compile(r"[a-záéíóúñA-ZÁÉÍÓÚÑ0-9]+")


def expandir_abreviaturas(nombre: str) -> str:
    """
    Devuelve el nombre con las expansiones agregadas al final (el original se
    conserva intacto: el índice necesita las dos formas). Si no hay ninguna
    abreviatura conocida, devuelve el nombre tal cual.
    """
    extras: list[str] = []
    for tok in _TOKEN_RE.findall(nombre or ""):
        exp = ABREVIATURAS.get(tok.lower())
        if exp and exp not in extras:
            extras.append(exp)
    return f"{nombre} {' '.join(extras)}" if extras else nombre


class CatalogoEnriquecido:
    """Índice sku_id → datos enriquecidos por el batch LLM."""

    def __init__(self, path: str):
        self._por_sku: dict[str, dict] = {}
        p = Path(path)
        if not p.exists():
            logger.info(f"Catálogo enriquecido no encontrado ({path}) — búsqueda sin enriquecer")
            return
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sku_id = (row.get("sku_id") or "").strip()
                if not sku_id:
                    continue
                self._por_sku[sku_id] = {
                    "hash_nombre": (row.get("hash_nombre") or "").strip(),
                    "nombre_expandido": (row.get("nombre_expandido") or "").strip(),
                    "tipo": (row.get("tipo") or "").strip(),
                    "variante": (row.get("variante") or "").strip(),
                    "sinonimos": [s.strip() for s in (row.get("sinonimos") or "").split("|") if s.strip()],
                }
        logger.info(f"Catálogo enriquecido: {len(self._por_sku)} productos desde {path}")

    def get(self, sku_id: str) -> Optional[dict]:
        return self._por_sku.get(sku_id)

    def texto_busqueda(self, sku_id: str) -> str:
        info = self._por_sku.get(sku_id)
        if not info:
            return ""
        partes = [info["nombre_expandido"], info["tipo"], info["variante"]] + info["sinonimos"]
        return " ".join(p for p in partes if p).lower()

    @property
    def total(self) -> int:
        return len(self._por_sku)
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `python -m pytest tests/test_logic.py::TestCatalogoEnriquecido -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/catalogo_enriquecido.py tests/test_logic.py
git commit -m "Catálogo enriquecido: expansión de abreviaturas y carga del CSV de enriquecimiento"
```

---

### Task 2: Script batch de enriquecimiento por LLM

Genera `data/catalogo_enriquecido.csv` a partir del catálogo activo. Cachea por hash del nombre: re-correrlo tras un cambio de CSV solo enriquece los productos nuevos o renombrados.

**Files:**
- Create: `scripts/enriquecer_catalogo.py`
- Test: `tests/test_logic.py` (clase `TestEnriquecerCatalogo` — se testea el armado de prompt/parseo y el cache, NO la llamada real al LLM)

**Interfaces:**
- Consumes: `SKUService` (`get_sku_service(csv_path)`, atributo `_skus: list[SKU]`), settings (`anthropic_api_key`, `sku_csv_path`) de `app.config`.
- Produces: CSV `data/catalogo_enriquecido.csv` con columnas exactas `sku_id,hash_nombre,nombre_expandido,tipo,variante,sinonimos` (sinonimos separados por `|`). Funciones testeables: `hash_nombre(nombre: str) -> str` (sha1 hex corto, 12 chars), `parsear_respuesta(texto: str) -> list[dict]`, `armar_prompt(lote: list[dict]) -> str`, `cargar_cache(path: str) -> dict[str, dict]` (clave sku_id).

- [ ] **Step 1: Escribir los tests que fallan**

```python
class TestEnriquecerCatalogo:
    def test_hash_nombre_estable(self):
        from scripts.enriquecer_catalogo import hash_nombre
        assert hash_nombre("SEDAL CERAMICAS SHA") == hash_nombre("SEDAL CERAMICAS SHA")
        assert hash_nombre("SEDAL CERAMICAS SHA") != hash_nombre("SEDAL RIZOS SHA")
        assert len(hash_nombre("x")) == 12

    def test_parsear_respuesta_json_valido(self):
        from scripts.enriquecer_catalogo import parsear_respuesta
        texto = '''[
          {"sku_id": "1", "nombre_expandido": "Shampoo Sedal Ceramidas 340ml",
           "tipo": "shampoo", "variante": "ceramidas",
           "sinonimos": ["crema de enjuague"]}
        ]'''
        out = parsear_respuesta(texto)
        assert out[0]["sku_id"] == "1"
        assert out[0]["sinonimos"] == ["crema de enjuague"]

    def test_parsear_respuesta_con_fences(self):
        from scripts.enriquecer_catalogo import parsear_respuesta
        texto = '```json\n[{"sku_id": "9", "nombre_expandido": "X", "tipo": "", "variante": "", "sinonimos": []}]\n```'
        assert parsear_respuesta(texto)[0]["sku_id"] == "9"

    def test_parsear_respuesta_invalida_devuelve_vacio(self):
        from scripts.enriquecer_catalogo import parsear_respuesta
        assert parsear_respuesta("no soy json") == []

    def test_armar_prompt_incluye_nombres(self):
        from scripts.enriquecer_catalogo import armar_prompt
        lote = [{"sku_id": "1", "nombre": "SEDAL CERAMICAS SHA", "marca": "Sedal", "categoria": "Perfumería"}]
        p = armar_prompt(lote)
        assert "SEDAL CERAMICAS SHA" in p and '"sku_id"' in p

    def test_cargar_cache(self, tmp_path):
        from scripts.enriquecer_catalogo import cargar_cache
        p = tmp_path / "e.csv"
        p.write_text("sku_id,hash_nombre,nombre_expandido,tipo,variante,sinonimos\n"
                     "1,aaa,X,shampoo,,\n", encoding="utf-8")
        cache = cargar_cache(str(p))
        assert cache["1"]["hash_nombre"] == "aaa"
        assert cargar_cache(str(tmp_path / "no_existe.csv")) == {}
```

- [ ] **Step 2: Correr y verificar que fallan**

Run: `python -m pytest tests/test_logic.py::TestEnriquecerCatalogo -v`
Expected: FAIL con `ModuleNotFoundError: scripts.enriquecer_catalogo` (verificar que `scripts/` tenga `__init__.py`; si no existe, crearlo vacío en este paso).

- [ ] **Step 3: Implementar el script**

Crear `scripts/enriquecer_catalogo.py`:

```python
"""
Enriquecimiento batch del catálogo con LLM (correr OFFLINE, no en producción).

Uso:
    python -m scripts.enriquecer_catalogo                # usa settings.sku_csv_path
    python -m scripts.enriquecer_catalogo --dry-run      # muestra qué enriquecería
    python -m scripts.enriquecer_catalogo --limit 100    # solo primeros 100 pendientes

Lee el catálogo activo, y para cada producto SIN entrada en
data/catalogo_enriquecido.csv (o cuyo nombre cambió → hash distinto) le pide
al LLM en lotes de 25:
  - nombre_expandido: nombre legible completo, abreviaturas expandidas
  - tipo: qué es (shampoo, tintura, analgésico, pañales...)
  - variante: color/aroma/talle/graduación si aplica
  - sinonimos: hasta 4 términos coloquiales con los que un cliente lo pediría

El resultado se mergea al CSV (los ya enriquecidos se conservan) y se
commitea al repo. El bot lo levanta en el próximo deploy/recarga.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOTE = 25
SALIDA_DEFAULT = "data/catalogo_enriquecido.csv"
COLUMNAS = ["sku_id", "hash_nombre", "nombre_expandido", "tipo", "variante", "sinonimos"]


def hash_nombre(nombre: str) -> str:
    return hashlib.sha1((nombre or "").strip().lower().encode()).hexdigest()[:12]


def armar_prompt(lote: list[dict]) -> str:
    items = "\n".join(
        f'- sku_id "{p["sku_id"]}": "{p["nombre"]}"'
        + (f' (marca: {p["marca"]})' if p.get("marca") else "")
        + (f' (categoría: {p["categoria"]})' if p.get("categoria") else "")
        for p in lote
    )
    return (
        "Sos un farmacéutico argentino. Estos son nombres de productos tal como "
        "figuran en el sistema de una farmacia, con abreviaturas de góndola.\n"
        "Para CADA producto devolvé un objeto JSON con:\n"
        '- "sku_id": el mismo id recibido (string)\n'
        '- "nombre_expandido": el nombre completo y legible, con las abreviaturas '
        "expandidas (SHA=shampoo, ACO=acondicionador, COMP=comprimidos, etc.) y "
        "el formato/presentación explícito\n"
        '- "tipo": qué tipo de producto es, en 1-3 palabras en minúsculas '
        "(shampoo, tintura de pelo, analgésico, pañales, crema corporal...)\n"
        '- "variante": color/aroma/talle/graduación si el nombre lo indica, si no ""\n'
        '- "sinonimos": lista de hasta 4 términos coloquiales con los que un '
        "cliente argentino pediría este producto por WhatsApp (sin repetir la marca)\n\n"
        "Respondé SOLO el array JSON, sin texto adicional.\n\n"
        f"Productos:\n{items}"
    )


def parsear_respuesta(texto: str) -> list[dict]:
    t = (texto or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        data = json.loads(t)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def cargar_cache(path: str) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8-sig") as f:
        return {row["sku_id"]: row for row in csv.DictReader(f) if row.get("sku_id")}


def guardar(path: str, filas: dict[str, dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS)
        w.writeheader()
        for sku_id in sorted(filas):
            w.writerow({c: filas[sku_id].get(c, "") for c in COLUMNAS})


async def llamar_llm(prompt: str, settings) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--salida", default=SALIDA_DEFAULT)
    args = ap.parse_args()

    from app.config import get_settings
    from app.services.sku_service import get_sku_service

    settings = get_settings()
    sku_svc = get_sku_service(settings.sku_csv_path)
    cache = cargar_cache(args.salida)

    pendientes = []
    for sku in sku_svc._skus:
        h = hash_nombre(sku.sku_nombre_original)
        entrada = cache.get(sku.sku_id)
        if not entrada or entrada.get("hash_nombre") != h:
            pendientes.append({"sku_id": sku.sku_id, "nombre": sku.sku_nombre_original,
                               "marca": sku.marca, "categoria": sku.categoria, "hash": h})
    if args.limit:
        pendientes = pendientes[: args.limit]

    print(f"Catálogo: {sku_svc.total} | ya enriquecidos: {len(cache)} | pendientes: {len(pendientes)}")
    if args.dry_run or not pendientes:
        return

    ok, err = 0, 0
    for i in range(0, len(pendientes), LOTE):
        lote = pendientes[i : i + LOTE]
        try:
            respuesta = await llamar_llm(armar_prompt(lote), settings)
            por_sku = {str(r.get("sku_id")): r for r in parsear_respuesta(respuesta)}
        except Exception as e:
            print(f"  lote {i//LOTE + 1}: ERROR {e}")
            err += len(lote)
            continue
        for p in lote:
            r = por_sku.get(str(p["sku_id"]))
            if not r or not r.get("nombre_expandido"):
                err += 1
                continue
            cache[p["sku_id"]] = {
                "sku_id": p["sku_id"],
                "hash_nombre": p["hash"],
                "nombre_expandido": r["nombre_expandido"],
                "tipo": r.get("tipo", ""),
                "variante": r.get("variante", ""),
                "sinonimos": "|".join(r.get("sinonimos") or []),
            }
            ok += 1
        guardar(args.salida, cache)   # guardado incremental: cortar no pierde trabajo
        print(f"  lote {i//LOTE + 1}/{-(-len(pendientes)//LOTE)}: {ok} ok, {err} errores")

    print(f"Listo: {ok} enriquecidos, {err} errores → {args.salida}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `python -m pytest tests/test_logic.py::TestEnriquecerCatalogo -v`
Expected: 6 PASS

- [ ] **Step 5: Probar el script en dry-run contra el catálogo real**

Run: `python -m scripts.enriquecer_catalogo --dry-run`
Expected: imprime el total del catálogo y pendientes = total (cache vacío). Sin llamadas al LLM.

- [ ] **Step 6: Commit**

```bash
git add scripts/enriquecer_catalogo.py scripts/__init__.py tests/test_logic.py
git commit -m "Script batch de enriquecimiento de catálogo con LLM (cache por hash de nombre)"
```

---

### Task 3: Integrar el enriquecimiento a la búsqueda de SKUService

El índice de búsqueda pasa de "nombre + marca + laboratorio" a incluir también la expansión determinista y el texto enriquecido. `nombre_coincide` y `_to_response` conocen el nombre expandido. Sin archivo enriquecido, la única diferencia vs. hoy es la expansión determinista de abreviaturas (mejora pura).

**Files:**
- Modify: `app/services/sku_service.py` (método `_load` ~línea 142-175, `nombre_coincide` ~línea 83, `_to_response` ~línea 340, `get_sku_service`/`reload_sku_service` ~línea 366)
- Modify: `app/models/sku.py` (sin cambios de campos; el nombre legible viaja en el dict de respuesta, no en el modelo)
- Test: `tests/test_logic.py` (clase `TestBusquedaEnriquecida`)

**Interfaces:**
- Consumes: `expandir_abreviaturas`, `CatalogoEnriquecido` (Task 1).
- Produces: `SKUService.__init__(csv_path, enriquecido_path: str | None = None)` — default `data/catalogo_enriquecido.csv` si existe junto al catálogo; `_to_response` agrega clave `"nombre_legible"` (nombre_expandido si hay, si no `sku_nombre`); `nombre_coincide(query, nombre, extra: str = "")` acepta texto extra donde también buscar coincidencia. **Todo consumidor de `_to_response` puede leer `r["nombre_legible"]`; Task 5 depende de esta clave.**

- [ ] **Step 1: Escribir los tests que fallan**

```python
class TestBusquedaEnriquecida:
    def _svc(self, tmp_path, con_enriquecido=True):
        from app.services.sku_service import SKUService
        cat = tmp_path / "cat.csv"
        cat.write_text(
            "SKU,Nombre,Precio,Marca,Laboratorio,Codigo_Barras_1,Codigo_Barras_2,"
            "Codigo_Barras_3,Codigo_Barras_4,Categoria,Es_Medicamento\n"
            "1,SEDAL CERAMICAS SHA,5000,Sedal,Unilever,111,,,,Perfumeria,false\n"
            "2,CAPILATIS ORTIGA SHA,4000,Capilatis,Capilatis,222,,,,Perfumeria,false\n"
            "3,KOLESTON SING 60 RUBIO OSCURO,9555,Koleston,Wella,333,,,,Perfumeria,false\n",
            encoding="utf-8",
        )
        enr = tmp_path / "enr.csv"
        if con_enriquecido:
            enr.write_text(
                "sku_id,hash_nombre,nombre_expandido,tipo,variante,sinonimos\n"
                '1,x,"Shampoo Sedal Ceramidas",shampoo,ceramidas,"crema de enjuague"\n'
                '3,y,"Tintura Koleston Singular tono 60 Rubio Oscuro",tintura de pelo,'
                '"rubio oscuro","tintura|coloración"\n',
                encoding="utf-8",
            )
        return SKUService(str(cat), enriquecido_path=str(enr))

    def test_busca_por_abreviatura_expandida(self, tmp_path):
        # "shampoo sedal" debe encontrar "SEDAL CERAMICAS SHA" primero
        svc = self._svc(tmp_path, con_enriquecido=False)
        r = svc.buscar("shampoo sedal ceramidas")
        assert r and "SEDAL" in r[0]["nombre"]

    def test_busca_por_sinonimo_enriquecido(self, tmp_path):
        svc = self._svc(tmp_path)
        r = svc.buscar("tintura rubio oscuro")
        assert r and r[0]["sku_id"] == "3"

    def test_nombre_legible_en_respuesta(self, tmp_path):
        svc = self._svc(tmp_path)
        r = svc.buscar("shampoo sedal ceramidas")
        assert r[0]["nombre_legible"] == "Shampoo Sedal Ceramidas"
        # Producto sin enriquecer: nombre_legible = nombre
        r2 = svc.buscar("capilatis ortiga")
        assert r2[0]["nombre_legible"] == r2[0]["nombre"]

    def test_nombre_coincide_con_extra(self):
        from app.services.sku_service import nombre_coincide
        # "shampoo" no está en "SEDAL CERAMICAS SHA" pero sí en el texto enriquecido
        assert not nombre_coincide("shampoo ceramidas", "CAPILATIS ORTIGA SHA")
        assert nombre_coincide("shampoo ceramidas", "SEDAL CERAMICAS SHA",
                               extra="shampoo sedal ceramidas crema de enjuague")

    def test_sin_archivo_enriquecido_funciona_igual(self, tmp_path):
        svc = self._svc(tmp_path, con_enriquecido=False)
        assert svc.buscar("sedal")  # no explota, encuentra
```

- [ ] **Step 2: Correr y verificar que fallan**

Run: `python -m pytest tests/test_logic.py::TestBusquedaEnriquecida -v`
Expected: FAIL (`__init__` no acepta `enriquecido_path`, falta `nombre_legible`, etc.)

- [ ] **Step 3: Implementar en sku_service.py**

Cambios concretos:

3a. Imports (arriba del archivo):

```python
from app.services.catalogo_enriquecido import CatalogoEnriquecido, expandir_abreviaturas
```

3b. `__init__` y `_load`:

```python
    def __init__(self, csv_path: str, enriquecido_path: str | None = None):
        self._skus: list[SKU] = []
        self._search_index: list[str] = []
        # Por convención el enriquecido vive junto al catálogo como
        # catalogo_enriquecido.csv; si no existe, el objeto queda vacío (no-op).
        if enriquecido_path is None:
            enriquecido_path = str(Path(csv_path).parent / "catalogo_enriquecido.csv")
        self._enriquecido = CatalogoEnriquecido(enriquecido_path)
        self._load(csv_path)
```

En `_load`, reemplazar el armado del índice (líneas 157-163) por:

```python
                    # Índice: nombre (con abreviaturas expandidas) + marca +
                    # laboratorio + texto enriquecido por LLM si existe.
                    search_text = " ".join(filter(None, [
                        expandir_abreviaturas(sku.sku_nombre).lower(),
                        sku.marca.lower(),
                        sku.laboratorio.lower(),
                        self._enriquecido.texto_busqueda(sku.sku_id),
                    ]))
                    self._search_index.append(search_text)
```

3c. `nombre_coincide` (firma nueva, retrocompatible):

```python
def nombre_coincide(query: str, nombre: str, extra: str = "") -> bool:
    """
    True si el producto encontrado corresponde a lo que se pidió: algún término
    distintivo de la consulta (4+ letras, con tolerancia a typos por prefijo)
    aparece en el nombre o en el texto enriquecido (extra).
    """
    n = f"{nombre or ''} {extra or ''}".lower()
    for tok in _tokens(query):
        if len(tok) >= 4 and tok[:5] in n:
            return True
    return False
```

3d. `_to_response` — agregar al dict (y convertir de `@staticmethod` a método de instancia; actualizar el ÚNICO llamador externo, `webhook.py:1152` `deps["sku"]._to_response(_sku)`, que ya lo llama como método de instancia — no requiere cambio):

```python
    def _to_response(self, sku: SKU) -> dict:
        info = self._enriquecido.get(sku.sku_id)
        return {
            "sku_id": sku.sku_id,
            ...  # (campos existentes sin cambios)
            "nombre_legible": (info or {}).get("nombre_expandido") or sku.sku_nombre,
            "texto_enriquecido": self._enriquecido.texto_busqueda(sku.sku_id),
        }
```

(Escribir el dict completo: copiar los campos actuales de la línea 342-359 y agregar las dos claves nuevas al final.)

Nota: `_to_response` se usa en `buscar()` como `self._to_response(s)` — al dejar de ser staticmethod sigue funcionando igual.

3e. En el STOP de `buscar()` (línea 263-267) NO se cambia nada: las palabras de formato siguen filtrándose de la query; la ganancia viene de que el índice ahora sí contiene "shampoo" para los productos "SHA".

- [ ] **Step 4: Correr los tests nuevos y TODA la suite**

Run: `python -m pytest tests/test_logic.py -v --timeout=120`
Expected: los 5 nuevos PASS y los 178 existentes PASS (si alguno de los existentes asserteaba las claves exactas de `_to_response`, agregarle las claves nuevas al expected).

- [ ] **Step 5: Commit**

```bash
git add app/services/sku_service.py tests/test_logic.py
git commit -m "La búsqueda usa el catálogo enriquecido: abreviaturas expandidas, sinónimos y nombre legible"
```

---

### Task 4: Correr el enriquecimiento real y deployar

**Files:**
- Create: `data/catalogo_enriquecido.csv` (generado)
- Modify: `README.md` (sección nueva "Enriquecimiento del catálogo": cuándo y cómo re-correrlo)

**Interfaces:**
- Consumes: Task 2 (script) y Task 3 (carga automática por convención de path).

- [ ] **Step 1: Dry-run para dimensionar**

Run: `python -m scripts.enriquecer_catalogo --dry-run`
Expected: reporta N pendientes. Anotar N (estimar costo: ~N/25 llamadas a Haiku).
Requiere `ANTHROPIC_API_KEY` en el entorno local (NUNCA pegarla en el chat ni commitearla — pedirle a Mariano que la exporte en su terminal si no está).

- [ ] **Step 2: Corrida de prueba acotada**

Run: `python -m scripts.enriquecer_catalogo --limit 50`
Expected: genera `data/catalogo_enriquecido.csv` con ~50 filas. Abrir el CSV y revisar a ojo 10 filas: ¿`nombre_expandido` es fiel al producto? ¿`sinonimos` son razonables? Si el LLM alucina presentaciones o marcas, ajustar el prompt de `armar_prompt` y volver a correr (borrar las filas malas del CSV para que se regeneren).

- [ ] **Step 3: Corrida completa**

Run: `python -m scripts.enriquecer_catalogo`
Expected: termina con "Listo: N enriquecidos". El guardado es incremental por lote: si se corta (rate limit, red), re-correr continúa donde quedó.

- [ ] **Step 4: Verificación con los casos reales que fallaron**

```bash
python -c "
from app.config import get_settings
from app.services.sku_service import get_sku_service
s = get_sku_service(get_settings().sku_csv_path)
for q in ['tintura rubio ceniza', 'shampoo sedal ceramidas', 'gotas nasales', 'gomitas de menta']:
    r = s.buscar(q)
    print(q, '→', [x['nombre_legible'] for x in r[:3]])
"
```
Expected: resultados razonables para cada query histórica problemática. Documentar el antes/después en el mensaje de commit.

- [ ] **Step 5: README + commit + push**

Agregar al `README.md`:

```markdown
## Enriquecimiento del catálogo

`data/catalogo_enriquecido.csv` mejora la búsqueda con nombres expandidos y
sinónimos generados por LLM. Re-correr cuando cambia el catálogo:

    python -m scripts.enriquecer_catalogo    # solo procesa productos nuevos/renombrados

Requiere ANTHROPIC_API_KEY en el entorno. Commitear el CSV resultante.
```

```bash
git add data/catalogo_enriquecido.csv README.md
git commit -m "Catálogo enriquecido generado (N productos) — mejora búsqueda de abreviaturas y coloquiales"
git push origin develop
```

- [ ] **Step 6: Verificar el deploy**

Run: `curl -s https://cerca.remedia.ar/health`
Expected: commit nuevo. Probar por WhatsApp 2-3 búsquedas históricas ("shampoo sedal", "tintura rubio ceniza").

---

## Fase 2 — Oferta determinista (desde estado, no desde redacción)

### Task 5: Plantillas de oferta en oferta_helper.py

La redacción de ofertas y listas de opciones sale de plantillas alimentadas por los registros del catálogo. El modelo nunca más "redacta precios".

**Files:**
- Create: `app/services/oferta_helper.py`
- Test: `tests/test_logic.py` (clase `TestOfertaHelper`)

**Interfaces:**
- Consumes: dicts de producto de `SKUService._to_response` (claves: `nombre`, `nombre_legible`, `precio`, `sku_id`, `vendible`, `sin_stock`, `texto_enriquecido`).
- Produces:
  - `redactar_oferta(producto: dict, cantidad: int = 1, coincide: bool = True) -> str`
  - `redactar_opciones(entidad: str, opciones: list[dict]) -> str`
  - `redactar_sin_stock(entidad: str) -> str`
  Task 6 llama exactamente estas tres funciones.

- [ ] **Step 1: Escribir los tests que fallan**

```python
class TestOfertaHelper:
    P = {"sku_id": "3", "nombre": "KOLESTON SING 60 RUBIO OSCURO",
         "nombre_legible": "Tintura Koleston Singular tono 60 Rubio Oscuro",
         "precio": 9555.28, "vendible": True}

    def test_oferta_simple_contiene_nombre_y_precio(self):
        from app.services.oferta_helper import redactar_oferta
        t = redactar_oferta(self.P)
        assert "Tintura Koleston Singular tono 60 Rubio Oscuro" in t
        assert "9,555.28" in t
        assert "?" in t   # siempre termina preguntando confirmación

    def test_oferta_con_cantidad_multiplica(self):
        from app.services.oferta_helper import redactar_oferta
        t = redactar_oferta(self.P, cantidad=2)
        assert "2" in t and "19,110.56" in t

    def test_oferta_no_coincide_avisa_similar(self):
        from app.services.oferta_helper import redactar_oferta
        t = redactar_oferta(self.P, coincide=False)
        assert "parecido" in t.lower()
        assert "9,555.28" in t

    def test_opciones_lista_numerada_con_precios(self):
        from app.services.oferta_helper import redactar_opciones
        ops = [self.P, {"sku_id": "1", "nombre": "LOREAL MAGIC RETOUCH",
                        "nombre_legible": "Loreal Magic Retouch Rubio Claro Medio",
                        "precio": 33437.53, "vendible": True}]
        t = redactar_opciones("tintura rubio ceniza", ops)
        assert "1." in t and "2." in t
        assert "9,555.28" in t and "33,437.53" in t
        assert "número" in t.lower() or "cuál" in t.lower()

    def test_sin_stock_menciona_lo_pedido(self):
        from app.services.oferta_helper import redactar_sin_stock
        t = redactar_sin_stock("tintura rubio ceniza")
        assert "tintura rubio ceniza" in t.lower()
```

- [ ] **Step 2: Correr y verificar que fallan**

Run: `python -m pytest tests/test_logic.py::TestOfertaHelper -v`
Expected: FAIL con ModuleNotFoundError

- [ ] **Step 3: Implementar**

Crear `app/services/oferta_helper.py`:

```python
"""
Redacción determinista de ofertas de producto.

Principio: el texto de una oferta se construye DESDE el registro del catálogo
que quedó como pendiente en la sesión — nunca desde la redacción libre del
modelo. Así el link de pago no puede diferir de lo ofrecido: texto y estado
salen del mismo dato. (Reemplaza la inferencia inversa de producto_respaldado,
que queda como red de seguridad.)
"""


def _nombre(p: dict) -> str:
    return p.get("nombre_legible") or p.get("nombre") or "el producto"


def redactar_oferta(producto: dict, cantidad: int = 1, coincide: bool = True) -> str:
    precio_total = (producto.get("precio") or 0.0) * max(1, cantidad)
    prefijo_cant = f"{cantidad} x " if cantidad > 1 else ""
    if coincide:
        cuerpo = f"¡Sí! Tengo {prefijo_cant}{_nombre(producto)} por ${precio_total:,.2f} ✅"
    else:
        cuerpo = (f"No lo encontré tal cual, pero lo más parecido que tengo es "
                  f"{prefijo_cant}{_nombre(producto)} por ${precio_total:,.2f}")
    return (f"{cuerpo}\n¿Te lo preparo? Decime si lo retirás en la farmacia "
            "o te lo enviamos a domicilio 🙂")


def redactar_opciones(entidad: str, opciones: list[dict]) -> str:
    lineas = "\n".join(
        f"{i}. {_nombre(p)} — ${(p.get('precio') or 0.0):,.2f}"
        for i, p in enumerate(opciones, start=1)
    )
    return (f"Para {entidad} tengo estas opciones:\n\n{lineas}\n\n"
            "¿Cuál preferís? Decime el número o el nombre 🙂")


def redactar_sin_stock(entidad: str) -> str:
    return (f"Justo no tengo {entidad} disponible en este momento 😔")
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `python -m pytest tests/test_logic.py::TestOfertaHelper -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/oferta_helper.py tests/test_logic.py
git commit -m "oferta_helper: redacción determinista de ofertas, opciones y sin stock"
```

---

### Task 6: El webhook ofrece desde estado

Reescritura del bloque de oferta (`webhook.py` líneas ~1194-1292). El intent classifier sigue igual (intención, entidad, cantidad, índice), pero cuando hay resultados y la intención es de compra, la respuesta del modelo se DESCARTA y se rinde con `oferta_helper` + `set_pending` en el mismo acto.

**Files:**
- Modify: `app/routers/webhook.py` (bloque `if resultados_sku:` líneas 1194-1292; import nuevo arriba)
- Test: `tests/test_logic.py` (los tests existentes de este flujo se ACTUALIZAN; ver Step 4)

**Interfaces:**
- Consumes: `redactar_oferta`, `redactar_opciones`, `redactar_sin_stock` (Task 5); `nombre_coincide(query, nombre, extra)` (Task 3); claves `nombre_legible`/`texto_enriquecido` de los resultados (Task 3).
- Produces: mismo contrato externo de siempre (respuesta por WhatsApp + sesión con pending). `productos_con_precio`/`producto_respaldado` DEJAN de usarse en este bloque (siguen usados por el flujo de confirmación como red de seguridad — no borrar de `checkout_helper.py`).

- [ ] **Step 1: Escribir el reemplazo del bloque**

Import arriba de `webhook.py` (junto a los de checkout_helper, línea ~46):

```python
from app.services.oferta_helper import redactar_oferta, redactar_opciones, redactar_sin_stock
```

Reemplazar el contenido de `if resultados_sku:` (desde `sku_index = ...` línea 1196 hasta el final del `elif producto_elegido and producto_elegido.get("vendible", True):` línea 1292) por:

```python
                _opciones_ofrecidas = False
                _INTENCIONES_COMPRA = ("pedido", "consulta_precio", "consulta_stock")
                if resultados_sku and intencion in _INTENCIONES_COMPRA:
                    solicita_imagen = False   # pedir foto deriva; no se manda imagen de catálogo
                    vendibles = [r for r in resultados_sku if r.get("vendible", True)]
                    producto_elegido = None

                    # 1. Índice elegido por el modelo (el cliente nombró uno puntual)
                    sku_index = intent_result.get("sku_seleccionado_index")
                    if sku_index is not None:
                        try:
                            idx = int(sku_index) - 1
                            if 0 <= idx < len(resultados_sku) and \
                                    resultados_sku[idx].get("vendible", True):
                                producto_elegido = resultados_sku[idx]
                        except (ValueError, TypeError):
                            pass

                    # 2. Sin índice: un único vendible es oferta directa;
                    #    varios son lista de opciones (el cliente elige).
                    if producto_elegido is None and len(vendibles) == 1:
                        producto_elegido = vendibles[0]

                    if producto_elegido is None and len(vendibles) > 1:
                        # LISTA DE OPCIONES — el texto y el estado salen del
                        # mismo dato: imposible que difieran.
                        _opciones_ofrecidas = True
                        respuesta = redactar_opciones(entidad or "eso", vendibles[:3])
                        await deps["session"].set_pending(
                            phone=phone,
                            sku_id=vendibles[0]["sku_id"],
                            sku_nombre=vendibles[0]["nombre"],
                            precio=vendibles[0]["precio"],
                            cantidad=cantidad,
                            opciones=vendibles[:3],
                        )
                        _s_op = await deps["session"].get(phone)
                        _s_op["_espera_eleccion"] = True
                        await deps["session"].save(phone, _s_op)
                        logger.info(f"{len(vendibles)} opciones ofrecidas para {entidad!r} "
                                    "(redacción determinista)")

                    elif producto_elegido and intent_result.get("agregar_al_pedido") \
                            and session.get("pending_sku_id"):
                        # "Agregame también..." → suma al pedido en curso (igual que hoy)
                        _cfg_rec = await deps["config"].get_all()
                        if necesita_receta(deps["sku"], producto_elegido["sku_id"],
                                           _cfg_rec.get("receta_mode", "conservador")):
                            respuesta = (f"{producto_elegido['nombre']} requiere receta 🩺, así que "
                                         "ese no lo puedo sumar al pedido. El resto sigue como está "
                                         "— ¿lo confirmamos?")
                        else:
                            items = await deps["session"].agregar_item(
                                phone, producto_elegido["sku_id"], producto_elegido["nombre"],
                                producto_elegido["precio"], cantidad,
                            )
                            _total_items = sum(i["precio"] * i.get("cantidad", 1) for i in items)
                            _lista = "\n".join(
                                f"• {i['nombre']} — ${i['precio'] * i.get('cantidad', 1):,.2f}"
                                for i in items
                            )
                            respuesta = (f"¡Listo, lo sumé! Tu pedido queda así:\n{_lista}\n\n"
                                         f"Total: *${_total_items:,.2f}* — ¿lo confirmamos?")
                            _intencion = "item_agregado"

                    elif producto_elegido:
                        # OFERTA DIRECTA — pending y texto desde el mismo registro.
                        _coincide = nombre_coincide(
                            entidad or texto, producto_elegido["nombre"],
                            extra=producto_elegido.get("texto_enriquecido", ""),
                        )
                        respuesta = redactar_oferta(producto_elegido, cantidad,
                                                    coincide=_coincide)
                        await deps["session"].set_pending(
                            phone=phone,
                            sku_id=producto_elegido["sku_id"],
                            sku_nombre=producto_elegido["nombre"],
                            precio=producto_elegido["precio"],
                            cantidad=cantidad,
                            opciones=resultados_sku,
                        )
                        _sku_pendiente_nuevo = producto_elegido["sku_id"]
                        if not _coincide:
                            await deps["metrics"].evento(
                                "busqueda_sin_resultado", phone=phone,
                                dato=" ".join((entidad or "").lower().split())[:80])
                        await deps["metrics"].evento(
                            "producto_ofrecido", phone=phone,
                            dato=producto_elegido["nombre"][:120],
                            monto=producto_elegido["precio"], ref=producto_elegido["sku_id"],
                        )
                        send_images_cfg = await deps["config"].get("send_images")
                        await _maybe_send_image(
                            deps["wa"], phone, resultados_sku,
                            producto_elegido, solicita_imagen, send_images_cfg,
                        )
                    else:
                        # Hay resultados pero ninguno vendible → mensaje sin stock
                        # determinista; el flujo sin_stock de abajo agrega la oferta
                        # de consultar con el equipo según config.
                        respuesta = redactar_sin_stock(entidad or "eso")

                elif resultados_sku:
                    # Intención NO de compra (consulta general, saludo con mención
                    # de producto): la respuesta del modelo se mantiene, pero jamás
                    # queda pending — sin pending no hay confirmación posible.
                    logger.info(f"Resultados sin intención de compra ({intencion}); "
                                "respuesta del modelo sin pending")
```

Notas de integración (verificar durante la edición):
- `nombre_coincide` ya se importa dentro del bloque `_extras` (línea 1302); mover el import al top del archivo y borrar el import local.
- El bloque de `_extras` (entidades_adicionales, líneas 1294-1325) queda IGUAL.
- El bloque sin-stock (líneas 1327-1348) queda IGUAL: con `respuesta = redactar_sin_stock(...)` como base, el modo "preguntar" le agrega la oferta de consultar al equipo, como hoy.
- `productos_con_precio` deja de importarse/usarse en webhook.py si ya no queda ningún uso (verificar con grep antes de quitar el import; `producto_respaldado` se sigue usando en el flujo de confirmación).

- [ ] **Step 2: Correr la suite completa y catalogar los tests rotos**

Run: `python -m pytest tests/test_logic.py -v 2>&1 | tail -40`
Expected: fallan los tests que asserteaban la mecánica vieja (inferencia por precio en este bloque), típicamente `test_lista_de_opciones_no_es_oferta` y vecinos de la clase de selección de producto. Listar cada uno con su assert.

- [ ] **Step 3: Actualizar los tests rotos al contrato nuevo**

Para cada test roto, el comportamiento esperado nuevo es:
- Varios resultados vendibles sin índice → `respuesta` contiene lista numerada con TODOS los precios, sesión con `_espera_eleccion=True` y `pending_opciones` poblado.
- Un resultado vendible → `respuesta` contiene el `nombre_legible` y el precio EXACTO del pending (`session["pending_precio"]`).
- El texto de la respuesta y `session["pending_sku_id"]` provienen del mismo registro (assert nuevo recomendado: el precio del pending aparece en la respuesta formateado `${precio:,.2f}`).
NO borrar ningún test: cada uno se reescribe apuntando al mismo bug histórico que cubría.

- [ ] **Step 4: Correr toda la suite en verde**

Run: `python -m pytest tests/ -v --timeout=180`
Expected: TODO PASS (los de DB usan pgserver; si el entorno local no los levanta, correr al menos `tests/test_logic.py` completo y anotarlo en el commit).

- [ ] **Step 5: Commit**

```bash
git add app/routers/webhook.py tests/test_logic.py
git commit -m "La oferta de producto se redacta desde el estado: texto y pending salen del mismo registro"
```

---

### Task 7: Selección de opción también determinista (flujo ya_tiene_pending)

Cuando el cliente elige de la lista ("el 2", "rubio oscuro"), la confirmación de la elección también se rinde desde el registro (hoy el modelo redacta y puede volver a inventar).

**Files:**
- Modify: `app/routers/webhook.py` (bloque `elif ya_tiene_pending:` líneas ~1350-1390)
- Test: `tests/test_logic.py`

**Interfaces:**
- Consumes: `redactar_oferta` (Task 5).
- Produces: al seleccionar índice válido, `respuesta = redactar_oferta(elegido, nueva_cantidad)` reemplaza la respuesta del modelo.

- [ ] **Step 1: Escribir el test que falla**

```python
class TestSeleccionDeterminista:
    def test_respuesta_de_seleccion_sale_del_registro(self):
        # La respuesta al elegir una opción contiene nombre y precio del elegido,
        # generada por redactar_oferta — no la redacción libre del modelo.
        from app.services.oferta_helper import redactar_oferta
        elegido = {"sku_id": "3", "nombre": "KOLESTON SING 60 RUBIO OSCURO",
                   "nombre_legible": "Tintura Koleston tono 60 Rubio Oscuro",
                   "precio": 9555.28, "vendible": True}
        t = redactar_oferta(elegido, 1)
        assert "9,555.28" in t and "Koleston" in t
```

(El test de integración del flujo completo vive en los tests del webhook si existen fixtures; si el repo no tiene test de integración de este bloque, este test unitario + verificación manual del Step 4 cubren el cambio.)

- [ ] **Step 2: Implementar**

En el bloque `if sku_index is not None and pending_opciones:` (línea 1366), después del `set_pending` exitoso (línea 1372-1379), agregar:

```python
                            # Confirmación redactada desde el registro elegido,
                            # no desde la redacción libre del modelo.
                            respuesta = redactar_oferta(elegido, nueva_cantidad)
```

- [ ] **Step 3: Correr la suite**

Run: `python -m pytest tests/test_logic.py -v --timeout=120`
Expected: TODO PASS

- [ ] **Step 4: Commit y push**

```bash
git add app/routers/webhook.py tests/test_logic.py
git commit -m "Elegir una opción confirma con texto redactado desde el registro elegido"
git push origin develop
```

---

### Task 8: Verificación end-to-end en producción

**Files:** ninguno (verificación).

- [ ] **Step 1: Verificar deploy**

Run: `curl -s https://cerca.remedia.ar/health`
Expected: commit del push de Task 7.

- [ ] **Step 2: Repetir las conversaciones históricas que fallaron (por WhatsApp, con Mariano)**

Guión de prueba (los 4 bugs históricos):
1. "tenés tintura rubio ceniza?" → debe listar opciones numeradas → "perfecto, rubio oscuro" → debe confirmar KOLESTON con SU precio → confirmar → el link debe ser de KOLESTON.
2. "quiero framintrol" → debe ofrecer Framintrol (no Gillette).
3. "shampoo sedal de ceramidas" → debe encontrar el Sedal (no Capilatis) gracias al enriquecimiento.
4. "tenés gotas nasales?" → si hay una sola opción vendible, oferta directa con precio; "no esta bien" → NO debe confirmar.

- [ ] **Step 3: Revisar métricas a la semana**

En `/dashboard`: `busquedas_sin_resultado` debería bajar vs. semana previa; registrar el antes/después para el status a la farmacia.

---

## Self-review (hecho al escribir el plan)

- **Cobertura:** causa 1 (catálogo) → Tasks 1-4; causa 3 (oferta desde estado) → Tasks 5-7; verificación → Task 8. La causa 2 (embeddings híbridos) queda explícitamente FUERA de este plan (acordado: se evalúa después de medir el efecto de la Fase 1).
- **Placeholders:** ninguno; todo step tiene código o comando concreto.
- **Consistencia de tipos:** `nombre_legible`/`texto_enriquecido` se definen en Task 3 `_to_response` y se consumen en Tasks 5-6; `nombre_coincide(query, nombre, extra="")` definida en Task 3, consumida en Task 6; las tres funciones de `oferta_helper` (Task 5) coinciden con las llamadas de Tasks 6-7.
- **Riesgos señalados:** (a) Task 6 es el cambio más invasivo — el diff debe limitarse al bloque indicado; (b) si el catálogo real usa sku_id con notación científica (visto en `catalogo_corregido.csv`: `7.79364E+12`), el join del enriquecido usa el MISMO valor que carga SKUService, así que es consistente por construcción; (c) el tono de las plantillas lo valida Mariano en Task 8 y se ajusta en `oferta_helper.py` sin tocar lógica.
