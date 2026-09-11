"""
Carga el catálogo y expone búsqueda fuzzy por nombre, marca o laboratorio.

Soporta dos formatos de CSV:
  A) Catálogo base (siemprebien_skus):
       SKU, Nombre, Precio, Marca, Laboratorio, Codigo_Barras_1..4, Categoria, Es_Medicamento
  B) Catálogo procesado (con stock ETL):
       sku_id, barcode, sku_nombre, sku_nombre_original, marca, laboratorio, categoria,
       es_medicamento, precio_venta, stock_actual, ventas_mes, prom_semanal,
       cantidad_visible, tipo_producto, pausado

Si el CSV es el formato B (tiene la columna 'cantidad_visible'), usa los valores calculados.
Si es el formato A (sin stock), todos los productos quedan en estado "consultar" hasta
que se corra el ETL de ObServer.
"""

import csv
import logging
import math
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process

from app.models.sku import SKU
from app.services.catalogo_enriquecido import expandir_abreviaturas, tipos_mencionados

logger = logging.getLogger(__name__)

# Umbrales de búsqueda.
# 66 filtra los falsos positivos por fragmento (ej. "ibuprofeno"→"FREESTYLE LIBRE",
# "vecetam"→"esponja VEGetal") sin perder los aciertos reales.
SCORE_CUTOFF = 66
TOP_N_DEFAULT = 3

# Sinónimos droga↔marca: el catálogo usa nombres de marca, así que el nombre
# genérico que tipea el cliente no siempre matchea. La clave es lo que puede
# escribir el cliente; los valores son términos adicionales a buscar (marcas
# equivalentes presentes en el catálogo). Si algún valor no existe, la búsqueda
# extra simplemente no devuelve nada (inofensivo). Ampliable a medida que surjan.
SINONIMOS: dict[str, list[str]] = {
    "ibuprofeno":   ["ibupirac", "actron"],
    "omeprazol":    ["aziatop"],
    "escopolamina": ["buscapina", "sertal"],
    "butilhioscina":["buscapina", "sertal"],
    "hioscina":     ["buscapina", "sertal"],
    "diclofenac":   ["voltaren"],
    "aspirina":     ["bayaspirina", "aspirineta"],
    "acido acetilsalicilico": ["aspirina", "bayaspirina"],
    "amoxicilina":  ["amoxidal"],
    "paracetamol":  ["tafirol"],
}

# Lista blanca de VENTA LIBRE: drogas/marcas OTC conocidas cuyo flag de receta
# del catálogo suele venir mal (ej. omeprazol/Aziatop marcado "si" o "ambiguo"
# siendo venta libre). Si el nombre contiene alguna de estas, se fuerza
# requiere_receta="no". Ampliable/editable — VALIDAR con criterio farmacéutico.
VENTA_LIBRE: set[str] = {
    # Analgésicos / antipiréticos / AINEs OTC
    "paracetamol", "tafirol", "ibuprofeno", "ibupirac", "actron", "ibuevanol",
    "aspirina", "aspirineta", "bayaspirina", "naproxeno",
    # Antiácidos / IBP / digestivos OTC
    "omeprazol", "aziatop", "esomeprazol", "pantoprazol", "ranitidina",
    "mylanta", "gaviscon", "sertal", "buscapina",
    # Antihistamínicos OTC
    "loratadina",
    # Confirmados por la farmacia (minuta 2026-07-31)
    "advance", "betacort", "betacor",
    # Caso 30 (19/8, Belén): derivado "por receta" siendo venta libre
    "ultraflex",
}


def es_venta_libre(nombre: str) -> bool:
    """True si el nombre del producto corresponde a un OTC de la lista blanca."""
    n = (nombre or "").lower()
    return any(kw in n for kw in VENTA_LIBRE)


def _tokens(texto: str) -> list[str]:
    """Palabras de 3+ caracteres, para medir cuán distintivo es cada término."""
    import re as _re
    return _re.findall(r"[a-záéíóúñ0-9]{3,}", (texto or "").lower())


import re as _re_mod

