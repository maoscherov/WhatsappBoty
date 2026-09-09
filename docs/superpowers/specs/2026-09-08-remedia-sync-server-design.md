# Remedia — lado servidor del sync de catálogo (`/v1/sync/*`, `/v1/agent/ws`)

Contraparte en la API de Remedia (`app/`, FastAPI) del agente `remedia-agent/`
(Rust, servicio de Windows en la farmacia). El agente ya está implementado y
testeado: **este documento diseña el servidor para el contrato que el agente ya
habla**, no al revés. Contrato de referencia:
[`2026-09-08-remedia-agent-design.md`](2026-09-08-remedia-agent-design.md) §2,
`remedia-agent/src/remedia/client.rs` y `remedia-agent/src/remedia/ws.rs`.

Fecha: 2026-09-08.

---

## 0. Resumen

Hoy el catálogo de Remedia es **un CSV cargado en memoria** (`SKUService`) que la
farmacia sube a mano desde el panel (`/bo/sku/import*`). Con el agente, el catálogo
pasa a **entrar solo desde el ERP**, sucursal por sucursal, con stock y precio
actualizados cada 15 minutos y consulta en vivo al momento de cobrar.

Cuatro piezas:

1. **Registro de sucursales** con token propio (tabla `branches`, auth Bearer).
2. **Recepción del catálogo** (`/v1/sync/catalog`, `/v1/sync/full-manifest`,
   `/v1/sync/heartbeat`) → tabla `catalog_items` en Postgres.
3. **El bot lee el catálogo desde Postgres** en lugar del CSV cuando la sucursal
   tiene datos del ERP; el CSV sigue funcionando para clientes sin ERP.
4. **Websocket saliente del agente** (`/v1/agent/ws`) → consulta de stock en vivo
   antes de generar el link de pago, y `sync_now` desde el panel.

Fuera de alcance: multitenant completo (`docs/plan-multitenant.md`), usuarios del
panel, alertas por WhatsApp/email cuando una sucursal se cae, ofertas.

---

## 1. Contexto y restricciones del código actual

Relevado el 2026-09-08 (rutas con línea para el implementador):

- **Catálogo en memoria**: `app/services/sku_service.py` — `SKUService(csv_path)`
  carga un CSV en `self._skus: list[SKU]` + `self._search_index`; búsqueda fuzzy con
  rapidfuzz (`buscar`, :266), `get_by_id` (:352), `get_by_barcode` (:358),
  `_to_response` (:365). Singleton `get_sku_service()` / `reload_sku_service()`
  (:397-409). Modelo `app/models/sku.py` con propiedades derivadas (`sin_stock`,
  `vendible`, `estado`) que gobiernan la venta. **La búsqueda se queda en memoria**:
  Postgres no reemplaza rapidfuzz.
- **Receta**: `requiere_receta` se deriva en `_parse_base` (:211-215): columna
  explícita > `categoria == "medicamentos bajo receta"` > override a `"no"` si
  `es_venta_libre(nombre)` (lista blanca OTC, :58-71) o `_categoria_sin_receta`.
  El Excel de `POST /bo/sku/import-receta` puede pisar el flag por SKU.
- **Persistencia**: Postgres vía asyncpg (`app/services/db.py`), pool de 5,
  solo `execute`/`fetch` que **tragan errores** y devuelven `None`/`[]`. Sin
  transacciones ni `executemany`. Migraciones Alembic en SQL crudo; la última es
  `0004_config.py`; se aplican al arrancar (`app/main.py:66-76`).
- **Auth existente**: `BO_KEY` compartida, fail-open si está vacía
  (`backoffice.py:41-47`). No hay Bearer ni auth por sucursal.
- **Servidor**: uvicorn, **un solo worker** (`Procfile`, `Dockerfile`). Sin
  websockets en el proyecto (uvicorn[standard] ya trae soporte). Sin pub/sub de Redis.
- **Punto único del cobro**: `app/services/checkout_helper.py:484`
  `crear_link_y_responder(...)` — arma el total y en :552 llama a
  `payment_svc.crear_link(...)`. Pasan por ahí el bot, el simulador y el panel.
- **Sin stock**: `webhook.py:1536-1556` aplica `sin_stock_mode`
  (`preguntar | derivar | nunca`) con mensajes configurables.
