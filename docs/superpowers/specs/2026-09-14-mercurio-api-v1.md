# Mercurio (Mascotas del Oeste) — API REST v1 y relevamiento

Fuente: "API REST Mercurio — Documentación para integración de sistemas
externos", v1, última actualización 10/9/2026 (Axon). Relevamiento contra
**preproducción** el 14/9/2026 con `scripts/mercurio_explorar.py`; respuestas
crudas en `tests/fixtures/mercurio/`.

Reemplaza al doc SOAP "Conexión Mercurio ERP MO" (mismos conceptos, otro
transporte). El esqueleto zeep de `mercurio_service.py` quedó obsoleto.

## Contrato (resumen de la doc)

- Base `https://api.mercurio.com.ar/v1`, HTTPS, JSON UTF-8.
- Auth `Authorization: Bearer mrc_…` — clave única por sistema, se entrega una
  vez; si se pierde se regenera. **Solo en variable de entorno** (`MERCURIO_API_KEY`).
- Límite 120 req/min por clave → `429` + `Retry-After: 60`.
- Errores: `{"error": true, "message": "..."}`. 401 clave inválida, 403
  desactivada, 413 cuerpo > 1 MB, 422 validación, 5xx reintentar.
- `GET /estado` sin auth: `{"ok": true, "hora": "..."}`.
- Catálogos (misma forma `{catalogo, cantidad, items:[{codigo, descripcion, id_X}]}`):
  `/rubros` (id_rubro), `/marcas` (id_marca), `/materiales` (id_material),
  `/grupos` (id_grupo), `/subgrupos` (id_subgrupo), `/tamanios-mascota`
  (id_tamanio), `/edades-mascota` (id_edad).
- Artículos: `GET /articulos?pagina=N` (100 por página; sin `pagina` o 0 =
  todos, NO usar), `GET /articulos/paginas` → `{por_pagina, paginas}`,
  `GET /articulos/{id}/stock` → `{id_articulo, stock_x_deposito:[...]}`.
- Pedidos: `POST /pedidos` con `Idempotency-Key` (8-100 chars, única por
  pedido, 7 días; reintento → `Idempotent-Replay: true`). 201 →
  `{ok, id_comprobante, numero}`; 422 no se registró. Máx. 200 ítems.
  Campos obligatorios: `id`, `number`, `state`, `customer_id` (CUIT/DNI),
  `total`, `line_items[{variant_id|product_id, quantity, subtotal, total}]`.
  `payment_details[0]`: si `total_financied > 0` reemplaza `total`.

## Lo que la doc no dice y el relevamiento contestó

### Estructura padre / variante
Cada producto viene como un artículo **padre** (`codigo == codigo_padre`,
`precio` 0, `stock` null; ej. "KIPPER PRETAL DE CUERO") más sus **variantes**
vendibles (`codigo` propio, `variacion` "Nº 4" / "400 gr" / "ROSA", precio y
stock). Preprod: 27 artículos = 5 padres + 22 variantes. En el pedido,
`variant_id = codigo` de la variante y `product_id = codigo_padre`.

### Esquema del artículo (todos los valores son **strings**)
```
RowNum, orden, id_articulo_mercurio, codigo, descripcion, descripcion_adicional,
id_grupo, id_subgrupo, id_marca, id_material, id_edad_mascota, id_tamanio_mascota,
observaciones (texto largo de marketing), id_temporada,
precio ("1728.4210000"), stock ("9.00"), presentacion (".40000"),
unidad_presentacion, dsc_unidad_presentacion ("Kgs"), destacado ("0"),
codigo_barras, codigo_ean, info_adicional, alto/largo/ancho/peso,
stock_x_deposito ("1|8.00ç4|0.00ç27|1.00"), id_rubro, talles, codigo_padre,
variacion ("400 gr"), atributos_variacion ("PESO"), *_correo (medidas),
titulo_producto, descripcion_producto, meta_title, meta_descripcion, meta_keywords
```
- **Sin campo de imagen.**
- `codigo_barras` y `codigo_ean` a veces difieren y a veces son códigos
  internos ("15181000"). Se toman los que tienen 8-14 dígitos.
