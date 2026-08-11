# Plan — Métricas de performance (lote 1 y siguientes)

> Estado: **propuesta aprobada en concepto, sin implementar**. Redactado el 2026-08-11.
> Alcance acordado del lote 1: embudo de venta con abandonos (1), búsquedas sin
> resultado (4), fiabilidad de pagos por marca (6) y fallas de envío de WhatsApp (8).
> Lote 2 inmediato: ventas en pesos (2). El resto queda priorizado al final.

## Contexto

Ya existe la base del dashboard histórico (commit `bdc6ca7`):
- Tabla `interacciones` en Postgres (tipo, intención, tiempos por paso, por respuesta).
- `MetricsStore` con agregaciones y `GET /bo/dashboard` + `GET /bo/conversaciones`.
- Página propia `/dashboard` (canvas sin dependencias, clave compartida con `/bo`).

Este plan agrega los **eventos de negocio** que hoy no se registran y sus vistas.

---

## Pieza central: tabla `eventos`

Una sola tabla genérica para todos los eventos de negocio (en lugar de una tabla
por métrica). Migración Alembic `0003_eventos`:

```sql
CREATE TABLE IF NOT EXISTS eventos (
    id          BIGSERIAL PRIMARY KEY,
    tipo        TEXT NOT NULL,          -- ver catálogo abajo
    phone       TEXT,
    dato        TEXT,                   -- payload corto (ej. entidad buscada, marca de tarjeta)
    monto       DOUBLE PRECISION,       -- cuando aplica (links, pagos)
    ref         TEXT,                   -- id externo (pid de pago, order_id)
    extra       JSONB,                  -- detalle libre
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_eventos_tipo   ON eventos (tipo, created_at);
CREATE INDEX IF NOT EXISTS idx_eventos_phone  ON eventos (phone, created_at);
```

Catálogo de tipos (lote 1):

| tipo | cuándo se emite | dato / monto / ref |
|---|---|---|
| `producto_ofrecido` | se setea un pending vendible | dato=nombre SKU, monto=precio, ref=sku_id |
| `link_enviado` | `crear_link_y_responder` devuelve link | monto=total, ref=pid o preference |
| `pago_aprobado` | charge aprobado (Payway) o webhook approved (MP) | monto=total, ref=payway_id/mp_id |
| `pago_rechazado` | intento de cobro rechazado | dato=marca+motivo, ref=id transacción |
| `busqueda_sin_resultado` | `buscar()` devuelve [] para una entidad | dato=entidad pedida |
| `wa_send_fallo` | `send_text/send_image` devuelve False | dato=tipo de mensaje, extra=contexto |

Escritura vía `MetricsStore.evento(tipo, phone=None, dato=None, monto=None, ref=None, extra=None)`
— best-effort como todo lo de Postgres (sin DB, no-op; nunca rompe el flujo).

---

## Lote 1 — pasos detallados

### Paso 0 — Infraestructura (prerequisito de todo)
1. Migración `0003_eventos` (arriba).
2. `MetricsStore.evento(...)` + test contra pgserver (mismo patrón que `test_tabla_interacciones`).
3. Actualizar `test_alembic_version` a `"0003"`.

### Paso 1 — Embudo de venta (métrica 1) y abandonos (métrica 3)
**Instrumentación:**
1. `producto_ofrecido`: en `webhook.py` y `simulate.py`, junto al `set_pending` de
   producto vendible (los dos lugares hasta que se unifique la lógica).
2. `link_enviado`: en `checkout_helper.crear_link_y_responder` (punto único por el
   que pasan bot, simulador y `/bo/paylink`).
3. `pago_aprobado`:
   - Payway: en `payway_charge` cuando `estado == "approved"`.
   - Mercado Pago: en `mp_webhook` cuando `status == "approved"`.

**Agregación (`MetricsStore.embudo(days)`):**
```
conversaciones   (COUNT DISTINCT phone de interacciones)
con_oferta       (phones con evento producto_ofrecido)
con_link         (phones con evento link_enviado)
pagados          (phones con evento pago_aprobado)
% de conversión por etapa + total
links_enviados / links_pagados / % abandono
mediana de tiempo link_enviado → pago_aprobado (por phone+ref)
```

**Exposición:** bloque `embudo` dentro de `GET /bo/dashboard`.

**UI (`dashboard.html`):** sección "Embudo de venta" con 4 barras decrecientes
(estilo funnel con divs, mismo look), % entre etapas, y una tarjeta "Links sin
pagar" con cantidad + monto acumulado (la plata que quedó sobre la mesa).

**Tests:** insertar eventos sintéticos en pgserver → verificar conteos y
porcentajes del embudo; test de que un link pagado no cuenta como abandonado.

### Paso 2 — Búsquedas sin resultado (métrica 4)
**Instrumentación:** en `webhook.py`/`simulate.py`, donde `buscar(entidad)` (y el
fallback semántico) devuelven vacío → `evento("busqueda_sin_resultado", dato=entidad)`.
Normalizar el dato (lower, trim) para que el ranking agrupe bien.

**Agregación:** `MetricsStore.busquedas_sin_resultado(days, limit=20)` →
`[{termino, veces, ultima_vez}]` con `GROUP BY dato`.

**Exposición:** bloque `busquedas_sin_resultado` en `/bo/dashboard`.