- **Tests**: `tests/conftest.py` levanta Postgres embebido (pgserver) y aplica
  `alembic upgrade head`; `pytest.ini` con `asyncio_mode = auto`. No hay tests
  HTTP con `TestClient` todavía. `tests/test_db_rag.py:36` asume `version_num == "0003"`
  (ya está desactualizado).
- **Escala** (spec del agente §1): ~54k productos por sucursal, ~5.7k con stock,
  ~15.7k con precio 0, ~6.3k sin CB, ~1.2k CB compartidos. `external_id`
  (`idProducto`) es la clave; el CB es índice secundario no único.

---

## 2. Decisiones

| # | Decisión | Por qué |
|---|---|---|
| D1 | **Postgres es la fuente de verdad del catálogo ERP**; el bot lo carga a memoria por sucursal y lo recarga tras cada sync. | La búsqueda fuzzy necesita todo en memoria; Postgres da persistencia, historia y multi-sucursal. 54k filas cargan en ~1-2 s. |
| D2 | **Una sucursal activa por despliegue** (`default_branch_id`) hasta que exista multitenant. El esquema ya lleva `branch_id` en todo. | Hoy hay un comercio por deploy. El plan multitenant mapeará tenant → sucursal sin cambiar tablas. |
| D3 | **El CSV sigue vivo** como fallback y para clientes sin ERP. Si la sucursal por defecto tiene filas en `catalog_items`, gana Postgres; si no, CSV. | Mascotas del Oeste y otros no tienen ObServer. |
| D4 | **Auth Bearer por sucursal, fail-closed.** Token aleatorio, guardado hasheado, se muestra una sola vez. | Un secreto compartido no sirve con varias sucursales. No copiar el fail-open de `BO_KEY`. |
| D5 | **Registro de conexiones websocket en memoria del proceso.** | Un solo worker. Si algún día hay `--workers N`, hace falta Redis pub/sub; queda documentado en el código. |
| D6 | **Consulta en vivo al cobrar = solo stock, fail-open.** Sin agente o con timeout se cobra con el dato cacheado. Precio: se usa el cotizado; si el ERP devuelve otro se loguea y se marca en la orden. | Frenar una venta porque el agente está caído es peor que vender con stock de 15 minutos. Recotizar precio en medio del checkout es otra conversación (futuro). |
| D7 | **Datos que no vienen del ERP** (receta manual, imagen, rotación, pausa manual) viven en `catalog_extras` y sobreviven a los syncs. | El sync pisa `catalog_items` entero; lo manual no puede vivir ahí. |
| D8 | **RAG (pgvector) se reindexa incremental** solo para los `external_id` cambiados, y solo si `openai_api_key` está seteada. | `reindex_catalogo` hoy es total; un delta de 3 productos no puede re-embeber 54k. |
| D9 | `Database` gana `transaction()` y `executemany()`; **los endpoints de sync propagan errores** (500) en lugar de tragarlos. | El agente reintenta ante no-2xx; un 200 sobre un write fallido desincroniza la sucursal hasta el manifiesto siguiente. |

---

## 3. Modelo de datos (migración `0005_catalog_sync.py`, `down_revision = "0004"`)