- `stock_x_deposito`: pares `depósito|cantidad` separados por `ç`; depósitos
  vistos **1, 4 y 27**; `stock` = suma. `GET /articulos/{id}/stock` devuelve el
  mismo string en `stock_x_deposito[0].stock_x_zona`.
- Precios de preprod parecen viejos (Royal Canin 400 g a $1.728): dato de
  prueba, no de referencia.
- Latencia 0,5-5 s por request; preprod tiene 1 página, prod ~47.

### Taxonomía (para el índice de búsqueda)
- grupos = tipo de mascota (PERROS, GATOS, PECES, REPTILES, ROEDORES, PÁJAROS,
  VETERINARIA, LIQUIDACIÓN) → `rubro` del ítem.
- rubros = categoría (ALIMENTOS, ACCESORIOS, PIEDRAS SANITARIAS, SALUD, HIGIENE
  Y ESTÉTICA, SNACKS, …) → `category` (nunca "medicamentos bajo receta" →
  `requiere_receta = no`).
- subgrupos (116: SECOS, CORREAS, PRETALES, …) → `subrubro`.
- materiales = etapa (CACHORROS, ADULTOS +7, KITTEN…), edades, tamaños →
  `therapeutic_actions` (entran al texto de búsqueda).
- marcas (526) → `brand`.

## Preguntas abiertas (mail al proveedor, 14/9)
1. Precio: ¿con IVA? ¿lista de venta al público?
2. Depósitos 1/4/27: cuál vende online; ¿el pedido descuenta stock?
3. Cuál es el EAN real entre `codigo_ean` y `codigo_barras`.
4. Imágenes por artículo.
5. `customer_id` para compradores sin alta; consulta de cliente por DNI.
6. Valores de `state` y `payment_method` (tarjeta online, cuenta corriente, retiro).
7. Cambios incrementales / webhooks; `GET` de pedido por `id_comprobante`.
8. Pase a producción (clave productiva). Turnos: sin API todavía.

## Implementación (servidor)
- `app/services/mercurio_service.py`: `MercurioClient` (httpx, Bearer,
  reintentos: 429 → `Retry-After`, 5xx → 1/2/4 s), `parsear_stock_x_deposito`,
  `es_padre`, `articulo_a_item` (→ `CatalogItemIn`, hash sha256 de los campos
  que usa el bot), `MercurioSync.sincronizar()` (taxonomías + páginas →
  variantes → `catalog_items` con `source="mercurio"`; ausentes → inactivos vía
  `full_manifest`; heartbeat en `branches`; recarga si es la sucursal activa).
- Job periódico en `main.py` (`MERCURIO_API_KEY` + `DATABASE_URL`), intervalo
  `MERCURIO_SYNC_INTERVAL_SECS` (900). Manual: `POST /bo/mercurio/sync`,
  estado: `GET /bo/mercurio/estado`.
- Stock en vivo: `catalog_live.lookup_vivo` despacha al agente (WS) o a
  `GET /articulos/{id}/stock` según la sucursal; lo usan la oferta y el cobro.
- Alta de pedidos: pendiente, detrás de `MERCURIO_PEDIDOS_ENABLED` (false).
- Un deploy = una sucursal activa: Mascotas del Oeste corre en su propio
  servicio de Railway con su Postgres. Si conviviera con farmacia-mutual en la
  misma base, haría falta `DEFAULT_BRANCH_ID`.

## Incidente 15/9 — convivencia de sucursales

El sync corrió en el deploy de la farmacia (la clave estaba en Railway) y creó
`mascotas-oeste` junto a `farmacia-mutual`: con dos sucursales y sin
`DEFAULT_BRANCH_ID` el bot quedó sin sucursal activa, dejó de recargar y se
congeló con datos viejos (OFF Defense: base stock 1, memoria 0). Correcciones:
resolución pegajosa (mantiene la sucursal que venía usando y reporta
`conflicto` en `/bo/catalogo/estado`), y `MercurioSync` aborta con
`MercurioConvivenciaError` si la base ya tiene otra sucursal con catálogo,
salvo `MERCURIO_CONVIVIR=true` + `DEFAULT_BRANCH_ID`.