**UI:** panel "Nos pidieron y no tenemos" — ranking con contador. Es la vista
que la farmacia usa para decidir compras/altas de catálogo.

**Tests:** dos búsquedas iguales + una distinta → ranking [2,1]; términos se
normalizan (mayúsculas/espacios).

### Paso 3 — Fiabilidad de pagos por marca (métrica 6)
**Instrumentación:**
1. En `payway_charge`: además del registro en Redis (`intentos`), emitir
   `pago_rechazado` con `dato = marca|error_tipo|error_motivo` (marca derivada del
   BIN con `_metodos_por_bin`/card_brand de la respuesta) y `pago_aprobado` con la marca.
2. En `mp_webhook`: `pago_aprobado` (MP no informa rechazo por webhook — se anota
   la asimetría en el panel).

**Agregación:** `MetricsStore.pagos_por_marca(days)` →
`[{marca, intentos, aprobados, rechazados, tasa_aprobacion, motivo_top}]`.

**Exposición:** bloque `pagos` en `/bo/dashboard`.

**UI:** tabla chica "Pagos por tarjeta" con tasa de aprobación y ⚠️ en rojo si
una marca tiene tasa < 50% con 3+ intentos (el caso Mastercard se hubiera visto
el primer día).

**Tests:** eventos sintéticos → tasas correctas; marca con 0 aprobados dispara
el flag de alerta en el payload.

### Paso 4 — Fallas de envío de WhatsApp (métrica 8)
**Instrumentación:**
1. `whatsapp_service.send_text/send_image`: hoy devuelven `False` en silencio.
   Capturar status code y body del error (sin loguear el contenido del mensaje).
2. En los llamadores del flujo (webhook, jobs de cierre/aviso, post-pago):
   si devuelve False → `evento("wa_send_fallo", phone, dato=contexto)` + `logger.warning`.
   Para no tocar 30 call sites: wrapper `send_text_tracked()` en el propio
   service que emite el evento, y migrar los llamadores al wrapper.

**Agregación:** conteo diario en la serie del dashboard + total del período.

**Exposición:** KPI "Envíos fallidos" (rojo si > 0) + columna en `por_dia`.

**UI:** tarjeta KPI adicional; si hay fallas, lista de los últimos 10 con hora
y contexto (job de cierre, respuesta, post-pago).

**Tests:** mock de httpx que devuelve 400 → se emite el evento y `send_text`
sigue devolviendo False sin lanzar.

### Paso 5 — Cierre del lote
1. Suite completa verde + prueba manual del dashboard con datos sintéticos.
2. Commit por paso (0 a 4 separados, revisables).
3. Verificar en producción post-deploy: `/bo/dashboard` responde con los bloques
   nuevos vacíos (sin errores) y se van poblando con el uso.

**Orden de ejecución:** 0 → 1 → 4 → 3 → 2 (el 4 temprano porque es el punto
ciego operativo; el 2 es el más independiente).

---

## Lote 2 — Ventas en pesos (métrica 2)

Requiere persistir pedidos en Postgres (hoy solo Redis con TTL):

1. Migración `0004_orders`: tabla espejo de lo que arma `order_service.create`
   (order_id, phone, sku, cantidad, total, payment_id, tipo_entrega, estados con
   timestamps que ya agregó la trazabilidad de operador).
2. `order_service.create/update` escribe en ambos lados (Redis sigue siendo la
   fuente operativa; Postgres es histórico).
3. `MetricsStore.ventas(days)` → por día: pedidos, facturación, ticket promedio;
   por entrega (retiro/envío); top productos vendidos.
4. Dashboard: sección "Ventas" con serie de facturación + top productos.

**Nota multitenant:** esta tabla nace con columna `tenant_id` nullable ya puesta
(default null = farmacia), para no migrarla de nuevo en la Fase 2 del plan
multitenant.

---

## Backlog priorizado (sin fecha)

| # | Métrica | Nota |
|---|---|---|
| 5 | Salud de la derivación (tomadas, tiempo hasta tomar, cerradas sin tomar) | `derivada_at` y `take` ya existen; falta persistir el par en `eventos` |
| 7 | Uso de fallback de LLM | `intent_service._llamar` sabe qué proveedor respondió; agregarlo a `interacciones.apis` como `proveedor_usado` |
| 10 | Clientes nuevos vs recurrentes / socios vs no socios | sale de `messages` + padrón, solo agregación |
| 9 | Costo por conversación (tokens) | requiere capturar usage de las respuestas LLM; hacerlo antes de cotizar multitenant |
| — | Recordatorio de link abandonado (feature, no métrica) | habilitada por el embudo; job similar al de inactividad |

## Riesgos y decisiones

- **Volumen**: `eventos` crece sin límite → job de retención no urgente
  (a razón de cientos/día tarda años en pesar); revisar al año o al multitenant.
- **Duplicación webhook/simulate**: los eventos de instrumentación se agregan en
  ambos hasta unificar la lógica (deuda ya registrada en plan-multitenant Fase 0).
  Donde exista un punto único (checkout_helper, payway router) se instrumenta ahí.
- **Privacidad**: `busqueda_sin_resultado` guarda texto del cliente → solo la
  entidad de producto detectada, nunca el mensaje completo.
- **MP asimétrico**: sin webhook de rechazo, la tasa por marca solo es completa
  para Payway; se explicita en la UI para no leer mal el dato.