```sql
CREATE TABLE IF NOT EXISTS branches (
    branch_id        TEXT PRIMARY KEY,                 -- "farmacia-xxx", [a-z0-9-]{3,40}
    nombre           TEXT NOT NULL,
    token_hash       TEXT NOT NULL,                    -- sha256 hex del token
    activa           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- último heartbeat (se pisa entero en cada uno)
    last_heartbeat_at TIMESTAMPTZ,
    agent_version    TEXT,
    erp_version      TEXT,
    erp_status       TEXT,                             -- ok | no_autorizado | inalcanzable | error
    last_sync_ok_at  TIMESTAMPTZ,                      -- lo reporta el agente
    catalog_count    INTEGER,
    pending_batches  INTEGER,
    -- lo que ve el servidor
    last_catalog_push_at TIMESTAMPTZ,
    last_manifest_at     TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS branches_token_hash ON branches(token_hash);

CREATE TABLE IF NOT EXISTS catalog_items (
    branch_id            TEXT NOT NULL REFERENCES branches(branch_id) ON DELETE CASCADE,
    external_id          TEXT NOT NULL,                -- idProducto del ERP
    hash                 TEXT NOT NULL,                -- blake3 hex que manda el agente
    barcodes             TEXT[] NOT NULL DEFAULT '{}',
    troquel              BIGINT,
    name                 TEXT NOT NULL,
    brand                TEXT,
    drug                 TEXT,
    form                 TEXT,
    category             TEXT NOT NULL DEFAULT '',
    rubro                TEXT NOT NULL DEFAULT '',
    subrubro             TEXT NOT NULL DEFAULT '',
    therapeutic_actions  TEXT[] NOT NULL DEFAULT '{}',
    price                NUMERIC(12,2),                -- NULL = sin precio (no gratis)
    stock                INTEGER NOT NULL DEFAULT 0,
    visible              BOOLEAN NOT NULL DEFAULT TRUE,
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    requiere_receta      TEXT NOT NULL DEFAULT 'no',   -- derivado al escribir (ver §5.1)
    source               TEXT NOT NULL DEFAULT 'observer-gestion',
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, external_id)
);
CREATE INDEX IF NOT EXISTS catalog_items_barcodes ON catalog_items USING GIN (barcodes);
CREATE INDEX IF NOT EXISTS catalog_items_branch_active ON catalog_items(branch_id) WHERE active AND visible;

CREATE TABLE IF NOT EXISTS catalog_extras (
    branch_id                TEXT NOT NULL,
    external_id              TEXT NOT NULL,
    requiere_receta_override TEXT,                     -- si | ambiguo | no | NULL (sin override)
    pausado_manual           BOOLEAN NOT NULL DEFAULT FALSE,
    imagen_url               TEXT,
    ventas_mes               DOUBLE PRECISION,
    prom_semanal             DOUBLE PRECISION,
    clasificacion            TEXT,                     -- critico | riesgo_alto | ...
    tipo_producto            TEXT,                     -- regular | estacional
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, external_id)
);
```

`sku_embeddings` (RAG) no cambia de forma; `sku_id` pasa a ser `external_id`
para sucursales ERP. Cuando llegue multitenant se le agrega `branch_id`; hoy hay
una sola sucursal por deploy (D2).

Corregir `tests/test_db_rag.py:36` para que compare contra el head real de
Alembic en vez de un número fijo.

---

## 4. Auth por sucursal

- **Token**: `secrets.token_urlsafe(32)`; se guarda `sha256(token)` en
  `branches.token_hash`. Se muestra **una sola vez** al crear o rotar.
- **Dependencia `require_branch`** (`app/services/branch_auth.py`):
  1. Header `Authorization: Bearer <token>`; si falta o no es Bearer → **401**.
  2. `SELECT ... FROM branches WHERE token_hash = $1` con el hash del token; sin
     fila → **401**. `activa = false` → **403**.
  3. Devuelve `Branch(branch_id, nombre, activa)`.
  4. Los handlers verifican `body.branch_id == branch.branch_id`; si no → **403**
     `branch_id no coincide con el token`.
- Sin `DATABASE_URL` o Postgres caído: **503** `catálogo ERP no disponible`
  (el agente reintenta con backoff). Nunca fail-open.
- El lookup por hash evita comparaciones de tiempo variable; no se compara el
  token en claro.
- `CORSMiddleware` está en `allow_origins=["*"]`: los endpoints `/v1/*` no
  necesitan CORS y no lo usan (el agente no es un browser). No se cambia la
  política global en este trabajo; queda anotado en `plan-multitenant.md`.