# Números que DISCRIMINAN en farmacia: dosis (600), FPS (65), tamaño (x 30),
# concentración (4%). Hasta el 11/9 se borraban todos los dígitos de la
# consulta como si fueran cantidades pedidas, y "aveno infantil 65" devolvía
# el gel de baño en vez del protector solar F65.
_NUMERO_RE = _re_mod.compile(r"(?<![\d.,])(\d+%|\d{2,})(?![\d.,])")
# Cantidades pedidas, que sí se quitan: "dame 2", "3 cajas", "quiero 2".
_CANTIDAD_RE = _re_mod.compile(
    r"\b(?:dame|quiero|necesito|llevo|tra[eé]me|mand[aá]me)\s+(\d+)\b"
    r"|\b(\d+)\s+(?:unidades?|cajas?|frascos?|paquetes?|potes?|u)\b",
    _re_mod.IGNORECASE,
)
_FPS_RE = _re_mod.compile(r"\b(?:f\.?|fps\.?|factor)\s*(\d{2,3})\b", _re_mod.IGNORECASE)
_UNIDAD_RE = _re_mod.compile(r"(\d+)\s*(mg|ml|mcg|gr|g|ui|cc|kg|lt|l)\b", _re_mod.IGNORECASE)


def normalizar_numeros(texto: str) -> str:
    """
    Deja los números aislados y comparables entre consulta y nombre:
    "F65"/"FPS65"/"factor 65"/"por 65" → "fps 65"; "600mg" → "600 mg".
    """
    t = texto or ""
    t = _FPS_RE.sub(lambda m: f"fps {m.group(1)}", t)
    t = _re_mod.sub(r"\bpor\s+(\d{2,3})\b", r"fps \1", t, flags=_re_mod.IGNORECASE)
    t = _UNIDAD_RE.sub(lambda m: f"{m.group(1)} {m.group(2).lower()}", t)
    return t


def numeros_de(texto: str) -> list[str]:
    """Números discriminantes de un texto ('600', '65', '4%'). 1 dígito solo cuenta con %."""
    return _NUMERO_RE.findall(normalizar_numeros(texto or ""))


def quitar_cantidades(consulta: str) -> str:
    """Saca SOLO las cantidades pedidas ("dame 2"), nunca dosis ni tamaños."""
    return _CANTIDAD_RE.sub(lambda m: _re_mod.sub(r"\d+", "", m.group(0)), consulta or "")


def completar_numeros(entidad: Optional[str], texto: str) -> Optional[str]:
    """
    El buscador recibe la `entidad_producto` que extrae el modelo, no la frase
    del cliente — y el modelo tiende a tirar el número ("tenés aveno infantil
    por 65?" → "aveno infantil", caso real 11/9: ofreció el acondicionador).
    Si la frase original trae números discriminantes que la entidad no tiene,
    se le agregan (con su contexto: "fps 65", "600 mg", "4%").
    """
    if not entidad or not texto:
        return entidad
    tiene = set(numeros_de(entidad))
    faltan = [n for n in numeros_de(texto) if n not in tiene]
    if not faltan:
        return entidad
    t_norm = normalizar_numeros(texto).lower()
    extra = []
    for n in faltan:
        m = _re_mod.search(rf"\b(fps {_re_mod.escape(n)}|{_re_mod.escape(n)}\s*(?:mg|ml|mcg|gr|g|ui))\b", t_norm)
        extra.append(m.group(1) if m else n)
    return f"{entidad} {' '.join(extra)}".strip()


def resultado_coincide(variantes: list[str], texto_indexado: str) -> bool:
    """
    ¿El producto encontrado corresponde a lo pedido? Regla determinista que
    frena la basura del fuzzy (caso real 11/9: "dipirona" devolvía
    acondicionadores porque "dipirona" ~ "acon-DICIONA-dor").

    1. Si la consulta nombra un TIPO (talco, suspensión...), el producto tiene
       que ser de ese tipo.
    2. Algún término alfabético distintivo de la consulta (o de un sinónimo:
       ibuprofeno→ibupirac) aparece en el texto indexado, tolerante a typos por
       prefijo de 4 letras. Los números solo suman en el ranking, nunca
       alcanzan solos para "coincidir" ("dipirona 500" no es "paracetamol 500").
    """
    texto = (texto_indexado or "").lower()
    if not variantes:
        return True
    tipos_q = tipos_mencionados(variantes[0])
    if tipos_q and not (tipos_q & tipos_mencionados(texto)):
        return False
    for v in variantes:
        for tok in _tokens(v):
            if tok.isdigit() or tok.endswith("%"):
                continue
            pref = tok[:4] if len(tok) >= 4 else tok
            if pref in texto:
                return True
    return False