Endpoints del panel (auth `BO_KEY` como el resto de `/bo/*`):

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/bo/branches` | Body `{branch_id, nombre}`. Crea la sucursal y devuelve `{branch_id, token}`. 409 si existe. |
| `GET` | `/bo/branches` | Lista con estado: último heartbeat, `erp_status`, `catalog_count`, `pending_batches`, conectado por WS sí/no, ítems en `catalog_items`. |
| `POST` | `/bo/branches/{id}/rotate-token` | Nuevo token; el anterior deja de servir en el acto. |
| `PATCH` | `/bo/branches/{id}` | `{activa, nombre}`. |
| `POST` | `/bo/branches/{id}/sync-now` | Manda `{"op":"sync_now"}` por WS. 409 si no está conectada. |
| `GET` | `/bo/branches/{id}/lookup?barcode=…&id=…` | Lookup en vivo (debug/operador). 504 si el agente no responde. |
| `PUT` | `/bo/catalog/{id}/extras` | Escribe `catalog_extras` de la sucursal por defecto (imagen, receta manual, pausa manual). Dispara recarga. |

`POST /bo/sku/import-receta` (Excel) escribe `requiere_receta_override` en
`catalog_extras` cuando la sucursal por defecto es ERP; si no, sigue como hoy.

---

## 5. Recepción del catálogo

### 5.1 `POST /v1/sync/catalog`

Body (`CatalogBatch`, ver contrato) validado con Pydantic:

- `schema_version == 1` → si no, **422** `schema_version no soportado`.
- `mode ∈ {"delta","full"}` (informativo, se loguea).
- `1 ≤ len(items) ≤ 1000`. El agente manda 500.
- `CatalogItem.price` llega como **string** (`"19641.26"`) o `null` → `Decimal | None`.
  No usar `float`.
- `external_id` no vacío; `hash` 64 hex.

Procesamiento, **en una transacción**:

1. Para cada ítem calcular `requiere_receta` con `derivar_requiere_receta(category,
   rubro, subrubro, name)` (`app/services/catalog_rules.py`), que reproduce la
   regla actual: `"si"` si `category` o `rubro` o `subrubro` normalizados son
   `"medicamentos bajo receta"`; `"no"` si `es_venta_libre(name)` o
   `_categoria_sin_receta(category)`; `"no"` en cualquier otro caso. **La lista
   blanca OTC se reutiliza tal cual** de `sku_service.py`.
2. `executemany` de:
   ```sql
   INSERT INTO catalog_items (branch_id, external_id, hash, barcodes, troquel, name, brand,
       drug, form, category, rubro, subrubro, therapeutic_actions, price, stock, visible,
       active, requiere_receta, source, updated_at)
   VALUES ($1,…,$19, now())
   ON CONFLICT (branch_id, external_id) DO UPDATE SET
       hash = EXCLUDED.hash, barcodes = EXCLUDED.barcodes, …, updated_at = now()
   WHERE catalog_items.hash IS DISTINCT FROM EXCLUDED.hash
      OR catalog_items.active IS DISTINCT FROM EXCLUDED.active
   ```
   La segunda condición reactiva productos que el manifiesto había desactivado
   (§5.2) aunque su hash no haya cambiado.
   `upserted` = filas afectadas (asyncpg devuelve `INSERT 0 n`); `unchanged =
   received - upserted`.
3. `UPDATE branches SET last_catalog_push_at = now()`.
4. Tras el commit: encolar en `CatalogRefresher` los `external_id` cambiados
   (§6.2) y, si `batch == total_batches`, forzar la recarga inmediata.

Respuesta **200** `{ "received": n, "upserted": k, "unchanged": n-k }`.
Cualquier error de DB → **500** con detalle corto; el agente encola y reintenta.
Idempotente: reenviar el mismo lote da `upserted = 0`.

### 5.2 `POST /v1/sync/full-manifest`

Body `{branch_id, generated_at, items: [{external_id, hash}]}`, ~54k entradas
(~5 MB). Sin límite de tamaño en uvicorn; verificar en Railway al desplegar
(si hubiera límite, el agente ya soporta 500 → se agrega paginación al contrato).

1. `SELECT external_id, hash FROM catalog_items WHERE branch_id = $1` → dict.
2. `resend` = ids del manifiesto cuyo hash difiere o que no existen en DB.
3. `deactivated`: `UPDATE catalog_items SET active = false, updated_at = now()
   WHERE branch_id = $1 AND active AND NOT (external_id = ANY($2))` con la lista
   de ids del manifiesto. Devuelve la cantidad.
4. `UPDATE branches SET last_manifest_at = now()`; encolar recarga.

Respuesta **200** `{ "resend": [...], "deactivated": n }`.

Reactivación: cuando un producto desaparece del ERP, el agente lo poda de su
estado local en el mismo manifiesto. Si más adelante vuelve al ERP, el agente lo
ve como nuevo y lo manda en el próximo `catalog` con `active = true`; el upsert de
§5.1 lo reactiva aunque el hash coincida con el guardado.

### 5.3 `POST /v1/sync/heartbeat`

Body `Heartbeat` del contrato. `UPDATE branches SET last_heartbeat_at = now(),
agent_version, erp_version, erp_status, last_sync_ok_at, catalog_count,
pending_batches`. Respuesta **204**.

Estado derivado para el panel (`GET /bo/branches` y badge en el panel):

| Condición | Estado |
|---|---|
| `last_heartbeat_at` > 15 min o NULL | `sin_agente` |
| `erp_status != "ok"` | `erp_<status>` (p. ej. `erp_no_autorizado`) |
| `pending_batches > 0` | `atrasada` |
| resto | `ok` |

---

## 6. El bot lee el catálogo desde Postgres

### 6.1 Carga

`SKUService` gana un constructor alternativo `SKUService.from_rows(rows, extras)`
que arma los `SKU` a partir de `catalog_items ⋈ catalog_extras` y reutiliza el
mismo índice de búsqueda. Mapeo:

| `SKU` | Origen |
|---|---|
| `sku_id` | `external_id` |
| `barcode` | `barcodes[0]` o `""` |
| `sku_nombre`, `sku_nombre_original` | `name` |
| `marca` | `brand` (el ERP no distingue marca de laboratorio) |
| `laboratorio` | `brand` |
| `categoria` | `category` (`rubro`/`subrubro` entran al índice de búsqueda, no al modelo) |
| `es_medicamento` | `category` normalizada empieza con `"medicamento"` |
| `precio_venta` | `price` o `0.0` (→ `estado = "consultar"`, como hoy) |
| `stock_actual`, `cantidad_visible` | `stock`, `max(stock, 0)` |
| `pausado` | `not visible or extras.pausado_manual` |
| `requiere_receta` | `extras.requiere_receta_override` o `catalog_items.requiere_receta` |
| `imagen_url`, `ventas_mes`, `prom_semanal`, `clasificacion`, `tipo_producto` | `catalog_extras` |

Se cargan solo filas con `active = true`. Los `visible = false` se cargan como
`pausado` para que `get_by_id` los encuentre si un cliente los tenía pendientes.

Búsqueda: además de `get_by_barcode` (primer CB), `SKUService` indexa **todos**
los CB de cada producto (`self._by_barcode: dict[str, SKU]`); ante CB compartido
gana el que tiene stock, después el visible.

### 6.2 Recarga (`CatalogRefresher`, `app/services/catalog_refresher.py`)

- Singleton con `schedule(branch_id, changed_ids: set[str])`.
- Debounce: recarga **3 s** después de la última llamada, o inmediata cuando el
  handler indica fin de corrida (`batch == total_batches`, manifiesto). Nunca dos
  recargas concurrentes (lock).
- La recarga: `SELECT` de la sucursal, `SKUService.from_rows`, swap atómico del
  singleton (`_instance = nuevo`). Log con cantidad y duración.
- Si `openai_api_key` está seteada: `RagService.reindex_ids(branch_id, changed_ids)`
  (nuevo, incremental; embebe solo esos ids, en lotes de 256). Si `changed_ids`
  supera 5.000 (primera carga, manifiesto grande) se difiere a una tarea de fondo
  para no bloquear.
- En arranque (`lifespan`): si `DATABASE_URL` está y `catalog_items` tiene filas
  para `default_branch_id`, se carga desde Postgres **antes** que el CSV; si no,
  CSV como hoy. El blob de Redis del CSV no se toca.

### 6.3 Config nueva (`app/config.py`)

```python
default_branch_id: str = ""     # sucursal cuyo catálogo usa el bot; vacío = solo CSV
live_stock_check: str = "stock" # off | stock  (D6)
live_lookup_timeout_s: float = 5.0
```

---

## 7. Websocket `/v1/agent/ws` y consulta en vivo

### 7.1 Conexión

`@router.websocket("/v1/agent/ws")` en `app/routers/agent_ws.py`.

1. Leer `Authorization` del handshake; resolver sucursal con la misma lógica de
   §4. Si falla: `await ws.close(code=1008)` **sin** `accept()` (el cliente ve 403).
2. `accept()`. Esperar el primer mensaje ≤ 10 s; debe ser
   `{"op":"hello","branch_id","agent_version"}` con `branch_id` igual al del
   token; si no, cerrar con 1008.
3. Registrar en `AgentRegistry` (`app/services/agent_registry.py`): si ya había
   una conexión de esa sucursal, cerrarla (1000) y reemplazar. Actualizar
   `branches.agent_version`.
4. Loop de lectura:
   - `{"op":"ping"}` → responder `{"op":"pong"}` (**texto**, no frame de protocolo;
     el agente corta tras 2 pings sin pong de texto).
   - `{"op":"pong"}` → nada.
   - `{"op":"lookup_result", req_id, items, missing}` → resolver el `Future`
     pendiente de ese `req_id`; si no existe (llegó tarde), descartar con log.
   - Otro `op` → log, ignorar.
   - Sin mensajes por 90 s → cerrar (el agente pinga cada 30 s).
5. `WebSocketDisconnect` o error → desregistrar; los futures pendientes de esa
   sucursal se resuelven con `None`.

### 7.2 `AgentRegistry`

```python
class AgentRegistry:
    def register(branch_id, ws) / unregister(branch_id, ws)
    def connected(branch_id) -> bool
    async def lookup(branch_id, barcodes=[], ids=[], timeout=5.0) -> LookupResult | None
    async def sync_now(branch_id) -> bool