def nombre_coincide(query: str, nombre: str) -> bool:
    """
    True si el producto encontrado corresponde a lo que se pidió.

    Dos condiciones:
    1. Si la consulta nombra un TIPO de producto (talco, jabón, shampoo...),
       el producto tiene que ser de ese tipo. La marca sola no alcanza: pedir
       "talco rexona" y recibir un desodorante Rexona en crema es un sustituto
       encubierto, no el producto pedido (caso real 21/8).
    2. Algún término distintivo de la consulta (4+ letras, tolerante a typos
       por prefijo) aparece en el nombre — evita presentar otra marca como si
       fuera lo pedido ("sedal..." → Capilatis).
    """
    nombre_exp = expandir_abreviaturas(nombre or "").lower()

    tipos_pedidos = tipos_mencionados(query)
    if tipos_pedidos:
        tipos_producto = tipos_mencionados(nombre_exp)
        if not (tipos_pedidos & tipos_producto):
            return False

    for tok in _tokens(query):
        if len(tok) >= 4 and tok[:5] in nombre_exp:
            return True
    return False


def _categoria_sin_receta(categoria: str) -> bool:
    """
    True si la categoría del catálogo indica un producto NO medicinal
    (perfumería, accesorios, etc.) — esos nunca requieren receta, aunque el
    flag venga mal cargado. Sólo se afirma con categoría explícita.
    """
    c = (categoria or "").strip().lower()
    return bool(c) and "medicamento" not in c


def requiere_derivacion(requiere_receta: str, modo: str = "conservador") -> bool:
    """
    Decide si un producto debe derivarse a un humano por requerir receta.
    - "si"      → siempre deriva.
    - "ambiguo" → deriva en modo conservador (default), no en modo estricto.
    - "no"      → nunca deriva.
    """
    r = (requiere_receta or "no").lower()
    if r == "si":
        return True
    if r == "ambiguo":
        return modo != "estricto"
    return False


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val not in (None, "", "nan") else None
    except (ValueError, TypeError):
        return None


def _safe_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower().strip() in ("true", "1", "yes", "si", "sí")


class SKUService:
    def __init__(self, csv_path: str):
        self._skus: list[SKU] = []
        # Índice multi-campo para búsqueda: "nombre marca laboratorio"
        self._search_index: list[str] = []
        # Todos los CB de cada producto (el ERP trae varios por ítem). El CSV
        # solo aporta el principal; con catálogo ERP se indexan todos.
        self._by_barcode: dict[str, SKU] = {}
        if csv_path:
            self._load(csv_path)
        self._build_df()

    def _indexar(self, sku: SKU, texto_extra: str = "", barcodes: Optional[list[str]] = None):
        """Suma un SKU al catálogo en memoria y a los índices de búsqueda."""
        self._skus.append(sku)
        # Índice: nombre CON las siglas de góndola expandidas (TAL→talco,
        # JAB→jabón) + marca + laboratorio. Sin la expansión, "talco rexona"
        # no encontraba el talco y el bot ofrecía un desodorante en su lugar.
        search_text = " ".join(filter(None, [
            normalizar_numeros(expandir_abreviaturas(sku.sku_nombre)).lower(),
            sku.marca.lower(),
            sku.laboratorio.lower(),
            texto_extra.lower(),
        ]))
        self._search_index.append(search_text)
        for cb in (barcodes if barcodes is not None else [sku.barcode]):
            cb = (cb or "").strip()
            if not cb:
                continue
            actual = self._by_barcode.get(cb)
            # CB compartido entre productos: gana el que tiene stock, después
            # el visible (no pausado).
            if actual is None or (actual.sin_stock and not sku.sin_stock) \
                    or (actual.pausado and not sku.pausado):
                self._by_barcode[cb] = sku

    def _build_df(self):
        # Frecuencia de cada token en el catálogo: permite distinguir palabras
        # distintivas ("framintrol", en 2 productos) de genéricas de marketing
        # ("power", en decenas) al ordenar los resultados.
        from collections import Counter
        df: Counter = Counter()
        for texto in self._search_index:
            for tok in set(_tokens(texto)):
                df[tok] += 1
        self._token_df = df

    def _load(self, csv_path: str):
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Catálogo SKU no encontrado: {csv_path}")

        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = set(reader.fieldnames or [])
            is_processed = "cantidad_visible" in cols
            has_imagen = "imagen_url" in cols

            for row in reader:
                sku = self._parse_processed(row, has_imagen) if is_processed else self._parse_base(row, has_imagen)
                if sku:
                    self._indexar(sku)

        logger.info(f"SKUService: {len(self._skus)} productos cargados desde {csv_path}")

    @classmethod
    def from_rows(cls, rows: list[dict], extras: Optional[dict[str, dict]] = None) -> "SKUService":
        """
        Catálogo desde Postgres (catalog_items ⋈ catalog_extras) en vez del CSV.

        Se cargan TODAS las filas: las inactivas (el manifiesto las desactivó)
        y las no visibles entran como `pausado` — así get_by_id sigue
        encontrando el pendiente viejo de un cliente, y el chequeo en vivo al
        cobrar lo frena. La búsqueda igual las saltea (buscar filtra pausados).
        """
        extras = extras or {}
        svc = cls("")
        for row in rows:
            ex = extras.get(row["external_id"], {})
            barcodes = list(row.get("barcodes") or [])
            requiere = (ex.get("requiere_receta_override")
                        or row.get("requiere_receta") or "no")
            categoria = row.get("category") or ""
            stock = int(row.get("stock") or 0)
            precio = float(row["price"]) if row.get("price") is not None else 0.0
            sku = SKU(
                sku_id=str(row["external_id"]),
                barcode=barcodes[0] if barcodes else "",
                sku_nombre=row.get("name") or "",
                sku_nombre_original=row.get("name") or "",
                marca=row.get("brand") or "",
                # El ERP no distingue marca de laboratorio: brand va en ambos.
                laboratorio=row.get("brand") or "",
                categoria=categoria,
                es_medicamento=categoria.strip().lower().startswith("medicamento"),
                precio_venta=precio,
                stock_actual=float(stock),
                cantidad_visible=max(stock, 0),
                pausado=(not row.get("active", True)) or (not row.get("visible", True))
                        or bool(ex.get("pausado_manual")),
                requiere_receta=requiere if requiere in ("si", "ambiguo", "no") else "no",
                imagen_url=ex.get("imagen_url") or None,
                ventas_mes=ex.get("ventas_mes"),
                prom_semanal=ex.get("prom_semanal"),
                clasificacion=(ex.get("clasificacion") or "").strip().lower(),
                tipo_producto=ex.get("tipo_producto") or "regular",
            )
            # rubro/subrubro/droga entran al índice de búsqueda, no al modelo.
            texto_extra = " ".join(filter(None, [
                row.get("drug") or "", row.get("rubro") or "",
                row.get("subrubro") or "",
            ]))
            svc._indexar(sku, texto_extra=texto_extra, barcodes=barcodes)
        svc._build_df()
        logger.info(f"SKUService: {len(svc._skus)} productos cargados desde Postgres (ERP)")
        return svc

    @staticmethod
    def _parse_base(row: dict, has_imagen: bool = False) -> Optional[SKU]:
        nombre = row.get("Nombre", "").strip()
        if not nombre or nombre == "nan":
            return None
        barcode = (
            row.get("Codigo_Barras_1", "")
            or row.get("Codigo_Barras_2", "")
            or row.get("Codigo_Barras_3", "")
            or ""
        ).strip()
        categoria = row.get("Categoria", "").strip()
        # La receta se deriva de la categoría del propio catálogo: la farmacia
        # ya clasifica los productos como "Medicamentos Bajo Receta". Si el CSV
        # trae una columna requiere_receta explícita, esa tiene prioridad.
        requiere = (row.get("requiere_receta") or "").strip().lower()
        if requiere not in ("si", "ambiguo", "no"):
            requiere = "si" if categoria.lower() == "medicamentos bajo receta" else "no"
        if es_venta_libre(nombre) or _categoria_sin_receta(categoria):
            requiere = "no"   # override: OTC conocido o rubro no medicinal (perfumería, etc.)
        try:
            return SKU(
                sku_id=str(row.get("SKU", "")).strip(),
                barcode=barcode,
                sku_nombre=nombre,
                sku_nombre_original=nombre,
                marca=row.get("Marca", "").strip(),
                laboratorio=row.get("Laboratorio", "").strip(),
                categoria=categoria,
                es_medicamento=_safe_bool(row.get("Es_Medicamento", "")),
                precio_venta=_safe_float(row.get("Precio")) or 0.0,
                cantidad_visible=0,
                imagen_url=row.get("imagen_url", "").strip() or None if has_imagen else None,
                requiere_receta=requiere,
            )
        except Exception:
            return None

    @staticmethod
    def _parse_processed(row: dict, has_imagen: bool = False) -> Optional[SKU]:
        nombre = row.get("sku_nombre", "").strip()
        if not nombre:
            return None
        requiere = (row.get("requiere_receta") or "no").strip().lower()
        if es_venta_libre(nombre) or _categoria_sin_receta(row.get("categoria", "")):
            requiere = "no"   # override: OTC conocido o rubro no medicinal (perfumería, etc.)
        try:
            return SKU(
                sku_id=row.get("sku_id", "").strip(),
                barcode=row.get("barcode", "").strip(),
                sku_nombre=nombre,
                sku_nombre_original=row.get("sku_nombre_original", nombre).strip(),
                marca=row.get("marca", "").strip(),
                laboratorio=row.get("laboratorio", "").strip(),
                categoria=row.get("categoria", "").strip(),
                es_medicamento=_safe_bool(row.get("es_medicamento", "")),
                precio_venta=_safe_float(row.get("precio_venta")) or 0.0,
                stock_actual=_safe_float(row.get("stock_actual")),
                ventas_mes=_safe_float(row.get("ventas_mes")),
                prom_semanal=_safe_float(row.get("prom_semanal")),
                cantidad_visible=int(row.get("cantidad_visible") or 0),
                tipo_producto=row.get("tipo_producto", "regular"),
                pausado=_safe_bool(row.get("pausado", "False")),
                imagen_url=row.get("imagen_url", "").strip() or None if has_imagen else None,
                requiere_receta=requiere,
                clasificacion=(row.get("clasificacion") or "").strip().lower(),
            )
        except Exception:
            return None

    def buscar(self, query: str, top_n: int = TOP_N_DEFAULT, score_cutoff: int = SCORE_CUTOFF) -> list[dict]:
        """
        Busca productos por nombre coloquial, nombre técnico, marca o laboratorio.

        Devuelve hasta top_n resultados no pausados, ordenados por RELEVANCIA
        (score del match) primero, y sólo como desempate por disponibilidad y
        ventas. Si ningún producto supera el umbral, devuelve [] — así el bot
        responde "no lo encontramos" en vez de ofrecer un producto de otro rubro.
        """
        if not self._skus or not query.strip():
            return []

        # Limpiar la query: quitar cantidades ("dame 2"), palabras de intención
        # y de presentación (gotas/crema/jarabe) que arrastran el match a otros
        # productos del mismo formato pero distinto principio.
        import re as _re
        # OJO: acá van solo palabras de INTENCIÓN, nunca de tipo de producto.
        # "jabon", "shampoo", "crema", "gotas" estuvieron en esta lista y eran
        # el problema: "jabon dove" se limpiaba a "dove" y devolvía
        # desodorantes. Ahora el índice contiene esas palabras (expandidas
        # desde las siglas del catálogo), así que discriminan en vez de estorbar.
        # Los NÚMEROS se conservan (dosis, FPS, tamaño): solo se quitan las
        # cantidades pedidas ("dame 2") — ver normalizar_numeros/quitar_cantidades.
        _STOP = (
            r'\b(dame|quiero|necesito|ten[eé]s|tienen|hay|precio|cu[aá]nto|cuanto|sale|'
            r'en|de|para|un|una|unos|unas|el|la|los|las|me|mand[aá]s|env[ií]as|'
            r'env)\b'
        )
        clean_query = normalizar_numeros(quitar_cantidades(query.lower()))
        clean_query = _re.sub(_STOP, '', clean_query)
        clean_query = _re.sub(r'\s+', ' ', clean_query).strip()
        if not clean_query:
            clean_query = query.lower()

        # Expandir con sinónimos: si el cliente escribió un genérico cuyo nombre
        # no está en el catálogo (ej. "ibuprofeno"), agregamos las marcas equivalentes.
        variantes = [clean_query]
        for generico, marcas in SINONIMOS.items():
            if generico in clean_query:
                variantes.extend(marcas)

        # Se guarda el mejor score de CADA scorer por separado (entre todas las
        # variantes). Importa mantenerlos separados: con nombres largos del
        # catálogo WRatio satura (~85 para todo) y, si se tomara el máximo,
        # tapaba a token_set_ratio —que sí discrimina— dejando el orden al azar.
        best_wr: dict[int, float] = {}
        best_ts: dict[int, float] = {}
        for variante in variantes:
            for scorer, destino in ((fuzz.WRatio, best_wr), (fuzz.token_set_ratio, best_ts)):
                for _text, score, idx in process.extract(
                    variante,
                    self._search_index,
                    scorer=scorer,
                    limit=top_n * 8,
                    score_cutoff=score_cutoff,
                ):
                    if score > destino.get(idx, 0):
                        destino[idx] = score

        # Candidatos: los mismos de antes (cualquiera de los dos scorers supera
        # el umbral). Lo que cambia es cómo se ORDENAN.
        total_docs = max(1, len(self._search_index))
        q_tokens = {t for t in _tokens(clean_query) if len(t) >= 4 and not t.isdigit()}
        q_numeros = set(numeros_de(clean_query))
        log_total = math.log(total_docs + 1)

        def _relevancia(idx: int) -> float:
            wr = best_wr.get(idx, 0)
            ts = best_ts.get(idx, 0)
            base = 0.65 * ts + 0.35 * wr
            # Bonus por término distintivo: un token del pedido presente en el
            # nombre suma según su rareza en el catálogo. Así "framintrol"
            # (raro) pesa mucho más que "power" (común en perfumería).
            texto = self._search_index[idx]
            bonus = 0.0
            for tok in q_tokens:
                if tok in texto:
                    df = self._token_df.get(tok, total_docs)
                    bonus += 25.0 * (math.log(total_docs / max(df, 1)) / log_total)
            # Números (dosis, FPS, tamaño): el dato más discriminante de un
            # medicamento. Presentes → suben fuerte; ausentes cuando el
            # cliente los pidió → bajan (el F65 le gana al gel de baño).
            if q_numeros:
                texto_nums = set(numeros_de(texto))
                if q_numeros & texto_nums:
                    bonus += 30.0
                else:
                    bonus -= 15.0
            return base + min(bonus, 60.0)

        candidatos = [
            (idx, self._skus[idx], _relevancia(idx)) for idx in set(best_wr) | set(best_ts)
            if not self._skus[idx].pausado
        ]
        # Guarda determinista: lo que no comparte ningún término con lo pedido
        # (ni con sus sinónimos) no se ofrece, por más que el fuzzy lo puntúe.
        candidatos = [c for c in candidatos if resultado_coincide(variantes, self._search_index[c[0]])]
        # Relevancia primero; disponibilidad y ventas sólo desempatan matches parejos.
        candidatos.sort(key=lambda x: (-x[2], 0 if x[1].disponible else 1, -(x[1].ventas_mes or 0)))

        return [self._to_response(s) for _i, s, _sc in candidatos[:top_n]]

    def get_by_id(self, sku_id: str) -> Optional[SKU]:
        for sku in self._skus:
            if sku.sku_id == sku_id:
                return sku
        return None

    def get_by_barcode(self, barcode: str) -> Optional[SKU]:
        hit = self._by_barcode.get((barcode or "").strip())
        if hit:
            return hit
        for sku in self._skus:
            if sku.barcode == barcode:
                return sku
        return None

    @staticmethod
    def _to_response(sku: SKU) -> dict:
        return {
            "sku_id": sku.sku_id,
            "barcode": sku.barcode,
            "nombre": sku.sku_nombre,
            "marca": sku.marca,
            "laboratorio": sku.laboratorio,
            "precio": sku.precio_venta,
            "cantidad_visible": sku.cantidad_visible,
            "estado": sku.estado,
            "categoria": sku.categoria,
            "es_medicamento": sku.es_medicamento,
            "imagen_url": sku.imagen_url,
            "requiere_receta": sku.requiere_receta,
            "clasificacion": sku.clasificacion,
            "urgente": sku.urgente,
            "vendible": sku.vendible,
            "sin_stock": sku.sin_stock,
        }

    @property
    def total(self) -> int:
        return len(self._skus)

    def todos(self) -> list[SKU]:
        """Todos los SKU cargados (para fusiones/exportes, no para el flujo de búsqueda)."""
        return list(self._skus)


_instance: Optional[SKUService] = None


def get_sku_service(csv_path: str = "data/catalogo_base.csv") -> SKUService:
    global _instance
    if _instance is None:
        _instance = SKUService(csv_path)
    return _instance


def reload_sku_service(csv_path: str) -> SKUService:
    """Recarga el catálogo desde disco sin reiniciar el servidor."""
    global _instance
    _instance = SKUService(csv_path)
    logger.info(f"Catálogo recargado: {_instance.total} SKUs")
    return _instance


def set_sku_service(svc: SKUService) -> SKUService:
    """Swap atómico del singleton (recarga desde Postgres/ERP)."""
    global _instance
    _instance = svc
    return _instance