```

`lookup`: `req_id = uuid4().hex`, crea `asyncio.Future`, envía
`{"op":"lookup","req_id","barcodes","ids"}`, espera `timeout`; en timeout limpia
y devuelve `None`. `LookupResult(items: list[CatalogItemIn], missing: list[str])`.
Estado en memoria del proceso (D5); comentario en el módulo advirtiendo que con
varios workers hay que pasar a Redis pub/sub.

### 7.3 Chequeo en vivo al cobrar (D6)

En `crear_link_y_responder` (`checkout_helper.py`), **antes** de
`payment_svc.crear_link` (:552), si `live_stock_check == "stock"` y la sucursal
por defecto está conectada:

1. `ids` = `sku_id` de cada ítem pendiente (`pending_items` o `pending_sku_id`).
2. `res = await registry.lookup(branch, ids=ids, timeout=live_lookup_timeout_s)`.
3. `None` (sin agente/timeout) → seguir con lo cacheado; log `warning`.
4. Por cada ítem devuelto: actualizar en memoria (`SKUService`) y en
   `catalog_items` su `stock` y `price`. Si `stock < cantidad pedida` →
   **no generar el link**: `clear_pending`, mensaje configurable
   `live_sin_stock_message` (default: "Justo me fijé y no nos queda stock de
   {producto}. ¿Querés que lo consultemos con el equipo?") y aplicar
   `sin_stock_mode` como en `webhook.py:1541` (`derivar` → estado `operador`).
5. Si el precio difiere del cotizado: se cobra el cotizado (D6), se loguea
   `warning` y se guarda `precio_erp` en `extra` del evento `link_enviado`.
6. Ítems en `missing` (el ERP ya no los conoce) → tratar como stock 0.

Todo el bloque es best-effort: cualquier excepción se loguea y se sigue al link.
Latencia sumada al cobro: ≤ 5 s en el peor caso (el ERP tarda decenas de ms).

---

## 8. Estructura de archivos

```
app/
├── config.py                      # + default_branch_id, live_stock_check, live_lookup_timeout_s
├── routers/
│   ├── sync_api.py                # POST /v1/sync/catalog | full-manifest | heartbeat
│   ├── agent_ws.py                # WS /v1/agent/ws
│   └── backoffice.py              # + /bo/branches*, /bo/catalog/{id}/extras (o módulo backoffice_branches.py)
├── services/
│   ├── db.py                      # + transaction(), executemany(), fetchrow(); errores propagables
│   ├── branch_auth.py             # require_branch, crear/rotar token
│   ├── branch_store.py            # CRUD branches + heartbeat + estado derivado
│   ├── catalog_store.py           # upsert_items, full_manifest, load_rows(branch), update_stock
│   ├── catalog_rules.py           # derivar_requiere_receta (reusa VENTA_LIBRE)
│   ├── catalog_refresher.py       # debounce + swap de SKUService + reindex incremental
│   ├── agent_registry.py          # conexiones WS, lookup, sync_now
│   ├── sku_service.py             # + from_rows, índice por todos los CB
│   ├── rag_service.py             # + reindex_ids
│   └── checkout_helper.py         # + chequeo en vivo antes de crear_link
├── models/
│   └── sync.py                    # Pydantic: CatalogItemIn, CatalogBatchIn, ManifestIn, HeartbeatIn, respuestas
migrations/versions/0005_catalog_sync.py
tests/
├── test_sync_api.py               # httpx.AsyncClient + Postgres embebido: auth, upsert, idempotencia, manifest, heartbeat
├── test_catalog_load.py           # from_rows, mapeo, extras, CB compartido, receta derivada
├── test_agent_ws.py               # starlette TestClient websocket: hello, ping/pong, lookup round-trip, timeout, reemplazo de conexión
└── test_checkout_live.py          # crear_link_y_responder con registry falso: sin stock frena, timeout sigue, precio distinto loguea
```

---

## 9. Casos que el servidor debe manejar

- Token inválido, sucursal inactiva, `branch_id` del body distinto del token → 401/403.
- Postgres caído → 503 en `/v1/sync/*`; el WS acepta igual (lookup no necesita DB) pero el chequeo de cobro no actualiza `catalog_items`.
- Mismo lote dos veces (reintento del agente tras timeout de red) → `upserted = 0`.
- Lote con 1.000 ítems → 422 si más.
- Agente reconecta desde otra IP mientras la conexión vieja sigue "viva" → la nueva reemplaza a la vieja.
- Dos lookups simultáneos a la misma sucursal → `req_id` distintos, se resuelven independientemente.
- Manifiesto vacío (`items: []`) → **no** desactivar todo: 422 `manifiesto vacío` (protege contra un ERP que respondió 400 en el lote 1).
- Primera carga de 54k: 109 lotes en ~1-2 min; la recarga en memoria se hace una vez al final (debounce) más una por minuto como máximo mientras entran lotes.
- Producto pendiente en la sesión de un cliente que el manifiesto desactiva → `get_by_id` lo sigue encontrando (se carga como `pausado`); el chequeo en vivo al cobrar lo frena.

---

## 10. Tests

- **Unit**: `derivar_requiere_receta` (categoría bajo receta, OTC en lista blanca, perfumería); `SKUService.from_rows` (mapeo completo, `price NULL → consultar`, `visible=false → pausado`, override de receta, CB compartido con preferencia por stock).
- **Integración HTTP** (`httpx.AsyncClient(app=app)` + pgserver): 401 sin token; 403 con `branch_id` ajeno; upsert de un lote real del fixture del agente (`remedia-agent/tests/fixtures/lote1.json` convertido a `CatalogItem` — o un fixture JSON propio con el mismo shape); segundo envío `upserted = 0`; cambio de stock `upserted = 1`; manifiesto desactiva ausentes y pide `resend` de hash distinto; manifiesto vacío 422; heartbeat 204 y estado derivado `sin_agente`/`ok`.
- **WS** (`starlette.testclient.TestClient.websocket_connect`): rechazo sin Bearer; hello + ping → pong; lookup round-trip resolviendo el future; timeout devuelve `None`; segunda conexión reemplaza la primera.
- **Checkout**: con `AgentRegistry` falso: stock suficiente → link; stock 0 → sin link, `clear_pending`, mensaje; timeout → link con cache; `live_stock_check = off` → no consulta.
- **Degradación** (`tests/test_degradation.py`): sin `DATABASE_URL`, `/v1/sync/*` responde 503 y el bot sigue con CSV.

---

## 11. Orden de implementación sugerido

1. Migración 0005 + `db.py` (transacción, executemany) + `branch_store` + `branch_auth` + `/bo/branches*`.
2. `catalog_rules` + `catalog_store` + `/v1/sync/*` con tests HTTP.
3. `SKUService.from_rows` + `catalog_refresher` + arranque desde Postgres + `default_branch_id`.
4. `agent_registry` + `/v1/agent/ws` + `/bo/branches/{id}/sync-now` y `lookup`.
5. Chequeo en vivo en `checkout_helper` + config `live_stock_check`.
6. `rag_service.reindex_ids` + `catalog_extras` desde `import-receta` y `PUT /bo/catalog/{id}/extras`.

Cada paso deja el sistema funcionando; 1-2 ya permiten conectar el agente real y
ver el catálogo entrar aunque el bot todavía lea el CSV.
