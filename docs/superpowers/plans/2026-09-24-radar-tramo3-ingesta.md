# Radar tramo 3 — Ingesta y sincronización Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que un vínculo en `WORKING` empiece a llenar la base de Radar sin que entre nada de lo que el dueño no eligió: receptor de `message.*` con filtro de exclusión antes de persistir, pasada de conteo y pantalla P4, backfill en pasadas, reconciliación en dos pasos con `observado_hasta`, detección de silencio y el progreso de P5 y C1.

**Architecture:** El receptor `/webhook/waha` resuelve vínculo y chat por HMAC, decide (descartar sin payload / solo `{id,timestamp,fromMe}` / payload completo) y escribe `webhook_inbox` en el almacén de fuente; el worker normaliza con un job `ingesta_inbox`. Tres jobs más, todos por vínculo y auto-reprogramables: `conteo` (P4), `backfill` (pasadas +0/+30 min/+6 h/+24 h) y `reconciliacion` (dos pasos, cadencia 15/60 min, silencio). La idempotencia la fija `wa_message_provider_ids` (UNIQUE por línea) en el almacén de fuente: el UUID del mensaje sale de ahí y la fila sin texto de `wa_messages` se hace upsert con ese UUID en la base de resultados. Todo lo que entiende la forma cruda de WAHA vive en `app/radar/ingesta/reduccion.py`; las reglas de tiempo, en `app/radar/ingesta/reglas.py` (puras).

**Tech Stack:** Python 3.12, FastAPI, asyncpg, Alembic (dos árboles: `migrations_radar/`, `migrations_fuente/`), Postgres con RLS forzada en las dos bases, httpx con `MockTransport` (`tests/radar_tests/waha_falso.py`), pytest + pgserver, JS sin dependencias.

**Spec:** `docs/superpowers/specs/2026-09-21-onboarding-radar-whatsapp-design.md` (v5.2): §0.4, §0.6, §3 P4 y P5, Estados especiales, §3.1 C1, §4.1–4.2, §6.3 puntos 1–5 y 8, §6.4, §7 (retención de `webhook_inbox`), §8 tramo 3, §9 guardas.

**Rama sugerida:** `feature/radar-tramo3`, creada desde `feature/radar-tramo2`.

```bash
git switch feature/radar-tramo2 && git switch -c feature/radar-tramo3
python -m pytest tests/radar_tests -q      # línea de base: todo verde antes de empezar
```

## Global Constraints

- **Nada de contenido fuera del almacén de fuente.** La base de resultados no guarda texto, teléfonos, JID, lids ni ids de proveedor: solo UUID propios, HMAC (`contact_hmac`, `lid_hmac`), números, fechas y enums. El test de esquema de §9 lo verifica (se amplía para aceptar columnas `*_hmac` y nada más).
- **Filtro antes de persistir** (§6.3 punto 1): vínculo con `links.ingesta` NULL, `contando` o `pendiente_de_seleccion` → se descarta sin payload; chat excluido o contacto suprimido → se descarta sin payload; chat pendiente de decisión o desconocido → solo `{id, timestamp, fromMe}`. `webhook_inbox` nunca guarda el payload de un descartado (CHECK en la tabla) y borra el payload al normalizar.
- **Lecturas de WAHA:** solo por `WahaCliente` con lista blanca y `fullmatch`; rutas nuevas: `GET /api/{s}/chats`, `GET /api/{s}/chats/{chat}/messages` (con `chat` = `all` o un JID individual) y `GET /api/{s}/lids`. `downloadMedia=false` se fuerza en el cliente en toda lectura de mensajes. Nunca `chats/overview`. Las lecturas usan la **clave de lectura del vínculo** (`waha_lectura:{link_id}` en el SecretStore); solo el reinicio por silencio usa la clave admin del worker (`cliente_de`).
- **Paginación** (plan del spike, §6.3 punto 2): `offset += limit`; el fin es una **página vacía** (cero ítems crudos), nunca una corta.
- **`chats/all/messages`** solo en la pasada inicial del backfill y en la pasada de conteo (antes de que exista cualquier exclusión), y nunca en la reconciliación. Los chats excluidos o suprimidos no se leen nunca en la reconciliación (test de contrato).
- **Todo acceso a tablas con tenant** pasa por `RadarDB.tenant_tx(tenant_id)` (resultados) o `FuenteStore.tenant_tx(tenant_id)` (fuente, nuevo). Las funciones que cruzan tenants son `SECURITY DEFINER` creadas con `definir_funcion_admin`.
- **GRANT UPDATE por columna** con el dict `UPDATES_POR_COLUMNA` de cada migración, como r0002/r0003. `radar_app` nunca actualiza `id`, `tenant_id`, FK de pertenencia ni `created_at`.
- **Logs:** nunca payload, body, teléfono, JID, nombre ni id de proveedor; solo UUID internos y `type(e).__name__`. `MensajeWaha`, `EventoMensaje`, `ChatWaha` y `Jid` tienen `__repr__` sin contenido.
- **Tests:** nunca un WAHA real ni herramientas MCP de WAHA; siempre `WahaFalso`. Correr `python -m pytest tests/radar_tests -q` completo al final de cada task.
- **Commits** en español, uno por task como mínimo, terminando con la línea `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Decisiones de este plan

1. **Reparto receptor / worker / jobs.** El receptor hace lo mínimo que exige §6.3 punto 1: verifica HMAC, resuelve vínculo por metadata + nombre de sesión, calcula el HMAC del chat con `k_tenant`, consulta `suppressions` y `wa_chats` en una transacción corta, escribe **una** fila en `webhook_inbox` y encola `ingesta_inbox` (sin llamar a WAHA). Todo lo demás es del worker: normalización (`ingesta_inbox`), resolución lid→teléfono vía `/lids`, conteo, backfill y reconciliación. El mantenimiento del almacén de fuente (particiones y purga de `webhook_inbox` a 7 días) no es un job (la tabla `jobs` exige `link_id`): lo corre el `bucle` del worker cada hora, igual que hoy corre `programar_salud`.
2. **Tipos de job nuevos y su CHECK.** `ingesta_inbox`, `conteo`, `backfill`, `reconciliacion`. La lista vive en `app.radar.constantes.TIPOS_JOB` y r0004 reemplaza `jobs_tipo_check` con ella (mismo patrón que `ESTADOS_LINK`/`CAUSAS_FIN` en r0003). Los cuatro son por vínculo, así que el índice parcial `jobs_uno_vivo_por_link_y_tipo` ya garantiza uno vivo por tipo. Un handler puede devolver un número de segundos y el worker lo reprograma con ese plazo (hoy solo existía `"reprogramar"` a 5 min). Una función `SECURITY DEFINER` nueva, `radar_jobs_programar_ingesta()`, corre cada 30 s en el `bucle`: encola `conteo` para vínculos `vinculado` con `ingesta` NULL/`contando`, asegura una `reconciliacion` viva (con desfase fijo por línea) y adelanta la reconciliación de los vínculos que volvieron a `WORKING` después de una caída. No se toca `aplicar_status` del tramo 2.
3. **Cómo se comparte `k_tenant` con el worker.** Por el mismo `SecretStore` del contexto (`ctx.secretos` → `Seudonimizador`). El worker corre embebido en el servicio web por defecto (decisión del tramo 2: el `FileSecretStore` vive en un volumen de Railway montado en un solo servicio), así que receptor y worker leen el mismo archivo `k_tenant:<tenant>`. Pasar el worker a proceso aparte sigue requiriendo un gestor de secretos externo (ya documentado en `docs/radar-despliegue.md`). Si falta la `k_tenant` de un tenant, el receptor responde **503** sin persistir nada (WAHA reintenta) y loguea una alerta: no se puede filtrar sin HMAC, y filtrar mal es peor que perder un evento que la reconciliación recupera.
4. **Particionado mensual concreto.** `webhook_inbox` (por `recibido_at`) y `wa_message_bodies` (por `provider_ts`) son `PARTITION BY RANGE` con partición `DEFAULT` y particiones `<tabla>_YYYYMM`. La función `fuente_asegurar_particiones(p_meses)` (en f0002) crea los meses que faltan —para el cuerpo desde 2024-01, para el inbox desde el mes actual— hasta `now() + p_meses` meses, **sin** crear un mes si la `DEFAULT` ya tiene filas de ese rango (evita el error de Postgres), y descarta con `DROP TABLE` los meses de `webhook_inbox` enteramente más viejos que 7 días. La purga fina a 7 días es un `DELETE` permitido solo por una política RLS `FOR DELETE` que exige `app.purga = 'inbox'` y `recibido_at < now() - 7 días`. `wa_message_provider_ids` no se particiona: su `PRIMARY KEY (line_id, provider_msg_id)` es la `UNIQUE` de idempotencia y una clave única en tabla particionada tendría que incluir la columna de partición.
5. **Qué pasa si la pasada de conteo excede el tope de 10 minutos.** Se corta y la lista queda usable: `links.conteo_tope = true`, `ingesta = 'pendiente_de_seleccion'` y la pantalla dice "WhatsApp todavía nos está entregando chats; los que aparezcan después van a esperar tu decisión". Los chats que aparezcan después entran como "pendiente de decisión" por la reconciliación (regla de P4), así que cortar no pierde nada y no bloquea al dueño.
6. **`pendiente_de_selección` es de la línea, no de cada vínculo.** El estado del vínculo va en `links.ingesta` (`NULL` → `contando` → `pendiente_de_seleccion` → `importando` → `al_dia`); la selección confirmada va en `lines.seleccion_confirmada_at` y `lines.periodo_dias`. Una re-vinculación o reconexión de una línea que ya eligió no repite P4: el job `conteo` ve la selección y pasa directo a importar lo que falta.
7. **RLS en el almacén de fuente.** Las cuatro tablas nuevas llevan `tenant_id` con `ENABLE` + `FORCE ROW LEVEL SECURITY` y política sobre `app.tenant_id` (función `fuente_tenant_actual()`); `FuenteStore.tenant_tx()` la fija como `RadarDB.tenant_tx()`. Como el almacén usa una sola URL (dueño de las tablas) y `FORCE` alcanza al dueño, `FuenteStore.connect()` aborta si el rol es superusuario o `BYPASSRLS`: el despliegue necesita un rol propio (`radar_fuente`, paso nuevo en `docs/radar-despliegue.md`). Las particiones no tienen políticas propias: todo acceso va por la tabla padre.
8. **Identidad.** `wa_chats` guarda `contact_hmac` y/o `lid_hmac` (índices únicos parciales por línea). Un `@lid` se resuelve a teléfono con `GET /lids` (una carga por corrida, solo en memoria, como en el spike); si no se resuelve queda `pn_resuelto = false`. Números que `normalizar_e164` no soporta (otro país) se firman con `"+" + dígitos`. La identidad en claro (JID, lid, teléfono, nombre) va a `wa_contact_identities`; la de un chat excluido se borra (P4: el excluido conserva solo HMAC y motivo). Unificar dos filas del mismo contacto que llegaron por lid y por teléfono antes de poder mapearlas queda para el tramo 4 (con el `merge` de §4.2).
9. **Nombres de columnas** que no chocan con la guarda de §9: la FK al chat se llama `chat_uuid` (la guarda prohíbe `chat_id`), el último mensaje `ultimo_msg_at` (prohíbe `mensaje`).
10. **Pendientes de decisión.** Guardan solo metadatos (`solo_metadatos = true`, `tipo = 'otro'`, sin cuerpo ni `reply_to`). Si después se incluyen, el chat entra en `links.reimportar_chats` y el backfill lo relee completo; si se excluyen, se borran sus filas de mensajes, ids de proveedor, cuerpos e identidad.
11. **`message.revoked`** borra el cuerpo del almacén de fuente y marca `revocado` en `wa_message_provider_ids` (así ninguna pasada posterior lo vuelve a escribir). `message.edited` reemplaza el cuerpo. `message.ack` solo sube `wa_messages.ack` (en historial viene `-1`, §0.4: no se usa para "visto").
12. **Horario de P1.** P1 todavía no guarda horario de atención. La cadencia 15/60 y la regla de silencio usan `reglas.HORARIO_DEFECTO` (lunes a sábado, 8 a 20, hora argentina); cuando exista el horario por línea se reemplaza esa constante por un parámetro. "Franja" de la regla de silencio = "dentro del horario": el p95 se calcula sobre intervalos entre entrantes medidos en tiempo de horario de los últimos 28 días.
13. **Sugerencia local de exclusión** (§3 P4): funciones públicas de `app.services.checkout_helper` (`match_retiro`, `match_envio`, `pregunta_obra_social`, `pregunta_bono`, `pregunta_descuento`, `pide_foto`, `pide_receta_nube`, `pide_pago_manual`, `pide_cuenta_corriente`, `pide_efectivo`, `precios_mencionados`, `contiene_link`) más dos regex propias cortas (consulta comercial genérica y notificación). Se evalúan en memoria durante la pasada de conteo; el texto se descarta en el mismo paso.
14. **Profundidad.** NOWEB sin `fullSync` = 90 días; NOWEB con `fullSync` o GOWS = 365. El selector de P4 ofrece 30/90/180/365 hasta esa profundidad.
15. **Aviso de silencio.** Tras el reinicio automático, si a la hora sigue sin tráfico, un email único al dueño con "No recibimos mensajes desde el DD/MM HH:MM. Revisá que el teléfono tenga conexión." (reutiliza `fin_vinculo._emails_duenos` y `fin_vinculo._avisar`). El banner de P6 es del tramo 4.

## File Structure

| Archivo | Qué hace | Task |
|---|---|---|
| `app/radar/constantes.py` (mod) | `TIPOS_JOB`, `ESTADOS_INGESTA`, `TIPOS_MENSAJE`, `SUGERENCIAS_EXCLUSION` | 1 |
| `app/radar/ingesta/__init__.py` | paquete de la ingesta | 1 |
| `app/radar/ingesta/reduccion.py` | forma cruda de WAHA → `Jid`, `MensajeWaha`, `EventoMensaje`, `ChatWaha`; señales y sugerencia de exclusión | 1 |
| `app/radar/ingesta/reglas.py` | reglas puras: estabilización, ventanas, pasadas, cadencia, silencio | 2 |
| `app/radar/waha/cliente.py` (mod) | rutas de lectura, `downloadMedia=false` forzado, `listar_chats/mensajes/lids` | 3 |
| `tests/radar_tests/waha_falso.py` (mod) | chats, mensajes y lids en memoria; params y claves usadas | 3 |
| `migrations_radar/versions/r0004_ingesta.py` | `wa_chats`, `wa_messages`, `suppressions`, `conteo_chats`, `ingesta_conteos`, columnas de `links`/`lines`, CHECK de jobs, `radar_jobs_programar_ingesta()`, C1 nueva | 4 |
| `migrations_fuente/versions/f0002_conversaciones.py` | `wa_message_provider_ids`, `wa_message_bodies` (mensual), `wa_contact_identities`, `webhook_inbox` (mensual), RLS, particiones | 5 |
| `app/radar/fuente.py` (mod) | `tenant_tx`, rechazo de superusuario, `mantenimiento()` | 5 |
| `app/radar/ingesta/lectura.py` | cliente con clave de lectura, paginación hasta página vacía, presupuesto, `MapaLids` | 6 |
| `app/radar/ingesta/identidad.py` | HMAC del chat, `resolver_chat`, `leer_chat`, identidades en fuente | 6 |
| `app/radar/ingesta/almacen.py` | `guardar_lote` idempotente en las dos bases, ack/edición/revocado, borrado de un chat excluido | 7 |
| `app/radar/ingesta/receptor.py` + `app/radar/routers/webhook_waha.py` (mod) | filtro de exclusión antes de persistir y `webhook_inbox` | 8 |
| `app/radar/ingesta/inbox.py`, `app/radar/worker.py` (mod), `app/radar/jobs.py` (mod) | normalización, handlers con segundos, programación y mantenimiento | 9 |
| `app/radar/ingesta/seleccion.py`, `app/radar/auditoria.py` (mod), `app/radar/eventos_producto.py` (mod) | servicio de P4 y preparación de la importación | 10 |
| `app/radar/ingesta/conteo.py` | job de la pasada de conteo | 11 |
| `app/radar/routers/seleccion.py`, `app/radar/routers/paginas.py` (mod), `app/radar/app.py` (mod), `app/radar/static/seleccion.html`, `app/radar/static/seleccion.js`, `radar.js`/`consola.html`/`conectar.html` (mod) | API y pantalla P4 (dueño y Consola) | 12 |
| `app/radar/ingesta/backfill.py` | job de backfill en pasadas y reimportación de chats | 13 |
| `app/radar/ingesta/reconciliacion.py` | reconciliación en dos pasos, `observado_hasta`, silencio y reinicio | 14 |
| `app/radar/ingesta/progreso.py`, `app/radar/consola.py` (mod), `app/radar/vinculo_estados.py` (mod) | P5 carriles de importación y cobertura, C1 sin "—", semáforo | 15 |
| `docs/radar-despliegue.md` (mod), `docs/radar-waha-runbook-tramo3.md` | rol del almacén de fuente, jobs nuevos, verificación manual en staging | 16 |
| `tests/radar_tests/test_ingesta_*.py`, `test_waha_lecturas.py`, `test_fuente_conversaciones.py`, `test_webhook_ingesta.py`, `test_seleccion_api.py` | tests nuevos | 1–15 |
| `tests/radar_tests/conftest.py`, `helpers.py`, `test_esquema.py`, `test_fuente_y_health.py`, `test_waha_cliente.py`, `test_consola_api.py`, `test_worker.py` (mod) | fixtures y guardas actualizadas | 3–15 |

---

### Task 1: Reducción de WAHA a hechos y sugerencia local de exclusión

**Files:**
- Modify: `app/radar/constantes.py`
- Create: `app/radar/ingesta/__init__.py`, `app/radar/ingesta/reduccion.py`
- Test: `tests/radar_tests/test_ingesta_reduccion.py`

**Interfaces:**
- Consumes: `app.services.checkout_helper` (`match_retiro(t) -> bool`, `match_envio(t) -> bool`, `pregunta_descuento(t) -> bool`, `pide_foto(t) -> bool`, `pide_receta_nube(t) -> bool`, `pide_pago_manual(t) -> bool`, `pide_cuenta_corriente(t) -> bool`, `pide_efectivo(t) -> bool`, `pregunta_obra_social(t, lista_cfg=None) -> Optional[str]`, `pregunta_bono(t) -> Optional[str]`, `precios_mencionados(texto) -> set[float]`, `contiene_link(t) -> bool`).
- Produces: `TIPOS_JOB`, `ESTADOS_INGESTA`, `TIPOS_MENSAJE`, `SUGERENCIAS_EXCLUSION` en constantes; `Jid(usuario: str, es_lid: bool)` con `.crudo()` y `.telefono()`; `parsear_jid(valor) -> Optional[Jid]`; `jid_de_provider_id(pid) -> Optional[Jid]`; `ts_segundos(valor) -> Optional[int]`; `tipo_de(msg: dict) -> str`; `MensajeWaha(provider_id, chat, ts, from_me, tipo, has_media, texto=None, reply_to=None, ack=None)` con `.tiene_texto`; `reducir_mensaje(msg) -> Optional[MensajeWaha]`; `EventoMensaje(tipo, provider_id, chat, ts, from_me, mensaje=None, texto=None, ack=None)` con `.metadatos() -> dict`; `extraer_evento(evento: str, payload) -> Optional[EventoMensaje]`; `ChatWaha(jid, nombre, ultimo_ts)`; `reducir_chat(chat) -> Optional[ChatWaha]`; `Senales(comercial, precio, notificacion)`; `senales(texto) -> Senales`; `Acumulado` con `.sumar(m)`; `sugerir_exclusion(a: Acumulado) -> Optional[str]`; `EVENTOS_MENSAJE`.

- [ ] **Step 1: Constantes compartidas con las migraciones**

Agregar al final de `app/radar/constantes.py`:

```python
# Tipos de job (§6.2). r0004 reemplaza el CHECK de jobs.tipo con esta lista.
TIPOS_JOB = ("fin_vinculo", "chequeo_salud", "aviso_caida",
             "ingesta_inbox", "conteo", "backfill", "reconciliacion")
# Ingesta de un vínculo (links.ingesta, tramo 3). NULL = todavía no empezó (se trata
# como pendiente de selección: el receptor descarta sin payload).
ESTADOS_INGESTA = ("contando", "pendiente_de_seleccion", "importando", "al_dia")
# Tipo de mensaje en wa_messages (§6.3 punto 5: sin medios, solo el tipo).
TIPOS_MENSAJE = ("texto", "audio", "imagen", "video", "documento", "sticker", "ubicacion", "contacto", "otro")
# Sugerencias locales de exclusión de P4 (§3 P4), sin IA.
SUGERENCIAS_EXCLUSION = ("sin_entrantes", "notificacion", "sin_consulta_comercial")
```

Crear `app/radar/ingesta/__init__.py`:

```python
"""Ingesta de Radar (tramo 3, §6.3): receptor, conteo, backfill, reconciliación y P4."""
```

- [ ] **Step 2: Test que falla**

Crear `tests/radar_tests/test_ingesta_reduccion.py`:

```python
"""Reducción de mensajes y eventos de WAHA a hechos (§6.3) y sugerencia local de exclusión (§3 P4)."""
import pytest

from app.radar.ingesta.reduccion import (Acumulado, extraer_evento, jid_de_provider_id, parsear_jid, reducir_chat,
                                         reducir_mensaje, senales, sugerir_exclusion, tipo_de, ts_segundos)

PN = "5493411111111@c.us"
MSG = {"id": "false_5493411111111@c.us_AAA", "from": PN, "to": "5493419999999@c.us", "fromMe": False,
       "timestamp": 1758000000, "body": "hola, tienen ibuprofeno? cuánto sale", "hasMedia": False, "ack": 1,
       "replyTo": {"id": "true_5493411111111@c.us_BBB"}}


def test_jid_individual_y_no_individual():
    assert parsear_jid(PN).usuario == "5493411111111" and not parsear_jid(PN).es_lid
    assert parsear_jid({"_serialized": "123456789@lid"}).es_lid
    assert parsear_jid("5493411111111:12@s.whatsapp.net").crudo() == PN
    for malo in ("120363@g.us", "status@broadcast", "123@newsletter", "5493411111111@c.us\n", "", None, 5):
        assert parsear_jid(malo) is None


def test_jid_dentro_del_id_de_mensaje():
    assert jid_de_provider_id("false_5493411111111@c.us_3EB0AA").usuario == "5493411111111"
    assert jid_de_provider_id("true_123456789@lid_XYZ").es_lid
    assert jid_de_provider_id("false_1203@g.us_X") is None and jid_de_provider_id("raro") is None


def test_timestamps_en_segundos_y_en_ms():
    assert ts_segundos(1758000000) == 1758000000
    assert ts_segundos(1758000000123) == 1758000000
    assert ts_segundos(True) is None and ts_segundos(-1) is None and ts_segundos("1") is None


def test_reducir_mensaje_conserva_solo_hechos_y_repr_sin_contenido():
    m = reducir_mensaje(MSG)
    assert (m.chat.usuario, m.ts, m.from_me, m.tipo, m.has_media, m.ack) == ("5493411111111", 1758000000, False,
                                                                            "texto", False, 1)
    assert m.reply_to == "true_5493411111111@c.us_BBB" and m.tiene_texto
    assert "ibuprofeno" not in repr(m) and "549341" not in repr(m)
    propio = reducir_mensaje({**MSG, "fromMe": True, "chatId": PN, "from": "5493419999999@c.us"})
    assert propio.chat.usuario == "5493411111111" and propio.from_me


def test_reducir_mensaje_descarta_grupos_y_ids_invalidos():
    assert reducir_mensaje({**MSG, "from": "1203630@g.us"}) is None
    assert reducir_mensaje({**MSG, "id": "con espacio"}) is None
    assert reducir_mensaje({**MSG, "timestamp": None}) is None
    assert reducir_mensaje("no-es-dict") is None


@pytest.mark.parametrize("msg,tipo", [
    ({"_data": {"message": {"audioMessage": {}}}, "hasMedia": True}, "audio"),
    ({"_data": {"Message": {"imageMessage": {}}}, "hasMedia": True}, "imagen"),
    ({"hasMedia": True, "media": {"mimetype": "video/mp4"}}, "video"),
    ({"hasMedia": True, "media": {"mimetype": "application/pdf"}}, "documento"),
    ({"hasMedia": True}, "otro"),
    ({"body": "hola"}, "texto"),
    ({"body": "  "}, "otro"),
])
def test_tipo_de(msg, tipo):
    assert tipo_de(msg) == tipo


def test_eventos_ack_edicion_y_revocado():
    ack = extraer_evento("message.ack", {"id": "true_5493411111111@c.us_X", "from": "5493419999999@c.us",
                                         "to": PN, "fromMe": True, "ack": 3, "timestamp": 1758000001})
    assert (ack.tipo, ack.provider_id, ack.chat.usuario, ack.ack) == ("ack", "true_5493411111111@c.us_X",
                                                                      "5493411111111", 3)
    ed = extraer_evento("message.edited", {"id": "nuevo", "editedMessageId": "false_x_AAA", "from": PN,
                                           "body": "corregido", "timestamp": 1758000002})
    assert (ed.tipo, ed.provider_id, ed.texto) == ("edicion", "false_x_AAA", "corregido")
    rv = extraer_evento("message.revoked", {"revokedMessageId": "false_x_AAA",
                                            "after": {"from": PN, "timestamp": 1758000003}})
    assert (rv.tipo, rv.provider_id, rv.texto) == ("revocado", "false_x_AAA", None)
    assert extraer_evento("message.reaction", {}) is None
    assert extraer_evento("message.any", {**MSG, "from": "1@g.us"}) is None


def test_metadatos_de_un_pendiente_son_solo_id_timestamp_fromme():
    ev = extraer_evento("message.any", MSG)
    assert ev.metadatos() == {"id": MSG["id"], "timestamp": 1758000000, "fromMe": False}
    assert "ibuprofeno" not in repr(ev)


def test_reducir_chat():
    c = reducir_chat({"id": {"_serialized": PN}, "name": "  Marta  ", "conversationTimestamp": 1758000000})
    assert (c.jid.usuario, c.nombre, c.ultimo_ts) == ("5493411111111", "Marta", 1758000000)
    assert "Marta" not in repr(c)
    assert reducir_chat({"id": "1203@g.us"}) is None


def test_senales_locales():
    assert senales("¿cuánto sale el ibuprofeno?").comercial
    assert senales("lo paso a retirar por la sucursal").comercial
    assert senales("sale $18.500").precio
    assert senales("Tu código de verificación es 123456").notificacion
    assert senales("seguí tu envío en https://track.example/abc").notificacion
    assert senales(None) == senales("")


def test_sugerencia_de_exclusion():
    def acum(*mensajes):
        a = Acumulado()
        for from_me, texto in mensajes:
            a.sumar(reducir_mensaje({**MSG, "fromMe": from_me, "body": texto}))
        return a
    assert sugerir_exclusion(acum((True, "hola"))) == "sin_entrantes"
    assert sugerir_exclusion(acum((False, "Tu código de verificación es 1234"))) == "notificacion"
    assert sugerir_exclusion(acum((False, "feliz cumple!"), (True, "gracias!"))) == "sin_consulta_comercial"
    assert sugerir_exclusion(acum((False, "tienen protector solar?"), (True, "sí"))) is None
    a = acum((False, "hola"), (True, "chau"))
    assert (a.entrantes, a.propios, a.con_texto, a.ultimo_ts) == (1, 1, 2, 1758000000)
```

- [ ] **Step 3: Correrlo y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_reduccion.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.radar.ingesta.reduccion'`.

- [ ] **Step 4: Implementación**

Crear `app/radar/ingesta/reduccion.py`:

```python
"""
Reducción de lo que devuelve WAHA a hechos de mensaje (§6.3, §6.3 punto 8).

- Único lugar que entiende la forma cruda de un mensaje o evento de WAHA. Todo
  lo demás trabaja con MensajeWaha / EventoMensaje / ChatWaha.
- Solo chats individuales (`@c.us`, `@s.whatsapp.net`, `@lid`). Grupos,
  estados, canales y difusión devuelven None: no se persisten.
- El texto viaja en MensajeWaha.texto solo hasta el almacén de fuente o hasta
  `senales()`; __repr__ nunca lo muestra (ni ids, ni teléfonos).
- `senales()` es la sugerencia local de exclusión (§3 P4): regex de
  app.services.checkout_helper y un puñado de palabras propias, sin IA externa.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.radar.constantes import SUGERENCIAS_EXCLUSION, TIPOS_MENSAJE
from app.services import checkout_helper as ch

PATRON_JID = re.compile(r"^(?P<usuario>[0-9]{5,20})(?::[0-9]{1,3})?@(?P<dominio>c\.us|s\.whatsapp\.net|lid)$")
_ID_PROVEEDOR = re.compile(r"^[^\s]{1,200}$")
TIPOS = TIPOS_MENSAJE
_CLAVES_TIPO = {
    "conversation": "texto", "extendedTextMessage": "texto",
    "audioMessage": "audio", "imageMessage": "imagen", "videoMessage": "video",
    "documentMessage": "documento", "documentWithCaptionMessage": "documento",
    "stickerMessage": "sticker", "locationMessage": "ubicacion", "liveLocationMessage": "ubicacion",
    "contactMessage": "contacto", "contactsArrayMessage": "contacto",
}
EVENTOS_MENSAJE = ("message.any", "message.ack", "message.edited", "message.revoked")
SUGERENCIAS = SUGERENCIAS_EXCLUSION
_CONSULTA = re.compile(r"\b(precio|precios|cu[aá]nto (sale|cuesta|est[aá]|vale)|ten[eé]s|tienen|hay stock|stock|"
                       r"venden|vend[eé]s|pedido|comprar|encargar|reservar|turno|presupuesto)\b", re.IGNORECASE)
_NOTIFICACION = re.compile(r"\b(c[oó]digo de (verificaci[oó]n|seguridad|acceso)|tu c[oó]digo|seguimiento|tracking|"
                           r"no respondas|mensaje autom[aá]tico|no-reply|noreply)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Jid:
    usuario: str
    es_lid: bool

    def crudo(self) -> str:
        return f"{self.usuario}@lid" if self.es_lid else f"{self.usuario}@c.us"

    def telefono(self) -> str:
        if self.es_lid:
            raise ValueError("un lid no es un teléfono")
        return "+" + self.usuario

    def __repr__(self) -> str:
        return "Jid(lid)" if self.es_lid else "Jid(pn)"


def serializado(valor: Any) -> str:
    if isinstance(valor, dict):
        valor = valor.get("_serialized") or ""
    return valor if isinstance(valor, str) else ""


def parsear_jid(valor: Any) -> Optional[Jid]:
    m = PATRON_JID.fullmatch(serializado(valor))
    if not m:
        return None
    return Jid(usuario=m.group("usuario"), es_lid=m.group("dominio") == "lid")


_JID_EN_ID = re.compile(r"^(?:true|false)_(?P<jid>[^_]+)_")


def jid_de_provider_id(provider_id: str) -> Optional[Jid]:
    """El id de mensaje lleva el chat adentro (§0.4): 'false_549...@c.us_XXXX'."""
    m = _JID_EN_ID.match(provider_id or "")
    return parsear_jid(m.group("jid")) if m else None


def ts_segundos(valor: Any) -> Optional[int]:
    """payload.timestamp viene en segundos; el del sobre, en ms (§6.3 punto 1)."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor <= 0:
        return None
    return int(valor // 1000) if valor > 10**11 else int(valor)


def _chat_de(msg: dict) -> Optional[Jid]:
    cid = msg.get("chatId")
    if not cid:
        cid = msg.get("to") if msg.get("fromMe") else msg.get("from")
    return parsear_jid(cid)


def tipo_de(msg: dict) -> str:
    datos = msg.get("_data") if isinstance(msg.get("_data"), dict) else {}
    interno = datos.get("message") or datos.get("Message")
    if isinstance(interno, dict):
        for clave in interno:
            if clave in _CLAVES_TIPO:
                return _CLAVES_TIPO[clave]
    if msg.get("hasMedia"):
        media = msg.get("media") if isinstance(msg.get("media"), dict) else {}
        mime = str(media.get("mimetype") or "")
        for prefijo, tipo in (("audio/", "audio"), ("image/", "imagen"), ("video/", "video")):
            if mime.startswith(prefijo):
                return tipo
        return "documento" if mime else "otro"
    body = msg.get("body")
    return "texto" if isinstance(body, str) and body.strip() else "otro"


@dataclass(frozen=True, repr=False)
class MensajeWaha:
    provider_id: str
    chat: Jid
    ts: int
    from_me: bool
    tipo: str
    has_media: bool
    texto: Optional[str] = field(default=None)
    reply_to: Optional[str] = field(default=None)
    ack: Optional[int] = field(default=None)

    @property
    def tiene_texto(self) -> bool:
        return bool(self.texto)

    def __repr__(self) -> str:   # §6.3 punto 8: nunca texto, ids ni teléfonos
        return f"MensajeWaha(ts={self.ts}, from_me={self.from_me}, tipo={self.tipo})"


def _ack(valor: Any) -> Optional[int]:
    return int(valor) if isinstance(valor, int) and not isinstance(valor, bool) and -1 <= valor <= 4 else None


def reducir_mensaje(msg: Any) -> Optional[MensajeWaha]:
    if not isinstance(msg, dict):
        return None
    pid = serializado(msg.get("id"))
    chat = _chat_de(msg)
    ts = ts_segundos(msg.get("timestamp"))
    if not _ID_PROVEEDOR.fullmatch(pid) or chat is None or ts is None:
        return None
    body = msg.get("body")
    texto = body if isinstance(body, str) and body.strip() else None
    reply = msg.get("replyTo")
    reply_id = serializado(reply.get("id")) if isinstance(reply, dict) else serializado(reply)
    return MensajeWaha(provider_id=pid, chat=chat, ts=ts, from_me=bool(msg.get("fromMe")), tipo=tipo_de(msg),
                       has_media=bool(msg.get("hasMedia")), texto=texto,
                       reply_to=reply_id if _ID_PROVEEDOR.fullmatch(reply_id or "") else None,
                       ack=_ack(msg.get("ack")))


@dataclass(frozen=True, repr=False)
class EventoMensaje:
    tipo: str                     # 'mensaje' | 'ack' | 'edicion' | 'revocado'
    provider_id: str              # el mensaje afectado
    chat: Jid
    ts: int
    from_me: bool
    mensaje: Optional[MensajeWaha] = None
    texto: Optional[str] = None
    ack: Optional[int] = None

    def metadatos(self) -> dict:
        """Lo único que se guarda de un chat pendiente de decisión (§6.3 punto 1)."""
        return {"id": self.provider_id, "timestamp": self.ts, "fromMe": self.from_me}

    def __repr__(self) -> str:
        return f"EventoMensaje({self.tipo}, ts={self.ts})"


def extraer_evento(evento: str, payload: Any) -> Optional[EventoMensaje]:
    if evento not in EVENTOS_MENSAJE or not isinstance(payload, dict):
        return None
    if evento == "message.any":
        m = reducir_mensaje(payload)
        if m is None:
            return None
        return EventoMensaje("mensaje", m.provider_id, m.chat, m.ts, m.from_me, mensaje=m)
    if evento == "message.revoked":
        antes = payload.get("before") if isinstance(payload.get("before"), dict) else {}
        despues = payload.get("after") if isinstance(payload.get("after"), dict) else {}
        base = despues or antes
        pid = serializado(payload.get("revokedMessageId")) or serializado(antes.get("id"))
        tipo = "revocado"
    elif evento == "message.edited":
        base = payload
        pid = serializado(payload.get("editedMessageId"))
        tipo = "edicion"
    else:
        base = payload
        pid = serializado(payload.get("id"))
        tipo = "ack"
    chat = _chat_de(base)
    ts = ts_segundos(base.get("timestamp")) or 0
    if chat is None or not _ID_PROVEEDOR.fullmatch(pid):
        return None
    body = base.get("body")
    texto = body if tipo == "edicion" and isinstance(body, str) and body.strip() else None
    return EventoMensaje(tipo, pid, chat, ts, bool(base.get("fromMe")), texto=texto,
                         ack=_ack(base.get("ack")) if tipo == "ack" else None)


@dataclass(frozen=True, repr=False)
class ChatWaha:
    jid: Jid
    nombre: Optional[str]
    ultimo_ts: Optional[int]

    def __repr__(self) -> str:
        return f"ChatWaha(ultimo_ts={self.ultimo_ts})"


def reducir_chat(chat: Any) -> Optional[ChatWaha]:
    if not isinstance(chat, dict):
        return None
    jid = parsear_jid(chat.get("id"))
    if jid is None:
        return None
    nombre = chat.get("name")
    nombre = nombre.strip()[:200] if isinstance(nombre, str) and nombre.strip() else None
    return ChatWaha(jid=jid, nombre=nombre,
                    ultimo_ts=ts_segundos(chat.get("conversationTimestamp") or chat.get("timestamp")))


@dataclass(frozen=True)
class Senales:
    comercial: bool
    precio: bool
    notificacion: bool


def senales(texto: Optional[str]) -> Senales:
    t = texto or ""
    if not t:
        return Senales(False, False, False)
    comercial = (bool(_CONSULTA.search(t))
                 or any(f(t) for f in (ch.match_retiro, ch.match_envio, ch.pregunta_descuento, ch.pide_foto,
                                       ch.pide_receta_nube, ch.pide_pago_manual, ch.pide_cuenta_corriente,
                                       ch.pide_efectivo))
                 or ch.pregunta_obra_social(t) is not None or ch.pregunta_bono(t) is not None)
    return Senales(comercial=comercial, precio=bool(ch.precios_mencionados(t)),
                   notificacion=ch.contiene_link(t) or bool(_NOTIFICACION.search(t)))


@dataclass
class Acumulado:
    """Conteo de un chat en la pasada de conteo (§3 P4): solo números."""
    entrantes: int = 0
    propios: int = 0
    con_texto: int = 0
    comerciales: int = 0
    con_precio: int = 0
    notificaciones: int = 0
    ultimo_ts: Optional[int] = None

    def sumar(self, m: MensajeWaha) -> None:
        s = senales(m.texto)
        if m.from_me:
            self.propios += 1
        else:
            self.entrantes += 1
            self.notificaciones += int(s.notificacion)
        self.con_texto += int(m.tiene_texto)
        self.comerciales += int(s.comercial)
        self.con_precio += int(s.precio)
        self.ultimo_ts = m.ts if self.ultimo_ts is None else max(self.ultimo_ts, m.ts)


def sugerir_exclusion(a: Acumulado) -> Optional[str]:
    if a.entrantes == 0:
        return "sin_entrantes"
    if a.propios == 0 and a.notificaciones * 2 >= a.entrantes:
        return "notificacion"
    if a.comerciales == 0 and a.con_precio == 0:
        return "sin_consulta_comercial"
    return None
```

- [ ] **Step 5: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_reduccion.py -q`
Expected: PASS (12 tests, 18 casos con la parametrización).

- [ ] **Step 6: Commit**

```bash
git add app/radar/constantes.py app/radar/ingesta/__init__.py app/radar/ingesta/reduccion.py tests/radar_tests/test_ingesta_reduccion.py
git commit -m "Radar tramo 3: reducción de mensajes de WAHA a hechos y sugerencia local de exclusión

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Reglas puras de la ingesta

**Files:**
- Create: `app/radar/ingesta/reglas.py`
- Test: `tests/radar_tests/test_ingesta_reglas.py`

**Interfaces:**
- Consumes: nada del repo.
- Produces: constantes `ESTABLE`, `TOPE_CONTEO`, `REPASO_CONTEO_S`, `DESFASES_BACKFILL`, `MARGEN`, `CADENCIA_EN_HORARIO_S`, `CADENCIA_FUERA_S`, `DESFASE_MAX_S`, `SILENCIO_MINIMO_S`, `REINICIO_MINIMO`, `HORA_ARGENTINA`, `HORARIO_DEFECTO`, `PERIODOS_DIAS`; `estado_conteo(*, inicio, ahora, previo, actual, estable_desde) -> tuple[Literal["seguir","listo","tope"], Optional[datetime]]`; `profundidad_dias(engine, full_sync) -> int`; `periodos_permitidos(profundidad) -> list[int]`; `ventanas_a_importar(*, inicio, conectado_at, cubierto, purgada_hasta) -> list[tuple[datetime, datetime]]`; `proxima_pasada(conectado_at, hechas) -> Optional[datetime]`; `conteos_coinciden(anterior: dict, actual: dict) -> bool`; `en_horario(momento) -> bool`; `cadencia_s(ahora) -> int`; `desfase_s(line_id) -> int`; `segundos_en_horario(desde, hasta) -> float`; `p95(valores) -> Optional[float]`; `intervalos_en_horario(entrantes) -> list[float]`; `umbral_silencio_s(intervalos) -> float`; `hay_silencio(*, ultimo_msg, ahora, intervalos) -> bool`; `puede_reiniciar(ultimo_reinicio, ahora, restringida) -> bool`.

- [ ] **Step 1: Test que falla**

Crear `tests/radar_tests/test_ingesta_reglas.py`:

```python
"""Reglas puras de la ingesta: estabilización del conteo, ventanas, pasadas, cadencia y silencio."""
import uuid
from datetime import datetime, timedelta, timezone

from app.radar.ingesta import reglas as r

AR = timezone(timedelta(hours=-3))
LUNES_10 = datetime(2026, 9, 21, 10, 0, tzinfo=AR)       # lunes, en horario
DOMINGO_10 = datetime(2026, 9, 20, 10, 0, tzinfo=AR)


def test_conteo_estable_tope_y_cambio():
    t0 = LUNES_10
    d, est = r.estado_conteo(inicio=t0, ahora=t0 + timedelta(seconds=20), previo=None, actual=(10, 100),
                             estable_desde=None)
    assert (d, est) == ("seguir", None)
    d, est = r.estado_conteo(inicio=t0, ahora=t0 + timedelta(seconds=40), previo=(10, 100), actual=(10, 100),
                             estable_desde=None)
    assert d == "seguir" and est == t0 + timedelta(seconds=40)
    d, _ = r.estado_conteo(inicio=t0, ahora=est + timedelta(seconds=75), previo=(10, 100), actual=(10, 100),
                           estable_desde=est)
    assert d == "listo"
    d, est2 = r.estado_conteo(inicio=t0, ahora=t0 + timedelta(minutes=5), previo=(10, 100), actual=(11, 120),
                              estable_desde=est)
    assert (d, est2) == ("seguir", None)
    d, _ = r.estado_conteo(inicio=t0, ahora=t0 + timedelta(minutes=10), previo=(10, 100), actual=(12, 130),
                           estable_desde=None)
    assert d == "tope"


def test_profundidad_y_periodos():
    assert r.profundidad_dias("NOWEB", False) == 90 and r.profundidad_dias("NOWEB", True) == 365
    assert r.profundidad_dias("GOWS", False) == 365
    assert r.periodos_permitidos(90) == [30, 90] and r.periodos_permitidos(365) == [30, 90, 180, 365]


def test_ventanas_primer_vinculo_revinculacion_ampliacion_y_purga():
    con = LUNES_10
    ini = con - timedelta(days=90)
    assert r.ventanas_a_importar(inicio=ini, conectado_at=con, cubierto=None, purgada_hasta=None) == [(ini, con)]
    previo = (ini, con - timedelta(days=5))
    assert r.ventanas_a_importar(inicio=ini, conectado_at=con, cubierto=previo, purgada_hasta=None) == \
        [(con - timedelta(days=5) - r.MARGEN, con)]
    ini12 = con - timedelta(days=365)
    assert r.ventanas_a_importar(inicio=ini12, conectado_at=con, cubierto=previo, purgada_hasta=None) == \
        [(ini12, ini), (con - timedelta(days=5) - r.MARGEN, con)]
    purga = con - timedelta(days=30)
    assert r.ventanas_a_importar(inicio=ini12, conectado_at=con, cubierto=previo, purgada_hasta=purga) == \
        [(con - timedelta(days=5) - r.MARGEN, con)]
    assert r.ventanas_a_importar(inicio=con, conectado_at=con, cubierto=None, purgada_hasta=None) == []


def test_pasadas_de_backfill():
    con = LUNES_10
    assert [r.proxima_pasada(con, n) for n in range(5)] == [con, con + timedelta(minutes=30),
                                                            con + timedelta(hours=6), con + timedelta(hours=24), None]
    assert r.conteos_coinciden({"a": 3}, {"a": 3}) and not r.conteos_coinciden({"a": 3}, {"a": 4})
    assert r.conteos_coinciden({"a": 3}, {"a": 3, "b": 0}) and not r.conteos_coinciden({"a": 3}, {"a": 3, "b": 1})


def test_horario_cadencia_y_desfase():
    assert r.en_horario(LUNES_10) and not r.en_horario(DOMINGO_10)
    assert not r.en_horario(datetime(2026, 9, 21, 21, 0, tzinfo=AR))
    assert r.cadencia_s(LUNES_10) == 900 and r.cadencia_s(DOMINGO_10) == 3600
    lid = uuid.UUID("0000abc0-0000-0000-0000-000000000000")
    assert r.desfase_s(lid) == int("0000abc", 16) % 900
    assert all(0 <= r.desfase_s(uuid.uuid4()) < 900 for _ in range(50))


def test_segundos_en_horario_salta_noches_y_domingos():
    sab_19 = datetime(2026, 9, 19, 19, 0, tzinfo=AR)
    lun_9 = datetime(2026, 9, 21, 9, 0, tzinfo=AR)
    assert r.segundos_en_horario(sab_19, lun_9) == 2 * 3600
    assert r.segundos_en_horario(lun_9, sab_19) == 0


def test_silencio_y_reinicio():
    ultimo = LUNES_10
    assert not r.hay_silencio(ultimo_msg=ultimo, ahora=ultimo + timedelta(hours=3), intervalos=[])
    assert r.hay_silencio(ultimo_msg=ultimo, ahora=ultimo + timedelta(hours=5), intervalos=[])
    largos = [3 * 3600.0] * 30                     # p95 = 3 h -> umbral 9 h
    assert r.umbral_silencio_s(largos) == 9 * 3600
    assert not r.hay_silencio(ultimo_msg=ultimo, ahora=ultimo + timedelta(hours=5), intervalos=largos)
    assert r.p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]) == 19
    assert r.intervalos_en_horario([ultimo + timedelta(hours=1), ultimo]) == [3600.0]
    assert r.puede_reiniciar(None, ultimo, False) and not r.puede_reiniciar(None, ultimo, True)
    assert not r.puede_reiniciar(ultimo - timedelta(hours=23), ultimo, False)
    assert r.puede_reiniciar(ultimo - timedelta(hours=24), ultimo, False)
```

- [ ] **Step 2: Correrlo y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_reglas.py -q`
Expected: FAIL con `ImportError: cannot import name 'reglas' from 'app.radar.ingesta'`.

- [ ] **Step 3: Implementación**

Crear `app/radar/ingesta/reglas.py`:

```python
"""
Reglas puras de la ingesta (§3 P4, §6.3 puntos 2-4, Estados especiales).
Sin base, sin red y sin reloj propio: todo recibe `ahora`.
"""

import math
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Literal, Optional

# §3 P4: estable si no cambia por 60-90 s, con tope de 10 minutos.
ESTABLE = timedelta(seconds=75)
TOPE_CONTEO = timedelta(minutes=10)
REPASO_CONTEO_S = 20.0
# §6.3 punto 2: pasada inicial y repeticiones a +30 min, +6 h y +24 h del vínculo.
DESFASES_BACKFILL = (timedelta(0), timedelta(minutes=30), timedelta(hours=6), timedelta(hours=24))
# §6.3 puntos 3 y 4: margen de solapamiento.
MARGEN = timedelta(minutes=10)
# §6.3 punto 3: cadencia 15 / 60 min y desfase fijo de 0 a 15 min por línea.
CADENCIA_EN_HORARIO_S = 900
CADENCIA_FUERA_S = 3600
DESFASE_MAX_S = 900
# Estados especiales, "Sesión conectada sin tráfico".
SILENCIO_MINIMO_S = 4 * 3600
MIN_INTERVALOS_P95 = 20
REINICIO_MINIMO = timedelta(hours=24)
# P1 todavía no guarda horario: se usa este (lunes a sábado, 8 a 20, hora argentina).
HORA_ARGENTINA = timezone(timedelta(hours=-3))
HORARIO_DEFECTO = (frozenset(range(0, 6)), time(8, 0), time(20, 0))
PERIODOS_DIAS = (30, 90, 180, 365)

Decision = Literal["seguir", "listo", "tope"]


def estado_conteo(*, inicio: datetime, ahora: datetime, previo: Optional[tuple[int, int]],
                  actual: tuple[int, int], estable_desde: Optional[datetime]) -> tuple[Decision, Optional[datetime]]:
    """Devuelve la decisión y el nuevo `estable_desde`. Estable = mismo (chats, mensajes)
    que la pasada anterior durante ESTABLE; al TOPE_CONTEO se corta igual."""
    if previo != actual:
        estable_desde = None
    elif estable_desde is None:
        estable_desde = ahora
    if estable_desde is not None and ahora - estable_desde >= ESTABLE:
        return "listo", estable_desde
    if ahora - inicio >= TOPE_CONTEO:
        return "tope", estable_desde
    return "seguir", estable_desde


def profundidad_dias(engine: str, full_sync: bool) -> int:
    return 365 if engine == "GOWS" or full_sync else 90


def periodos_permitidos(profundidad: int) -> list[int]:
    return [p for p in PERIODOS_DIAS if p <= profundidad]


def ventanas_a_importar(*, inicio: datetime, conectado_at: datetime,
                        cubierto: Optional[tuple[datetime, datetime]],
                        purgada_hasta: Optional[datetime]) -> list[tuple[datetime, datetime]]:
    """§6.3 punto 4: lo que la línea todavía no tiene. `cubierto` = (desde, observado_hasta)
    de lo ya importado por vínculos anteriores; lo purgado no vuelve a entrar."""
    piso = max(inicio, purgada_hasta) if purgada_hasta else inicio
    if cubierto is None:
        return [(piso, conectado_at)] if piso < conectado_at else []
    desde_c, hasta_c = cubierto
    ventanas: list[tuple[datetime, datetime]] = []
    if piso < desde_c:                               # ampliación de profundidad: el tramo más antiguo
        ventanas.append((piso, min(desde_c, conectado_at)))
    nuevo = max(piso, hasta_c - MARGEN)
    if nuevo < conectado_at:
        ventanas.append((nuevo, conectado_at))
    return ventanas


def proxima_pasada(conectado_at: datetime, hechas: int) -> Optional[datetime]:
    if hechas >= len(DESFASES_BACKFILL):
        return None
    return conectado_at + DESFASES_BACKFILL[hechas]


def conteos_coinciden(anterior: dict, actual: dict) -> bool:
    """Dos pasadas coinciden si dan el mismo conteo por chat; un chat con 0 en una
    pasada y ausente en la otra es lo mismo (la pasada con chats/all solo ve chats con mensajes)."""
    return {k: v for k, v in anterior.items() if v} == {k: v for k, v in actual.items() if v}


def en_horario(momento: datetime) -> bool:
    dias, abre, cierra = HORARIO_DEFECTO
    local = momento.astimezone(HORA_ARGENTINA)
    return local.weekday() in dias and abre <= local.time() < cierra


def cadencia_s(ahora: datetime) -> int:
    return CADENCIA_EN_HORARIO_S if en_horario(ahora) else CADENCIA_FUERA_S


def desfase_s(line_id: uuid.UUID) -> int:
    """Igual que radar_jobs_programar_ingesta() en SQL: 7 hex del line_id módulo 900."""
    return int(line_id.hex[:7], 16) % DESFASE_MAX_S


def segundos_en_horario(desde: datetime, hasta: datetime) -> float:
    if hasta <= desde:
        return 0.0
    dias, abre, cierra = HORARIO_DEFECTO
    total = 0.0
    d = desde.astimezone(HORA_ARGENTINA).date()
    fin = hasta.astimezone(HORA_ARGENTINA).date()
    while d <= fin:
        if d.weekday() in dias:
            a = datetime.combine(d, abre, HORA_ARGENTINA)
            c = datetime.combine(d, cierra, HORA_ARGENTINA)
            ini, ter = max(a, desde), min(c, hasta)
            if ter > ini:
                total += (ter - ini).total_seconds()
        d += timedelta(days=1)
    return total


def p95(valores: list[float]) -> Optional[float]:
    if not valores:
        return None
    orden = sorted(valores)
    return orden[min(len(orden) - 1, math.ceil(0.95 * len(orden)) - 1)]


def intervalos_en_horario(entrantes: list[datetime]) -> list[float]:
    orden = sorted(entrantes)
    return [segundos_en_horario(a, b) for a, b in zip(orden, orden[1:])]


def umbral_silencio_s(intervalos: list[float]) -> float:
    """max(4 h dentro del horario, 3 × p95 del intervalo entre entrantes); con menos de
    MIN_INTERVALOS_P95 intervalos el p95 no es confiable y vale el mínimo."""
    if len(intervalos) < MIN_INTERVALOS_P95:
        return float(SILENCIO_MINIMO_S)
    return max(float(SILENCIO_MINIMO_S), 3 * (p95(intervalos) or 0.0))


def hay_silencio(*, ultimo_msg: datetime, ahora: datetime, intervalos: list[float]) -> bool:
    return segundos_en_horario(ultimo_msg, ahora) > umbral_silencio_s(intervalos)


def puede_reiniciar(ultimo_reinicio: Optional[datetime], ahora: datetime, restringida: bool) -> bool:
    return not restringida and (ultimo_reinicio is None or ahora - ultimo_reinicio >= REINICIO_MINIMO)
```

- [ ] **Step 4: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_reglas.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/radar/ingesta/reglas.py tests/radar_tests/test_ingesta_reglas.py
git commit -m "Radar tramo 3: reglas puras de estabilización, ventanas, pasadas, cadencia y silencio

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Cliente WAHA con rutas de lectura y WAHA falso con chats, mensajes y lids

**Files:**
- Modify: `app/radar/waha/cliente.py`, `tests/radar_tests/waha_falso.py`, `tests/radar_tests/test_waha_cliente.py`
- Test: `tests/radar_tests/test_waha_lecturas.py`

**Interfaces:**
- Consumes: `reducir_chat`, `reducir_mensaje`, `parsear_jid`, `ChatWaha`, `MensajeWaha` (Task 1); `verificar_ruta(metodo, ruta) -> Optional[str]`, `WahaCliente._request(...)` (tramo 2).
- Produces: `PaginaChats(chats: list[ChatWaha], crudos: int)`, `PaginaMensajes(mensajes: list[MensajeWaha], crudos: int)`, `PaginaLids(pares: list[tuple[str, str]], crudos: int)`; `WahaCliente.listar_chats(sesion, *, limit, offset) -> PaginaChats`; `WahaCliente.listar_mensajes(sesion, chat, *, limit, offset, gte=None, lte=None) -> PaginaMensajes`; `WahaCliente.listar_lids(sesion, *, limit, offset) -> PaginaLids`; `LIMITE_MAXIMO = 500`. En `WahaFalso`: `chats`, `mensajes`, `lids` (por sesión), `params`, `api_keys`, `falla_mensajes_de`; helpers `ME`, `msg_crudo(...)`, `chat_crudo(...)`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_waha_lecturas.py`:

```python
"""
Lecturas de WAHA del tramo 3 (§6.3): rutas nuevas en la lista blanca con el chat
validado por fullmatch, downloadMedia=false forzado y respuestas ya reducidas.
"""
import pytest

from app.radar.waha.cliente import RutaNoPermitida, WahaCliente, WahaError, verificar_ruta

from .waha_falso import WahaFalso, chat_crudo, msg_crudo

S = "v_0123456789ab"
PN = "5493411111111"


def _cli(waha):
    return WahaCliente("http://waha.interno", "clave-lectura", transport=waha.transporte())


def test_rutas_de_lectura_permitidas():
    assert verificar_ruta("GET", f"/api/{S}/chats") == S
    assert verificar_ruta("GET", f"/api/{S}/chats/all/messages") == S
    assert verificar_ruta("GET", f"/api/{S}/chats/{PN}@c.us/messages") == S
    assert verificar_ruta("GET", f"/api/{S}/chats/123456789@lid/messages") == S
    assert verificar_ruta("GET", f"/api/{S}/lids") == S


@pytest.mark.parametrize("metodo,ruta", [
    ("GET", f"/api/{S}/chats/overview"),
    ("GET", f"/api/{S}/chats/1203630@g.us/messages"),
    ("GET", f"/api/{S}/chats/status@broadcast/messages"),
    ("GET", f"/api/{S}/chats/x/messages"),
    ("GET", f"/api/{S}/chats/all/messages/read"),
    ("POST", f"/api/{S}/chats/all/messages"),
    ("GET", f"/api/{S}/chats/{PN}@c.us/messages\n"),
    ("GET", f"/api/{S}/lids/count"),
    ("GET", f"/api/{S}/lids/pn/{PN}"),
])
def test_lecturas_fuera_de_forma_se_rechazan(metodo, ruta):
    with pytest.raises(RutaNoPermitida):
        verificar_ruta(metodo, ruta)


async def test_download_media_false_siempre_aunque_se_pida_otra_cosa():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await cli._request("GET", f"/api/{S}/chats/all/messages", params={"downloadMedia": "true", "limit": 1})
        await cli.listar_mensajes(S, "all", limit=10, offset=0, gte=100)
    assert [p["downloadMedia"] for p in waha.params] == ["false", "false"]
    assert waha.params[1]["filter.timestamp.gte"] == "100" and "filter.timestamp.lte" not in waha.params[1]


async def test_listar_mensajes_reduce_y_cuenta_crudos():
    waha = WahaFalso()
    waha.mensajes[S] = [msg_crudo(PN, 1, ts=1000, body="hola secreto"),
                        {**msg_crudo("1203630", 2, ts=1001), "chatId": "1203630@g.us", "from": "1203630@g.us"}]
    async with _cli(waha) as cli:
        pagina = await cli.listar_mensajes(S, "all", limit=10, offset=0)
    assert pagina.crudos == 2 and len(pagina.mensajes) == 1        # el grupo no pasa
    assert pagina.mensajes[0].chat.usuario == PN and pagina.mensajes[0].texto == "hola secreto"
    assert "hola secreto" not in repr(pagina)


async def test_listar_chats_ordenados_por_ultimo_mensaje_y_lids():
    waha = WahaFalso()
    waha.chats[S] = [chat_crudo(PN, ts=10, nombre="Marta"), chat_crudo("5493412222222", ts=20),
                     {"id": "1203630@g.us", "conversationTimestamp": 30}]
    waha.lids[S] = [{"lid": "123456789@lid", "pn": f"{PN}@c.us"}, {"lid": "basura", "pn": "x"}]
    async with _cli(waha) as cli:
        chats = await cli.listar_chats(S, limit=10, offset=0)
        lids = await cli.listar_lids(S, limit=10, offset=0)
    assert chats.crudos == 3 and [c.jid.usuario for c in chats.chats] == ["5493412222222", PN]
    assert waha.params[0]["sortBy"] == "conversationTimestamp" and waha.params[0]["sortOrder"] == "desc"
    assert lids.crudos == 2 and lids.pares == [("123456789", PN)]


async def test_limite_y_errores():
    waha = WahaFalso()
    waha.falla_mensajes_de.add(f"{PN}@c.us")
    async with _cli(waha) as cli:
        with pytest.raises(ValueError):
            await cli.listar_chats(S, limit=501, offset=0)
        with pytest.raises(WahaError):
            await cli.listar_mensajes(S, f"{PN}@c.us", limit=10, offset=0)
```

Modificar `tests/radar_tests/test_waha_cliente.py`: en la parametrización de `test_rutas_fuera_de_la_lista_blanca_no_salen`, reemplazar la línea

```python
    ("GET", f"/api/{S}/chats"),
```

por

```python
    ("GET", f"/api/{S}/chats/1203630@g.us/messages"),
```

(`GET /api/{s}/chats` pasa a estar permitida; `chats/x/messages`, `chats/overview` y `messages/read` siguen rechazadas).

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_waha_lecturas.py tests/radar_tests/test_waha_cliente.py -q`
Expected: FAIL con `ImportError: cannot import name 'chat_crudo' from 'tests.radar_tests.waha_falso'` (y las rutas de lectura todavía lanzan `RutaNoPermitida`).

- [ ] **Step 3: WAHA falso con lecturas**

En `tests/radar_tests/waha_falso.py`:

1. Debajo de `PNG = ...` agregar:

```python
ME = "5493411234567@c.us"


def msg_crudo(usuario: str, n: int, *, ts: int, from_me: bool = False, body: str | None = "hola",
              dominio: str = "c.us", ack: int = -1) -> dict:
    """Mensaje como lo devuelve WAHA (forma NOWEB, sin _data)."""
    chat = f"{usuario}@{dominio}"
    return {"id": f"{'true' if from_me else 'false'}_{chat}_M{n:05d}", "chatId": chat,
            "from": ME if from_me else chat, "to": chat if from_me else ME, "fromMe": from_me,
            "timestamp": ts, "body": body, "hasMedia": False, "ack": ack}


def chat_crudo(usuario: str, *, ts: int, nombre: str | None = None, dominio: str = "c.us") -> dict:
    return {"id": {"_serialized": f"{usuario}@{dominio}"}, "name": nombre, "conversationTimestamp": ts}
```

2. En `WahaFalso.__init__`, después de `self._n = 0`, agregar:

```python
        self.chats: dict[str, list[dict]] = {}        # sesión -> chats crudos
        self.mensajes: dict[str, list[dict]] = {}     # sesión -> mensajes crudos (con chatId)
        self.lids: dict[str, list[dict]] = {}         # sesión -> [{"lid": "...@lid", "pn": "...@c.us"}]
        self.params: list[dict] = []                  # query de cada lectura, con la ruta
        self.api_keys: list[str] = []                 # X-Api-Key de cada llamada
        self.falla_mensajes_de: set[str] = set()      # chats cuyo GET de mensajes da 500
```

3. En `__call__`, reemplazar

```python
        self.llamadas.append(f"{m} {p}")
        partes = p.split("/")
```

por

```python
        self.llamadas.append(f"{m} {p}")
        self.api_keys.append(req.headers.get("x-api-key", ""))
        partes = p.split("/")
        if m == "GET" and len(partes) >= 4 and partes[1] == "api" and partes[3] in ("chats", "lids"):
            return self._lecturas(req, partes)
```

4. Agregar el método al final de la clase:

```python
    def _lecturas(self, req: httpx.Request, partes: list[str]) -> httpx.Response:
        q = dict(req.url.params)
        self.params.append({"ruta": req.url.path, **q})
        sesion = partes[2]
        limit, offset = int(q.get("limit", 100)), int(q.get("offset", 0))
        if partes[3] == "lids" and len(partes) == 4:
            return httpx.Response(200, json=self.lids.get(sesion, [])[offset:offset + limit])
        if partes[3] == "chats" and len(partes) == 4:
            filas = sorted(self.chats.get(sesion, []), key=lambda c: c.get("conversationTimestamp") or 0,
                           reverse=True)
            return httpx.Response(200, json=filas[offset:offset + limit])
        if partes[3] == "chats" and len(partes) == 6 and partes[5] == "messages":
            chat = partes[4]
            if chat in self.falla_mensajes_de:
                return httpx.Response(500, json={})
            filas = [x for x in self.mensajes.get(sesion, []) if chat == "all" or x["chatId"] == chat]
            if "filter.timestamp.gte" in q:
                filas = [x for x in filas if x["timestamp"] >= int(q["filter.timestamp.gte"])]
            if "filter.timestamp.lte" in q:
                filas = [x for x in filas if x["timestamp"] <= int(q["filter.timestamp.lte"])]
            filas.sort(key=lambda x: x["timestamp"], reverse=True)
            return httpx.Response(200, json=filas[offset:offset + limit])
        return httpx.Response(404, json={})
```

5. En `__init__`, reemplazar `self.me_id = "5493411234567@c.us"` por `self.me_id = ME`.

- [ ] **Step 4: Cliente con lecturas**

En `app/radar/waha/cliente.py`:

1. En el docstring, reemplazar la línea `  Las lecturas de chats y mensajes llegan en el tramo 3.` por:

```text
  Lecturas del tramo 3: lista de chats, mensajes de un chat individual o de
  `all` (con downloadMedia=false forzado) y el mapeo de lids. Nunca
  chats/overview (trae el cuerpo del último mensaje).
```

2. Reemplazar los imports y la lista blanca:

```python
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.radar.ingesta.reduccion import ChatWaha, MensajeWaha, parsear_jid, reducir_chat, reducir_mensaje

# Siempre con fullmatch: con `$` y re.match un salto de línea final pasaba el control.
PATRON_SESION = re.compile(r"^v_[0-9a-f]{12}$")
_S = r"(?P<sesion>[^/]+)"
# Solo `all` o un chat individual: nunca grupos, estados ni canales.
_CHAT = r"(?P<chat>all|[0-9]{5,20}@(?:c\.us|s\.whatsapp\.net|lid))"
_MENSAJES = re.compile(rf"^/api/{_S}/chats/{_CHAT}/messages$")
RUTAS_PERMITIDAS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/api/server/version$")),
    ("POST", re.compile(r"^/api/sessions$")),
    ("GET", re.compile(rf"^/api/sessions/{_S}$")),
    ("DELETE", re.compile(rf"^/api/sessions/{_S}$")),
    ("POST", re.compile(rf"^/api/sessions/{_S}/(start|stop|restart)$")),
    ("GET", re.compile(rf"^/api/{_S}/auth/qr$")),
    ("POST", re.compile(rf"^/api/{_S}/auth/request-code$")),
    ("POST", re.compile(r"^/api/keys$")),
    ("GET", re.compile(r"^/api/keys$")),
    ("DELETE", re.compile(r"^/api/keys/[A-Za-z0-9_-]{1,80}$")),
    # tramo 3: lecturas
    ("GET", re.compile(rf"^/api/{_S}/chats$")),
    ("GET", _MENSAJES),
    ("GET", re.compile(rf"^/api/{_S}/lids$")),
)
_CODIGO = re.compile(r"^[A-Z0-9]{4}-?[A-Z0-9]{4}$")
LIMITE_MAXIMO = 500
```

3. Debajo de la clase `WahaHttpError` agregar:

```python
@dataclass(frozen=True)
class PaginaChats:
    chats: list[ChatWaha]
    crudos: int          # ítems que devolvió WAHA antes de reducir: 0 = fin de datos


@dataclass(frozen=True)
class PaginaMensajes:
    mensajes: list[MensajeWaha]
    crudos: int


@dataclass(frozen=True)
class PaginaLids:
    pares: list[tuple[str, str]]     # (lid, teléfono) solo dígitos; vive en memoria, nunca se persiste
    crudos: int


def _limite(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= LIMITE_MAXIMO:
        raise ValueError(f"limit fuera de rango (1..{LIMITE_MAXIMO})")
```

4. En `WahaCliente._request`, reemplazar

```python
        sesion = verificar_ruta(metodo, ruta)
        if sesion is not None:
            verificar_nombre_sesion(sesion)
```

por

```python
        sesion = verificar_ruta(metodo, ruta)
        if sesion is not None:
            verificar_nombre_sesion(sesion)
        if metodo == "GET" and _MENSAJES.fullmatch(ruta):
            # §6.2: el default del servidor es true; nunca se descargan medios.
            params = {**(params or {}), "downloadMedia": "false"}
```

5. Al final de la clase `WahaCliente` agregar:

```python
    # --- lecturas (tramo 3) ------------------------------------------------------
    @staticmethod
    def _lista(r: httpx.Response) -> list:
        try:
            datos = r.json()
        except ValueError:
            raise WahaError("GET: respuesta que no es JSON") from None
        if not isinstance(datos, list):
            raise WahaError("GET: WAHA no devolvió una lista")
        return datos

    async def listar_chats(self, sesion: str, *, limit: int, offset: int) -> PaginaChats:
        _limite(limit)
        r = await self._request("GET", f"/api/{sesion}/chats", esperar=(200,),
                                params={"limit": limit, "offset": offset, "sortBy": "conversationTimestamp",
                                        "sortOrder": "desc"})
        crudos = self._lista(r)
        return PaginaChats(chats=[c for c in map(reducir_chat, crudos) if c is not None], crudos=len(crudos))

    async def listar_mensajes(self, sesion: str, chat: str, *, limit: int, offset: int,
                              gte: Optional[int] = None, lte: Optional[int] = None) -> PaginaMensajes:
        _limite(limit)
        params: dict[str, Any] = {"limit": limit, "offset": offset, "downloadMedia": "false"}
        if gte is not None:
            params["filter.timestamp.gte"] = int(gte)
        if lte is not None:
            params["filter.timestamp.lte"] = int(lte)
        r = await self._request("GET", f"/api/{sesion}/chats/{chat}/messages", params=params, esperar=(200,))
        crudos = self._lista(r)
        return PaginaMensajes(mensajes=[m for m in map(reducir_mensaje, crudos) if m is not None],
                              crudos=len(crudos))

    async def listar_lids(self, sesion: str, *, limit: int, offset: int) -> PaginaLids:
        _limite(limit)
        r = await self._request("GET", f"/api/{sesion}/lids", params={"limit": limit, "offset": offset},
                                esperar=(200,))
        crudos = self._lista(r)
        pares: list[tuple[str, str]] = []
        for fila in crudos:
            if isinstance(fila, dict):
                lid, pn = parsear_jid(fila.get("lid")), parsear_jid(fila.get("pn"))
                if lid is not None and pn is not None and lid.es_lid and not pn.es_lid:
                    pares.append((lid.usuario, pn.usuario))
        return PaginaLids(pares=pares, crudos=len(crudos))
```

- [ ] **Step 5: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_waha_lecturas.py tests/radar_tests/test_waha_cliente.py tests/radar_tests/test_waha_gestor.py tests/radar_tests/test_waha_sesion.py -q`
Expected: PASS.

- [ ] **Step 6: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/waha/cliente.py tests/radar_tests/waha_falso.py tests/radar_tests/test_waha_cliente.py tests/radar_tests/test_waha_lecturas.py
git commit -m "Radar tramo 3: el cliente de WAHA lee chats, mensajes y lids con downloadMedia=false forzado

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Migración r0004 — tablas de conversación sin contenido, estado de ingesta y programación

**Files:**
- Create: `migrations_radar/versions/r0004_ingesta.py`
- Modify: `tests/radar_tests/conftest.py`, `tests/radar_tests/test_esquema.py`
- Test: `tests/radar_tests/test_ingesta_esquema.py`

**Interfaces:**
- Consumes: `definir_funcion_admin`, `grants_app`, `politica_por_tenant` (`app/radar/rls_sql.py`); `TIPOS_JOB`, `ESTADOS_INGESTA`, `TIPOS_MENSAJE`, `SUGERENCIAS_EXCLUSION` (Task 1).
- Produces: tablas `wa_chats`, `wa_messages`, `suppressions`, `conteo_chats`, `ingesta_conteos`; columnas `lines.seleccion_confirmada_at`, `lines.periodo_dias`, y en `links`: `ingesta`, `conteo_inicio_at`, `conteo_estable_desde`, `conteo_n_chats`, `conteo_n_msgs`, `conteo_tope`, `ventanas_backfill`, `importar_desde`, `backfill_pasada`, `cobertura`, `reimportar_chats`, `ultimo_msg_at`, `reconciliado_at`, `recon_pendientes`, `silencio_desde`, `reinicio_silencio_at`, `aviso_silencio_at`; `jobs_tipo_check` con `TIPOS_JOB`; función `radar_jobs_programar_ingesta() RETURNS integer`; `radar_admin_consola_lineas()` con las columnas de antes más `ingesta, cobertura, ultimo_msg_at, silencio_desde, backfill_pasada, chats_total, chats_importados, chats_fallidos, chats_pendientes_decision`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_ingesta_esquema.py`:

```python
"""
Esquema de la ingesta en la base de resultados (r0004, §6.4): sin texto ni id de
proveedor, HMAC con formato, RLS, CHECK de jobs y programación de la ingesta.
"""
import uuid

import asyncpg
import pytest

from app.radar import jobs as cola
from app.radar.constantes import TIPOS_JOB

from .helpers import (crear_consentimiento_directo, crear_link_directo, crear_linea_directa, crear_tenant_directo,
                      crear_usuario, crear_worker_directo)

H1, H2 = "a" * 64, "b" * 64


async def _link(db, *, estado="vinculado", nombre="A"):
    t = await crear_tenant_directo(db, nombre)
    u = await crear_usuario(db, t, f"dueno-{nombre.lower()}@cliente.com", "dueno")
    li = await crear_linea_directa(db, t)
    c = await crear_consentimiento_directo(db, t, li, u)
    w = await crear_worker_directo(db, nombre=f"w{nombre.lower()}")
    return t, li, await crear_link_directo(db, t, li, w, c, estado=estado)


async def test_wa_messages_sin_texto_ni_id_de_proveedor(radar_urls):
    con = await asyncpg.connect(radar_urls["migrator"])
    try:
        cols = {r["column_name"]: r["data_type"] for r in await con.fetch(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'wa_messages'")}
    finally:
        await con.close()
    assert set(cols) == {"id", "tenant_id", "line_id", "link_id", "chat_uuid", "from_me", "provider_ts", "tipo",
                         "has_media", "tiene_texto", "reply_to_uuid", "via", "solo_metadatos", "ack", "editado",
                         "revocado", "created_at", "updated_at"}
    assert sorted(c for c, t in cols.items() if t == "text") == ["tipo", "via"]


async def test_wa_chats_hmac_con_formato_y_excluido_con_motivo(radar_db):
    t, li, _ = await _link(radar_db)
    async with radar_db.tenant_tx(t) as con:
        await con.execute("INSERT INTO wa_chats (line_id, contact_hmac, pn_resuelto) VALUES ($1, $2, TRUE)", li, H1)
        with pytest.raises(asyncpg.CheckViolationError):
            async with con.transaction():
                await con.execute("INSERT INTO wa_chats (line_id, contact_hmac, pn_resuelto) "
                                  "VALUES ($1, '5493411111111', TRUE)", li)
        with pytest.raises(asyncpg.CheckViolationError):
            async with con.transaction():
                await con.execute("INSERT INTO wa_chats (line_id, lid_hmac, pn_resuelto, estado) "
                                  "VALUES ($1, $2, FALSE, 'excluido')", li, H2)
        with pytest.raises(asyncpg.UniqueViolationError):
            async with con.transaction():
                await con.execute("INSERT INTO wa_chats (line_id, contact_hmac, pn_resuelto) VALUES ($1, $2, TRUE)",
                                  li, H1)


async def test_rls_en_wa_chats(radar_db):
    a, la, _ = await _link(radar_db, nombre="A")
    b, _, _ = await _link(radar_db, nombre="B")
    async with radar_db.tenant_tx(a) as con:
        await con.execute("INSERT INTO wa_chats (line_id, contact_hmac, pn_resuelto) VALUES ($1, $2, TRUE)", la, H1)
    async with radar_db.tenant_tx(b) as con:
        assert await con.fetchval("SELECT count(*) FROM wa_chats") == 0


async def test_jobs_acepta_los_tipos_nuevos_y_nada_mas(radar_db):
    t, _, k = await _link(radar_db)
    async with radar_db.tenant_tx(t) as con:
        for tipo in TIPOS_JOB:
            assert await cola.encolar(con, tipo=tipo, link_id=k) is not None
        with pytest.raises(asyncpg.CheckViolationError):
            await cola.encolar(con, tipo="otra_cosa", link_id=k)


async def test_programar_ingesta_encola_conteo_y_reconciliacion_con_desfase(radar_db):
    t, _, k1 = await _link(radar_db, nombre="A")
    t2, li2, k2 = await _link(radar_db, nombre="B")
    async with radar_db.tenant_tx(t2) as con:
        await con.execute("UPDATE links SET ingesta = 'importando', conectado_at = now() WHERE id = $1", k2)
    assert await cola.programar_ingesta(radar_db) == 2
    assert await cola.programar_ingesta(radar_db) == 0            # uno vivo por (link, tipo)
    async with radar_db.tenant_tx(t) as con:
        assert await con.fetchval("SELECT tipo FROM jobs WHERE link_id = $1", k1) == "conteo"
    async with radar_db.tenant_tx(t2) as con:
        j = await con.fetchrow("SELECT tipo, extract(epoch FROM ejecutar_desde - created_at)::int AS desfase "
                               "FROM jobs WHERE link_id = $1", k2)
    assert j["tipo"] == "reconciliacion" and j["desfase"] == int(li2.hex[:7], 16) % 900


async def test_programar_ingesta_adelanta_la_reconciliacion_tras_una_caida(radar_db):
    t, _, k = await _link(radar_db)
    async with radar_db.tenant_tx(t) as con:
        await con.execute("UPDATE links SET ingesta = 'al_dia', conectado_at = now(), "
                          "reconciliado_at = now() - interval '1 hour' WHERE id = $1", k)
        await con.execute("INSERT INTO jobs (tipo, link_id, ejecutar_desde) "
                          "VALUES ('reconciliacion', $1, now() + interval '10 minutes')", k)
        await con.execute("INSERT INTO link_status_events (link_id, waha_status, origen, estado_link) "
                          "VALUES ($1, 'FAILED', 'webhook', 'caido'), ($1, 'WORKING', 'webhook', 'vinculado')", k)
    assert await cola.programar_ingesta(radar_db) == 1
    async with radar_db.tenant_tx(t) as con:
        assert await con.fetchval("SELECT ejecutar_desde <= now() FROM jobs WHERE link_id = $1", k)


async def test_links_no_actualiza_columnas_de_identidad(radar_db):
    t, _, k = await _link(radar_db)
    async with radar_db.tenant_tx(t) as con:
        await con.execute("UPDATE links SET ingesta = 'contando', ultimo_msg_at = now(), "
                          "recon_pendientes = ARRAY[$2::uuid] WHERE id = $1", k, uuid.uuid4())
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.execute("UPDATE links SET line_id = line_id WHERE id = $1", k)


async def test_ventanas_backfill_solo_numeros(radar_db):
    t, _, k = await _link(radar_db)
    async with radar_db.tenant_tx(t) as con:
        await con.execute("UPDATE links SET ventanas_backfill = '[[1, 2]]'::jsonb WHERE id = $1", k)
        with pytest.raises(asyncpg.CheckViolationError):
            await con.execute("UPDATE links SET ventanas_backfill = '[\"5493411111111@c.us\"]'::jsonb "
                              "WHERE id = $1", k)
```

En `tests/radar_tests/test_esquema.py`:

1. Reemplazar `TABLAS_TENANT` por:

```python
TABLAS_TENANT = {"users", "memberships", "lines", "login_tokens", "sessions", "consents",
                 "support_grants", "access_audit_log", "product_events",
                 "waha_workers", "links", "link_status_events", "jobs",
                 "wa_chats", "wa_messages", "suppressions", "conteo_chats", "ingesta_conteos"}
```

2. En `test_sin_columnas_de_conversacion_ni_identificadores_de_whatsapp`, reemplazar la consulta por (los HMAC son la forma seudonimizada que §6.4 manda guardar acá):

```python
        filas = await con.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name ~ "
            "'(^|_)(phone|telefono|jid|lid|body|contenido|payload|chat_id|mensaje|provider_msg_id)(_|$)' "
            "AND column_name !~ '^(contact|lid)_hmac$'")
```

En `tests/radar_tests/conftest.py`, reemplazar `TABLAS` por:

```python
TABLAS = ["ingesta_conteos", "conteo_chats", "wa_messages", "wa_chats", "suppressions",
          "jobs", "link_status_events", "links", "waha_workers",
          "product_events", "access_audit_log", "support_grants", "consents", "sessions",
          "login_tokens", "memberships", "lines", "users", "tenants"]
```

Y en `app/radar/jobs.py`, al final:

```python
async def programar_ingesta(db: RadarDB) -> int:
    """Tramo 3: conteo de los vínculos nuevos, una reconciliación viva por vínculo que
    ya importa y adelanto de la reconciliación al volver a WORKING (§6.3 punto 3)."""
    async with db.sin_tenant() as con:
        return await con.fetchval("SELECT radar_jobs_programar_ingesta()")
```

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_esquema.py tests/radar_tests/test_esquema.py -q`
Expected: FAIL (`relation "wa_chats" does not exist`, `function radar_jobs_programar_ingesta() does not exist`, y el conjunto de tablas de `test_esquema` no coincide).

- [ ] **Step 3: Migración**

Crear `migrations_radar/versions/r0004_ingesta.py`:

```python
"""Radar r0004: ingesta del tramo 3 en la base de resultados (§6.3, §6.4).

Conversaciones SIN contenido: `wa_chats` (solo HMAC de contacto y de lid, estado y
motivo), `wa_messages` (UUID, tiempos, tipo y banderas; sin texto ni id de
proveedor), `suppressions` (solo HMAC), el conteo de P4 por chat y la contabilidad
por pasada del backfill. El texto, los ids de proveedor y la identidad viven en el
almacén de fuente (migrations_fuente/f0002).

Revision ID: r0004
Revises: r0003
Create Date: 2026-09-24
"""
from alembic import op

from app.radar.constantes import ESTADOS_INGESTA, SUGERENCIAS_EXCLUSION, TIPOS_JOB, TIPOS_MENSAJE
from app.radar.rls_sql import definir_funcion_admin, grants_app, politica_por_tenant

revision = "r0004"
down_revision = "r0003"
branch_labels = None
depends_on = None


def _lista(valores) -> str:
    return "(" + ", ".join(f"'{v}'" for v in valores) + ")"


# wa_messages admite DELETE: excluir un chat después de P4 borra lo que tenía (§3 P4).
GRANTS = {
    "wa_chats": "SELECT, INSERT",
    "wa_messages": "SELECT, INSERT, DELETE",
    "suppressions": "SELECT, INSERT",
    "conteo_chats": "SELECT, INSERT",
    "ingesta_conteos": "SELECT, INSERT",
}
# Criterio de r0002/r0003: nunca id, tenant_id, FK de pertenencia ni created_at.
UPDATES_POR_COLUMNA = {
    "wa_chats": "contact_hmac, lid_hmac, pn_resuelto, estado, motivo, sugerencia, decidido_at, updated_at",
    "wa_messages": ("tipo, has_media, tiene_texto, reply_to_uuid, solo_metadatos, ack, editado, revocado, "
                    "updated_at"),
    "conteo_chats": "entrantes, propios, con_texto, ultimo_ts, sugerencia, updated_at",
    "ingesta_conteos": "mensajes, estado, updated_at",
    "links": ("ingesta, conteo_inicio_at, conteo_estable_desde, conteo_n_chats, conteo_n_msgs, conteo_tope, "
              "ventanas_backfill, importar_desde, backfill_pasada, cobertura, reimportar_chats, ultimo_msg_at, "
              "reconciliado_at, recon_pendientes, silencio_desde, reinicio_silencio_at, aviso_silencio_at"),
    "lines": "seleccion_confirmada_at, periodo_dias",
}
_HMAC = "~ '^[0-9a-f]{64}$'"
_SUGERENCIAS = _lista(SUGERENCIAS_EXCLUSION)
# C1 (§3.1) del tramo 2 más los datos de la ingesta.
_CONSOLA = """
        CREATE FUNCTION radar_admin_consola_lineas()
            RETURNS TABLE (tenant_id uuid, tenant_nombre text, line_id uuid, line_nombre text, line_estado text,
                           link_id uuid, link_estado text, waha_status text, numero_sufijo text,
                           observado_hasta timestamptz, restriccion_hasta timestamptz, restriccion_sin_fecha bool,
                           caido_desde timestamptz, ultimo_status_at timestamptz, engine text,
                           worker_nombre text, worker_max_sesiones int, worker_sesiones int,
                           ingesta text, cobertura text, ultimo_msg_at timestamptz, silencio_desde timestamptz,
                           backfill_pasada int, chats_total int, chats_importados int, chats_fallidos int,
                           chats_pendientes_decision int)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT t.id, t.nombre, li.id, li.nombre, li.estado,
                       lk.id, lk.estado, lk.waha_status, lk.numero_sufijo, lk.observado_hasta,
                       lk.restriccion_hasta, lk.restriccion_sin_fecha, lk.caido_desde, lk.ultimo_status_at,
                       lk.engine, w.nombre, w.max_sesiones,
                       (SELECT count(*)::int FROM links x
                        WHERE x.worker_id = w.id AND x.estado IN ('creando', 'esperando_qr', 'vinculado', 'caido', 'cerrando')),
                       lk.ingesta, lk.cobertura, lk.ultimo_msg_at, lk.silencio_desde, lk.backfill_pasada::int,
                       (SELECT count(*)::int FROM wa_chats c WHERE c.line_id = li.id AND c.estado <> 'excluido'),
                       (SELECT count(*)::int FROM ingesta_conteos k
                         WHERE k.link_id = lk.id AND k.pasada = lk.backfill_pasada AND k.estado = 'ok'),
                       (SELECT count(*)::int FROM ingesta_conteos k
                         WHERE k.link_id = lk.id AND k.pasada = lk.backfill_pasada AND k.estado = 'fallido'),
                       (SELECT count(*)::int FROM wa_chats c
                         WHERE c.line_id = li.id AND c.estado = 'pendiente' AND li.seleccion_confirmada_at IS NOT NULL)
                FROM lines li
                JOIN tenants t ON t.id = li.tenant_id AND NOT t.es_kis
                LEFT JOIN LATERAL (SELECT * FROM links l WHERE l.line_id = li.id
                                   ORDER BY l.created_at DESC LIMIT 1) lk ON true
                LEFT JOIN waha_workers w ON w.id = lk.worker_id
                ORDER BY t.nombre, li.nombre
            $f$;
"""
# Copia textual de r0003, para el downgrade.
_CONSOLA_R0003 = """
        CREATE FUNCTION radar_admin_consola_lineas()
            RETURNS TABLE (tenant_id uuid, tenant_nombre text, line_id uuid, line_nombre text, line_estado text,
                           link_id uuid, link_estado text, waha_status text, numero_sufijo text,
                           observado_hasta timestamptz, restriccion_hasta timestamptz, restriccion_sin_fecha bool,
                           caido_desde timestamptz, ultimo_status_at timestamptz, engine text,
                           worker_nombre text, worker_max_sesiones int, worker_sesiones int)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT t.id, t.nombre, li.id, li.nombre, li.estado,
                       lk.id, lk.estado, lk.waha_status, lk.numero_sufijo, lk.observado_hasta,
                       lk.restriccion_hasta, lk.restriccion_sin_fecha, lk.caido_desde, lk.ultimo_status_at,
                       lk.engine, w.nombre, w.max_sesiones,
                       (SELECT count(*)::int FROM links x
                        WHERE x.worker_id = w.id AND x.estado IN ('creando', 'esperando_qr', 'vinculado', 'caido', 'cerrando'))
                FROM lines li
                JOIN tenants t ON t.id = li.tenant_id AND NOT t.es_kis
                LEFT JOIN LATERAL (SELECT * FROM links l WHERE l.line_id = li.id
                                   ORDER BY l.created_at DESC LIMIT 1) lk ON true
                LEFT JOIN waha_workers w ON w.id = lk.worker_id
                ORDER BY t.nombre, li.nombre
            $f$;
"""


def upgrade() -> None:
    op.execute("""
        ALTER TABLE lines
            ADD COLUMN seleccion_confirmada_at TIMESTAMPTZ NULL,
            ADD COLUMN periodo_dias INTEGER NULL CHECK (periodo_dias IN (30, 90, 180, 365));

        ALTER TABLE links
            ADD COLUMN ingesta TEXT NULL CHECK (ingesta IN """ + _lista(ESTADOS_INGESTA) + """),
            ADD COLUMN conteo_inicio_at TIMESTAMPTZ NULL,
            ADD COLUMN conteo_estable_desde TIMESTAMPTZ NULL,
            ADD COLUMN conteo_n_chats INTEGER NULL CHECK (conteo_n_chats >= 0),
            ADD COLUMN conteo_n_msgs INTEGER NULL CHECK (conteo_n_msgs >= 0),
            ADD COLUMN conteo_tope BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN ventanas_backfill JSONB NULL CHECK (ventanas_backfill IS NULL OR (
                jsonb_typeof(ventanas_backfill) = 'array'
                AND NOT jsonb_path_exists(ventanas_backfill, '$.** ? (@.type() == "string")'))),
            ADD COLUMN importar_desde TIMESTAMPTZ NULL,
            ADD COLUMN backfill_pasada SMALLINT NOT NULL DEFAULT 0 CHECK (backfill_pasada BETWEEN 0 AND 4),
            ADD COLUMN cobertura TEXT NULL CHECK (cobertura IN ('provisoria', 'estable')),
            ADD COLUMN reimportar_chats UUID[] NULL,
            ADD COLUMN ultimo_msg_at TIMESTAMPTZ NULL,
            ADD COLUMN reconciliado_at TIMESTAMPTZ NULL,
            ADD COLUMN recon_pendientes UUID[] NULL,
            ADD COLUMN silencio_desde TIMESTAMPTZ NULL,
            ADD COLUMN reinicio_silencio_at TIMESTAMPTZ NULL,
            ADD COLUMN aviso_silencio_at TIMESTAMPTZ NULL;

        ALTER TABLE jobs DROP CONSTRAINT jobs_tipo_check;
        ALTER TABLE jobs ADD CONSTRAINT jobs_tipo_check CHECK (tipo IN """ + _lista(TIPOS_JOB) + """);

        CREATE TABLE wa_chats (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            line_id      UUID NOT NULL REFERENCES lines(id),
            contact_hmac TEXT NULL CHECK (contact_hmac """ + _HMAC + """),
            lid_hmac     TEXT NULL CHECK (lid_hmac """ + _HMAC + """),
            pn_resuelto  BOOLEAN NOT NULL,
            estado       TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('incluido', 'excluido', 'pendiente')),
            motivo       TEXT NULL CHECK (motivo IN ('dueno', 'sugerencia', 'suprimido')),
            sugerencia   TEXT NULL CHECK (sugerencia IN """ + _SUGERENCIAS + """),
            decidido_at  TIMESTAMPTZ NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (contact_hmac IS NOT NULL OR lid_hmac IS NOT NULL),
            CHECK (pn_resuelto = (contact_hmac IS NOT NULL)),
            CHECK ((estado = 'excluido') = (motivo IS NOT NULL))
        );
        CREATE UNIQUE INDEX wa_chats_contacto ON wa_chats (line_id, contact_hmac) WHERE contact_hmac IS NOT NULL;
        CREATE UNIQUE INDEX wa_chats_lid ON wa_chats (line_id, lid_hmac) WHERE lid_hmac IS NOT NULL;

        -- El UUID lo fija wa_message_provider_ids (almacén de fuente): así el upsert
        -- de las dos bases es idempotente (§6.3 punto 4).
        CREATE TABLE wa_messages (
            id             UUID PRIMARY KEY,
            tenant_id      UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            line_id        UUID NOT NULL REFERENCES lines(id),
            link_id        UUID NOT NULL REFERENCES links(id),
            chat_uuid      UUID NOT NULL REFERENCES wa_chats(id),
            from_me        BOOLEAN NOT NULL,
            provider_ts    TIMESTAMPTZ NOT NULL,
            tipo           TEXT NOT NULL CHECK (tipo IN """ + _lista(TIPOS_MENSAJE) + """),
            has_media      BOOLEAN NOT NULL,
            tiene_texto    BOOLEAN NOT NULL,
            reply_to_uuid  UUID NULL,
            via            TEXT NOT NULL CHECK (via IN ('webhook', 'backfill', 'reconciliacion')),
            solo_metadatos BOOLEAN NOT NULL,
            ack            SMALLINT NULL CHECK (ack BETWEEN -1 AND 4),
            editado        BOOLEAN NOT NULL DEFAULT FALSE,
            revocado       BOOLEAN NOT NULL DEFAULT FALSE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX wa_messages_chat ON wa_messages (tenant_id, line_id, chat_uuid, provider_ts);
        CREATE INDEX wa_messages_linea ON wa_messages (tenant_id, line_id, provider_ts);

        -- Solo HMAC (§6.4). "Suprimir contacto" (tramo 6) inserta; la ingesta solo lee.
        CREATE TABLE suppressions (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            contact_hmac TEXT NULL CHECK (contact_hmac """ + _HMAC + """),
            lid_hmac     TEXT NULL CHECK (lid_hmac """ + _HMAC + """),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (contact_hmac IS NOT NULL OR lid_hmac IS NOT NULL)
        );
        CREATE UNIQUE INDEX suppressions_contacto ON suppressions (tenant_id, contact_hmac)
            WHERE contact_hmac IS NOT NULL;
        CREATE UNIQUE INDEX suppressions_lid ON suppressions (tenant_id, lid_hmac) WHERE lid_hmac IS NOT NULL;

        -- §3 P4: conteo por chat de la pasada de conteo. Solo números.
        CREATE TABLE conteo_chats (
            tenant_id  UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            link_id    UUID NOT NULL REFERENCES links(id),
            chat_uuid  UUID NOT NULL REFERENCES wa_chats(id),
            entrantes  INTEGER NOT NULL CHECK (entrantes >= 0),
            propios    INTEGER NOT NULL CHECK (propios >= 0),
            con_texto  INTEGER NOT NULL CHECK (con_texto >= 0),
            ultimo_ts  TIMESTAMPTZ NULL,
            sugerencia TEXT NULL CHECK (sugerencia IN """ + _SUGERENCIAS + """),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (link_id, chat_uuid)
        );

        -- Estados especiales, "Errores de sincronización": contabilidad por chat y pasada.
        CREATE TABLE ingesta_conteos (
            tenant_id  UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            link_id    UUID NOT NULL REFERENCES links(id),
            chat_uuid  UUID NOT NULL REFERENCES wa_chats(id),
            pasada     SMALLINT NOT NULL CHECK (pasada BETWEEN 1 AND 4),
            mensajes   INTEGER NOT NULL CHECK (mensajes >= 0),
            estado     TEXT NOT NULL CHECK (estado IN ('ok', 'fallido')),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (link_id, chat_uuid, pasada)
        );
    """)

    for tabla, privilegios in GRANTS.items():
        op.execute(politica_por_tenant(tabla))
        op.execute(grants_app(tabla, privilegios))
    for tabla, columnas in UPDATES_POR_COLUMNA.items():
        op.execute(f"GRANT UPDATE ({columnas}) ON {tabla} TO radar_app;")

    # radar_admin lee lo que necesitan C1 y la programación.
    op.execute("""
        GRANT SELECT ON wa_chats, ingesta_conteos, link_status_events TO radar_admin;
        CREATE POLICY wa_chats_admin ON wa_chats FOR SELECT TO radar_admin USING (true);
        CREATE POLICY ingesta_conteos_admin ON ingesta_conteos FOR SELECT TO radar_admin USING (true);
        CREATE POLICY link_status_events_admin ON link_status_events FOR SELECT TO radar_admin USING (true);
        DROP FUNCTION radar_admin_consola_lineas();
    """)
    op.execute(definir_funcion_admin("radar_admin_consola_lineas()", _CONSOLA))
    op.execute(definir_funcion_admin(
        "radar_jobs_programar_ingesta()",
        """
        CREATE FUNCTION radar_jobs_programar_ingesta() RETURNS integer
            LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                WITH conteos AS (
                    INSERT INTO jobs (tenant_id, tipo, link_id)
                    SELECT l.tenant_id, 'conteo', l.id FROM links l
                     WHERE l.estado = 'vinculado' AND (l.ingesta IS NULL OR l.ingesta = 'contando')
                    ON CONFLICT DO NOTHING
                    RETURNING 1),
                -- Desfase fijo por línea (§6.3 punto 3): igual que reglas.desfase_s().
                recons AS (
                    INSERT INTO jobs (tenant_id, tipo, link_id, ejecutar_desde)
                    SELECT l.tenant_id, 'reconciliacion', l.id,
                           now() + make_interval(secs =>
                               ('x' || substr(replace(l.line_id::text, '-', ''), 1, 7))::bit(28)::int % 900)
                      FROM links l
                     WHERE l.estado = 'vinculado' AND l.ingesta IN ('importando', 'al_dia')
                    ON CONFLICT DO NOTHING
                    RETURNING 1),
                -- La reconciliación corre también al volver a WORKING: hubo una caída
                -- después de la última reconciliación y el vínculo está vinculado de nuevo.
                adelantadas AS (
                    UPDATE jobs j SET ejecutar_desde = now(), updated_at = now()
                      FROM links l
                     WHERE j.link_id = l.id AND j.tipo = 'reconciliacion' AND j.estado = 'pendiente'
                       AND j.ejecutar_desde > now() AND l.estado = 'vinculado'
                       AND EXISTS (SELECT 1 FROM link_status_events e
                                    WHERE e.link_id = l.id AND e.estado_link = 'caido'
                                      AND e.created_at > COALESCE(l.reconciliado_at, '-infinity'::timestamptz))
                    RETURNING 1)
                SELECT (SELECT count(*)::int FROM conteos) + (SELECT count(*)::int FROM recons)
                     + (SELECT count(*)::int FROM adelantadas)
            $f$;
        """,
    ))


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS radar_jobs_programar_ingesta();
        DROP FUNCTION IF EXISTS radar_admin_consola_lineas();
        DROP POLICY IF EXISTS link_status_events_admin ON link_status_events;
        REVOKE SELECT ON link_status_events FROM radar_admin;
        DROP TABLE IF EXISTS ingesta_conteos, conteo_chats, suppressions, wa_messages, wa_chats;
        DELETE FROM jobs WHERE tipo IN ('ingesta_inbox', 'conteo', 'backfill', 'reconciliacion');
        ALTER TABLE jobs DROP CONSTRAINT jobs_tipo_check;
        ALTER TABLE jobs ADD CONSTRAINT jobs_tipo_check CHECK (tipo IN ('fin_vinculo', 'chequeo_salud', 'aviso_caida'));
        REVOKE UPDATE (seleccion_confirmada_at, periodo_dias) ON lines FROM radar_app;
        ALTER TABLE lines DROP COLUMN IF EXISTS periodo_dias, DROP COLUMN IF EXISTS seleccion_confirmada_at;
        ALTER TABLE links
            DROP COLUMN IF EXISTS aviso_silencio_at, DROP COLUMN IF EXISTS reinicio_silencio_at,
            DROP COLUMN IF EXISTS silencio_desde, DROP COLUMN IF EXISTS recon_pendientes,
            DROP COLUMN IF EXISTS reconciliado_at, DROP COLUMN IF EXISTS ultimo_msg_at,
            DROP COLUMN IF EXISTS reimportar_chats, DROP COLUMN IF EXISTS cobertura,
            DROP COLUMN IF EXISTS backfill_pasada, DROP COLUMN IF EXISTS importar_desde,
            DROP COLUMN IF EXISTS ventanas_backfill, DROP COLUMN IF EXISTS conteo_tope,
            DROP COLUMN IF EXISTS conteo_n_msgs, DROP COLUMN IF EXISTS conteo_n_chats,
            DROP COLUMN IF EXISTS conteo_estable_desde, DROP COLUMN IF EXISTS conteo_inicio_at,
            DROP COLUMN IF EXISTS ingesta;
    """)
    op.execute(definir_funcion_admin("radar_admin_consola_lineas()", _CONSOLA_R0003))
```

Nota: el `DELETE FROM jobs` del downgrade corre como el migrator, que con `FORCE RLS` no ve filas; en un downgrade real correrlo antes como superusuario (`docs/radar-despliegue.md`, sección de rollback). No hay tests de downgrade (como en r0003).

- [ ] **Step 4: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_esquema.py tests/radar_tests/test_esquema.py tests/radar_tests/test_jobs.py tests/radar_tests/test_consola_api.py -q`
Expected: PASS (la C1 del tramo 2 sigue funcionando: `fila_consola` ignora las columnas nuevas hasta la Task 15).

- [ ] **Step 5: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add migrations_radar/versions/r0004_ingesta.py app/radar/jobs.py tests/radar_tests/conftest.py tests/radar_tests/test_esquema.py tests/radar_tests/test_ingesta_esquema.py
git commit -m "Radar tramo 3: r0004 con chats y mensajes sin contenido, estado de ingesta y programación

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Migración f0002 — almacén de fuente con RLS, particiones mensuales y purga del inbox

**Files:**
- Create: `migrations_fuente/versions/f0002_conversaciones.py`
- Modify: `app/radar/fuente.py`, `tests/radar_tests/conftest.py`, `tests/radar_tests/test_fuente_y_health.py`
- Test: `tests/radar_tests/test_fuente_conversaciones.py`

**Interfaces:**
- Consumes: `_normalizar_dsn` (`app/radar/db.py`).
- Produces: tablas de fuente `wa_message_provider_ids (tenant_id, line_id, provider_msg_id, message_uuid, chat_uuid, reply_to_raw, revocado, created_at)` con `PRIMARY KEY (line_id, provider_msg_id)`, `wa_message_bodies (tenant_id, line_id, message_uuid, provider_ts, chat_uuid, texto, texto_redactado, created_at, updated_at)` particionada por mes, `wa_contact_identities (tenant_id, line_id, chat_uuid, jid_pn, jid_lid, telefono, nombre, updated_at)`, `webhook_inbox (id, recibido_at, tenant_id, line_id, link_id, evento, evento_id, motivo, solo_metadatos, chat_uuid, payload, procesado_at)` particionada por mes; funciones `fuente_tenant_actual()` y `fuente_asegurar_particiones(integer) RETURNS integer`; `FuenteStore.pool`, `FuenteStore.tenant_tx(tenant_id)`, `FuenteStore.mantenimiento() -> dict` (`{"particiones": int, "inbox_purgados": int}`); `MESES_ADELANTE = 2`; fixture `radar_urls["fuente_super"]`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_fuente_conversaciones.py`:

```python
"""
Almacén de fuente del tramo 3 (§6.4, §7): RLS forzada por tenant, UNIQUE por
línea del id de proveedor, webhook_inbox sin payload de descartados, particiones
mensuales y purga a 7 días.
"""
import json
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest

from app.radar.fuente import FuenteStore


async def test_tablas_y_particiones(radar_urls):
    con = await asyncpg.connect(radar_urls["fuente"])
    try:
        padres = {r["relname"] for r in await con.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition")}
        particiones = {r["relname"] for r in await con.fetch(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid")}
        forzadas = {r["relname"] for r in await con.fetch(
            "SELECT relname FROM pg_class WHERE relforcerowsecurity AND relrowsecurity")}
    finally:
        await con.close()
    assert padres == {"fuente_meta", "alembic_version_fuente", "wa_message_provider_ids", "wa_message_bodies",
                      "wa_contact_identities", "webhook_inbox"}
    mes = datetime.now(timezone.utc).strftime("%Y%m")
    assert {f"webhook_inbox_{mes}", "webhook_inbox_default", "wa_message_bodies_202401",
            f"wa_message_bodies_{mes}", "wa_message_bodies_default"} <= particiones
    assert {"wa_message_provider_ids", "wa_message_bodies", "wa_contact_identities", "webhook_inbox"} <= forzadas


async def test_rls_por_tenant_y_sin_tenant_cero_filas(radar_ctx):
    a, b = uuid.uuid4(), uuid.uuid4()
    linea = uuid.uuid4()
    async with radar_ctx.fuente.tenant_tx(a) as con:
        await con.execute("INSERT INTO wa_message_provider_ids (line_id, provider_msg_id, message_uuid, chat_uuid) "
                          "VALUES ($1, 'false_5493411111111@c.us_A', $2, $3)", linea, uuid.uuid4(), uuid.uuid4())
    async with radar_ctx.fuente.tenant_tx(b) as con:
        assert await con.fetchval("SELECT count(*) FROM wa_message_provider_ids") == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.execute("INSERT INTO wa_message_provider_ids (tenant_id, line_id, provider_msg_id, "
                              "message_uuid, chat_uuid) VALUES ($1, $2, 'x', $3, $4)",
                              a, linea, uuid.uuid4(), uuid.uuid4())
    async with radar_ctx.fuente.pool.acquire() as con:
        assert await con.fetchval("SELECT count(*) FROM wa_message_provider_ids") == 0


async def test_id_de_proveedor_unico_por_linea(radar_ctx):
    t, linea = uuid.uuid4(), uuid.uuid4()
    async with radar_ctx.fuente.tenant_tx(t) as con:
        sql = ("INSERT INTO wa_message_provider_ids (line_id, provider_msg_id, message_uuid, chat_uuid) "
               "VALUES ($1, 'false_x_A', $2, $3)")
        await con.execute(sql, linea, uuid.uuid4(), uuid.uuid4())
        await con.execute(sql, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())       # otra línea: vale
        with pytest.raises(asyncpg.UniqueViolationError):
            await con.execute(sql, linea, uuid.uuid4(), uuid.uuid4())


async def test_inbox_no_guarda_payload_de_descartados_ni_mas_que_metadatos(radar_ctx):
    t = uuid.uuid4()
    base = ("INSERT INTO webhook_inbox (line_id, link_id, evento, motivo, solo_metadatos, payload, procesado_at) "
            "VALUES ($1, $2, 'message.any', $3, $4, $5::jsonb, $6)")
    async with radar_ctx.fuente.tenant_tx(t) as con:
        await con.execute(base, uuid.uuid4(), uuid.uuid4(), "excluido", False, None, datetime.now(timezone.utc))
        await con.execute(base, uuid.uuid4(), uuid.uuid4(), None, True,
                          json.dumps({"id": "x", "timestamp": 1, "fromMe": False}), None)
        with pytest.raises(asyncpg.CheckViolationError):
            async with con.transaction():
                await con.execute(base, uuid.uuid4(), uuid.uuid4(), "excluido", False,
                                  json.dumps({"body": "hola"}), datetime.now(timezone.utc))
        with pytest.raises(asyncpg.CheckViolationError):
            async with con.transaction():
                await con.execute(base, uuid.uuid4(), uuid.uuid4(), None, True,
                                  json.dumps({"id": "x", "timestamp": 1, "fromMe": False, "body": "hola"}), None)


async def test_mantenimiento_purga_el_inbox_a_7_dias(radar_ctx):
    t = uuid.uuid4()
    async with radar_ctx.fuente.tenant_tx(t) as con:
        for dias in (8, 1):
            await con.execute("INSERT INTO webhook_inbox (recibido_at, line_id, link_id, evento, motivo, "
                              "procesado_at) VALUES (now() - make_interval(days => $1), $2, $3, 'message.ack', "
                              "'no_individual', now())", dias, uuid.uuid4(), uuid.uuid4())
    r = await radar_ctx.fuente.mantenimiento()
    assert r["inbox_purgados"] == 1 and r["particiones"] >= 0
    async with radar_ctx.fuente.tenant_tx(t) as con:
        assert await con.fetchval("SELECT count(*) FROM webhook_inbox") == 1
        # Sin app.purga, ni el dueño borra algo que no sea de su tenant de sesión.
        assert await con.execute("DELETE FROM webhook_inbox WHERE tenant_id <> $1", t) == "DELETE 0"


async def test_rechaza_superusuario(radar_urls):
    with pytest.raises(RuntimeError, match="superusuario"):
        await FuenteStore(radar_urls["fuente_super"]).connect()
```

En `tests/radar_tests/test_fuente_y_health.py`, reemplazar `test_fuente_sin_tablas_de_conversacion` por:

```python
async def test_fuente_solo_con_las_tablas_del_tramo_3(radar_urls):
    con = await asyncpg.connect(radar_urls["fuente"])
    try:
        tablas = {r["relname"] for r in await con.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition")}
    finally:
        await con.close()
    assert tablas == {"fuente_meta", "alembic_version_fuente", "wa_message_provider_ids", "wa_message_bodies",
                      "wa_contact_identities", "webhook_inbox"}
```

En `tests/radar_tests/conftest.py`:

1. En `radar_urls`, agregar al dict `urls`:

```python
        "fuente_super": info.get_uri(database="radar_fuente_test"),
```

2. Debajo de `TABLAS` agregar:

```python
TABLAS_FUENTE = ["webhook_inbox", "wa_message_bodies", "wa_message_provider_ids", "wa_contact_identities"]


async def _limpiar_fuente(url_super: str) -> None:
    con = await asyncpg.connect(url_super)
    try:
        await con.execute("TRUNCATE " + ", ".join(TABLAS_FUENTE))
    finally:
        await con.close()
```

3. En la fixture `radar_db`, después de `await _limpiar(radar_urls["super"])`, agregar `await _limpiar_fuente(radar_urls["fuente_super"])`.

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_fuente_conversaciones.py tests/radar_tests/test_fuente_y_health.py -q`
Expected: FAIL (`relation "wa_message_provider_ids" does not exist` al truncar y `AttributeError: 'FuenteStore' object has no attribute 'tenant_tx'`).

- [ ] **Step 3: Migración de fuente**

Crear `migrations_fuente/versions/f0002_conversaciones.py`:

```python
"""Fuente f0002: tablas de conversación del almacén permanente (§6.3, §6.4, §7).

- wa_message_provider_ids: el id de proveedor (lleva el teléfono adentro, §0.4)
  con PRIMARY KEY (line_id, provider_msg_id) = la UNIQUE de idempotencia por
  línea (§6.3 punto 4). Fija el UUID del mensaje que usa la base de resultados.
- wa_message_bodies: texto original (y el redactado del tramo 5), particionado
  por mes de provider_ts.
- wa_contact_identities: JID, lid, teléfono y nombre de cada chat no excluido.
- webhook_inbox: particionado por mes de recibido_at; nunca guarda el payload
  de un descartado (CHECK) y se purga a los 7 días (§7).

RLS forzada por tenant sobre app.tenant_id (FuenteStore.tenant_tx). Las
particiones no llevan políticas propias: todo acceso va por la tabla padre.

Revision ID: f0002
Revises: f0001
Create Date: 2026-09-24
"""
from alembic import op

revision = "f0002"
down_revision = "f0001"
branch_labels = None
depends_on = None

TABLAS = ("wa_message_provider_ids", "wa_message_bodies", "wa_contact_identities", "webhook_inbox")


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION fuente_tenant_actual() RETURNS uuid LANGUAGE sql STABLE
            AS $f$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $f$;

        CREATE TABLE wa_message_provider_ids (
            tenant_id       UUID NOT NULL DEFAULT fuente_tenant_actual(),
            line_id         UUID NOT NULL,
            provider_msg_id TEXT NOT NULL CHECK (provider_msg_id ~ '^[^\\s]{1,200}$'),
            message_uuid    UUID NOT NULL UNIQUE,
            chat_uuid       UUID NOT NULL,
            reply_to_raw    TEXT NULL CHECK (reply_to_raw ~ '^[^\\s]{1,200}$'),
            revocado        BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (line_id, provider_msg_id)
        );
        CREATE INDEX wa_message_provider_ids_chat ON wa_message_provider_ids (tenant_id, line_id, chat_uuid);

        CREATE TABLE wa_message_bodies (
            tenant_id       UUID NOT NULL DEFAULT fuente_tenant_actual(),
            line_id         UUID NOT NULL,
            message_uuid    UUID NOT NULL,
            provider_ts     TIMESTAMPTZ NOT NULL,
            chat_uuid       UUID NOT NULL,
            texto           TEXT NOT NULL CHECK (length(texto) BETWEEN 1 AND 65536),
            texto_redactado TEXT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (message_uuid, provider_ts)
        ) PARTITION BY RANGE (provider_ts);
        CREATE TABLE wa_message_bodies_default PARTITION OF wa_message_bodies DEFAULT;
        CREATE INDEX wa_message_bodies_chat ON wa_message_bodies (tenant_id, line_id, chat_uuid);
        CREATE INDEX wa_message_bodies_uuid ON wa_message_bodies (message_uuid);

        CREATE TABLE wa_contact_identities (
            tenant_id  UUID NOT NULL DEFAULT fuente_tenant_actual(),
            line_id    UUID NOT NULL,
            chat_uuid  UUID NOT NULL,
            jid_pn     TEXT NULL CHECK (jid_pn ~ '^[0-9]{5,20}@c\\.us$'),
            jid_lid    TEXT NULL CHECK (jid_lid ~ '^[0-9]{5,20}@lid$'),
            telefono   TEXT NULL CHECK (telefono ~ '^\\+[0-9]{8,15}$'),
            nombre     TEXT NULL CHECK (length(nombre) BETWEEN 1 AND 200),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (line_id, chat_uuid),
            CHECK (jid_pn IS NOT NULL OR jid_lid IS NOT NULL)
        );

        CREATE TABLE webhook_inbox (
            id             BIGSERIAL,
            recibido_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            tenant_id      UUID NOT NULL DEFAULT fuente_tenant_actual(),
            line_id        UUID NOT NULL,
            link_id        UUID NOT NULL,
            evento         TEXT NOT NULL
                           CHECK (evento IN ('message.any', 'message.ack', 'message.edited', 'message.revoked')),
            evento_id      TEXT NULL CHECK (evento_id ~ '^[A-Za-z0-9_.:-]{1,80}$'),
            motivo         TEXT NULL CHECK (motivo IN ('pendiente_de_seleccion', 'excluido', 'suprimido',
                                                       'no_individual', 'pendiente_de_decision')),
            solo_metadatos BOOLEAN NOT NULL DEFAULT FALSE,
            chat_uuid      UUID NULL,
            payload        JSONB NULL CHECK (payload IS NULL OR jsonb_typeof(payload) = 'object'),
            procesado_at   TIMESTAMPTZ NULL,
            PRIMARY KEY (id, recibido_at),
            -- §6.3 punto 1: un descartado nunca guarda payload.
            CHECK (motivo IS NULL OR (payload IS NULL AND procesado_at IS NOT NULL)),
            -- Pendiente de decisión: solo {id, timestamp, fromMe}.
            CHECK (NOT solo_metadatos OR payload IS NULL
                   OR (payload - 'id' - 'timestamp' - 'fromMe') = '{}'::jsonb)
        ) PARTITION BY RANGE (recibido_at);
        CREATE TABLE webhook_inbox_default PARTITION OF webhook_inbox DEFAULT;
        CREATE INDEX webhook_inbox_pendientes ON webhook_inbox (link_id, recibido_at) WHERE procesado_at IS NULL;

        CREATE FUNCTION fuente_asegurar_particiones(p_meses integer) RETURNS integer
            LANGUAGE plpgsql AS $f$
        DECLARE
            t text;
            col text;
            mes date;
            hasta date;
            nombre text;
            ocupado boolean;
            creadas integer := 0;
            viejo record;
        BEGIN
            FOREACH t IN ARRAY ARRAY['webhook_inbox', 'wa_message_bodies'] LOOP
                col := CASE WHEN t = 'webhook_inbox' THEN 'recibido_at' ELSE 'provider_ts' END;
                mes := CASE WHEN t = 'webhook_inbox' THEN date_trunc('month', now())::date
                            ELSE DATE '2024-01-01' END;
                hasta := (date_trunc('month', now()) + make_interval(months => p_meses))::date;
                WHILE mes <= hasta LOOP
                    nombre := t || '_' || to_char(mes, 'YYYYMM');
                    IF to_regclass(nombre) IS NULL THEN
                        -- Postgres no deja crear un rango que ya tiene filas en DEFAULT.
                        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I WHERE %I >= %L AND %I < %L)',
                                       t || '_default', col, mes, col, (mes + interval '1 month')::date)
                           INTO ocupado;
                        IF NOT ocupado THEN
                            EXECUTE format('CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                                           nombre, t, mes, (mes + interval '1 month')::date);
                            creadas := creadas + 1;
                        END IF;
                    END IF;
                    mes := (mes + interval '1 month')::date;
                END LOOP;
            END LOOP;
            -- §7: webhook_inbox se purga siempre a los 7 días. Un mes entero más viejo se descarta.
            FOR viejo IN
                SELECT c.relname FROM pg_inherits i
                  JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent
                 WHERE p.relname = 'webhook_inbox' AND c.relname ~ '^webhook_inbox_[0-9]{6}$'
            LOOP
                IF to_date(right(viejo.relname, 6), 'YYYYMM') + interval '1 month' < now() - interval '7 days' THEN
                    EXECUTE format('DROP TABLE %I', viejo.relname);
                END IF;
            END LOOP;
            RETURN creadas;
        END
        $f$;
    """)
    for tabla in TABLAS:
        op.execute(f"""
            ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {tabla}_por_tenant ON {tabla} FOR ALL
                USING (tenant_id = fuente_tenant_actual()) WITH CHECK (tenant_id = fuente_tenant_actual());
        """)
    op.execute("""
        -- La purga cruza tenants: solo DELETE, solo con app.purga = 'inbox' y solo lo vencido.
        CREATE POLICY webhook_inbox_purga ON webhook_inbox FOR DELETE
            USING (current_setting('app.purga', true) = 'inbox' AND recibido_at < now() - interval '7 days');
        SELECT fuente_asegurar_particiones(2);
        UPDATE fuente_meta SET valor = '2', updated_at = now() WHERE clave = 'esquema';
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS webhook_inbox, wa_contact_identities, wa_message_bodies, wa_message_provider_ids;
        DROP FUNCTION IF EXISTS fuente_asegurar_particiones(integer);
        DROP FUNCTION IF EXISTS fuente_tenant_actual();
        UPDATE fuente_meta SET valor = '1', updated_at = now() WHERE clave = 'esquema';
    """)
```

- [ ] **Step 4: `FuenteStore` con `tenant_tx` y mantenimiento**

Reemplazar `app/radar/fuente.py` completo por:

```python
"""
Almacén de fuente (§6.4): el Postgres separado donde viven el texto, los ids de
proveedor, la identidad de los contactos y webhook_inbox (tramo 3). Solo el
almacén PERMANENTE; el purgable es una política opcional del tramo 6.

- RLS forzada por tenant (f0002): todo acceso a tablas de conversación pasa por
  tenant_tx(tenant_id), que fija app.tenant_id igual que RadarDB.tenant_tx.
- Una sola URL, dueña de las tablas: FORCE alcanza al dueño, pero no a un
  superusuario ni a un rol BYPASSRLS, así que connect() los rechaza.
- mantenimiento(): particiones mensuales y purga de webhook_inbox a 7 días (§7).
"""

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg

from app.radar.db import _normalizar_dsn

ALMACENES_DISPONIBLES = frozenset({"permanente"})
MESES_ADELANTE = 2


class FuenteStore:
    def __init__(self, dsn: str, max_size: int = 3):
        self._dsn = _normalizar_dsn(dsn)
        self._max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None
        self._almacen: str = ""

    async def connect(self) -> None:
        if not self._dsn:
            raise RuntimeError("RADAR_FUENTE_DATABASE_URL vacía")
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=self._max_size, timeout=10)
        try:
            async with pool.acquire() as con:
                rol = await con.fetchrow("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                if rol is None or rol["rolsuper"] or rol["rolbypassrls"]:
                    raise RuntimeError("RADAR_FUENTE_DATABASE_URL conecta con un rol superusuario o BYPASSRLS: "
                                       "RLS no tendría efecto")
                almacen = await con.fetchval("SELECT valor FROM fuente_meta WHERE clave = 'almacen'")
            if almacen not in ALMACENES_DISPONIBLES:
                raise RuntimeError(f"RADAR_FUENTE_DATABASE_URL no apunta a un almacén conocido: {almacen!r}")
        except BaseException:
            await pool.close()
            raise
        self._pool = pool
        self._almacen = almacen

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def almacen(self) -> str:
        return self._almacen

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("FuenteStore sin conectar")
        return self._pool

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: uuid.UUID) -> AsyncIterator[asyncpg.Connection]:
        if not isinstance(tenant_id, uuid.UUID):
            raise TypeError("tenant_tx requiere un uuid.UUID")
        async with self.pool.acquire() as con:
            async with con.transaction():
                await con.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                yield con

    async def mantenimiento(self) -> dict:
        async with self.pool.acquire() as con:
            async with con.transaction():
                creadas = await con.fetchval("SELECT fuente_asegurar_particiones($1)", MESES_ADELANTE)
            async with con.transaction():
                await con.execute("SELECT set_config('app.purga', 'inbox', true)")
                estado = await con.execute("DELETE FROM webhook_inbox WHERE recibido_at < now() - interval '7 days'")
        return {"particiones": creadas, "inbox_purgados": int(estado.split()[-1])}

    async def salud(self) -> dict:
        try:
            if self._pool is None:
                raise RuntimeError("sin pool")
            async with self._pool.acquire() as con:
                almacen = await con.fetchval("SELECT valor FROM fuente_meta WHERE clave = 'almacen'")
            return {"ok": True, "almacen": almacen}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__}
```

- [ ] **Step 5: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_fuente_conversaciones.py tests/radar_tests/test_fuente_y_health.py -q`
Expected: PASS.

- [ ] **Step 6: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add migrations_fuente/versions/f0002_conversaciones.py app/radar/fuente.py tests/radar_tests/conftest.py tests/radar_tests/test_fuente_y_health.py tests/radar_tests/test_fuente_conversaciones.py
git commit -m "Radar tramo 3: f0002 con ids de proveedor, cuerpos e inbox particionados y RLS en el almacén de fuente

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Lecturas paginadas con la clave del vínculo e identidad seudonimizada

**Files:**
- Create: `app/radar/ingesta/lectura.py`, `app/radar/ingesta/identidad.py`
- Modify: `tests/radar_tests/helpers.py`
- Test: `tests/radar_tests/test_ingesta_identidad.py`

**Interfaces:**
- Consumes: `WahaCliente.listar_chats/listar_mensajes/listar_lids`, `WahaError` (Task 3); `leer_worker(ctx, worker_id) -> Worker`, `nombre_clave_lectura(link_id) -> str` (`app/radar/workers.py`); `Seudonimizador(store).contact_hmac(tenant_id, telefono)`, `.lid_hmac(tenant_id, lid)`, `contact_hmac(k, e164)`, `obtener_k_tenant(store, tenant_id)`, `crear_k_tenant(store, tenant_id)` (`app/radar/secrets.py`); `TelefonoNoSoportado` (`app/radar/telefonos.py`); `Jid`, `ChatWaha`, `MensajeWaha` (Task 1).
- Produces (lectura): `LIMITE_CHATS = 100`, `LIMITE_MENSAJES = 100`, `LIMITE_LIDS = 500`; `ClaveLecturaAusente`, `PresupuestoAgotado`; `Presupuesto(paginas: Optional[int])` con `.gastar()` y `.restantes`; `cliente_lectura(ctx, *, link_id, worker_id)` (async context manager que da un `WahaCliente`); `paginar_chats(cli, sesion, *, corte_ts=None, presupuesto=None) -> AsyncIterator[list[ChatWaha]]`; `paginar_mensajes(cli, sesion, chat, *, gte, lte=None, presupuesto=None) -> AsyncIterator[list[MensajeWaha]]`; `cargar_lids(cli, sesion) -> dict[str, str]`; `MapaLids(cli, sesion)` con `await .pn_de(lid) -> Optional[str]`.
- Produces (identidad): `ChatRef(id: uuid.UUID, estado: str, contact_hmac: Optional[str], lid_hmac: Optional[str], suprimido: bool)`; `hmacs_de(ctx, tenant_id, jid, pn=None) -> tuple[Optional[str], Optional[str]]`; `buscar_chat(con, *, line_id, contact_hmac, lid_hmac) -> Optional[asyncpg.Record]`; `esta_suprimido(con, *, contact_hmac, lid_hmac) -> bool`; `resolver_chat(ctx, *, tenant_id, line_id, jid, lids=None, nombre=None) -> ChatRef`; `leer_chat(ctx, tenant_id, chat_id) -> Optional[ChatRef]`; `jids_de_chat(ctx, tenant_id, line_id, chat_id) -> list[str]`; `nombres_de(ctx, tenant_id, line_id, chat_ids) -> dict[uuid.UUID, Optional[str]]`.
- Produces (helpers de test): `escenario_vinculable` crea la `k_tenant`; `preparar_ingesta(ctx, waha, *, ingesta="importando", nombre="Farmacia A") -> dict`; `chat_de_prueba(ctx, esc, usuario, *, estado="incluido", dominio="c.us", nombre=None) -> uuid.UUID`; `fuente_fetch(ctx, tenant_id, sql, *args) -> list`; `worker_de(ctx, esc) -> uuid.UUID`.

- [ ] **Step 1: Helpers de test**

En `tests/radar_tests/helpers.py`:

1. Agregar a los imports `from app.radar.secrets import crear_k_tenant`.
2. En `escenario_vinculable`, después de `t = await crear_tenant_directo(ctx.db, nombre)`, agregar `crear_k_tenant(ctx.secretos, t)` (en producción la crea el alta del tenant, `admin_kis.py`).
3. Agregar al final:

```python
async def preparar_ingesta(ctx, waha, *, ingesta: str = "importando", nombre: str = "Farmacia A") -> dict:
    """Escenario vinculado (WORKING) con links.ingesta en el estado pedido. Si ya importa,
    la línea tiene la selección confirmada a 90 días."""
    esc = await escenario_vinculable(ctx, nombre)
    v = await vincular_de_prueba(ctx, waha, esc)
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET ingesta = $2 WHERE id = $1", v["link_id"], ingesta)
        if ingesta in ("importando", "al_dia"):
            await con.execute("UPDATE lines SET seleccion_confirmada_at = now(), periodo_dias = 90 WHERE id = $1",
                              esc["line_id"])
    return {**esc, **v}


async def chat_de_prueba(ctx, esc: dict, usuario: str, *, estado: str = "incluido", dominio: str = "c.us",
                         nombre: Optional[str] = None) -> uuid.UUID:
    from app.radar.ingesta.identidad import resolver_chat
    from app.radar.ingesta.reduccion import parsear_jid

    ref = await resolver_chat(ctx, tenant_id=esc["tenant_id"], line_id=esc["line_id"],
                              jid=parsear_jid(f"{usuario}@{dominio}"), nombre=nombre)
    if estado != "pendiente":
        async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
            await con.execute("UPDATE wa_chats SET estado = $2, decidido_at = now(), "
                              "motivo = CASE WHEN $2 = 'excluido' THEN 'dueno' END WHERE id = $1", ref.id, estado)
    if estado == "excluido":
        async with ctx.fuente.tenant_tx(esc["tenant_id"]) as con:
            await con.execute("DELETE FROM wa_contact_identities WHERE chat_uuid = $1", ref.id)
    return ref.id


async def fuente_fetch(ctx, tenant_id: uuid.UUID, sql: str, *args) -> list:
    async with ctx.fuente.tenant_tx(tenant_id) as con:
        return await con.fetch(sql, *args)


async def worker_de(ctx, esc: dict) -> uuid.UUID:
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchval("SELECT worker_id FROM links WHERE id = $1", esc["link_id"])
```

- [ ] **Step 2: Tests que fallan**

Crear `tests/radar_tests/test_ingesta_identidad.py`:

```python
"""
Identidad seudonimizada (§6.4): contact_hmac / lid_hmac con k_tenant, lid → teléfono
con /lids (una carga por corrida, solo en memoria), identidad en claro solo en el
almacén de fuente y nunca para excluidos o suprimidos. Lecturas con la clave de
lectura del vínculo, paginadas hasta una página vacía y con presupuesto.
"""
import pytest

from app.radar.ingesta.identidad import leer_chat, resolver_chat
from app.radar.ingesta.lectura import (ClaveLecturaAusente, MapaLids, Presupuesto, PresupuestoAgotado,
                                       cliente_lectura, paginar_mensajes)
from app.radar.ingesta.reduccion import parsear_jid
from app.radar.secrets import Seudonimizador, contact_hmac, obtener_k_tenant
from app.radar.workers import nombre_clave_lectura

from .helpers import chat_de_prueba, fuente_fetch, preparar_ingesta, worker_de
from .waha_falso import msg_crudo

PN = "5493411111111"
LID = "123456789"


def _args(esc):
    return {"tenant_id": esc["tenant_id"], "line_id": esc["line_id"]}


async def test_chat_por_telefono_se_crea_una_vez_con_identidad_en_fuente(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    a = await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid(f"{PN}@c.us"), nombre="Marta")
    b = await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid(f"{PN}@c.us"))
    assert a.id == b.id and a.estado == "pendiente" and not a.suprimido and a.lid_hmac is None
    assert a.contact_hmac == Seudonimizador(ctx_waha.secretos).contact_hmac(esc["tenant_id"], "+" + PN)
    ident = await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT jid_pn, jid_lid, telefono, nombre "
                               "FROM wa_contact_identities WHERE chat_uuid = $1", a.id)
    assert tuple(ident[0]) == (f"{PN}@c.us", None, "+" + PN, "Marta")


async def test_lid_se_resuelve_con_lids_y_se_une_al_chat_del_telefono(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    pn = await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid(f"{PN}@c.us"))
    waha.lids[esc["session_name"]] = [{"lid": f"{LID}@lid", "pn": f"{PN}@c.us"}]
    async with cliente_lectura(ctx_waha, link_id=esc["link_id"], worker_id=await worker_de(ctx_waha, esc)) as cli:
        lids = MapaLids(cli, esc["session_name"])
        ref = await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid(f"{LID}@lid"), lids=lids)
        await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid(f"{LID}@lid"), lids=lids)
    assert ref.id == pn.id and ref.lid_hmac == Seudonimizador(ctx_waha.secretos).lid_hmac(esc["tenant_id"], LID)
    assert sum(1 for x in waha.llamadas if x.endswith("/lids")) == 2            # página con datos + página vacía
    assert waha.api_keys[-1].startswith("valor-secreto-")                       # clave de lectura, no la admin
    ident = await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT jid_pn, jid_lid FROM wa_contact_identities "
                               "WHERE chat_uuid = $1", pn.id)
    assert tuple(ident[0]) == (f"{PN}@c.us", f"{LID}@lid")


async def test_lid_sin_resolver_queda_pendiente_de_telefono_y_se_completa_despues(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    sin = await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid(f"{LID}@lid"))
    assert sin.contact_hmac is None and sin.lid_hmac is not None
    waha.lids[esc["session_name"]] = [{"lid": f"{LID}@lid", "pn": f"{PN}@c.us"}]
    async with cliente_lectura(ctx_waha, link_id=esc["link_id"], worker_id=await worker_de(ctx_waha, esc)) as cli:
        con = await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid(f"{LID}@lid"),
                                  lids=MapaLids(cli, esc["session_name"]))
    assert con.id == sin.id and con.contact_hmac is not None
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as c:
        assert await c.fetchval("SELECT pn_resuelto FROM wa_chats WHERE id = $1", sin.id)


async def test_excluido_y_suprimido_no_guardan_identidad(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    excluido = await chat_de_prueba(ctx_waha, esc, PN, estado="excluido")
    ref = await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid(f"{PN}@c.us"), nombre="Personal")
    assert (ref.id, ref.estado) == (excluido, "excluido")
    otro = "5493412222222"
    h = Seudonimizador(ctx_waha.secretos).contact_hmac(esc["tenant_id"], "+" + otro)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("INSERT INTO suppressions (contact_hmac) VALUES ($1)", h)
    sup = await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid(f"{otro}@c.us"), nombre="Suprimido")
    assert sup.suprimido and (await leer_chat(ctx_waha, esc["tenant_id"], sup.id)).suprimido
    assert await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT 1 FROM wa_contact_identities") == []


async def test_numero_de_otro_pais_se_firma_con_sus_digitos(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    ref = await resolver_chat(ctx_waha, **_args(esc), jid=parsear_jid("14155550100@c.us"))
    k = obtener_k_tenant(ctx_waha.secretos, esc["tenant_id"])
    assert ref.contact_hmac == contact_hmac(k, "+14155550100")


async def test_paginacion_hasta_pagina_vacia_y_presupuesto(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    s = esc["session_name"]
    waha.mensajes[s] = [msg_crudo(PN, n, ts=1000 + n) for n in range(150)]
    async with cliente_lectura(ctx_waha, link_id=esc["link_id"], worker_id=await worker_de(ctx_waha, esc)) as cli:
        paginas = [p async for p in paginar_mensajes(cli, s, "all", gte=None)]
        with pytest.raises(PresupuestoAgotado):
            async for _ in paginar_mensajes(cli, s, "all", gte=None, presupuesto=Presupuesto(1)):
                pass
    assert [len(p) for p in paginas] == [100, 50]
    assert [p["offset"] for p in waha.params if p["ruta"].endswith("/messages")][:3] == ["0", "100", "200"]


async def test_sin_clave_de_lectura_no_lee(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    ctx_waha.secretos.delete(nombre_clave_lectura(esc["link_id"]))
    with pytest.raises(ClaveLecturaAusente):
        async with cliente_lectura(ctx_waha, link_id=esc["link_id"], worker_id=await worker_de(ctx_waha, esc)):
            pass
```

- [ ] **Step 3: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_identidad.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.radar.ingesta.identidad'`.

- [ ] **Step 4: Lecturas**

Crear `app/radar/ingesta/lectura.py`:

```python
"""
Lecturas de WAHA para la ingesta (§6.3), con las reglas de paginación del spike:
offset += limit y fin de datos = página vacía (cero ítems crudos), nunca una
página corta. Siempre con la clave de LECTURA del vínculo (§6.1), nunca con la
admin del worker. El mapeo lid → teléfono se carga una vez por corrida y vive
solo en memoria.
"""

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from app.radar.contexto import RadarContexto
from app.radar.ingesta.reduccion import ChatWaha, MensajeWaha
from app.radar.waha.cliente import WahaCliente, WahaError
from app.radar.workers import leer_worker, nombre_clave_lectura

LIMITE_CHATS = 100
LIMITE_MENSAJES = 100
LIMITE_LIDS = 500


class ClaveLecturaAusente(RuntimeError):
    pass


class PresupuestoAgotado(RuntimeError):
    pass


class Presupuesto:
    """Máximo de páginas por corrida (§6.3 punto 3). None = sin tope."""

    def __init__(self, paginas: Optional[int]) -> None:
        self.restantes = paginas

    def gastar(self) -> None:
        if self.restantes is None:
            return
        if self.restantes <= 0:
            raise PresupuestoAgotado("presupuesto de páginas agotado")
        self.restantes -= 1


@asynccontextmanager
async def cliente_lectura(ctx: RadarContexto, *, link_id: uuid.UUID,
                          worker_id: uuid.UUID) -> AsyncIterator[WahaCliente]:
    worker = await leer_worker(ctx, worker_id)
    clave = ctx.secretos.get(nombre_clave_lectura(link_id))
    if clave is None:
        raise ClaveLecturaAusente(str(link_id))
    async with WahaCliente(worker.base_url, clave.decode(), transport=ctx.waha_transport,
                           timeout=ctx.settings.waha_timeout_s) as cli:
        yield cli


async def paginar_chats(cli: WahaCliente, sesion: str, *, corte_ts: Optional[int] = None,
                        presupuesto: Optional[Presupuesto] = None) -> AsyncIterator[list[ChatWaha]]:
    """Chats por último mensaje descendente. Con `corte_ts` corta después de la primera
    página que ya tiene un chat anterior al corte (paso 1 de la reconciliación)."""
    offset = 0
    while True:
        if presupuesto is not None:
            presupuesto.gastar()
        pagina = await cli.listar_chats(sesion, limit=LIMITE_CHATS, offset=offset)
        if pagina.crudos == 0:
            return
        yield pagina.chats
        if corte_ts is not None and any(c.ultimo_ts is not None and c.ultimo_ts < corte_ts for c in pagina.chats):
            return
        offset += LIMITE_CHATS


async def paginar_mensajes(cli: WahaCliente, sesion: str, chat: str, *, gte: Optional[int],
                           lte: Optional[int] = None,
                           presupuesto: Optional[Presupuesto] = None) -> AsyncIterator[list[MensajeWaha]]:
    offset = 0
    while True:
        if presupuesto is not None:
            presupuesto.gastar()
        pagina = await cli.listar_mensajes(sesion, chat, limit=LIMITE_MENSAJES, offset=offset, gte=gte, lte=lte)
        if pagina.crudos == 0:
            return
        yield pagina.mensajes
        offset += LIMITE_MENSAJES


async def cargar_lids(cli: WahaCliente, sesion: str) -> dict[str, str]:
    mapa: dict[str, str] = {}
    offset = 0
    while True:
        pagina = await cli.listar_lids(sesion, limit=LIMITE_LIDS, offset=offset)
        if pagina.crudos == 0:
            return mapa
        mapa.update(dict(pagina.pares))
        offset += LIMITE_LIDS


class MapaLids:
    """lid → teléfono (solo dígitos) de una corrida. Si /lids falla, el contacto
    queda con pn_resuelto = false (§6.4) y se reintenta en la próxima corrida."""

    def __init__(self, cli: WahaCliente, sesion: str) -> None:
        self._cli = cli
        self._sesion = sesion
        self._mapa: Optional[dict[str, str]] = None

    async def pn_de(self, lid: str) -> Optional[str]:
        if self._mapa is None:
            try:
                self._mapa = await cargar_lids(self._cli, self._sesion)
            except WahaError:
                self._mapa = {}
        return self._mapa.get(lid)
```

- [ ] **Step 5: Identidad**

Crear `app/radar/ingesta/identidad.py`:

```python
"""
Identidad seudonimizada de los chats (§6.4).

- wa_chats (resultados) guarda solo contact_hmac y/o lid_hmac con la k_tenant.
- Un @lid se resuelve a teléfono con /lids (MapaLids); si no, pn_resuelto = false
  y se completa en una corrida posterior. Dos filas que ya existen por separado
  (lid y teléfono) no se funden acá: eso es del tramo 4.
- La identidad en claro (JID, lid, teléfono, nombre) va a wa_contact_identities
  en el almacén de fuente, nunca para un chat excluido o un contacto suprimido.
- Exclusiones y supresiones se resuelven siempre por HMAC, nunca por el id crudo.
"""

import uuid
from dataclasses import dataclass
from typing import Optional

import asyncpg

from app.radar.contexto import RadarContexto
from app.radar.ingesta.lectura import MapaLids
from app.radar.ingesta.reduccion import Jid
from app.radar.secrets import Seudonimizador, contact_hmac, obtener_k_tenant
from app.radar.telefonos import TelefonoNoSoportado

_COLS = "id, estado, contact_hmac, lid_hmac"


@dataclass(frozen=True)
class ChatRef:
    id: uuid.UUID
    estado: str                    # 'incluido' | 'excluido' | 'pendiente'
    contact_hmac: Optional[str]
    lid_hmac: Optional[str]
    suprimido: bool


def _contact(ctx: RadarContexto, tenant_id: uuid.UUID, digitos: str) -> str:
    try:
        return Seudonimizador(ctx.secretos).contact_hmac(tenant_id, "+" + digitos)
    except TelefonoNoSoportado:
        # Otro país: E.164 = "+" y los dígitos del JID, sin normalización argentina.
        return contact_hmac(obtener_k_tenant(ctx.secretos, tenant_id), "+" + digitos)


def hmacs_de(ctx: RadarContexto, tenant_id: uuid.UUID, jid: Jid,
             pn: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """(contact_hmac, lid_hmac). `pn` = teléfono (dígitos) de un lid ya resuelto."""
    if jid.es_lid:
        return (_contact(ctx, tenant_id, pn) if pn else None,
                Seudonimizador(ctx.secretos).lid_hmac(tenant_id, jid.usuario))
    return _contact(ctx, tenant_id, jid.usuario), None


async def buscar_chat(con: asyncpg.Connection, *, line_id: uuid.UUID, contact_hmac: Optional[str],
                      lid_hmac: Optional[str]) -> Optional[asyncpg.Record]:
    return await con.fetchrow(
        f"SELECT {_COLS} FROM wa_chats WHERE line_id = $1 AND (contact_hmac = $2 OR lid_hmac = $3) "
        "ORDER BY (contact_hmac IS NOT DISTINCT FROM $2) DESC LIMIT 1", line_id, contact_hmac, lid_hmac)


async def esta_suprimido(con: asyncpg.Connection, *, contact_hmac: Optional[str], lid_hmac: Optional[str]) -> bool:
    return await con.fetchval("SELECT EXISTS (SELECT 1 FROM suppressions WHERE contact_hmac = $1 OR lid_hmac = $2)",
                              contact_hmac, lid_hmac)


async def _guardar_identidad(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID, chat_id: uuid.UUID,
                             jid: Jid, pn: Optional[str], nombre: Optional[str]) -> None:
    telefono = "+" + pn if pn and 8 <= len(pn) <= 15 else None
    async with ctx.fuente.tenant_tx(tenant_id) as con:
        await con.execute(
            """
            INSERT INTO wa_contact_identities (line_id, chat_uuid, jid_pn, jid_lid, telefono, nombre)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (line_id, chat_uuid) DO UPDATE SET
                jid_pn = COALESCE(EXCLUDED.jid_pn, wa_contact_identities.jid_pn),
                jid_lid = COALESCE(EXCLUDED.jid_lid, wa_contact_identities.jid_lid),
                telefono = COALESCE(EXCLUDED.telefono, wa_contact_identities.telefono),
                nombre = COALESCE(EXCLUDED.nombre, wa_contact_identities.nombre),
                updated_at = now()
            """,
            line_id, chat_id, f"{pn}@c.us" if pn else None, jid.crudo() if jid.es_lid else None, telefono,
            nombre[:200] if nombre else None)


async def resolver_chat(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID, jid: Jid,
                        lids: Optional[MapaLids] = None, nombre: Optional[str] = None) -> ChatRef:
    """Devuelve el chat de la línea para este JID, creándolo como 'pendiente' si no existe."""
    pn = await lids.pn_de(jid.usuario) if (jid.es_lid and lids is not None) else (None if jid.es_lid else jid.usuario)
    ch, lh = hmacs_de(ctx, tenant_id, jid, pn)
    async with ctx.db.tenant_tx(tenant_id) as con:
        fila = await buscar_chat(con, line_id=line_id, contact_hmac=ch, lid_hmac=lh)
        if fila is None:
            fila = await con.fetchrow(
                f"INSERT INTO wa_chats (line_id, contact_hmac, lid_hmac, pn_resuelto) VALUES ($1, $2, $3, $4) "
                f"ON CONFLICT DO NOTHING RETURNING {_COLS}", line_id, ch, lh, ch is not None)
            if fila is None:                         # otra corrida lo creó recién
                fila = await buscar_chat(con, line_id=line_id, contact_hmac=ch, lid_hmac=lh)
        else:
            if ch and fila["contact_hmac"] is None and \
                    await buscar_chat(con, line_id=line_id, contact_hmac=ch, lid_hmac=None) is None:
                fila = await con.fetchrow(f"UPDATE wa_chats SET contact_hmac = $2, pn_resuelto = TRUE, "
                                          f"updated_at = now() WHERE id = $1 RETURNING {_COLS}", fila["id"], ch)
            if lh and fila["lid_hmac"] is None and \
                    await buscar_chat(con, line_id=line_id, contact_hmac=None, lid_hmac=lh) is None:
                fila = await con.fetchrow(f"UPDATE wa_chats SET lid_hmac = $2, updated_at = now() "
                                          f"WHERE id = $1 RETURNING {_COLS}", fila["id"], lh)
        suprimido = await esta_suprimido(con, contact_hmac=fila["contact_hmac"] or ch, lid_hmac=fila["lid_hmac"] or lh)
    ref = ChatRef(id=fila["id"], estado=fila["estado"], contact_hmac=fila["contact_hmac"], lid_hmac=fila["lid_hmac"],
                  suprimido=suprimido)
    if ref.estado != "excluido" and not suprimido:
        await _guardar_identidad(ctx, tenant_id=tenant_id, line_id=line_id, chat_id=ref.id, jid=jid, pn=pn,
                                 nombre=nombre)
    return ref


async def leer_chat(ctx: RadarContexto, tenant_id: uuid.UUID, chat_id: uuid.UUID) -> Optional[ChatRef]:
    async with ctx.db.tenant_tx(tenant_id) as con:
        fila = await con.fetchrow(f"SELECT {_COLS} FROM wa_chats WHERE id = $1", chat_id)
        if fila is None:
            return None
        suprimido = await esta_suprimido(con, contact_hmac=fila["contact_hmac"], lid_hmac=fila["lid_hmac"])
    return ChatRef(id=fila["id"], estado=fila["estado"], contact_hmac=fila["contact_hmac"],
                   lid_hmac=fila["lid_hmac"], suprimido=suprimido)


async def jids_de_chat(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID,
                       chat_id: uuid.UUID) -> list[str]:
    async with ctx.fuente.tenant_tx(tenant_id) as con:
        fila = await con.fetchrow("SELECT jid_pn, jid_lid FROM wa_contact_identities "
                                  "WHERE line_id = $1 AND chat_uuid = $2", line_id, chat_id)
    return [j for j in (fila["jid_pn"], fila["jid_lid"]) if j] if fila else []


async def nombres_de(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID,
                     chat_ids: list[uuid.UUID]) -> dict[uuid.UUID, Optional[str]]:
    async with ctx.fuente.tenant_tx(tenant_id) as con:
        filas = await con.fetch("SELECT chat_uuid, nombre FROM wa_contact_identities "
                                "WHERE line_id = $1 AND chat_uuid = ANY($2::uuid[])", line_id, chat_ids)
    return {f["chat_uuid"]: f["nombre"] for f in filas}
```

- [ ] **Step 6: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_identidad.py -q`
Expected: PASS (7 tests).

- [ ] **Step 7: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/ingesta/lectura.py app/radar/ingesta/identidad.py tests/radar_tests/helpers.py tests/radar_tests/test_ingesta_identidad.py
git commit -m "Radar tramo 3: lecturas paginadas con la clave del vínculo e identidad seudonimizada de chats

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Almacén idempotente de mensajes en las dos bases

**Files:**
- Create: `app/radar/ingesta/almacen.py`
- Test: `tests/radar_tests/test_ingesta_almacen.py`

**Interfaces:**
- Consumes: `ChatRef` (Task 6); `MensajeWaha` (Task 1); `FuenteStore.tenant_tx`, `RadarDB.tenant_tx`.
- Produces: `VIAS = ("webhook", "backfill", "reconciliacion")`; `ChatExcluido(RuntimeError)`; `guardar_lote(ctx, *, tenant_id, line_id, link_id, chat: ChatRef, mensajes: list[MensajeWaha], via: str, solo_metadatos: bool = False) -> int` (cantidad de mensajes nuevos); `uuid_de(ctx, tenant_id, line_id, provider_id) -> Optional[uuid.UUID]`; `aplicar_ack(ctx, tenant_id, line_id, provider_id, ack) -> bool`; `aplicar_edicion(ctx, tenant_id, line_id, provider_id, texto) -> bool`; `aplicar_revocado(ctx, tenant_id, line_id, provider_id) -> bool`; `borrar_contenido_de_chat(ctx, tenant_id, line_id, chat_id) -> None`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_ingesta_almacen.py`:

```python
"""
Idempotencia por línea (§6.3 punto 4): el UUID del mensaje lo fija
wa_message_provider_ids (UNIQUE(line_id, provider_msg_id)) y wa_messages se hace
upsert con ese UUID. Pendientes: solo metadatos. Excluidos: nunca.
"""
import pytest

from app.radar.ingesta.almacen import (ChatExcluido, aplicar_ack, aplicar_edicion, aplicar_revocado,
                                       borrar_contenido_de_chat, guardar_lote)
from app.radar.ingesta.identidad import leer_chat
from app.radar.ingesta.reduccion import reducir_mensaje

from .helpers import chat_de_prueba, fuente_fetch, preparar_ingesta
from .waha_falso import msg_crudo

PN = "5493411111111"


async def _guardar(ctx, esc, chat_id, crudos, via="backfill", **kw):
    ref = await leer_chat(ctx, esc["tenant_id"], chat_id)
    return await guardar_lote(ctx, tenant_id=esc["tenant_id"], line_id=esc["line_id"], link_id=esc["link_id"],
                              chat=ref, mensajes=[reducir_mensaje(c) for c in crudos], via=via, **kw)


async def _msgs(ctx, esc):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetch("SELECT * FROM wa_messages ORDER BY provider_ts")


async def test_guardar_dos_veces_no_duplica_y_el_texto_va_solo_a_fuente(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    chat = await chat_de_prueba(ctx_waha, esc, PN)
    crudos = [msg_crudo(PN, 1, ts=1_758_000_000, body="hola, precio?"),
              {**msg_crudo(PN, 2, ts=1_758_000_060, from_me=True, body="sale $100"),
               "replyTo": {"id": msg_crudo(PN, 1, ts=0)["id"]}}]
    assert await _guardar(ctx_waha, esc, chat, crudos) == 2
    assert await _guardar(ctx_waha, esc, chat, crudos, via="reconciliacion") == 0
    filas = await _msgs(ctx_waha, esc)
    assert len(filas) == 2 and [f["from_me"] for f in filas] == [False, True]
    assert filas[1]["reply_to_uuid"] == filas[0]["id"] and all(f["via"] == "backfill" for f in filas)
    assert all(f["tiene_texto"] and not f["solo_metadatos"] for f in filas)
    cuerpos = await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT message_uuid, texto FROM wa_message_bodies")
    assert {c["texto"] for c in cuerpos} == {"hola, precio?", "sale $100"}
    ids = await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT message_uuid FROM wa_message_provider_ids")
    assert {i["message_uuid"] for i in ids} == {f["id"] for f in filas}


async def test_pendiente_guarda_solo_metadatos_y_despues_se_completa(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    chat = await chat_de_prueba(ctx_waha, esc, PN, estado="pendiente")
    crudo = msg_crudo(PN, 1, ts=1_758_000_000, body="texto que no se guarda")
    await _guardar(ctx_waha, esc, chat, [crudo], via="webhook")
    [f] = await _msgs(ctx_waha, esc)
    assert (f["solo_metadatos"], f["tipo"], f["tiene_texto"]) == (True, "otro", False)
    assert await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT 1 FROM wa_message_bodies") == []
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE wa_chats SET estado = 'incluido' WHERE id = $1", chat)
    await _guardar(ctx_waha, esc, chat, [crudo])
    [f] = await _msgs(ctx_waha, esc)
    assert (f["solo_metadatos"], f["tipo"], f["tiene_texto"]) == (False, "texto", True)


async def test_excluido_o_suprimido_no_se_guarda_nunca(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    chat = await chat_de_prueba(ctx_waha, esc, PN, estado="excluido")
    with pytest.raises(ChatExcluido):
        await _guardar(ctx_waha, esc, chat, [msg_crudo(PN, 1, ts=1_758_000_000)])
    assert await _msgs(ctx_waha, esc) == []


async def test_ack_edicion_y_revocado(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    chat = await chat_de_prueba(ctx_waha, esc, PN)
    crudo = msg_crudo(PN, 1, ts=1_758_000_000, body="original", from_me=True, ack=1)
    await _guardar(ctx_waha, esc, chat, [crudo], via="webhook")
    t, li = esc["tenant_id"], esc["line_id"]
    assert await aplicar_ack(ctx_waha, t, li, crudo["id"], 3)
    assert await aplicar_edicion(ctx_waha, t, li, crudo["id"], "corregido")
    [f] = await _msgs(ctx_waha, esc)
    assert (f["ack"], f["editado"]) == (3, True)
    assert [c["texto"] for c in await fuente_fetch(ctx_waha, t, "SELECT texto FROM wa_message_bodies")] == ["corregido"]
    assert await aplicar_revocado(ctx_waha, t, li, crudo["id"])
    assert await fuente_fetch(ctx_waha, t, "SELECT 1 FROM wa_message_bodies") == []
    await _guardar(ctx_waha, esc, chat, [crudo])                     # una pasada posterior no lo revive
    assert await fuente_fetch(ctx_waha, t, "SELECT 1 FROM wa_message_bodies") == []
    [f] = await _msgs(ctx_waha, esc)
    assert (f["revocado"], f["tiene_texto"]) == (True, False)
    assert not await aplicar_ack(ctx_waha, t, li, "false_desconocido_X", 2)


async def test_ultimo_mensaje_solo_con_trafico_posterior_al_vinculo(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    chat = await chat_de_prueba(ctx_waha, esc, PN)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        conectado = await con.fetchval("SELECT extract(epoch FROM conectado_at)::bigint FROM links WHERE id = $1",
                                       esc["link_id"])
    await _guardar(ctx_waha, esc, chat, [msg_crudo(PN, 1, ts=conectado - 3600)], via="reconciliacion")
    await _guardar(ctx_waha, esc, chat, [msg_crudo(PN, 2, ts=conectado + 60)], via="backfill")
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT ultimo_msg_at FROM links WHERE id = $1", esc["link_id"]) is None
    await _guardar(ctx_waha, esc, chat, [msg_crudo(PN, 3, ts=conectado + 120)], via="webhook")
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT extract(epoch FROM ultimo_msg_at)::bigint FROM links WHERE id = $1",
                                  esc["link_id"]) == conectado + 120


async def test_borrar_contenido_de_un_chat_excluido(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    chat = await chat_de_prueba(ctx_waha, esc, PN, nombre="Personal")
    await _guardar(ctx_waha, esc, chat, [msg_crudo(PN, 1, ts=1_758_000_000, body="privado")])
    await borrar_contenido_de_chat(ctx_waha, esc["tenant_id"], esc["line_id"], chat)
    assert await _msgs(ctx_waha, esc) == []
    for tabla in ("wa_message_bodies", "wa_message_provider_ids", "wa_contact_identities"):
        assert await fuente_fetch(ctx_waha, esc["tenant_id"], f"SELECT 1 FROM {tabla}") == [], tabla
```

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_almacen.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.radar.ingesta.almacen'`.

- [ ] **Step 3: Implementación**

Crear `app/radar/ingesta/almacen.py`:

```python
"""
Escritura de mensajes en las dos bases (§6.3 punto 4, §6.4).

Orden fijo, idempotente de punta a punta:
1. Almacén de fuente: upsert en wa_message_provider_ids (PK = line_id +
   provider_msg_id). El UUID del mensaje sale de ahí: la primera vez es nuevo, las
   siguientes es el mismo. Después, el cuerpo (si el chat está incluido y el
   mensaje no fue revocado).
2. Base de resultados: upsert en wa_messages por ese UUID, sin texto ni id.
Si el proceso muere entre 1 y 2, el reintento repite ambos con el mismo UUID.

Un chat excluido o suprimido nunca llega acá (ChatExcluido, defensa en
profundidad). Un chat pendiente de decisión guarda solo metadatos.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.radar.contexto import RadarContexto
from app.radar.ingesta.identidad import ChatRef
from app.radar.ingesta.reduccion import MensajeWaha

VIAS = ("webhook", "backfill", "reconciliacion")

_UPSERT_ID = """
    INSERT INTO wa_message_provider_ids (line_id, provider_msg_id, message_uuid, chat_uuid, reply_to_raw)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (line_id, provider_msg_id) DO UPDATE
       SET reply_to_raw = COALESCE(wa_message_provider_ids.reply_to_raw, EXCLUDED.reply_to_raw)
    RETURNING message_uuid, revocado, (xmax = 0) AS nuevo
"""
_UPSERT_CUERPO = """
    INSERT INTO wa_message_bodies (line_id, message_uuid, provider_ts, chat_uuid, texto)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (message_uuid, provider_ts) DO UPDATE SET texto = EXCLUDED.texto, updated_at = now()
     WHERE wa_message_bodies.texto IS DISTINCT FROM EXCLUDED.texto
"""
_UPSERT_MENSAJE = """
    INSERT INTO wa_messages (id, line_id, link_id, chat_uuid, from_me, provider_ts, tipo, has_media, tiene_texto,
                             reply_to_uuid, via, solo_metadatos, ack)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    ON CONFLICT (id) DO UPDATE SET
        tipo = CASE WHEN EXCLUDED.solo_metadatos THEN wa_messages.tipo ELSE EXCLUDED.tipo END,
        has_media = wa_messages.has_media OR EXCLUDED.has_media,
        tiene_texto = CASE WHEN wa_messages.revocado THEN FALSE
                           ELSE wa_messages.tiene_texto OR EXCLUDED.tiene_texto END,
        reply_to_uuid = COALESCE(wa_messages.reply_to_uuid, EXCLUDED.reply_to_uuid),
        solo_metadatos = wa_messages.solo_metadatos AND EXCLUDED.solo_metadatos,
        ack = GREATEST(wa_messages.ack, EXCLUDED.ack),
        updated_at = now()
"""


class ChatExcluido(RuntimeError):
    pass


def _ts(segundos: int) -> datetime:
    return datetime.fromtimestamp(segundos, timezone.utc)


async def guardar_lote(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID, link_id: uuid.UUID,
                       chat: ChatRef, mensajes: list[MensajeWaha], via: str, solo_metadatos: bool = False) -> int:
    if via not in VIAS:
        raise ValueError(f"vía desconocida: {via}")
    if chat.estado == "excluido" or chat.suprimido:
        raise ChatExcluido("un chat excluido o suprimido no se persiste")
    if not mensajes:
        return 0
    solo = solo_metadatos or chat.estado == "pendiente"
    filas = []
    async with ctx.fuente.tenant_tx(tenant_id) as f:
        for m in mensajes:
            r = await f.fetchrow(_UPSERT_ID, line_id, m.provider_id, uuid.uuid4(), chat.id,
                                 None if solo else m.reply_to)
            reply_uuid = None
            if m.reply_to and not solo:
                reply_uuid = await f.fetchval("SELECT message_uuid FROM wa_message_provider_ids "
                                              "WHERE line_id = $1 AND provider_msg_id = $2", line_id, m.reply_to)
            if not solo and m.texto and not r["revocado"]:
                await f.execute(_UPSERT_CUERPO, line_id, r["message_uuid"], _ts(m.ts), chat.id, m.texto)
            filas.append((r["message_uuid"], r["nuevo"], r["revocado"], reply_uuid, m))
    nuevos = 0
    async with ctx.db.tenant_tx(tenant_id) as con:
        for mid, nuevo, revocado, reply_uuid, m in filas:
            await con.execute(_UPSERT_MENSAJE, mid, line_id, link_id, chat.id, m.from_me, _ts(m.ts),
                              "otro" if solo else m.tipo, False if solo else m.has_media,
                              (not solo) and m.tiene_texto and not revocado, reply_uuid, via, solo, m.ack)
            nuevos += int(nuevo)
        if via != "backfill":
            # Tráfico real posterior al vínculo (regla de silencio): webhook y reconciliación.
            await con.execute("UPDATE links SET ultimo_msg_at = GREATEST(COALESCE(ultimo_msg_at, $2), $2) "
                              "WHERE id = $1 AND conectado_at IS NOT NULL AND $2 > conectado_at",
                              link_id, _ts(max(m.ts for m in mensajes)))
    return nuevos


async def uuid_de(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID,
                  provider_id: str) -> Optional[uuid.UUID]:
    async with ctx.fuente.tenant_tx(tenant_id) as f:
        return await f.fetchval("SELECT message_uuid FROM wa_message_provider_ids "
                                "WHERE line_id = $1 AND provider_msg_id = $2", line_id, provider_id)


async def aplicar_ack(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID, provider_id: str,
                      ack: Optional[int]) -> bool:
    mid = await uuid_de(ctx, tenant_id, line_id, provider_id)
    if mid is None or ack is None:
        return False
    async with ctx.db.tenant_tx(tenant_id) as con:
        r = await con.execute("UPDATE wa_messages SET ack = GREATEST(ack, $2), updated_at = now() WHERE id = $1",
                              mid, ack)
    return r == "UPDATE 1"


async def aplicar_edicion(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID, provider_id: str,
                          texto: Optional[str]) -> bool:
    mid = await uuid_de(ctx, tenant_id, line_id, provider_id)
    if mid is None:
        return False
    async with ctx.db.tenant_tx(tenant_id) as con:
        fila = await con.fetchrow("SELECT m.provider_ts, m.chat_uuid, m.revocado, c.estado FROM wa_messages m "
                                  "JOIN wa_chats c ON c.id = m.chat_uuid WHERE m.id = $1", mid)
        if fila is None or fila["estado"] != "incluido" or fila["revocado"]:
            return False
        await con.execute("UPDATE wa_messages SET editado = TRUE, tiene_texto = tiene_texto OR $2, "
                          "updated_at = now() WHERE id = $1", mid, bool(texto))
    if texto:
        async with ctx.fuente.tenant_tx(tenant_id) as f:
            await f.execute(_UPSERT_CUERPO, line_id, mid, fila["provider_ts"], fila["chat_uuid"], texto)
    return True


async def aplicar_revocado(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID, provider_id: str) -> bool:
    async with ctx.fuente.tenant_tx(tenant_id) as f:
        mid = await f.fetchval("UPDATE wa_message_provider_ids SET revocado = TRUE "
                               "WHERE line_id = $1 AND provider_msg_id = $2 RETURNING message_uuid",
                               line_id, provider_id)
        if mid is None:
            return False
        await f.execute("DELETE FROM wa_message_bodies WHERE message_uuid = $1", mid)
    async with ctx.db.tenant_tx(tenant_id) as con:
        await con.execute("UPDATE wa_messages SET revocado = TRUE, tiene_texto = FALSE, updated_at = now() "
                          "WHERE id = $1", mid)
    return True


async def borrar_contenido_de_chat(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID,
                                   chat_id: uuid.UUID) -> None:
    """P4: un chat excluido no deja contenido en nuestra base (§3 P4, §9 guardas).
    Quedan solo su fila de wa_chats (HMAC + motivo) y los conteos numéricos."""
    async with ctx.fuente.tenant_tx(tenant_id) as f:
        await f.execute("DELETE FROM wa_message_bodies WHERE line_id = $1 AND chat_uuid = $2", line_id, chat_id)
        await f.execute("DELETE FROM wa_message_provider_ids WHERE line_id = $1 AND chat_uuid = $2", line_id, chat_id)
        await f.execute("DELETE FROM wa_contact_identities WHERE line_id = $1 AND chat_uuid = $2", line_id, chat_id)
    async with ctx.db.tenant_tx(tenant_id) as con:
        await con.execute("DELETE FROM wa_messages WHERE line_id = $1 AND chat_uuid = $2", line_id, chat_id)
```

La grant de `UPDATE (revocado)` sobre `wa_message_provider_ids` no hace falta: en el almacén de fuente el rol es dueño de las tablas y lo acota la RLS forzada.

- [ ] **Step 4: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_almacen.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/ingesta/almacen.py tests/radar_tests/test_ingesta_almacen.py
git commit -m "Radar tramo 3: guardado idempotente por línea en fuente y resultados, ack, edición y revocado

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Receptor de `message.*` con filtro de exclusión antes de persistir

**Files:**
- Create: `app/radar/ingesta/receptor.py`
- Modify: `app/radar/routers/webhook_waha.py`, `tests/radar_tests/helpers.py`
- Test: `tests/radar_tests/test_webhook_ingesta.py`

**Interfaces:**
- Consumes: `extraer_evento`, `EVENTOS_MENSAJE` (Task 1); `hmacs_de`, `buscar_chat`, `esta_suprimido` (Task 6); `KTenantAusente` (`app/radar/secrets.py`); `jobs.encolar(con, *, tipo, link_id, causa=None, en_segundos=0)`; `verificar_hmac(crudo, cabecera, clave)`, `_uuid(valor)` (router del tramo 2).
- Produces: `MOTIVOS = ("pendiente_de_seleccion", "excluido", "suprimido", "no_individual", "pendiente_de_decision")`; `recibir(ctx, sobre: dict, *, tenant_id, line_id, link_id) -> dict | JSONResponse` (`{"ok": True, "descartado": bool}`, `{"ok": True, "ignorado": True}` o 503); helpers de test `firmar(crudo, clave=HMAC_TEST) -> str`, `post_webhook(cliente, sobre) -> Response`, `sobre_mensaje(esc, evento, payload) -> dict`.

- [ ] **Step 1: Helpers de test**

Agregar al final de `tests/radar_tests/helpers.py` (y `import hashlib`, `import hmac`, `import json` arriba):

```python
def firmar(crudo: bytes, clave: str = HMAC_TEST) -> str:
    return hmac.new(clave.encode(), crudo, hashlib.sha512).hexdigest()


async def post_webhook(cliente, sobre: dict):
    crudo = json.dumps(sobre).encode()
    return await cliente.post("/webhook/waha", content=crudo,
                              headers={"content-type": "application/json", "x-webhook-hmac": firmar(crudo)})


def sobre_mensaje(esc: dict, evento: str, payload: dict) -> dict:
    return {"id": "evt_01TEST", "event": evento, "session": esc["session_name"],
            "metadata": {"tenant_id": str(esc["tenant_id"]), "line_id": str(esc["line_id"]),
                         "link_id": str(esc["link_id"])},
            "payload": payload}
```

- [ ] **Step 2: Tests que fallan**

Crear `tests/radar_tests/test_webhook_ingesta.py`:

```python
"""
Receptor de message.* (§6.3 punto 1): filtro de exclusión ANTES de persistir.
- Vínculo pendiente de selección: descarta sin payload.
- Chat excluido o contacto suprimido: descarta sin payload.
- Chat pendiente de decisión o desconocido: solo {id, timestamp, fromMe}.
- webhook_inbox nunca guarda el payload de un descartado.
"""
import json
import logging

from app.radar.secrets import Seudonimizador, destruir_k_tenant

from .helpers import chat_de_prueba, fuente_fetch, post_webhook, preparar_ingesta, sobre_mensaje
from .waha_falso import msg_crudo

PN = "5493411111111"
SECRETO = "mi DNI es 30111222"


async def _inbox(ctx, esc):
    return await fuente_fetch(ctx, esc["tenant_id"], "SELECT evento, evento_id, motivo, solo_metadatos, chat_uuid, "
                              "payload, procesado_at FROM webhook_inbox ORDER BY id")


async def _jobs_inbox(ctx, esc):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchval("SELECT count(*) FROM jobs WHERE tipo = 'ingesta_inbox'")


async def test_pendiente_de_seleccion_descarta_sin_payload(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta="pendiente_de_seleccion")
    r = await post_webhook(cliente, sobre_mensaje(esc, "message.any", msg_crudo(PN, 1, ts=1_758_000_000,
                                                                                body=SECRETO)))
    assert r.status_code == 200 and r.json() == {"ok": True, "descartado": True}
    [fila] = await _inbox(ctx_waha, esc)
    assert (fila["motivo"], fila["payload"], fila["evento_id"]) == ("pendiente_de_seleccion", None, "evt_01TEST")
    assert fila["procesado_at"] is not None and await _jobs_inbox(ctx_waha, esc) == 0


async def test_incluido_guarda_el_payload_y_encola_la_normalizacion(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    chat = await chat_de_prueba(ctx_waha, esc, PN)
    r = await post_webhook(cliente, sobre_mensaje(esc, "message.any", msg_crudo(PN, 1, ts=1_758_000_000,
                                                                                body="hola")))
    assert r.json() == {"ok": True, "descartado": False}
    [fila] = await _inbox(ctx_waha, esc)
    assert fila["motivo"] is None and not fila["solo_metadatos"] and fila["chat_uuid"] == chat
    assert json.loads(fila["payload"])["body"] == "hola" and fila["procesado_at"] is None
    assert await _jobs_inbox(ctx_waha, esc) == 1


async def test_excluido_y_suprimido_se_descartan_sin_payload(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    await chat_de_prueba(ctx_waha, esc, PN, estado="excluido")
    otro = "5493412222222"
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("INSERT INTO suppressions (contact_hmac) VALUES ($1)",
                          Seudonimizador(ctx_waha.secretos).contact_hmac(esc["tenant_id"], "+" + otro))
    for usuario in (PN, otro):
        r = await post_webhook(cliente, sobre_mensaje(esc, "message.any",
                                                      msg_crudo(usuario, 1, ts=1_758_000_000, body=SECRETO)))
        assert r.json() == {"ok": True, "descartado": True}
    filas = await _inbox(ctx_waha, esc)
    assert [(f["motivo"], f["payload"]) for f in filas] == [("excluido", None), ("suprimido", None)]
    assert await _jobs_inbox(ctx_waha, esc) == 0


async def test_pendiente_o_desconocido_guarda_solo_id_timestamp_fromme(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    pendiente = await chat_de_prueba(ctx_waha, esc, PN, estado="pendiente")
    await post_webhook(cliente, sobre_mensaje(esc, "message.any", msg_crudo(PN, 1, ts=1_758_000_000, body=SECRETO)))
    await post_webhook(cliente, sobre_mensaje(esc, "message.any",
                                              msg_crudo("5493413333333", 2, ts=1_758_000_001, body=SECRETO)))
    filas = await _inbox(ctx_waha, esc)
    assert [f["chat_uuid"] for f in filas] == [pendiente, None]
    for f in filas:
        assert f["solo_metadatos"] and set(json.loads(f["payload"])) == {"id", "timestamp", "fromMe"}
        assert SECRETO not in f["payload"]


async def test_ack_de_un_pendiente_y_grupos_se_descartan(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    await chat_de_prueba(ctx_waha, esc, PN, estado="pendiente")
    ack = {"id": msg_crudo(PN, 1, ts=0, from_me=True)["id"], "from": "5493411234567@c.us", "to": f"{PN}@c.us",
           "fromMe": True, "ack": 3, "timestamp": 1_758_000_000}
    await post_webhook(cliente, sobre_mensaje(esc, "message.ack", ack))
    grupo = {**msg_crudo("1203630", 2, ts=1_758_000_000, body=SECRETO), "chatId": "1203630@g.us",
             "from": "1203630@g.us"}
    await post_webhook(cliente, sobre_mensaje(esc, "message.any", grupo))
    assert [(f["motivo"], f["payload"]) for f in await _inbox(ctx_waha, esc)] == \
        [("pendiente_de_decision", None), ("no_individual", None)]


async def test_sin_k_tenant_responde_503_sin_persistir(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    destruir_k_tenant(ctx_waha.secretos, esc["tenant_id"])
    r = await post_webhook(cliente, sobre_mensaje(esc, "message.any", msg_crudo(PN, 1, ts=1_758_000_000)))
    assert r.status_code == 503 and await _inbox(ctx_waha, esc) == []


async def test_evento_cruzado_se_ignora_y_nada_se_loguea(cliente, ctx_waha, waha, caplog):
    caplog.set_level(logging.DEBUG)
    esc = await preparar_ingesta(ctx_waha, waha)
    await chat_de_prueba(ctx_waha, esc, PN)
    sobre = {**sobre_mensaje(esc, "message.any", msg_crudo(PN, 1, ts=1_758_000_000, body=SECRETO)),
             "session": "v_ffffffffffff"}
    assert (await post_webhook(cliente, sobre)).json() == {"ok": True, "ignorado": True}
    await post_webhook(cliente, sobre_mensaje(esc, "message.any", msg_crudo(PN, 2, ts=1_758_000_000, body=SECRETO)))
    assert len(await _inbox(ctx_waha, esc)) == 1
    assert SECRETO not in caplog.text and PN not in caplog.text
```

- [ ] **Step 3: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_webhook_ingesta.py -q`
Expected: FAIL (el receptor del tramo 2 responde `{"ok": True, "descartado": True}` a todo `message.*` sin escribir `webhook_inbox`).

- [ ] **Step 4: Receptor**

Crear `app/radar/ingesta/receptor.py`:

```python
"""
Filtro de exclusión ANTES de persistir (§6.3 punto 1) para message.any,
message.ack, message.edited y message.revoked.

Decide en una transacción corta, sin llamar a WAHA:
- vínculo sin selección (links.ingesta NULL/contando/pendiente_de_seleccion):
  descarta sin payload;
- evento que no es de un chat individual: descarta sin payload;
- contacto suprimido o chat excluido (por HMAC): descarta sin payload;
- chat pendiente de decisión o desconocido: message.any guarda solo
  {id, timestamp, fromMe}; ack/edición/revocado se descartan;
- chat incluido: guarda el payload hasta que el worker lo normalice.
Un descartado deja una fila con evento, id de evento y motivo (P4: "solo se
guarda id de evento, tipo y motivo"). Nunca loguea el cuerpo.
"""

import json
import logging
import re
import uuid
from typing import Union

from fastapi.responses import JSONResponse

from app.radar import jobs as cola
from app.radar.contexto import RadarContexto
from app.radar.ingesta.identidad import buscar_chat, esta_suprimido, hmacs_de
from app.radar.ingesta.reduccion import extraer_evento
from app.radar.secrets import KTenantAusente

logger = logging.getLogger("app.radar.ingesta.receptor")

MOTIVOS = ("pendiente_de_seleccion", "excluido", "suprimido", "no_individual", "pendiente_de_decision")
SIN_SELECCION = (None, "contando", "pendiente_de_seleccion")
_EVENTO_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
IGNORADO = {"ok": True, "ignorado": True}


async def _descartar(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID, link_id: uuid.UUID,
                     evento: str, evento_id, motivo: str) -> dict:
    async with ctx.fuente.tenant_tx(tenant_id) as f:
        await f.execute("INSERT INTO webhook_inbox (line_id, link_id, evento, evento_id, motivo, procesado_at) "
                        "VALUES ($1, $2, $3, $4, $5, now())", line_id, link_id, evento, evento_id, motivo)
    return {"ok": True, "descartado": True}


async def recibir(ctx: RadarContexto, sobre: dict, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                  link_id: uuid.UUID) -> Union[dict, JSONResponse]:
    async with ctx.db.tenant_tx(tenant_id) as con:
        link = await con.fetchrow("SELECT line_id, session_name, ingesta FROM links WHERE id = $1", link_id)
    if link is None or link["line_id"] != line_id or link["session_name"] != sobre.get("session"):
        return IGNORADO
    evento = sobre["event"]
    evento_id = sobre.get("id") if isinstance(sobre.get("id"), str) and _EVENTO_ID.fullmatch(sobre["id"]) else None
    base = {"tenant_id": tenant_id, "line_id": line_id, "link_id": link_id, "evento": evento, "evento_id": evento_id}
    ev = extraer_evento(evento, sobre.get("payload"))
    if ev is None:
        return await _descartar(ctx, **base, motivo="no_individual")
    if link["ingesta"] in SIN_SELECCION:
        return await _descartar(ctx, **base, motivo="pendiente_de_seleccion")
    try:
        ch, lh = hmacs_de(ctx, tenant_id, ev.chat)
    except KTenantAusente:
        # Sin HMAC no se puede filtrar: no se persiste nada y WAHA reintenta.
        logger.warning("ALERTA webhook del vínculo %s: el tenant no tiene k_tenant", link_id)
        return JSONResponse({"ok": False}, status_code=503)
    async with ctx.db.tenant_tx(tenant_id) as con:
        if await esta_suprimido(con, contact_hmac=ch, lid_hmac=lh):
            return await _descartar(ctx, **base, motivo="suprimido")
        chat = await buscar_chat(con, line_id=line_id, contact_hmac=ch, lid_hmac=lh)
    if chat is not None and chat["estado"] == "excluido":
        return await _descartar(ctx, **base, motivo="excluido")
    incluido = chat is not None and chat["estado"] == "incluido"
    if not incluido and ev.tipo != "mensaje":
        return await _descartar(ctx, **base, motivo="pendiente_de_decision")
    payload = sobre["payload"] if incluido else ev.metadatos()
    async with ctx.fuente.tenant_tx(tenant_id) as f:
        await f.execute("INSERT INTO webhook_inbox (line_id, link_id, evento, evento_id, solo_metadatos, chat_uuid, "
                        "payload) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)",
                        line_id, link_id, evento, evento_id, not incluido, chat["id"] if chat else None,
                        json.dumps(payload))
    async with ctx.db.tenant_tx(tenant_id) as con:
        await cola.encolar(con, tipo="ingesta_inbox", link_id=link_id)
    return {"ok": True, "descartado": False}
```

- [ ] **Step 5: Router**

En `app/radar/routers/webhook_waha.py`:

1. Reemplazar las dos líneas del docstring

```text
- En este tramo solo aplica session.status. Los message.* se aceptan con 200
  (para que WAHA no reintente) y se descartan SIN persistir nada: la ingesta,
  el filtro de exclusión y webhook_inbox son del tramo 3.
```

por

```text
- session.status aplica la máquina de estados del tramo 2. message.any,
  message.ack, message.edited y message.revoked pasan por el filtro de
  exclusión (app.radar.ingesta.receptor) antes de persistir nada; cualquier
  otro message.* se acepta con 200 y se descarta sin persistir.
```

2. Agregar el import `from app.radar.ingesta import receptor` y `from app.radar.ingesta.reduccion import EVENTOS_MENSAJE`.

3. Reemplazar el cuerpo de `recibir` desde `evento = sobre["event"]` hasta el final por:

```python
    evento = sobre["event"]
    if evento not in EVENTOS_MENSAJE and evento != "session.status":
        return {"ok": True, "descartado": True} if evento.startswith("message") else _IGNORADO
    meta = sobre.get("metadata") if isinstance(sobre.get("metadata"), dict) else {}
    tenant_id, line_id, link_id = _uuid(meta.get("tenant_id")), _uuid(meta.get("line_id")), _uuid(meta.get("link_id"))
    if not (tenant_id and line_id and link_id):
        return _IGNORADO
    if evento in EVENTOS_MENSAJE:
        return await receptor.recibir(ctx, sobre, tenant_id=tenant_id, line_id=line_id, link_id=link_id)
    payload = sobre.get("payload") if isinstance(sobre.get("payload"), dict) else {}
    status = payload.get("status")
    if not isinstance(status, str) or not PATRON_STATUS.match(status):
        return _IGNORADO
    me = sobre.get("me")
    me_id = me.get("id") if isinstance(me, dict) else None
    async with ctx.db.tenant_tx(tenant_id) as con:
        fila = await con.fetchrow("SELECT line_id, session_name FROM links WHERE id = $1", link_id)
        if fila is None or fila["line_id"] != line_id or fila["session_name"] != sobre.get("session"):
            return _IGNORADO
        r = await aplicar_status(con, tenant_id=tenant_id, link_id=link_id, waha_status=status, origen="webhook",
                                 me_id=me_id)
    return {"ok": True, "aplicado": r["aplicado"]}
```

Los tests del tramo 2 siguen valiendo: `test_mensajes_se_descartan_sin_persistir_nada` manda un `message.any` a un vínculo con `ingesta` NULL (descartado con motivo `pendiente_de_seleccion`, en el almacén de fuente, sin jobs ni filas en la base de resultados) y `test_no_loguea_el_cuerpo` manda `event: "message"`, que sigue descartándose sin persistir.

- [ ] **Step 6: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_webhook_ingesta.py tests/radar_tests/test_webhook_waha.py -q`
Expected: PASS.

- [ ] **Step 7: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/ingesta/receptor.py app/radar/routers/webhook_waha.py tests/radar_tests/helpers.py tests/radar_tests/test_webhook_ingesta.py
git commit -m "Radar tramo 3: el receptor filtra message.* por selección, exclusión y supresión antes de persistir

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Normalización del inbox, handlers con plazo y tareas periódicas del worker

**Files:**
- Create: `app/radar/ingesta/inbox.py`
- Modify: `app/radar/worker.py`
- Test: `tests/radar_tests/test_ingesta_inbox.py`

**Interfaces:**
- Consumes: `guardar_lote`, `aplicar_ack`, `aplicar_edicion`, `aplicar_revocado` (Task 7); `leer_chat`, `resolver_chat` (Task 6); `extraer_evento`, `jid_de_provider_id`, `ts_segundos`, `MensajeWaha` (Task 1); `cola.programar_salud(db)`, `cola.programar_ingesta(db)` (Task 4), `cola.reprogramar(db, job, en_segundos)`, `FuenteStore.mantenimiento()` (Task 5).
- Produces: `inbox.LOTE = 200`; `inbox.ejecutar(ctx, job) -> Literal["hecho"]`; `inbox.drenar(ctx, *, tenant_id, link_id) -> int`; en el worker: `HANDLERS["ingesta_inbox"]`, un handler puede devolver `"hecho"`, `"reprogramar"` (5 min, como antes) o un `float` de segundos; `bucle(ctx, *, parar, pausa_s=2.0, salud_cada_s=300.0, ingesta_cada_s=30.0, mantenimiento_cada_s=3600.0)`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_ingesta_inbox.py`:

```python
"""
El worker normaliza webhook_inbox (§6.3 punto 1): mensajes de chats incluidos con
texto al almacén de fuente, pendientes solo con metadatos, nada de excluidos, y el
payload se borra del inbox al procesarlo. Handlers con plazo y tareas periódicas.
"""
import asyncio
import json

from app.radar import jobs as cola
from app.radar import worker
from app.radar.ingesta import inbox

from .helpers import chat_de_prueba, fuente_fetch, post_webhook, preparar_ingesta, sobre_mensaje
from .waha_falso import msg_crudo

PN = "5493411111111"


async def _msgs(ctx, esc):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetch("SELECT m.*, c.estado FROM wa_messages m JOIN wa_chats c ON c.id = m.chat_uuid "
                               "ORDER BY m.provider_ts")


async def _drenar(ctx, esc):
    return await inbox.drenar(ctx, tenant_id=esc["tenant_id"], link_id=esc["link_id"])


async def test_incluido_se_normaliza_y_el_inbox_queda_sin_payload(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    await chat_de_prueba(ctx_waha, esc, PN)
    crudo = msg_crudo(PN, 1, ts=1_758_000_000, body="hola, tienen?")
    await post_webhook(cliente, sobre_mensaje(esc, "message.any", crudo))
    await post_webhook(cliente, sobre_mensaje(esc, "message.any", crudo))           # reintento de WAHA
    assert await _drenar(ctx_waha, esc) == 2
    [m] = await _msgs(ctx_waha, esc)
    assert (m["via"], m["solo_metadatos"], m["tiene_texto"]) == ("webhook", False, True)
    assert [c["texto"] for c in await fuente_fetch(ctx_waha, esc["tenant_id"],
                                                   "SELECT texto FROM wa_message_bodies")] == ["hola, tienen?"]
    filas = await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT payload, procesado_at FROM webhook_inbox")
    assert all(f["payload"] is None and f["procesado_at"] is not None for f in filas)
    assert await _drenar(ctx_waha, esc) == 0


async def test_pendiente_y_desconocido_quedan_solo_con_metadatos(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    await chat_de_prueba(ctx_waha, esc, PN, estado="pendiente")
    await post_webhook(cliente, sobre_mensaje(esc, "message.any", msg_crudo(PN, 1, ts=1_758_000_000, body="x")))
    await post_webhook(cliente, sobre_mensaje(esc, "message.any",
                                              msg_crudo("5493413333333", 2, ts=1_758_000_001, body="y")))
    await _drenar(ctx_waha, esc)
    filas = await _msgs(ctx_waha, esc)
    assert [(f["estado"], f["solo_metadatos"], f["tiene_texto"]) for f in filas] == \
        [("pendiente", True, False), ("pendiente", True, False)]
    assert await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT 1 FROM wa_message_bodies") == []


async def test_chat_excluido_despues_de_recibir_no_se_guarda(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    chat = await chat_de_prueba(ctx_waha, esc, PN)
    await post_webhook(cliente, sobre_mensaje(esc, "message.any", msg_crudo(PN, 1, ts=1_758_000_000, body="z")))
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE wa_chats SET estado = 'excluido', motivo = 'dueno' WHERE id = $1", chat)
    await _drenar(ctx_waha, esc)
    assert await _msgs(ctx_waha, esc) == []
    assert await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT 1 FROM wa_message_bodies") == []


async def test_ack_edicion_y_revocado_por_webhook(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    await chat_de_prueba(ctx_waha, esc, PN)
    crudo = msg_crudo(PN, 1, ts=1_758_000_000, from_me=True, body="precio $100", ack=1)
    await post_webhook(cliente, sobre_mensaje(esc, "message.any", crudo))
    await post_webhook(cliente, sobre_mensaje(esc, "message.ack", {**crudo, "ack": 3}))
    await post_webhook(cliente, sobre_mensaje(esc, "message.edited", {
        "id": "nuevo", "editedMessageId": crudo["id"], "from": crudo["from"], "to": crudo["to"], "fromMe": True,
        "body": "precio $120", "timestamp": 1_758_000_010}))
    await _drenar(ctx_waha, esc)
    [m] = await _msgs(ctx_waha, esc)
    assert (m["ack"], m["editado"]) == (3, True)
    assert [c["texto"] for c in await fuente_fetch(ctx_waha, esc["tenant_id"],
                                                   "SELECT texto FROM wa_message_bodies")] == ["precio $120"]
    await post_webhook(cliente, sobre_mensaje(esc, "message.revoked", {
        "revokedMessageId": crudo["id"], "after": {"from": crudo["from"], "to": crudo["to"], "fromMe": True,
                                                   "timestamp": 1_758_000_020}}))
    await _drenar(ctx_waha, esc)
    assert await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT 1 FROM wa_message_bodies") == []


async def test_el_worker_corre_el_job_de_inbox(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha)
    await chat_de_prueba(ctx_waha, esc, PN)
    await post_webhook(cliente, sobre_mensaje(esc, "message.any", msg_crudo(PN, 1, ts=1_758_000_000)))
    assert await worker.correr_una_vez(ctx_waha) == 1
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT estado FROM jobs WHERE tipo = 'ingesta_inbox'") == "hecho"
    assert len(await _msgs(ctx_waha, esc)) == 1


async def test_un_handler_puede_pedir_su_proximo_plazo(ctx_waha, waha, monkeypatch):
    esc = await preparar_ingesta(ctx_waha, waha)

    async def en_42(ctx, job):
        return 42.0

    monkeypatch.setitem(worker.HANDLERS, "ingesta_inbox", en_42)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await cola.encolar(con, tipo="ingesta_inbox", link_id=esc["link_id"])
    await worker.correr_una_vez(ctx_waha)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        j = await con.fetchrow("SELECT estado, extract(epoch FROM ejecutar_desde - now()) AS falta FROM jobs "
                               "WHERE tipo = 'ingesta_inbox'")
    assert j["estado"] == "pendiente" and 35 < j["falta"] <= 42


async def test_bucle_programa_la_ingesta_y_el_mantenimiento(ctx_waha, waha, monkeypatch):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta="contando")
    llamadas = []

    async def mantenimiento():
        llamadas.append(1)
        return {"particiones": 0, "inbox_purgados": 0}

    monkeypatch.setattr(ctx_waha.fuente, "mantenimiento", mantenimiento)
    parar = asyncio.Event()

    async def cortar():
        await asyncio.sleep(0.3)
        parar.set()

    await asyncio.gather(worker.bucle(ctx_waha, parar=parar, pausa_s=0.05, salud_cada_s=3600), cortar())
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT count(*) FROM jobs WHERE tipo = 'conteo'") == 1
    assert llamadas == [1]
```

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_inbox.py -q`
Expected: FAIL con `ImportError: cannot import name 'inbox' from 'app.radar.ingesta'`.

- [ ] **Step 3: Normalización**

Crear `app/radar/ingesta/inbox.py`:

```python
"""
Normalización de webhook_inbox (§6.3 punto 1: "el worker normaliza después").

Procesa las filas pendientes de un vínculo en orden de llegada, vuelve a mirar el
estado del chat (pudo excluirse entre la recepción y ahora) y borra el payload
del inbox al terminar cada fila. Lo usan el job ingesta_inbox y, para no dejar
filas huérfanas, la reconciliación.
"""

import json
import logging
import uuid
from typing import Literal

from app.radar.contexto import RadarContexto
from app.radar.ingesta.almacen import aplicar_ack, aplicar_edicion, aplicar_revocado, guardar_lote
from app.radar.ingesta.identidad import leer_chat, resolver_chat
from app.radar.ingesta.reduccion import MensajeWaha, extraer_evento, jid_de_provider_id, ts_segundos
from app.radar.jobs import Job

logger = logging.getLogger("app.radar.ingesta.inbox")

LOTE = 200


async def ejecutar(ctx: RadarContexto, job: Job) -> Literal["hecho"]:
    await drenar(ctx, tenant_id=job.tenant_id, link_id=job.link_id)
    return "hecho"


async def drenar(ctx: RadarContexto, *, tenant_id: uuid.UUID, link_id: uuid.UUID) -> int:
    async with ctx.db.tenant_tx(tenant_id) as con:
        line_id = await con.fetchval("SELECT line_id FROM links WHERE id = $1", link_id)
    if line_id is None:
        return 0
    total = 0
    while True:
        async with ctx.fuente.tenant_tx(tenant_id) as f:
            filas = await f.fetch("SELECT id, recibido_at, evento, solo_metadatos, chat_uuid, payload "
                                  "FROM webhook_inbox WHERE link_id = $1 AND procesado_at IS NULL "
                                  "ORDER BY recibido_at, id LIMIT $2", link_id, LOTE)
        if not filas:
            return total
        for fila in filas:
            await _normalizar(ctx, tenant_id, line_id, link_id, fila)
            async with ctx.fuente.tenant_tx(tenant_id) as f:
                await f.execute("UPDATE webhook_inbox SET procesado_at = now(), payload = NULL "
                                "WHERE id = $1 AND recibido_at = $2", fila["id"], fila["recibido_at"])
            total += 1


async def _normalizar(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID, link_id: uuid.UUID,
                      fila) -> None:
    payload = json.loads(fila["payload"]) if fila["payload"] else {}
    comun = {"tenant_id": tenant_id, "line_id": line_id, "link_id": link_id}
    if fila["solo_metadatos"]:
        pid = payload.get("id")
        jid = jid_de_provider_id(pid) if isinstance(pid, str) else None
        ts = ts_segundos(payload.get("timestamp"))
        if jid is None or ts is None:
            return
        if fila["chat_uuid"] is not None:
            chat = await leer_chat(ctx, tenant_id, fila["chat_uuid"])
        else:
            chat = await resolver_chat(ctx, tenant_id=tenant_id, line_id=line_id, jid=jid)
        if chat is None or chat.estado == "excluido" or chat.suprimido:
            return
        m = MensajeWaha(provider_id=pid, chat=jid, ts=ts, from_me=bool(payload.get("fromMe")), tipo="otro",
                        has_media=False)
        await guardar_lote(ctx, **comun, chat=chat, mensajes=[m], via="webhook", solo_metadatos=True)
        return
    ev = extraer_evento(fila["evento"], payload)
    chat = await leer_chat(ctx, tenant_id, fila["chat_uuid"]) if fila["chat_uuid"] is not None else None
    if ev is None or chat is None or chat.estado == "excluido" or chat.suprimido:
        return
    if ev.tipo == "mensaje":
        await guardar_lote(ctx, **comun, chat=chat, mensajes=[ev.mensaje], via="webhook")
    elif ev.tipo == "ack":
        await aplicar_ack(ctx, tenant_id, line_id, ev.provider_id, ev.ack)
    elif ev.tipo == "edicion":
        await aplicar_edicion(ctx, tenant_id, line_id, ev.provider_id, ev.texto)
    else:
        await aplicar_revocado(ctx, tenant_id, line_id, ev.provider_id)
```

- [ ] **Step 4: Worker**

En `app/radar/worker.py`:

1. Reemplazar el import `from typing import Awaitable, Callable, Optional` por `from typing import Awaitable, Callable, Union` y agregar `from app.radar.ingesta import inbox`.

2. Reemplazar `HANDLERS` por:

```python
# Un handler devuelve "hecho", "reprogramar" (chequeo de salud: 5 min) o los
# segundos hasta su próxima corrida (jobs de ingesta, tramo 3).
Resultado = Union[str, float]
HANDLERS: dict[str, Callable[[RadarContexto, Job], Awaitable[Resultado]]] = {
    "fin_vinculo": fin_vinculo.ejecutar,
    "chequeo_salud": salud.ejecutar,
    "aviso_caida": fin_vinculo.avisar_caida,
    "ingesta_inbox": inbox.ejecutar,
}
```

3. En `correr_una_vez`, reemplazar

```python
            if resultado == "reprogramar":
                await cola.reprogramar(ctx.db, job, salud.INTERVALO_S)
            else:
                await cola.completar(ctx.db, job)
```

por

```python
            if resultado == "reprogramar":
                await cola.reprogramar(ctx.db, job, salud.INTERVALO_S)
            elif isinstance(resultado, (int, float)) and not isinstance(resultado, bool):
                await cola.reprogramar(ctx.db, job, max(float(resultado), 1.0))
            else:
                await cola.completar(ctx.db, job)
```

4. Reemplazar `bucle` completo por:

```python
async def bucle(ctx: RadarContexto, *, parar: asyncio.Event, pausa_s: float = 2.0,
                salud_cada_s: float = float(salud.INTERVALO_S), ingesta_cada_s: float = 30.0,
                mantenimiento_cada_s: float = 3600.0) -> None:
    """Tareas periódicas sin job propio: programar salud, programar la ingesta (conteo,
    reconciliación y su adelanto al volver a WORKING) y el mantenimiento del almacén
    de fuente (particiones y purga de webhook_inbox a 7 días)."""
    periodicas = (
        ("programar_salud", salud_cada_s, lambda: cola.programar_salud(ctx.db)),
        ("programar_ingesta", ingesta_cada_s, lambda: cola.programar_ingesta(ctx.db)),
        ("mantenimiento_fuente", mantenimiento_cada_s, lambda: ctx.fuente.mantenimiento()),
    )
    ultimas: dict[str, float] = {}
    while not parar.is_set():
        for nombre, cada, tarea in periodicas:
            ahora = time.monotonic()
            if nombre not in ultimas or ahora - ultimas[nombre] >= cada:
                try:
                    await tarea()
                except Exception as e:
                    logger.warning("tarea periódica %s falló: %s", nombre, type(e).__name__)
                ultimas[nombre] = ahora
        try:
            hechos = await correr_una_vez(ctx)
        except Exception as e:
            logger.warning("ciclo del worker falló: %s", type(e).__name__)
            hechos = 0
        if hechos == 0:
            try:
                await asyncio.wait_for(parar.wait(), timeout=pausa_s)
            except asyncio.TimeoutError:
                pass
```

(`mantenimiento` va dentro de una lambda para que un `monkeypatch` sobre la instancia se vea en los tests.)

- [ ] **Step 5: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_inbox.py tests/radar_tests/test_worker.py -q`
Expected: PASS (`test_bucle_programa_salud_y_para` del tramo 2 sigue valiendo: el `conteo` que ahora se programa no toca el chequeo de salud).

- [ ] **Step 6: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/ingesta/inbox.py app/radar/worker.py tests/radar_tests/test_ingesta_inbox.py
git commit -m "Radar tramo 3: el worker normaliza el inbox, acepta plazos de los handlers y programa la ingesta

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Servicio de P4 (selección) y preparación de la importación

**Files:**
- Create: `app/radar/ingesta/seleccion.py`
- Modify: `app/radar/auditoria.py`, `app/radar/eventos_producto.py`
- Test: `tests/radar_tests/test_ingesta_seleccion.py`

**Interfaces:**
- Consumes: `reglas.profundidad_dias`, `reglas.periodos_permitidos`, `reglas.ventanas_a_importar` (Task 2); `nombres_de` (Task 6); `borrar_contenido_de_chat` (Task 7); `auditoria.registrar(con, *, tenant_id, actor_user_id, actor_rol, accion, tipo_objeto, objeto_id=None, ip=None, detalle=None)`; `eventos_producto.registrar_evento(con, *, tenant_id, evento, line_id=None, user_id=None, objeto_id=None, valores=None)`; `jobs.encolar`.
- Produces: `SeleccionRechazada(status: int, codigo: str)`; `listar_seleccion(ctx, *, tenant_id, line_id, con_nombres: bool = True) -> dict` (con `con_nombres=False`, `nombre` va en null y no se lee el almacén de fuente); `confirmar_seleccion(ctx, *, tenant_id, line_id, periodo_dias: int, excluir: list[uuid.UUID], actor_user_id, actor_rol, ip) -> dict`; `decidir_chat(ctx, *, tenant_id, line_id, chat_id, excluir: bool, actor_user_id, actor_rol, ip) -> dict`; `preparar_importacion(ctx, tenant_id, link_id) -> list[tuple[datetime, datetime]]`; acciones de auditoría `seleccion_confirmada`, `chat_decidido`, `seleccion_vista`; eventos de producto `seleccion_confirmada`, `importacion_estable`.

La respuesta de `listar_seleccion` es:

```json
{"estado": "sin_vinculo | contando | lista | confirmada", "chats_encontrados": 0, "mensajes": 0,
 "tope_alcanzado": false, "periodos": [30, 90], "periodo_dias": null, "a_analizar": 0, "excluidos": 0,
 "pendientes_de_decision": 0,
 "chats": [{"chat": "<uuid>", "nombre": "…", "estado": "pendiente", "entrantes": 0, "propios": 0,
            "ultimo_at": "<iso> | null", "sugerencia": "sin_entrantes | notificacion | sin_consulta_comercial | null",
            "excluir": true}]}
```

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_ingesta_seleccion.py`:

```python
"""
P4 (§3 P4): lista con sugerencias preseleccionadas, confirmación que excluye sin
dejar contenido, decisión de chats nuevos y ventanas de importación (§6.3 punto 4):
primer vínculo, re-vinculación y ampliación de profundidad.
"""
import json
from datetime import timedelta

import pytest

from app.radar.ingesta import reglas
from app.radar.ingesta.seleccion import (SeleccionRechazada, confirmar_seleccion, decidir_chat, listar_seleccion,
                                         preparar_importacion)

from .helpers import (chat_de_prueba, como_superusuario, crear_link_directo, fuente_fetch, preparar_ingesta,
                      worker_de)

A, B, C = "5493411111111", "5493412222222", "5493413333333"


async def _lista_con_conteo(ctx, waha):
    esc = await preparar_ingesta(ctx, waha, ingesta="pendiente_de_seleccion")
    chats = {u: await chat_de_prueba(ctx, esc, u, estado="pendiente", nombre=n)
             for u, n in ((A, "Marta"), (B, "Banco"), (C, None))}
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        for u, (ent, pro, sug) in {A: (3, 2, None), B: (5, 0, "notificacion"), C: (0, 1, "sin_entrantes")}.items():
            await con.execute("INSERT INTO conteo_chats (link_id, chat_uuid, entrantes, propios, con_texto, "
                              "ultimo_ts, sugerencia) VALUES ($1, $2, $3, $4, 0, now(), $5)",
                              esc["link_id"], chats[u], ent, pro, sug)
            await con.execute("UPDATE wa_chats SET sugerencia = $2 WHERE id = $1", chats[u], sug)
        await con.execute("UPDATE links SET conteo_n_chats = 3, conteo_n_msgs = 11 WHERE id = $1", esc["link_id"])
    return esc, chats


def _actor(esc):
    return {"actor_user_id": esc["dueno_id"], "actor_rol": "dueno", "ip": None}


async def test_lista_con_sugerencias_preseleccionadas(ctx_waha, waha):
    esc, chats = await _lista_con_conteo(ctx_waha, waha)
    r = await listar_seleccion(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"])
    assert (r["estado"], r["chats_encontrados"], r["mensajes"], r["periodos"]) == ("lista", 3, 11, [30, 90])
    por = {c["chat"]: c for c in r["chats"]}
    assert por[str(chats[A])]["nombre"] == "Marta" and not por[str(chats[A])]["excluir"]
    assert por[str(chats[B])]["excluir"] and por[str(chats[C])]["nombre"] == "Sin nombre"
    assert r["a_analizar"] == 1


async def test_confirmar_excluye_sin_contenido_e_importa(ctx_waha, waha):
    esc, chats = await _lista_con_conteo(ctx_waha, waha)
    t = esc["tenant_id"]
    r = await confirmar_seleccion(ctx_waha, tenant_id=t, line_id=esc["line_id"], periodo_dias=90,
                                  excluir=[chats[B], chats[C]], **_actor(esc))
    assert r["estado"] == "confirmada"
    async with ctx_waha.db.tenant_tx(t) as con:
        estados = {f["id"]: (f["estado"], f["motivo"]) for f in await con.fetch("SELECT id, estado, motivo FROM wa_chats")}
        link = await con.fetchrow("SELECT ingesta, cobertura, ventanas_backfill, conectado_at, importar_desde "
                                  "FROM links WHERE id = $1", esc["link_id"])
        linea = await con.fetchrow("SELECT seleccion_confirmada_at, periodo_dias FROM lines WHERE id = $1",
                                   esc["line_id"])
        backfill = await con.fetchval("SELECT count(*) FROM jobs WHERE tipo = 'backfill'")
        auditado = await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'seleccion_confirmada'")
    assert estados == {chats[A]: ("incluido", None), chats[B]: ("excluido", "sugerencia"),
                       chats[C]: ("excluido", "sugerencia")}
    assert (link["ingesta"], link["cobertura"], linea["periodo_dias"], backfill, auditado) == \
        ("importando", "provisoria", 90, 1, 1)
    inicio = link["conectado_at"] - timedelta(days=90)
    assert json.loads(link["ventanas_backfill"]) == [[int(inicio.timestamp()), int(link["conectado_at"].timestamp())]]
    idents = await fuente_fetch(ctx_waha, t, "SELECT chat_uuid FROM wa_contact_identities")
    assert [i["chat_uuid"] for i in idents] == [chats[A]]


async def test_confirmar_valida_estado_periodo_y_chats(ctx_waha, waha):
    esc, chats = await _lista_con_conteo(ctx_waha, waha)
    args = {"tenant_id": esc["tenant_id"], "line_id": esc["line_id"], **_actor(esc)}
    with pytest.raises(SeleccionRechazada) as e:
        await confirmar_seleccion(ctx_waha, periodo_dias=365, excluir=[], **args)
    assert (e.value.status, e.value.codigo) == (422, "periodo_invalido")
    with pytest.raises(SeleccionRechazada) as e:
        await confirmar_seleccion(ctx_waha, periodo_dias=90, excluir=[esc["link_id"]], **args)
    assert e.value.codigo == "chat_inexistente"
    await confirmar_seleccion(ctx_waha, periodo_dias=30, excluir=[], **args)
    with pytest.raises(SeleccionRechazada) as e:
        await confirmar_seleccion(ctx_waha, periodo_dias=30, excluir=[], **args)
    assert (e.value.status, e.value.codigo) == (409, "seleccion_no_disponible")


async def test_decidir_chats_nuevos(ctx_waha, waha):
    esc, chats = await _lista_con_conteo(ctx_waha, waha)
    t = esc["tenant_id"]
    await confirmar_seleccion(ctx_waha, tenant_id=t, line_id=esc["line_id"], periodo_dias=90, excluir=[],
                              **_actor(esc))
    nuevo = await chat_de_prueba(ctx_waha, esc, "5493414444444", estado="pendiente", nombre="Nuevo")
    otro = await chat_de_prueba(ctx_waha, esc, "5493415555555", estado="pendiente", nombre="Otro")
    r = await listar_seleccion(ctx_waha, tenant_id=t, line_id=esc["line_id"])
    assert r["pendientes_de_decision"] == 2
    await decidir_chat(ctx_waha, tenant_id=t, line_id=esc["line_id"], chat_id=nuevo, excluir=False, **_actor(esc))
    await decidir_chat(ctx_waha, tenant_id=t, line_id=esc["line_id"], chat_id=otro, excluir=True, **_actor(esc))
    async with ctx_waha.db.tenant_tx(t) as con:
        link = await con.fetchrow("SELECT reimportar_chats FROM links WHERE id = $1", esc["link_id"])
        estados = dict(await con.fetch("SELECT id, estado FROM wa_chats WHERE id = ANY($1::uuid[])", [nuevo, otro]))
    assert link["reimportar_chats"] == [nuevo] and estados == {nuevo: "incluido", otro: "excluido"}
    with pytest.raises(SeleccionRechazada) as e:
        await decidir_chat(ctx_waha, tenant_id=t, line_id=esc["line_id"], chat_id=otro, excluir=False, **_actor(esc))
    assert e.value.codigo == "chat_excluido"


async def test_revinculacion_importa_solo_lo_que_falta_y_la_ampliacion_el_tramo_viejo(ctx_waha, waha, radar_urls):
    esc = await preparar_ingesta(ctx_waha, waha)
    t = esc["tenant_id"]
    viejo = await crear_link_directo(ctx_waha.db, t, esc["line_id"], await worker_de(ctx_waha, esc),
                                     esc["consent_id"], estado="cerrado")
    async with ctx_waha.db.tenant_tx(t) as con:
        con_at = await con.fetchval("SELECT conectado_at FROM links WHERE id = $1", esc["link_id"])
        await con.execute("UPDATE links SET importar_desde = $2, observado_hasta = $3 WHERE id = $1", viejo,
                          con_at - timedelta(days=90), con_at - timedelta(days=5))
    ventanas = await preparar_importacion(ctx_waha, t, esc["link_id"])
    assert ventanas == [(con_at - timedelta(days=5) - reglas.MARGEN, con_at)]
    # Ampliación: el vínculo nuevo tiene 12 meses (full_sync no es actualizable por radar_app).
    async with ctx_waha.db.tenant_tx(t) as con:
        await con.execute("UPDATE lines SET periodo_dias = 365 WHERE id = $1", esc["line_id"])
    await como_superusuario(radar_urls, "UPDATE links SET full_sync = TRUE WHERE id = $1", esc["link_id"])
    ventanas = await preparar_importacion(ctx_waha, t, esc["link_id"])
    assert ventanas == [(con_at - timedelta(days=365), con_at - timedelta(days=90)),
                        (con_at - timedelta(days=5) - reglas.MARGEN, con_at)]
```

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_seleccion.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.radar.ingesta.seleccion'`.

- [ ] **Step 3: Auditoría y eventos**

En `app/radar/auditoria.py`, dentro de `ACCIONES`, después de `"worker_registrado",` agregar:

```python
    # tramo 3: P4
    "seleccion_confirmada", "chat_decidido", "seleccion_vista",
```

En `app/radar/eventos_producto.py`, dentro de `EVENTOS`, después de `"vinculo_iniciado", "vinculo_working", "vinculo_cerrado",` agregar:

```python
    # tramo 3 (§9: WORKING → lista para elegir; vínculos que completan las 3 pasadas)
    "seleccion_confirmada", "importacion_estable",
```

- [ ] **Step 4: Servicio**

Crear `app/radar/ingesta/seleccion.py`:

```python
"""
P4 — Elegí qué analizar (§3 P4), la decisión de chats nuevos y la preparación
de la importación (§6.3 puntos 2 y 4). Lo usan igual la Consola KIS y el dueño.

- La selección es de la LÍNEA (lines.seleccion_confirmada_at): una
  re-vinculación no vuelve a pedirla.
- Confirmar excluye (motivo 'sugerencia' si venía sugerido, 'dueno' si no),
  incluye el resto y borra todo contenido de los excluidos: queda solo su HMAC
  y el motivo en wa_chats.
- Un chat nuevo después de P4 queda 'pendiente' hasta que el dueño decida;
  incluirlo lo pone en links.reimportar_chats para que el backfill lo relea.
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Optional

from app.radar import auditoria, eventos_producto
from app.radar import jobs as cola
from app.radar.contexto import RadarContexto
from app.radar.ingesta import reglas
from app.radar.ingesta.almacen import borrar_contenido_de_chat
from app.radar.ingesta.identidad import nombres_de

_LINK = ("SELECT l.id, l.line_id, l.estado, l.ingesta, l.engine, l.full_sync, l.conectado_at, l.conteo_n_chats, "
         "l.conteo_n_msgs, l.conteo_tope, li.seleccion_confirmada_at, li.periodo_dias, li.fuente_purgada_hasta "
         "FROM links l JOIN lines li ON li.id = l.line_id")


class SeleccionRechazada(Exception):
    def __init__(self, status: int, codigo: str) -> None:
        super().__init__(codigo)
        self.status = status
        self.codigo = codigo


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def _estado(confirmada: bool, link) -> str:
    if confirmada:
        return "confirmada"
    if link is None or link["estado"] != "vinculado":
        return "sin_vinculo"
    return "lista" if link["ingesta"] == "pendiente_de_seleccion" else "contando"


async def listar_seleccion(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                           con_nombres: bool = True) -> dict:
    async with ctx.db.tenant_tx(tenant_id) as con:
        linea = await con.fetchrow("SELECT seleccion_confirmada_at, periodo_dias FROM lines WHERE id = $1", line_id)
        if linea is None:
            raise SeleccionRechazada(404, "linea_inexistente")
        link = await con.fetchrow(_LINK + " WHERE l.line_id = $1 ORDER BY l.created_at DESC LIMIT 1", line_id)
        filas = await con.fetch(
            "SELECT c.id, c.estado, c.sugerencia, k.entrantes, k.propios, k.ultimo_ts FROM wa_chats c "
            "LEFT JOIN conteo_chats k ON k.chat_uuid = c.id AND k.link_id = $2 "
            "WHERE c.line_id = $1 AND c.estado <> 'excluido' ORDER BY k.ultimo_ts DESC NULLS LAST, c.created_at",
            line_id, link["id"] if link else None)
        excluidos = await con.fetchval("SELECT count(*) FROM wa_chats WHERE line_id = $1 AND estado = 'excluido'",
                                       line_id)
    confirmada = linea["seleccion_confirmada_at"] is not None
    nombres = await nombres_de(ctx, tenant_id, line_id, [f["id"] for f in filas]) if (filas and con_nombres) else {}
    chats = [{"chat": str(f["id"]), "nombre": (nombres.get(f["id"]) or "Sin nombre") if con_nombres else None,
              "estado": f["estado"],
              "entrantes": f["entrantes"] or 0, "propios": f["propios"] or 0, "ultimo_at": _iso(f["ultimo_ts"]),
              "sugerencia": f["sugerencia"],
              "excluir": (not confirmada) and f["sugerencia"] is not None} for f in filas]
    profundidad = reglas.profundidad_dias(link["engine"], link["full_sync"]) if link else 90
    return {
        "estado": _estado(confirmada, link),
        "chats_encontrados": (link["conteo_n_chats"] or 0) if link else 0,
        "mensajes": (link["conteo_n_msgs"] or 0) if link else 0,
        "tope_alcanzado": bool(link and link["conteo_tope"]),
        "periodos": reglas.periodos_permitidos(profundidad),
        "periodo_dias": linea["periodo_dias"],
        "a_analizar": sum(1 for c in chats if not c["excluir"]),
        "excluidos": excluidos,
        "pendientes_de_decision": sum(1 for c in chats if confirmada and c["estado"] == "pendiente"),
        "chats": chats,
    }


async def preparar_importacion(ctx: RadarContexto, tenant_id: uuid.UUID,
                               link_id: uuid.UUID) -> list[tuple[datetime, datetime]]:
    """Fija las ventanas del backfill (§6.3 punto 4) y encola la primera pasada."""
    async with ctx.db.tenant_tx(tenant_id) as con:
        link = await con.fetchrow(_LINK + " WHERE l.id = $1", link_id)
        previo = await con.fetchrow(
            "SELECT min(importar_desde) AS desde, max(COALESCE(observado_hasta, conectado_at)) AS hasta "
            "FROM links WHERE line_id = $1 AND id <> $2 AND importar_desde IS NOT NULL", link["line_id"], link_id)
        profundidad = reglas.profundidad_dias(link["engine"], link["full_sync"])
        periodo = min(link["periodo_dias"] or profundidad, profundidad)
        conectado = link["conectado_at"]
        cubierto = (previo["desde"], previo["hasta"]) if previo and previo["desde"] and previo["hasta"] else None
        ventanas = reglas.ventanas_a_importar(inicio=conectado - timedelta(days=periodo), conectado_at=conectado,
                                              cubierto=cubierto, purgada_hasta=link["fuente_purgada_hasta"])
        desde = min([v[0] for v in ventanas] + ([cubierto[0]] if cubierto else []), default=conectado)
        await con.execute(
            "UPDATE links SET ingesta = 'importando', importar_desde = $2, ventanas_backfill = $3::jsonb, "
            "cobertura = 'provisoria', backfill_pasada = 0, updated_at = now() WHERE id = $1",
            link_id, desde, json.dumps([[int(a.timestamp()), int(b.timestamp())] for a, b in ventanas]))
        await cola.encolar(con, tipo="backfill", link_id=link_id)
    return ventanas


async def confirmar_seleccion(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID, periodo_dias: int,
                              excluir: list[uuid.UUID], actor_user_id: Optional[uuid.UUID], actor_rol: str,
                              ip: Optional[str]) -> dict:
    excluir_set = set(excluir)
    async with ctx.db.tenant_tx(tenant_id) as con:
        link = await con.fetchrow(_LINK + " WHERE l.line_id = $1 ORDER BY l.created_at DESC LIMIT 1 FOR UPDATE OF l",
                                  line_id)
        if (link is None or link["estado"] != "vinculado" or link["ingesta"] != "pendiente_de_seleccion"
                or link["seleccion_confirmada_at"] is not None):
            raise SeleccionRechazada(409, "seleccion_no_disponible")
        if periodo_dias not in reglas.periodos_permitidos(reglas.profundidad_dias(link["engine"], link["full_sync"])):
            raise SeleccionRechazada(422, "periodo_invalido")
        pendientes = {f["id"] for f in await con.fetch(
            "SELECT id FROM wa_chats WHERE line_id = $1 AND estado = 'pendiente'", line_id)}
        if not excluir_set <= pendientes:
            raise SeleccionRechazada(422, "chat_inexistente")
        await con.execute(
            "UPDATE wa_chats SET estado = 'excluido', decidido_at = now(), updated_at = now(), "
            "motivo = CASE WHEN sugerencia IS NOT NULL THEN 'sugerencia' ELSE 'dueno' END "
            "WHERE id = ANY($1::uuid[])", list(excluir_set))
        incluidos = await con.execute("UPDATE wa_chats SET estado = 'incluido', decidido_at = now(), "
                                      "updated_at = now() WHERE line_id = $1 AND estado = 'pendiente'", line_id)
        await con.execute("UPDATE lines SET seleccion_confirmada_at = now(), periodo_dias = $2, updated_at = now() "
                          "WHERE id = $1", line_id, periodo_dias)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="seleccion_confirmada", tipo_objeto="line", objeto_id=line_id, ip=ip,
                                  detalle={"cantidad": len(excluir_set)})
        await eventos_producto.registrar_evento(
            con, tenant_id=tenant_id, evento="seleccion_confirmada", line_id=line_id,
            user_id=actor_user_id if actor_rol == "dueno" else None, objeto_id=link["id"],
            valores={"chats": int(incluidos.split()[-1]), "excluidos": len(excluir_set)})
    for chat_id in excluir_set:
        await borrar_contenido_de_chat(ctx, tenant_id, line_id, chat_id)
    await preparar_importacion(ctx, tenant_id, link["id"])
    return await listar_seleccion(ctx, tenant_id=tenant_id, line_id=line_id)


async def decidir_chat(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID, chat_id: uuid.UUID,
                       excluir: bool, actor_user_id: Optional[uuid.UUID], actor_rol: str,
                       ip: Optional[str]) -> dict:
    async with ctx.db.tenant_tx(tenant_id) as con:
        confirmada = await con.fetchval("SELECT seleccion_confirmada_at FROM lines WHERE id = $1", line_id)
        if confirmada is None:
            raise SeleccionRechazada(409, "seleccion_no_confirmada")
        estado = await con.fetchval("SELECT estado FROM wa_chats WHERE id = $1 AND line_id = $2 FOR UPDATE",
                                    chat_id, line_id)
        if estado is None:
            raise SeleccionRechazada(404, "chat_inexistente")
        if estado == "excluido":
            raise SeleccionRechazada(409, "chat_excluido")
        if excluir:
            await con.execute("UPDATE wa_chats SET estado = 'excluido', motivo = 'dueno', decidido_at = now(), "
                              "updated_at = now() WHERE id = $1", chat_id)
        elif estado == "pendiente":
            await con.execute("UPDATE wa_chats SET estado = 'incluido', decidido_at = now(), updated_at = now() "
                              "WHERE id = $1", chat_id)
            link_id = await con.fetchval("SELECT id FROM links WHERE line_id = $1 AND estado IN ('vinculado', 'caido') "
                                         "ORDER BY created_at DESC LIMIT 1", line_id)
            if link_id is not None:
                await con.execute(
                    "UPDATE links SET reimportar_chats = array_append(COALESCE(reimportar_chats, '{}'::uuid[]), $2) "
                    "WHERE id = $1 AND NOT ($2 = ANY(COALESCE(reimportar_chats, '{}'::uuid[])))", link_id, chat_id)
                await cola.encolar(con, tipo="backfill", link_id=link_id)
                await con.execute("UPDATE jobs SET ejecutar_desde = now(), updated_at = now() WHERE link_id = $1 "
                                  "AND tipo = 'backfill' AND estado = 'pendiente'", link_id)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="chat_decidido", tipo_objeto="line", objeto_id=line_id, ip=ip)
    if excluir:
        await borrar_contenido_de_chat(ctx, tenant_id, line_id, chat_id)
    return {"chat": str(chat_id), "estado": "excluido" if excluir else "incluido"}
```

- [ ] **Step 5: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_seleccion.py tests/radar_tests/test_auditoria.py -q`
Expected: PASS.

- [ ] **Step 6: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/ingesta/seleccion.py app/radar/auditoria.py app/radar/eventos_producto.py tests/radar_tests/test_ingesta_seleccion.py
git commit -m "Radar tramo 3: servicio de P4 con exclusión sin contenido y ventanas de importación por línea

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Job `conteo` — la pasada de conteo con estabilización y tope

**Files:**
- Create: `app/radar/ingesta/conteo.py`
- Modify: `app/radar/worker.py`, `tests/radar_tests/helpers.py`
- Test: `tests/radar_tests/test_ingesta_conteo.py`

**Interfaces:**
- Consumes: `cliente_lectura`, `paginar_chats`, `paginar_mensajes`, `MapaLids` (Task 6); `resolver_chat`, `ChatRef` (Task 6); `Acumulado`, `sugerir_exclusion` (Task 1); `reglas.estado_conteo`, `reglas.profundidad_dias`, `reglas.REPASO_CONTEO_S` (Task 2); `preparar_importacion` (Task 10); `Job` (`app/radar/jobs.py`).
- Produces: `conteo.ejecutar(ctx, job) -> Union[Literal["hecho"], float]`; `conteo.pasada(ctx, tenant_id, link) -> dict[uuid.UUID, Acumulado]`; `HANDLERS["conteo"]`; helper de test `job_de(esc, tipo) -> Job` y `preparar_ingesta(..., ingesta: Optional[str])`.

- [ ] **Step 1: Helpers de test**

En `tests/radar_tests/helpers.py`: cambiar la firma de `preparar_ingesta` a `ingesta: Optional[str] = "importando"` y agregar al final:

```python
def job_de(esc: dict, tipo: str):
    """Job en memoria para llamar un handler directo (los handlers no usan job.id)."""
    from app.radar.jobs import Job
    return Job(id=uuid.uuid4(), tenant_id=esc["tenant_id"], tipo=tipo, link_id=esc["link_id"], causa=None, intentos=1)
```

- [ ] **Step 2: Tests que fallan**

Crear `tests/radar_tests/test_ingesta_conteo.py`:

```python
"""
Pasada de conteo (§3 P4): lista de chats por GET /chats (nunca overview), conteo
leyendo mensajes del período con downloadMedia=false, el texto solo en memoria para
las señales locales, estabilización 60–90 s y tope de 10 minutos.
"""
import time

from app.radar.ingesta import conteo
from app.radar.secrets import Seudonimizador

from .helpers import fuente_fetch, job_de, preparar_ingesta
from .waha_falso import chat_crudo, msg_crudo

A, B, C, D = "5493411111111", "5493412222222", "5493413333333", "5493414444444"


def _datos(waha, sesion):
    ahora = int(time.time())
    waha.chats[sesion] = [chat_crudo(A, ts=ahora - 60, nombre="Marta"), chat_crudo(B, ts=ahora - 120, nombre="Banco"),
                          chat_crudo(C, ts=ahora - 180, nombre="Sin mensajes"),
                          {"id": "1203630@g.us", "conversationTimestamp": ahora}]
    waha.mensajes[sesion] = [
        msg_crudo(A, 1, ts=ahora - 600, body="hola, tienen protector solar? cuánto sale"),
        msg_crudo(A, 2, ts=ahora - 500, from_me=True, body="sí, sale $18.500"),
        msg_crudo(A, 3, ts=ahora - 200 * 86400, body="mensaje fuera del período"),
        msg_crudo(B, 4, ts=ahora - 400, body="Tu código de verificación es 1234"),
        msg_crudo(D, 5, ts=ahora - 300, body="hola"),                          # no está en la lista de chats
    ]


async def _link(ctx, esc):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow("SELECT * FROM links WHERE id = $1", esc["link_id"])


async def test_primera_pasada_cuenta_sin_guardar_texto(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta=None)
    _datos(waha, esc["session_name"])
    assert await conteo.ejecutar(ctx_waha, job_de(esc, "conteo")) == 20.0
    link = await _link(ctx_waha, esc)
    assert (link["ingesta"], link["conteo_n_chats"], link["conteo_n_msgs"]) == ("contando", 4, 4)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        filas = await con.fetch("SELECT k.entrantes, k.propios, k.sugerencia, c.contact_hmac, c.estado "
                                "FROM conteo_chats k JOIN wa_chats c ON c.id = k.chat_uuid")
        assert await con.fetchval("SELECT count(*) FROM wa_messages") == 0
    s = Seudonimizador(ctx_waha.secretos)
    por = {f["contact_hmac"]: f for f in filas}
    fa, fb, fc = (por[s.contact_hmac(esc["tenant_id"], "+" + u)] for u in (A, B, C))
    assert (fa["entrantes"], fa["propios"], fa["sugerencia"]) == (1, 1, None)
    assert (fb["sugerencia"], fc["sugerencia"]) == ("notificacion", "sin_entrantes")
    assert all(f["estado"] == "pendiente" for f in filas)
    assert await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT 1 FROM wa_message_bodies") == []
    assert await fuente_fetch(ctx_waha, esc["tenant_id"], "SELECT 1 FROM wa_message_provider_ids") == []
    nombres = {f["nombre"] for f in await fuente_fetch(ctx_waha, esc["tenant_id"],
                                                        "SELECT nombre FROM wa_contact_identities")}
    assert nombres == {"Marta", "Banco", "Sin mensajes", None}
    lecturas = [p for p in waha.params if p["ruta"].endswith("/messages")]
    assert lecturas and all(p["downloadMedia"] == "false" and "filter.timestamp.gte" in p for p in lecturas)
    assert not any("overview" in x for x in waha.llamadas)


async def test_estable_pasa_a_pendiente_de_seleccion(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta=None)
    _datos(waha, esc["session_name"])
    await conteo.ejecutar(ctx_waha, job_de(esc, "conteo"))
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET conteo_estable_desde = now() - interval '80 seconds' WHERE id = $1",
                          esc["link_id"])
    assert await conteo.ejecutar(ctx_waha, job_de(esc, "conteo")) == "hecho"
    link = await _link(ctx_waha, esc)
    assert (link["ingesta"], link["conteo_tope"]) == ("pendiente_de_seleccion", False)


async def test_tope_de_diez_minutos_corta_con_la_lista_usable(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta="contando")
    _datos(waha, esc["session_name"])
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET conteo_inicio_at = now() - interval '11 minutes', conteo_n_chats = 1, "
                          "conteo_n_msgs = 1 WHERE id = $1", esc["link_id"])
    assert await conteo.ejecutar(ctx_waha, job_de(esc, "conteo")) == "hecho"
    link = await _link(ctx_waha, esc)
    assert (link["ingesta"], link["conteo_tope"], link["conteo_n_chats"]) == ("pendiente_de_seleccion", True, 4)


async def test_suprimido_no_aparece_en_la_lista(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta=None)
    _datos(waha, esc["session_name"])
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("INSERT INTO suppressions (contact_hmac) VALUES ($1)",
                          Seudonimizador(ctx_waha.secretos).contact_hmac(esc["tenant_id"], "+" + B))
    await conteo.ejecutar(ctx_waha, job_de(esc, "conteo"))
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        estados = sorted(tuple(f) for f in await con.fetch("SELECT estado, motivo FROM wa_chats"))
    assert estados == [("excluido", "suprimido"), ("pendiente", None), ("pendiente", None), ("pendiente", None)]
    assert (await _link(ctx_waha, esc))["conteo_n_chats"] == 3


async def test_linea_con_seleccion_no_repite_p4(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta=None)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE lines SET seleccion_confirmada_at = now(), periodo_dias = 30 WHERE id = $1",
                          esc["line_id"])
    assert await conteo.ejecutar(ctx_waha, job_de(esc, "conteo")) == "hecho"
    link = await _link(ctx_waha, esc)
    assert link["ingesta"] == "importando" and waha.params == []
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT count(*) FROM jobs WHERE tipo = 'backfill'") == 1
```

- [ ] **Step 3: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_conteo.py -q`
Expected: FAIL con `ImportError: cannot import name 'conteo' from 'app.radar.ingesta'`.

- [ ] **Step 4: Implementación**

Crear `app/radar/ingesta/conteo.py`:

```python
"""
Pasada de conteo (§3 P4), entre WORKING y "Empezar análisis".

- Lista de chats: GET /api/{s}/chats paginado (id, nombre, último mensaje).
  Nunca chats/overview: trae el cuerpo del último mensaje.
- Conteo: chats/all/messages del período con downloadMedia=false. Todavía no
  existe ninguna exclusión (la selección es la primera de la línea). De cada
  mensaje queda en memoria solo el acumulado por chat y las señales locales; el
  texto se descarta en el mismo paso y no se escribe ni se loguea.
- Cada corrida es una pasada completa; se repite cada REPASO_CONTEO_S hasta que
  (chats, mensajes) no cambia durante ESTABLE, con tope TOPE_CONTEO. Al tope se
  corta con la lista usable (decisión 5 del plan).
- Una línea que ya eligió (re-vinculación) no repite P4: prepara la importación.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Union

from app.radar.contexto import RadarContexto
from app.radar.ingesta import reglas
from app.radar.ingesta.identidad import resolver_chat
from app.radar.ingesta.lectura import MapaLids, cliente_lectura, paginar_chats, paginar_mensajes
from app.radar.ingesta.reduccion import Acumulado, Jid, sugerir_exclusion
from app.radar.ingesta.seleccion import preparar_importacion
from app.radar.jobs import Job

_LINK = ("SELECT l.id, l.line_id, l.worker_id, l.session_name, l.engine, l.full_sync, l.estado, l.ingesta, "
         "l.conectado_at, l.conteo_inicio_at, l.conteo_estable_desde, l.conteo_n_chats, l.conteo_n_msgs, "
         "li.seleccion_confirmada_at, now() AS ahora FROM links l JOIN lines li ON li.id = l.line_id WHERE l.id = $1")


def _sumar(a: Acumulado, b: Acumulado) -> Acumulado:
    """Un contacto que aparece por lid y por teléfono es un solo chat."""
    ultimo = max((x for x in (a.ultimo_ts, b.ultimo_ts) if x is not None), default=None)
    return Acumulado(entrantes=a.entrantes + b.entrantes, propios=a.propios + b.propios,
                     con_texto=a.con_texto + b.con_texto, comerciales=a.comerciales + b.comerciales,
                     con_precio=a.con_precio + b.con_precio, notificaciones=a.notificaciones + b.notificaciones,
                     ultimo_ts=ultimo)


async def pasada(ctx: RadarContexto, tenant_id: uuid.UUID, link) -> dict[uuid.UUID, Acumulado]:
    sesion = link["session_name"]
    profundidad = reglas.profundidad_dias(link["engine"], link["full_sync"])
    desde = int((link["conectado_at"] - timedelta(days=profundidad)).timestamp())
    jids: dict[str, tuple[Jid, object]] = {}
    acumulados: dict[str, Acumulado] = {}
    por_chat: dict[uuid.UUID, Acumulado] = {}
    async with cliente_lectura(ctx, link_id=link["id"], worker_id=link["worker_id"]) as cli:
        async for pagina in paginar_chats(cli, sesion):
            for c in pagina:
                jids[c.jid.crudo()] = (c.jid, c.nombre)
        async for pagina in paginar_mensajes(cli, sesion, "all", gte=desde):
            for m in pagina:
                jids.setdefault(m.chat.crudo(), (m.chat, None))
                acumulados.setdefault(m.chat.crudo(), Acumulado()).sumar(m)
        lids = MapaLids(cli, sesion)
        for clave, (jid, nombre) in jids.items():
            ref = await resolver_chat(ctx, tenant_id=tenant_id, line_id=link["line_id"], jid=jid, lids=lids,
                                      nombre=nombre)
            if ref.suprimido:
                async with ctx.db.tenant_tx(tenant_id) as con:
                    await con.execute("UPDATE wa_chats SET estado = 'excluido', motivo = 'suprimido', "
                                      "decidido_at = now(), updated_at = now() WHERE id = $1 AND estado = 'pendiente'",
                                      ref.id)
                continue
            if ref.estado == "excluido":
                continue
            a = acumulados.get(clave, Acumulado())
            por_chat[ref.id] = _sumar(por_chat[ref.id], a) if ref.id in por_chat else a
    async with ctx.db.tenant_tx(tenant_id) as con:
        for chat_id, a in por_chat.items():
            sugerencia = sugerir_exclusion(a)
            ultimo = datetime.fromtimestamp(a.ultimo_ts, timezone.utc) if a.ultimo_ts else None
            await con.execute(
                """
                INSERT INTO conteo_chats (link_id, chat_uuid, entrantes, propios, con_texto, ultimo_ts, sugerencia)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (link_id, chat_uuid) DO UPDATE SET entrantes = EXCLUDED.entrantes,
                    propios = EXCLUDED.propios, con_texto = EXCLUDED.con_texto, ultimo_ts = EXCLUDED.ultimo_ts,
                    sugerencia = EXCLUDED.sugerencia, updated_at = now()
                """, link["id"], chat_id, a.entrantes, a.propios, a.con_texto, ultimo, sugerencia)
            await con.execute("UPDATE wa_chats SET sugerencia = $2, updated_at = now() "
                              "WHERE id = $1 AND estado = 'pendiente' AND decidido_at IS NULL", chat_id, sugerencia)
    return por_chat


async def ejecutar(ctx: RadarContexto, job: Job) -> Union[Literal["hecho"], float]:
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        link = await con.fetchrow(_LINK, job.link_id)
        if link is None or link["estado"] != "vinculado" or link["ingesta"] not in (None, "contando"):
            return "hecho"
        if link["seleccion_confirmada_at"] is None and link["ingesta"] is None:
            await con.execute("UPDATE links SET ingesta = 'contando', conteo_inicio_at = now(), "
                              "conteo_estable_desde = NULL, updated_at = now() WHERE id = $1", link["id"])
    if link["seleccion_confirmada_at"] is not None:
        await preparar_importacion(ctx, job.tenant_id, link["id"])
        return "hecho"
    inicio = link["conteo_inicio_at"] or link["ahora"]
    por_chat = await pasada(ctx, job.tenant_id, link)
    actual = (len(por_chat), sum(a.entrantes + a.propios for a in por_chat.values()))
    previo = (link["conteo_n_chats"], link["conteo_n_msgs"]) if link["conteo_n_chats"] is not None else None
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        ahora = await con.fetchval("SELECT now()")
        decision, estable = reglas.estado_conteo(inicio=inicio, ahora=ahora, previo=previo, actual=actual,
                                                 estable_desde=link["conteo_estable_desde"])
        await con.execute(
            "UPDATE links SET conteo_n_chats = $2, conteo_n_msgs = $3, conteo_estable_desde = $4, conteo_tope = $5, "
            "ingesta = CASE WHEN $6 THEN 'pendiente_de_seleccion' ELSE ingesta END, updated_at = now() "
            "WHERE id = $1 AND ingesta = 'contando'",
            link["id"], actual[0], actual[1], estable, decision == "tope", decision != "seguir")
    return reglas.REPASO_CONTEO_S if decision == "seguir" else "hecho"
```

En `app/radar/worker.py`, agregar `conteo` al import (`from app.radar.ingesta import conteo, inbox`) y `"conteo": conteo.ejecutar,` a `HANDLERS`.

- [ ] **Step 5: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_conteo.py tests/radar_tests/test_ingesta_inbox.py -q`
Expected: PASS.

- [ ] **Step 6: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/ingesta/conteo.py app/radar/worker.py tests/radar_tests/helpers.py tests/radar_tests/test_ingesta_conteo.py
git commit -m "Radar tramo 3: pasada de conteo sin guardar texto, con estabilización y tope de 10 minutos

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: API y pantalla de P4 y P5 para el dueño y la Consola KIS

**Files:**
- Create: `app/radar/ingesta/progreso.py`, `app/radar/routers/seleccion.py`, `app/radar/static/seleccion.html`, `app/radar/static/seleccion.js`
- Modify: `app/radar/app.py`, `app/radar/routers/paginas.py`, `app/radar/static/radar.js`, `app/radar/static/conectar.html`, `app/radar/static/consola.html`
- Test: `tests/radar_tests/test_seleccion_api.py`

**Interfaces:**
- Consumes: `listar_seleccion`, `confirmar_seleccion`, `decidir_chat`, `SeleccionRechazada` (Task 10); `requiere_rol(rol)`, `sesion_actual`, `ip_de(request)`, `Sesion` (`app/radar/auth.py`); `_linea_visible(ctx, sesion, line_id)` (`app/radar/routers/vinculo.py`); `_tenant_cliente` (`app/radar/routers/consola.py`); `auditoria.registrar`; `_html(nombre)`, `_TIPOS` (`app/radar/routers/paginas.py`).
- Produces: `progreso_linea(ctx, *, tenant_id, line_id) -> dict`; rutas `GET|PUT /radar/api/lineas/{line_id}/seleccion`, `PATCH /radar/api/lineas/{line_id}/seleccion/chats/{chat_id}`, `GET /radar/api/lineas/{line_id}/seleccion/progreso`, y las mismas bajo `/radar/admin/tenants/{tenant_id}/lineas/{line_id}/seleccion` (el GET admin acepta `?con_nombres=true` y entonces audita `seleccion_vista`); páginas `GET /radar/seleccion?linea=…` (dueño) y `GET /radar/consola/seleccion?tenant=…&linea=…` (admin); `seleccion.js` servido por `/radar/estaticos/`; enlace `ir-seleccion` en P3 y en el panel de la Consola.

La respuesta de `progreso_linea` es:

```json
{"importacion": {"estado": "importando", "chats_total": 530, "chats_importados": 412, "reintentando": 118,
                 "mensajes": 18230, "pasada": 1, "pasadas": 4, "cobertura": "provisoria",
                 "texto": "Importamos 412 de 530 chats; reintentando 118. Cobertura provisoria."},
 "metricas": {"pendiente": "tramo 4", "texto": "Próximamente."},
 "analisis_ia": {"pendiente": "tramo 5", "texto": "Próximamente."},
 "observado_hasta": null, "ultimo_mensaje": null, "posible_hueco_desde": null,
 "chats_nuevos_esperan_decision": 0}
```

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_seleccion_api.py`:

```python
"""
API y pantallas de P4/P5 (§3 P4, §3 P5, §3.1 C2 paso 5): el dueño y los admins de
KIS sobre el mismo servicio; la Consola ve nombres solo a pedido y queda auditado.
"""
import pathlib
import re

from app.radar.constantes import TENANT_KIS
from app.radar.routers.paginas import CSP

from .helpers import chat_de_prueba, crear_usuario, entrar, preparar_ingesta

ESTATICOS = pathlib.Path(__file__).resolve().parents[2] / "app" / "radar" / "static"
A, B = "5493411111111", "5493412222222"


async def _escenario(ctx, waha):
    esc = await preparar_ingesta(ctx, waha, ingesta="pendiente_de_seleccion")
    a = await chat_de_prueba(ctx, esc, A, estado="pendiente", nombre="Marta")
    b = await chat_de_prueba(ctx, esc, B, estado="pendiente", nombre="Banco")
    return esc, a, b


async def _admin(cliente, ctx):
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


async def test_dueno_ve_confirma_y_sigue_el_progreso(cliente, ctx_waha, waha):
    esc, a, b = await _escenario(ctx_waha, waha)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    url = f"/radar/api/lineas/{esc['line_id']}/seleccion"
    r = await cliente.get(url)
    assert r.status_code == 200 and {c["nombre"] for c in r.json()["chats"]} == {"Marta", "Banco"}
    r = await cliente.put(url, json={"periodo_dias": 30, "excluir": [str(b)]})
    assert r.status_code == 200 and r.json()["estado"] == "confirmada" and r.json()["excluidos"] == 1
    p = await cliente.get(url + "/progreso")
    assert p.status_code == 200 and p.json()["importacion"]["chats_total"] == 1
    assert p.json()["importacion"]["texto"].startswith("Preparando la importación")
    assert (await cliente.put(url, json={"periodo_dias": 30, "excluir": []})).status_code == 409
    assert (await cliente.put(url, json={"periodo_dias": "x"})).status_code == 422


async def test_solo_el_dueno_opera_y_otros_roles_ven_el_progreso(cliente, ctx_waha, waha):
    esc, a, _ = await _escenario(ctx_waha, waha)
    gestor = await crear_usuario(ctx_waha.db, esc["tenant_id"], "gestor@cliente.com", "gestor")
    await entrar(cliente, ctx_waha, esc["tenant_id"], gestor, "gestor")
    url = f"/radar/api/lineas/{esc['line_id']}/seleccion"
    assert (await cliente.get(url)).status_code == 403
    assert (await cliente.patch(url + f"/chats/{a}", json={"excluir": True})).status_code == 403
    assert (await cliente.get(url + "/progreso")).status_code == 200


async def test_consola_sin_nombres_salvo_a_pedido_y_auditado(cliente, ctx_waha, waha):
    esc, a, b = await _escenario(ctx_waha, waha)
    uid = await _admin(cliente, ctx_waha)
    url = f"/radar/admin/tenants/{esc['tenant_id']}/lineas/{esc['line_id']}/seleccion"
    r = await cliente.get(url)
    assert r.status_code == 200 and {c["nombre"] for c in r.json()["chats"]} == {None}
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'seleccion_vista'") == 0
    r = await cliente.get(url + "?con_nombres=true")
    assert {c["nombre"] for c in r.json()["chats"]} == {"Marta", "Banco"}
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'seleccion_vista' "
                                  "AND actor_user_id = $1", uid) == 1
    r = await cliente.put(url, json={"periodo_dias": 90, "excluir": []})
    assert r.status_code == 200
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT actor_rol FROM access_audit_log WHERE accion = 'seleccion_confirmada'") \
            == "admin"


async def test_paginas_con_csp_y_sin_inline(cliente, ctx_waha, waha):
    esc, _, _ = await _escenario(ctx_waha, waha)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    r = await cliente.get(f"/radar/seleccion?linea={esc['line_id']}")
    assert r.status_code == 200 and r.headers["content-security-policy"] == CSP
    assert '<script src="/radar/estaticos/seleccion.js" defer></script>' in r.text
    assert re.search(r"<script(?![^>]*\bsrc=)", r.text) is None and " style=" not in r.text
    assert (await cliente.get("/radar/consola/seleccion")).status_code == 403
    await _admin(cliente, ctx_waha)
    assert (await cliente.get("/radar/consola/seleccion")).status_code == 200
    js = await cliente.get("/radar/estaticos/seleccion.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")


def test_el_js_de_seleccion_no_arma_html_y_sus_ids_existen():
    js = (ESTATICOS / "seleccion.js").read_text(encoding="utf-8")
    for prohibido in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert prohibido not in js, prohibido
    usados = set(re.findall(r'\$\("([a-z0-9-]+)"\)', js))
    html = set(re.findall(r'\bid="([a-z0-9-]+)"', (ESTATICOS / "seleccion.html").read_text(encoding="utf-8")))
    assert usados and usados <= html, usados - html
    assert "Contamos tus mensajes sin guardar su texto." in (ESTATICOS / "seleccion.html").read_text(encoding="utf-8")
```

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_seleccion_api.py -q`
Expected: FAIL (404 en `/radar/api/lineas/{id}/seleccion` y `FileNotFoundError` de `seleccion.js`).

- [ ] **Step 3: Progreso (P5)**

Crear `app/radar/ingesta/progreso.py`:

```python
"""
P5 — Progreso con resultados parciales (§3 P5): el carril de importación con
checkpoint por chat y cobertura provisoria/estable. Los carriles de métricas (tramo
4) y de IA (tramo 5) van marcados como pendientes. Solo conteos: nada de nombres.
"""

import uuid
from datetime import datetime
from typing import Optional

from app.radar.contexto import RadarContexto


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def texto_importacion(*, pasada: int, total: int, ok: int, fallidos: int, cobertura: Optional[str]) -> str:
    if pasada == 0:
        return "Preparando la importación."
    texto = f"Importamos {ok} de {total} chats" + (f"; reintentando {fallidos}" if fallidos else "") + "."
    if cobertura:
        texto += " Cobertura " + ("estable." if cobertura == "estable" else "provisoria.")
    return texto


async def progreso_linea(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID) -> dict:
    async with ctx.db.tenant_tx(tenant_id) as con:
        link = await con.fetchrow("SELECT id, ingesta, cobertura, backfill_pasada, observado_hasta, ultimo_msg_at, "
                                  "silencio_desde FROM links WHERE line_id = $1 ORDER BY created_at DESC LIMIT 1",
                                  line_id)
        total = await con.fetchval("SELECT count(*) FROM wa_chats WHERE line_id = $1 AND estado <> 'excluido'",
                                   line_id)
        mensajes = await con.fetchval("SELECT count(*) FROM wa_messages WHERE line_id = $1", line_id)
        pendientes = await con.fetchval(
            "SELECT count(*) FROM wa_chats c JOIN lines li ON li.id = c.line_id "
            "WHERE c.line_id = $1 AND c.estado = 'pendiente' AND li.seleccion_confirmada_at IS NOT NULL", line_id)
        ok = fallidos = 0
        if link is not None:
            fila = await con.fetchrow(
                "SELECT count(*) FILTER (WHERE estado = 'ok') AS ok, count(*) FILTER (WHERE estado = 'fallido') AS f "
                "FROM ingesta_conteos WHERE link_id = $1 AND pasada = $2", link["id"], link["backfill_pasada"])
            ok, fallidos = fila["ok"], fila["f"]
    pasada = link["backfill_pasada"] if link else 0
    cobertura = link["cobertura"] if link else None
    return {
        "importacion": {"estado": link["ingesta"] if link else None, "chats_total": total, "chats_importados": ok,
                        "reintentando": fallidos, "mensajes": mensajes, "pasada": pasada, "pasadas": 4,
                        "cobertura": cobertura,
                        "texto": texto_importacion(pasada=pasada, total=total, ok=ok, fallidos=fallidos,
                                                   cobertura=cobertura)},
        "metricas": {"pendiente": "tramo 4", "texto": "Próximamente."},
        "analisis_ia": {"pendiente": "tramo 5", "texto": "Próximamente."},
        "observado_hasta": _iso(link["observado_hasta"]) if link else None,
        "ultimo_mensaje": _iso(link["ultimo_msg_at"]) if link else None,
        "posible_hueco_desde": _iso(link["silencio_desde"]) if link else None,
        "chats_nuevos_esperan_decision": pendientes,
    }
```

- [ ] **Step 4: Router**

Crear `app/radar/routers/seleccion.py`:

```python
"""
P4 (Elegí qué analizar) y P5 (progreso) sobre un mismo servicio para el dueño y la
Consola KIS (§3 P4, §3 P5, §3.1 C2 paso 5).

GET   /radar/api/lineas/{l}/seleccion                         dueño
PUT   /radar/api/lineas/{l}/seleccion                         dueño: "Empezar análisis"
PATCH /radar/api/lineas/{l}/seleccion/chats/{c}               dueño: chat nuevo
GET   /radar/api/lineas/{l}/seleccion/progreso                cualquier rol que vea la línea
GET|PUT|PATCH /radar/admin/tenants/{t}/lineas/{l}/seleccion…  admin KIS

La Consola recibe los nombres de los contactos solo con ?con_nombres=true, y esa
lectura queda en access_audit_log (seleccion_vista). El sondeo sin nombres no audita.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.radar import auditoria
from app.radar.auth import Sesion, ip_de, requiere_rol, sesion_actual
from app.radar.contexto import contexto
from app.radar.ingesta.progreso import progreso_linea
from app.radar.ingesta.seleccion import SeleccionRechazada, confirmar_seleccion, decidir_chat, listar_seleccion
from app.radar.routers.consola import _tenant_cliente
from app.radar.routers.vinculo import _linea_visible

router = APIRouter(tags=["radar-seleccion"])
_DUENO = "/radar/api/lineas/{line_id}/seleccion"
_ADMIN = "/radar/admin/tenants/{tenant_id}/lineas/{line_id}/seleccion"


class ConfirmarIn(BaseModel):
    periodo_dias: int
    excluir: list[uuid.UUID] = Field(default_factory=list, max_length=20000)


class DecidirIn(BaseModel):
    excluir: bool


def _http(e: SeleccionRechazada) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"error": e.codigo})


def _actor(s: Sesion, request: Request) -> dict:
    return {"actor_user_id": s.user_id, "actor_rol": s.rol, "ip": ip_de(request)}


@router.get(_DUENO)
async def ver(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return await listar_seleccion(ctx, tenant_id=sesion.tenant_id, line_id=line_id)
    except SeleccionRechazada as e:
        raise _http(e)


@router.put(_DUENO)
async def confirmar(line_id: uuid.UUID, body: ConfirmarIn, request: Request,
                    sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return await confirmar_seleccion(ctx, tenant_id=sesion.tenant_id, line_id=line_id,
                                         periodo_dias=body.periodo_dias, excluir=body.excluir,
                                         **_actor(sesion, request))
    except SeleccionRechazada as e:
        raise _http(e)


@router.patch(_DUENO + "/chats/{chat_id}")
async def decidir(line_id: uuid.UUID, chat_id: uuid.UUID, body: DecidirIn, request: Request,
                  sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return await decidir_chat(ctx, tenant_id=sesion.tenant_id, line_id=line_id, chat_id=chat_id,
                                  excluir=body.excluir, **_actor(sesion, request))
    except SeleccionRechazada as e:
        raise _http(e)


@router.get(_DUENO + "/progreso")
async def progreso(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    return await progreso_linea(ctx, tenant_id=sesion.tenant_id, line_id=line_id)


@router.get(_ADMIN)
async def ver_admin(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request, con_nombres: bool = False,
                    admin: Sesion = Depends(_tenant_cliente)):
    ctx = contexto(request)
    try:
        r = await listar_seleccion(ctx, tenant_id=tenant_id, line_id=line_id, con_nombres=con_nombres)
    except SeleccionRechazada as e:
        raise _http(e)
    if con_nombres:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                      accion="seleccion_vista", tipo_objeto="line", objeto_id=line_id,
                                      ip=ip_de(request))
    return r


@router.put(_ADMIN)
async def confirmar_admin(tenant_id: uuid.UUID, line_id: uuid.UUID, body: ConfirmarIn, request: Request,
                          admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await confirmar_seleccion(contexto(request), tenant_id=tenant_id, line_id=line_id,
                                         periodo_dias=body.periodo_dias, excluir=body.excluir,
                                         **_actor(admin, request))
    except SeleccionRechazada as e:
        raise _http(e)


@router.patch(_ADMIN + "/chats/{chat_id}")
async def decidir_admin(tenant_id: uuid.UUID, line_id: uuid.UUID, chat_id: uuid.UUID, body: DecidirIn,
                        request: Request, admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await decidir_chat(contexto(request), tenant_id=tenant_id, line_id=line_id, chat_id=chat_id,
                                  excluir=body.excluir, **_actor(admin, request))
    except SeleccionRechazada as e:
        raise _http(e)


@router.get(_ADMIN + "/progreso")
async def progreso_admin(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                         admin: Sesion = Depends(_tenant_cliente)):
    return await progreso_linea(contexto(request), tenant_id=tenant_id, line_id=line_id)
```

En `app/radar/app.py`: agregar `seleccion` al import de routers (`from app.radar.routers import admin, consola, cuenta, health, login, paginas, parametros, seleccion, soporte, vinculo, webhook_waha`) y `app.include_router(seleccion.router)` después de `app.include_router(vinculo.router)`.

- [ ] **Step 5: Páginas y estáticos**

En `app/radar/routers/paginas.py`:

1. Reemplazar `_TIPOS` por:

```python
_TIPOS = {"radar.js": "text/javascript; charset=utf-8", "seleccion.js": "text/javascript; charset=utf-8",
          "radar.css": "text/css; charset=utf-8"}
```

2. Agregar, antes de `estatico`:

```python
@router.get("/radar/seleccion", response_class=HTMLResponse)
async def seleccion(dueno: Sesion = Depends(requiere_rol("dueno"))):
    return _html("seleccion.html")


@router.get("/radar/consola/seleccion", response_class=HTMLResponse)
async def seleccion_consola(admin: Sesion = Depends(requiere_rol("admin"))):
    return _html("seleccion.html")
```

Crear `app/radar/static/seleccion.html`:

```html
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Radar — Elegí qué analizar</title>
<link rel="stylesheet" href="/radar/estaticos/radar.css">
<script src="/radar/estaticos/seleccion.js" defer></script>
</head>
<body>
<header>
  <h1>Elegí qué analizar</h1>
  <p class="nota">Contamos tus mensajes sin guardar su texto.</p>
</header>
<p id="error" class="error" role="alert"></p>

<section>
  <p id="estado-seleccion" role="status"></p>

  <div id="paso-elegir" hidden>
    <p class="nota">Lo que excluyas no pasa a nuestra base de análisis ni a la IA. Los chats sugeridos para excluir vienen marcados; podés cambiarlos.</p>
    <label>Período <select id="periodo"></select></label>
    <label>Buscar <input type="search" id="buscar" autocomplete="off"></label>
    <p id="contador"></p>
    <table>
      <thead><tr><th>Chat</th><th>Recibidos</th><th>Enviados</th><th>Último</th><th>Sugerencia</th><th>Excluir</th></tr></thead>
      <tbody id="chats"></tbody>
    </table>
    <button type="button" id="btn-empezar">Empezar análisis</button>
  </div>

  <div id="paso-nuevos" hidden>
    <h2 id="titulo-nuevos"></h2>
    <table><tbody id="nuevos"></tbody></table>
  </div>

  <div id="paso-progreso" hidden>
    <h2>Progreso</h2>
    <p id="carril-importacion"></p>
    <p id="carril-metricas" class="nota"></p>
    <p id="carril-ia" class="nota"></p>
    <p id="frescura" class="nota"></p>
  </div>
</section>
</body>
</html>
```

Crear `app/radar/static/seleccion.js`:

```javascript
"use strict";
// Radar: P4 (Elegí qué analizar) y P5 (progreso) para el dueño y la Consola KIS.
// Sin dependencias. Todo dato del servidor se escribe con textContent; nunca se arma HTML.
(function () {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(window.location.search);
  const linea = params.get("linea") || "";
  const tenant = params.get("tenant") || "";
  const UUID = /^[0-9a-f-]{36}$/;
  const REFRESCO_MS = 3000;
  const SUGERENCIAS = {
    sin_entrantes: "Sin mensajes recibidos",
    notificacion: "Parece notificación",
    sin_consulta_comercial: "Sin consultas de compra",
  };
  const MENSAJES = {
    seleccion_no_disponible: "La selección ya no está disponible. Recargá la página.",
    periodo_invalido: "Elegí un período dentro de lo que permite la conexión.",
    chat_inexistente: "Uno de los chats ya no existe. Recargá la página.",
    chat_excluido: "Ese chat ya fue excluido.",
    seleccion_no_confirmada: "Primero confirmá la selección.",
    linea_inexistente: "La línea no existe.",
    linea_invalida: "El link no indica una línea válida.",
  };
  let datos = null;
  const excluir = new Set();
  let preseleccionado = false;

  function mostrarError(e) {
    $("error").textContent = e ? (MENSAJES[e.message] || ("Error: " + e.message)) : "";
  }

  function base() {
    if (tenant) {
      return "/radar/admin/tenants/" + encodeURIComponent(tenant) + "/lineas/" + encodeURIComponent(linea) + "/seleccion";
    }
    return "/radar/api/lineas/" + encodeURIComponent(linea) + "/seleccion";
  }

  async function pedir(metodo, url, cuerpo) {
    const opciones = { method: metodo, credentials: "same-origin", headers: {} };
    if (cuerpo !== undefined) {
      opciones.headers["Content-Type"] = "application/json";
      opciones.body = JSON.stringify(cuerpo);
    }
    const r = await fetch(url, opciones);
    let d = null;
    try { d = await r.json(); } catch (e) { d = null; }
    if (!r.ok) {
      const det = d && d.detail;
      throw new Error((det && det.error) || (typeof det === "string" ? det : "error_" + r.status));
    }
    return d;
  }

  async function accion(fn) {
    try { await fn(); mostrarError(null); } catch (e) { mostrarError(e); }
  }

  function fecha(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleDateString("es-AR", {
      day: "2-digit", month: "2-digit", timeZone: "America/Argentina/Buenos_Aires",
    });
  }

  function celda(texto) {
    const td = document.createElement("td");
    td.textContent = texto;
    return td;
  }

  function contar() {
    const n = datos.chats.filter((c) => !excluir.has(c.chat)).length;
    $("contador").textContent = "Vamos a analizar " + n + " chats.";
  }

  function pintarLista() {
    const filtro = $("buscar").value.trim().toLowerCase();
    const cuerpo = $("chats");
    cuerpo.replaceChildren();
    for (const c of datos.chats) {
      const nombre = c.nombre || "Chat";
      if (filtro && !nombre.toLowerCase().includes(filtro)) continue;
      const tr = document.createElement("tr");
      tr.appendChild(celda(nombre));
      tr.appendChild(celda(String(c.entrantes)));
      tr.appendChild(celda(String(c.propios)));
      tr.appendChild(celda(fecha(c.ultimo_at)));
      tr.appendChild(celda(c.sugerencia ? (SUGERENCIAS[c.sugerencia] || c.sugerencia) : ""));
      const td = document.createElement("td");
      const caja = document.createElement("input");
      caja.type = "checkbox";
      caja.checked = excluir.has(c.chat);
      caja.setAttribute("aria-label", "Excluir " + nombre);
      caja.addEventListener("change", () => {
        if (caja.checked) excluir.add(c.chat); else excluir.delete(c.chat);
        contar();
      });
      td.appendChild(caja);
      tr.appendChild(td);
      cuerpo.appendChild(tr);
    }
    contar();
  }

  function llenarPeriodos() {
    const sel = $("periodo");
    if (sel.options.length) return;
    for (const p of datos.periodos) {
      const op = document.createElement("option");
      op.value = String(p);
      op.textContent = p + " días";
      sel.appendChild(op);
    }
    sel.value = String(datos.periodos[datos.periodos.length - 1]);
  }

  function pintarNuevos() {
    const pendientes = datos.chats.filter((c) => c.estado === "pendiente");
    $("paso-nuevos").hidden = pendientes.length === 0;
    $("titulo-nuevos").textContent = pendientes.length + " chats nuevos esperan tu decisión";
    const cuerpo = $("nuevos");
    cuerpo.replaceChildren();
    for (const c of pendientes) {
      const tr = document.createElement("tr");
      tr.appendChild(celda(c.nombre || "Chat"));
      for (const [texto, valor] of [["Analizar", false], ["Excluir", true]]) {
        const td = document.createElement("td");
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = texto;
        b.addEventListener("click", () => accion(async () => {
          await pedir("PATCH", base() + "/chats/" + encodeURIComponent(c.chat), { excluir: valor });
          await cargar(true);
        }));
        td.appendChild(b);
        tr.appendChild(td);
      }
      cuerpo.appendChild(tr);
    }
  }

  async function cargarProgreso() {
    const p = await pedir("GET", base() + "/progreso");
    $("carril-importacion").textContent = p.importacion.texto;
    $("carril-metricas").textContent = "Métricas medidas: " + p.metricas.texto;
    $("carril-ia").textContent = "Análisis con IA: " + p.analisis_ia.texto;
    $("frescura").textContent = p.observado_hasta ? "Datos verificados hasta el " + fecha(p.observado_hasta) + "." : "";
  }

  function mostrar() {
    const encontrados = "Encontramos " + datos.chats_encontrados + " chats";
    const textos = {
      sin_vinculo: "La línea no está conectada.",
      contando: encontrados + "… seguimos buscando.",
      lista: encontrados + "." + (datos.tope_alcanzado
        ? " WhatsApp todavía nos está entregando chats; los que aparezcan después van a esperar tu decisión." : ""),
      confirmada: "Selección confirmada.",
    };
    $("estado-seleccion").textContent = textos[datos.estado] || datos.estado;
    const eligiendo = datos.estado === "contando" || datos.estado === "lista";
    $("paso-elegir").hidden = !eligiendo;
    $("btn-empezar").disabled = datos.estado !== "lista";
    if (eligiendo) {
      if (!preseleccionado && datos.estado === "lista") {
        for (const c of datos.chats) if (c.excluir) excluir.add(c.chat);
        preseleccionado = true;
      }
      llenarPeriodos();
      pintarLista();
    }
    $("paso-progreso").hidden = datos.estado !== "confirmada";
    if (datos.estado === "confirmada") pintarNuevos();
  }

  async function cargar(conNombres) {
    // Consola: nombres solo a pedido (queda auditado); el dueño siempre los recibe.
    datos = await pedir("GET", base() + (tenant && conNombres ? "?con_nombres=true" : ""));
    mostrar();
    if (datos.estado === "confirmada") await cargarProgreso();
  }

  async function sondear() {
    if (datos.estado === "contando") {
      const nuevo = await pedir("GET", base());
      if (nuevo.estado === "contando") {
        datos = nuevo;
        mostrar();
      } else {
        await cargar(true);
      }
    } else if (datos.estado === "confirmada") {
      await cargarProgreso();
    }
  }

  if (!UUID.test(linea) || (tenant && !UUID.test(tenant))) {
    mostrarError(new Error("linea_invalida"));
    return;
  }
  $("buscar").addEventListener("input", () => { if (datos) pintarLista(); });
  $("btn-empezar").addEventListener("click", () => accion(async () => {
    await pedir("PUT", base(), { periodo_dias: Number($("periodo").value), excluir: Array.from(excluir) });
    await cargar(true);
  }));
  accion(() => cargar(true));
  setInterval(() => { if (datos) sondear().catch(mostrarError); }, REFRESCO_MS);
})();
```

- [ ] **Step 6: Enlaces desde P3 y la Consola**

En `app/radar/static/conectar.html`, después de `<p id="aviso" class="nota"></p>` (dentro de `paso-estado`), agregar:

```html
    <p><a id="ir-seleccion" href="#" hidden>Elegí qué analizar</a></p>
```

En `app/radar/static/consola.html`, después de `<p id="aviso" class="nota"></p>` agregar la misma línea, y reemplazar

```html
  <p class="nota">"—" = dato que llega con la ingesta (tramo 3) o con la IA (tramo 5).</p>
```

por

```html
  <p class="nota">"—" = todavía no hay dato (el gasto de IA llega con el tramo 5).</p>
```

En `app/radar/static/radar.js`, al final de `mostrarEstado(e)` (después de la línea de `btn-desconectar`), agregar:

```javascript
    const ir = $("ir-seleccion");
    if (ir) {
      const q = new URLSearchParams({ linea: actual.line_id });
      if (modo === "consola") q.set("tenant", actual.tenant_id);
      ir.setAttribute("href", (modo === "consola" ? "/radar/consola/seleccion?" : "/radar/seleccion?") + q.toString());
      ir.hidden = e.estado !== "vinculado";
    }
```

- [ ] **Step 7: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_seleccion_api.py tests/radar_tests/test_paginas.py -q`
Expected: PASS (`test_los_ids_que_usa_el_js_existen_en_la_consola` ve `ir-seleccion` en `consola.html`).

- [ ] **Step 8: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/ingesta/progreso.py app/radar/routers/seleccion.py app/radar/app.py app/radar/routers/paginas.py app/radar/static/seleccion.html app/radar/static/seleccion.js app/radar/static/radar.js app/radar/static/conectar.html app/radar/static/consola.html tests/radar_tests/test_seleccion_api.py
git commit -m "Radar tramo 3: API y pantalla de P4 y P5 para el dueño y la Consola, con nombres auditados

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Job `backfill` — pasadas +0 / +30 min / +6 h / +24 h y reimportación de chats

**Files:**
- Create: `app/radar/ingesta/backfill.py`
- Modify: `app/radar/worker.py`
- Test: `tests/radar_tests/test_ingesta_backfill.py`

**Interfaces:**
- Consumes: `cliente_lectura`, `paginar_mensajes`, `MapaLids`, `Presupuesto` (Task 6); `resolver_chat`, `leer_chat`, `jids_de_chat`, `ChatRef` (Task 6); `guardar_lote` (Task 7); `reglas.proxima_pasada`, `reglas.conteos_coinciden` (Task 2); `eventos_producto.registrar_evento`; `WahaError`.
- Produces: `ESPERA_CAIDO_S = 300.0`; `backfill.ejecutar(ctx, job) -> Union[Literal["hecho"], float]`; `backfill.importar_chat(ctx, cli, tenant_id, link, chat: ChatRef, ventanas: list[tuple[int, Optional[int]]], *, via: str, presupuesto=None) -> int`; `HANDLERS["backfill"]`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_ingesta_backfill.py`:

```python
"""
Backfill en pasadas (§6.3 punto 2): ventana lte = t_vínculo; chats/all/messages
solo en la pasada inicial y sin exclusiones; chat por chat si hay exclusiones o
después; página vacía como fin; repeticiones a +30 min, +6 h y +24 h; cobertura
provisoria hasta que dos pasadas coinciden; contabilidad por chat.
"""
from app.radar.ingesta import backfill
from app.radar.ingesta.seleccion import preparar_importacion

from .helpers import chat_de_prueba, fuente_fetch, job_de, preparar_ingesta
from .waha_falso import msg_crudo

A, B, X = "5493411111111", "5493412222222", "5493419999999"


async def _preparar(ctx, waha, *, excluir_x=False):
    esc = await preparar_ingesta(ctx, waha)
    await chat_de_prueba(ctx, esc, A, nombre="Marta")
    await chat_de_prueba(ctx, esc, B, nombre="Pedro")
    await chat_de_prueba(ctx, esc, X, estado="excluido" if excluir_x else "incluido")
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        conectado = int((await con.fetchval("SELECT conectado_at FROM links WHERE id = $1",
                                            esc["link_id"])).timestamp())
    s = esc["session_name"]
    waha.mensajes[s] = [msg_crudo(A, 1, ts=conectado - 3600, body="hola"),
                        msg_crudo(A, 2, ts=conectado - 3500, from_me=True, body="buen día"),
                        msg_crudo(B, 3, ts=conectado - 100 * 86400, body="viejo, fuera del período"),
                        msg_crudo(X, 4, ts=conectado - 1800, body="personal"),
                        msg_crudo(A, 5, ts=conectado + 60, body="posterior al vínculo")]
    await preparar_importacion(ctx, esc["tenant_id"], esc["link_id"])
    return esc, conectado


async def _link(ctx, esc):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow("SELECT * FROM links WHERE id = $1", esc["link_id"])


async def _rutas(waha):
    return [p["ruta"].rsplit("/", 2)[-2] for p in waha.params if p["ruta"].endswith("/messages")]


async def test_pasada_inicial_sin_exclusiones_usa_all_y_respeta_la_ventana(ctx_waha, waha):
    esc, conectado = await _preparar(ctx_waha, waha)
    espera = await backfill.ejecutar(ctx_waha, job_de(esc, "backfill"))
    assert 1790 <= espera <= 1800                                   # próxima pasada a +30 min
    assert set(await _rutas(waha)) == {"all"}
    assert all(p["filter.timestamp.lte"] == str(conectado) for p in waha.params if "filter.timestamp.lte" in p)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        n = await con.fetchval("SELECT count(*) FROM wa_messages")
        conteos = await con.fetch("SELECT mensajes, estado FROM ingesta_conteos WHERE pasada = 1")
    assert n == 3                                                    # A1, A2 y X4; ni el viejo ni el posterior
    assert sorted(c["mensajes"] for c in conteos) == [0, 1, 2] and {c["estado"] for c in conteos} == {"ok"}
    link = await _link(ctx_waha, esc)
    assert (link["backfill_pasada"], link["cobertura"], link["ingesta"]) == (1, "provisoria", "importando")


async def test_con_exclusiones_va_chat_por_chat_y_nunca_lee_el_excluido(ctx_waha, waha):
    esc, _ = await _preparar(ctx_waha, waha, excluir_x=True)
    await backfill.ejecutar(ctx_waha, job_de(esc, "backfill"))
    rutas = await _rutas(waha)
    assert "all" not in rutas and f"{X}@c.us" not in rutas and set(rutas) == {f"{A}@c.us", f"{B}@c.us"}
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT count(*) FROM wa_messages") == 2
    assert "personal" not in {c["texto"] for c in await fuente_fetch(ctx_waha, esc["tenant_id"],
                                                                     "SELECT texto FROM wa_message_bodies")}


async def test_dos_pasadas_iguales_dan_cobertura_estable(ctx_waha, waha):
    esc, _ = await _preparar(ctx_waha, waha)
    await backfill.ejecutar(ctx_waha, job_de(esc, "backfill"))
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET conectado_at = conectado_at - interval '31 minutes' WHERE id = $1",
                          esc["link_id"])
    assert await backfill.ejecutar(ctx_waha, job_de(esc, "backfill")) == "hecho"
    link = await _link(ctx_waha, esc)
    assert (link["backfill_pasada"], link["cobertura"], link["ingesta"]) == (2, "estable", "al_dia")
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT count(*) FROM product_events WHERE evento = 'importacion_estable'") == 1


async def test_falla_de_un_chat_queda_contada_y_no_frena_a_los_demas(ctx_waha, waha):
    esc, _ = await _preparar(ctx_waha, waha, excluir_x=True)
    waha.falla_mensajes_de.add(f"{B}@c.us")
    await backfill.ejecutar(ctx_waha, job_de(esc, "backfill"))
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        estados = sorted(f["estado"] for f in await con.fetch("SELECT estado FROM ingesta_conteos WHERE pasada = 1"))
        assert await con.fetchval("SELECT count(*) FROM wa_messages") == 2
    assert estados == ["fallido", "ok"]


async def test_antes_de_tiempo_espera_y_reimporta_chats_incluidos_despues(ctx_waha, waha):
    esc, conectado = await _preparar(ctx_waha, waha, excluir_x=True)
    await backfill.ejecutar(ctx_waha, job_de(esc, "backfill"))
    nuevo = "5493415555555"
    chat = await chat_de_prueba(ctx_waha, esc, nuevo, estado="incluido")
    waha.mensajes[esc["session_name"]].append(msg_crudo(nuevo, 9, ts=conectado - 60, body="consulta"))
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET reimportar_chats = ARRAY[$2::uuid] WHERE id = $1", esc["link_id"], chat)
    espera = await backfill.ejecutar(ctx_waha, job_de(esc, "backfill"))
    assert 0 < espera <= 1800
    link = await _link(ctx_waha, esc)
    assert link["reimportar_chats"] == [] and link["backfill_pasada"] == 1
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        assert await con.fetchval("SELECT count(*) FROM wa_messages WHERE chat_uuid = $1", chat) == 1


async def test_vinculo_caido_espera_y_cerrado_termina(ctx_waha, waha):
    esc, _ = await _preparar(ctx_waha, waha)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET estado = 'caido' WHERE id = $1", esc["link_id"])
    assert await backfill.ejecutar(ctx_waha, job_de(esc, "backfill")) == backfill.ESPERA_CAIDO_S
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET estado = 'cerrado' WHERE id = $1", esc["link_id"])
    assert await backfill.ejecutar(ctx_waha, job_de(esc, "backfill")) == "hecho"
    assert waha.params == []
```

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_backfill.py -q`
Expected: FAIL con `ImportError: cannot import name 'backfill' from 'app.radar.ingesta'`.

- [ ] **Step 3: Implementación**

Crear `app/radar/ingesta/backfill.py`:

```python
"""
Backfill en pasadas (§6.3 punto 2), un job por vínculo.

- Ventanas fijadas por preparar_importacion (§6.3 punto 4): lte = t_vínculo
  (conectado_at) y gte = inicio del período, lo que la línea ya tenía o lo purgado.
- Pasada 1 sin exclusiones (ni chats excluidos en la línea ni supresiones en el
  tenant): chats/all/messages. Cualquier otro caso: chat por chat, solo incluidos
  y pendientes, nunca excluidos ni suprimidos.
- offset += limit hasta una página vacía (lectura.paginar_mensajes).
- Repetición a +30 min, +6 h y +24 h del vínculo con upsert idempotente; se corta
  cuando dos pasadas consecutivas dan el mismo conteo por chat (cobertura
  'estable'). Si se agotan las pasadas sin coincidir, queda 'provisoria'.
- Checkpoint por chat: en una pasada chat por chat, un chat con fila 'ok' no se
  vuelve a leer si el job se reintenta; uno que falla queda 'fallido' y la
  pasada sigue con los demás.
- Chats incluidos después de P4 (links.reimportar_chats) se releen completos antes
  de esperar la próxima pasada.
"""

import json
import uuid
from datetime import datetime
from typing import Literal, Optional, Union

from app.radar import eventos_producto
from app.radar.contexto import RadarContexto
from app.radar.ingesta import reglas
from app.radar.ingesta.almacen import guardar_lote
from app.radar.ingesta.identidad import ChatRef, jids_de_chat, leer_chat, resolver_chat
from app.radar.ingesta.lectura import MapaLids, Presupuesto, cliente_lectura, paginar_mensajes
from app.radar.jobs import Job
from app.radar.waha.cliente import WahaCliente, WahaError

ESPERA_CAIDO_S = 300.0
_LINK = ("SELECT l.id, l.line_id, l.worker_id, l.session_name, l.estado, l.ingesta, l.conectado_at, "
         "l.ventanas_backfill, l.backfill_pasada, l.reimportar_chats, now() AS ahora "
         "FROM links l WHERE l.id = $1")


async def importar_chat(ctx: RadarContexto, cli: WahaCliente, tenant_id: uuid.UUID, link, chat: ChatRef,
                        ventanas: list[tuple[int, Optional[int]]], *, via: str,
                        presupuesto: Optional[Presupuesto] = None) -> int:
    """Lee un chat incluido o pendiente por cada uno de sus JID y cada ventana. Devuelve
    cuántos mensajes leyó (para la contabilidad por pasada)."""
    if chat.estado == "excluido" or chat.suprimido:
        return 0
    leidos = 0
    for jid in await jids_de_chat(ctx, tenant_id, link["line_id"], chat.id):
        for gte, lte in ventanas:
            async for pagina in paginar_mensajes(cli, link["session_name"], jid, gte=gte, lte=lte,
                                                 presupuesto=presupuesto):
                await guardar_lote(ctx, tenant_id=tenant_id, line_id=link["line_id"], link_id=link["id"],
                                   chat=chat, mensajes=pagina, via=via)
                leidos += len(pagina)
    return leidos


async def _hay_exclusiones(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID) -> bool:
    async with ctx.db.tenant_tx(tenant_id) as con:
        return await con.fetchval(
            "SELECT EXISTS (SELECT 1 FROM wa_chats WHERE line_id = $1 AND estado = 'excluido') "
            "OR EXISTS (SELECT 1 FROM suppressions)", line_id)


async def _registrar(ctx: RadarContexto, tenant_id: uuid.UUID, link_id: uuid.UUID, pasada: int,
                     conteos: dict[uuid.UUID, int], estado: str = "ok") -> None:
    async with ctx.db.tenant_tx(tenant_id) as con:
        for chat_id, mensajes in conteos.items():
            await con.execute(
                "INSERT INTO ingesta_conteos (link_id, chat_uuid, pasada, mensajes, estado) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (link_id, chat_uuid, pasada) DO UPDATE SET mensajes = EXCLUDED.mensajes, "
                "estado = EXCLUDED.estado, updated_at = now()", link_id, chat_id, pasada, mensajes, estado)


async def _pasada_todos(ctx: RadarContexto, cli: WahaCliente, tenant_id: uuid.UUID, link,
                        ventanas: list[tuple[int, Optional[int]]], pasada: int, lids: MapaLids) -> None:
    conteos: dict[uuid.UUID, int] = {}
    refs: dict[str, ChatRef] = {}
    for gte, lte in ventanas:
        async for pagina in paginar_mensajes(cli, link["session_name"], "all", gte=gte, lte=lte):
            por_chat: dict[str, list] = {}
            for m in pagina:
                por_chat.setdefault(m.chat.crudo(), []).append(m)
            for clave, mensajes in por_chat.items():
                if clave not in refs:
                    refs[clave] = await resolver_chat(ctx, tenant_id=tenant_id, line_id=link["line_id"],
                                                      jid=mensajes[0].chat, lids=lids)
                ref = refs[clave]
                if ref.estado == "excluido" or ref.suprimido:
                    continue
                await guardar_lote(ctx, tenant_id=tenant_id, line_id=link["line_id"], link_id=link["id"],
                                   chat=ref, mensajes=mensajes, via="backfill")
                conteos[ref.id] = conteos.get(ref.id, 0) + len(mensajes)
    async with ctx.db.tenant_tx(tenant_id) as con:
        sin_mensajes = [f["id"] for f in await con.fetch(
            "SELECT id FROM wa_chats WHERE line_id = $1 AND estado <> 'excluido'", link["line_id"])]
    await _registrar(ctx, tenant_id, link["id"], pasada, {**{c: 0 for c in sin_mensajes}, **conteos})


async def _pasada_por_chat(ctx: RadarContexto, cli: WahaCliente, tenant_id: uuid.UUID, link,
                           ventanas: list[tuple[int, Optional[int]]], pasada: int) -> None:
    async with ctx.db.tenant_tx(tenant_id) as con:
        pendientes = [f["id"] for f in await con.fetch(
            "SELECT c.id FROM wa_chats c WHERE c.line_id = $1 AND c.estado <> 'excluido' AND NOT EXISTS ("
            "SELECT 1 FROM ingesta_conteos k WHERE k.link_id = $2 AND k.chat_uuid = c.id AND k.pasada = $3 "
            "AND k.estado = 'ok') ORDER BY c.created_at", link["line_id"], link["id"], pasada)]
    for chat_id in pendientes:
        ref = await leer_chat(ctx, tenant_id, chat_id)
        if ref is None or ref.suprimido:
            continue
        try:
            n = await importar_chat(ctx, cli, tenant_id, link, ref, ventanas, via="backfill")
            await _registrar(ctx, tenant_id, link["id"], pasada, {ref.id: n})
        except WahaError:
            await _registrar(ctx, tenant_id, link["id"], pasada, {ref.id: 0}, estado="fallido")


async def _reimportar(ctx: RadarContexto, cli: WahaCliente, tenant_id: uuid.UUID, link,
                      ventanas: list[tuple[int, Optional[int]]]) -> None:
    posterior = [(int(link["conectado_at"].timestamp()), None)]      # también lo capturado después del vínculo
    for chat_id in list(link["reimportar_chats"] or []):
        ref = await leer_chat(ctx, tenant_id, chat_id)
        if ref is not None and ref.estado == "incluido" and not ref.suprimido:
            await importar_chat(ctx, cli, tenant_id, link, ref, ventanas + posterior, via="backfill")
        async with ctx.db.tenant_tx(tenant_id) as con:
            await con.execute("UPDATE links SET reimportar_chats = array_remove(reimportar_chats, $2) WHERE id = $1",
                              link["id"], chat_id)


def _por_chat(filas) -> dict[uuid.UUID, int]:
    return {f["chat_uuid"]: f["mensajes"] for f in filas}


async def _cerrar_pasada(ctx: RadarContexto, tenant_id: uuid.UUID, link,
                         pasada: int) -> Union[Literal["hecho"], float]:
    sql = "SELECT chat_uuid, mensajes FROM ingesta_conteos WHERE link_id = $1 AND pasada = $2 AND estado = 'ok'"
    async with ctx.db.tenant_tx(tenant_id) as con:
        actual = _por_chat(await con.fetch(sql, link["id"], pasada))
        anterior = _por_chat(await con.fetch(sql, link["id"], pasada - 1)) if pasada > 1 else None
        estable = anterior is not None and reglas.conteos_coinciden(anterior, actual)
        proxima = None if estable else reglas.proxima_pasada(link["conectado_at"], pasada)
        await con.execute(
            "UPDATE links SET backfill_pasada = $2, cobertura = $3, updated_at = now(), "
            "ingesta = CASE WHEN $4 THEN 'al_dia' ELSE ingesta END WHERE id = $1",
            link["id"], pasada, "estable" if estable else "provisoria", proxima is None)
        if estable:
            await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="importacion_estable",
                                                    line_id=link["line_id"], objeto_id=link["id"],
                                                    valores={"pasadas": pasada, "chats": len(actual)})
        ahora: datetime = await con.fetchval("SELECT now()")
    if proxima is None:
        return "hecho"
    return max(1.0, (proxima - ahora).total_seconds())


async def ejecutar(ctx: RadarContexto, job: Job) -> Union[Literal["hecho"], float]:
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        link = await con.fetchrow(_LINK, job.link_id)
    if link is None or link["ingesta"] not in ("importando", "al_dia") or link["estado"] in ("cerrando", "cerrado",
                                                                                              "abortado"):
        return "hecho"
    if link["estado"] != "vinculado":
        return ESPERA_CAIDO_S
    ventanas = [(int(a), int(b)) for a, b in json.loads(link["ventanas_backfill"] or "[]")]
    proxima = reglas.proxima_pasada(link["conectado_at"], link["backfill_pasada"])
    async with cliente_lectura(ctx, link_id=link["id"], worker_id=link["worker_id"]) as cli:
        if link["reimportar_chats"]:
            await _reimportar(ctx, cli, job.tenant_id, link, ventanas)
        if proxima is None:
            return "hecho"
        if link["ahora"] < proxima:
            return max(1.0, (proxima - link["ahora"]).total_seconds())
        pasada = link["backfill_pasada"] + 1
        if pasada == 1 and not await _hay_exclusiones(ctx, job.tenant_id, link["line_id"]):
            await _pasada_todos(ctx, cli, job.tenant_id, link, ventanas, pasada, MapaLids(cli, link["session_name"]))
        else:
            await _pasada_por_chat(ctx, cli, job.tenant_id, link, ventanas, pasada)
    return await _cerrar_pasada(ctx, job.tenant_id, link, pasada)
```

En `app/radar/worker.py`: `from app.radar.ingesta import backfill, conteo, inbox` y `"backfill": backfill.ejecutar,` en `HANDLERS`.

- [ ] **Step 4: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_backfill.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/ingesta/backfill.py app/radar/worker.py tests/radar_tests/test_ingesta_backfill.py
git commit -m "Radar tramo 3: backfill en pasadas con checkpoint por chat y cobertura provisoria o estable

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Job `reconciliacion` — dos pasos, `observado_hasta` y sesión conectada sin tráfico

**Files:**
- Create: `app/radar/ingesta/reconciliacion.py`
- Modify: `app/radar/worker.py`, `app/radar/salud.py` (docstring)
- Test: `tests/radar_tests/test_ingesta_reconciliacion.py`

**Interfaces:**
- Consumes: `cliente_lectura`, `paginar_chats`, `MapaLids`, `Presupuesto`, `PresupuestoAgotado` (Task 6); `resolver_chat`, `leer_chat` (Task 6); `backfill.importar_chat` (Task 13); `inbox.drenar` (Task 9); `reglas.MARGEN`, `reglas.cadencia_s`, `reglas.hay_silencio`, `reglas.intervalos_en_horario`, `reglas.puede_reiniciar`, `reglas.HORA_ARGENTINA` (Task 2); `restriccion_activa(hasta, sin_fecha, ahora)` (`app/radar/vinculo_estados.py`); `leer_worker`, `cliente_de`, `ClaveAdminAusente` (`app/radar/workers.py`); `WahaCliente.reiniciar_sesion(nombre)`; `fin_vinculo._emails_duenos(con)`, `fin_vinculo._avisar(ctx, destinatarios, asunto, texto, link_id)`.
- Produces: `PRESUPUESTO_PAGINAS = 200`, `CONTINUAR_S = 60.0`, `AVISO_TRAS_REINICIO = timedelta(hours=1)`, `ASUNTO_SILENCIO`, `TEXTO_SILENCIO`; `reconciliacion.ejecutar(ctx, job) -> Union[Literal["hecho"], float]`; `HANDLERS["reconciliacion"]`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_ingesta_reconciliacion.py`:

```python
"""
Reconciliación en dos pasos (§6.3 punto 3): nunca chats/all/messages; paso 1 por
GET /chats hasta observado_hasta − 10 min; paso 2 solo incluidos y pendientes; los
excluidos y suprimidos no se leen nunca; presupuesto de páginas; observado_hasta.
Sesión conectada sin tráfico (Estados especiales): un reinicio cada 24 h como
máximo, nunca con restricción, y aviso si no se recupera.
"""
import time

from app.radar.ingesta import reconciliacion

from .helpers import chat_de_prueba, job_de, preparar_ingesta
from .waha_falso import chat_crudo, msg_crudo

A, X, P, V = "5493411111111", "5493419999999", "5493413333333", "5493417777777"


async def _escenario(ctx, waha):
    esc = await preparar_ingesta(ctx, waha, ingesta="al_dia")
    await chat_de_prueba(ctx, esc, A, nombre="Marta")
    await chat_de_prueba(ctx, esc, X, estado="excluido")
    await chat_de_prueba(ctx, esc, P, estado="pendiente")
    ahora = int(time.time())
    s = esc["session_name"]
    waha.chats[s] = [chat_crudo(A, ts=ahora - 60), chat_crudo(X, ts=ahora - 60), chat_crudo(P, ts=ahora - 120),
                     chat_crudo(V, ts=ahora - 3 * 3600)]
    waha.mensajes[s] = [msg_crudo(A, 1, ts=ahora - 60, body="¿abren hoy?"),
                        msg_crudo(X, 2, ts=ahora - 60, body="personal"),
                        msg_crudo(P, 3, ts=ahora - 120, body="consulta nueva"),
                        msg_crudo(V, 4, ts=ahora - 3 * 3600, body="viejo")]
    return esc


async def _link(ctx, esc):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow("SELECT * FROM links WHERE id = $1", esc["link_id"])


def _leidos(waha):
    return [p["ruta"].rsplit("/", 2)[-2] for p in waha.params if p["ruta"].endswith("/messages")]


async def test_dos_pasos_sin_leer_excluidos_ni_usar_all(ctx_waha, waha):
    esc = await _escenario(ctx_waha, waha)
    espera = await reconciliacion.ejecutar(ctx_waha, job_de(esc, "reconciliacion"))
    assert espera in (900.0, 3600.0)
    leidos = _leidos(waha)
    assert set(leidos) == {f"{A}@c.us", f"{P}@c.us"} and "all" not in leidos and f"{X}@c.us" not in leidos
    assert all("filter.timestamp.gte" in p and "filter.timestamp.lte" not in p
               for p in waha.params if p["ruta"].endswith("/messages"))
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        filas = await con.fetch("SELECT c.estado, m.solo_metadatos, m.via FROM wa_messages m "
                                "JOIN wa_chats c ON c.id = m.chat_uuid ORDER BY c.estado")
        viejos = await con.fetchval("SELECT count(*) FROM wa_chats")
    assert [tuple(f) for f in filas] == [("incluido", False, "reconciliacion"), ("pendiente", True, "reconciliacion")]
    assert viejos == 3                                   # el chat de hace 3 h quedó fuera del paso 1
    link = await _link(ctx_waha, esc)
    assert link["observado_hasta"] == link["reconciliado_at"] and link["recon_pendientes"] is None


async def test_presupuesto_agotado_continua_en_la_siguiente(ctx_waha, waha, monkeypatch):
    esc = await _escenario(ctx_waha, waha)
    monkeypatch.setattr(reconciliacion, "PRESUPUESTO_PAGINAS", 2)
    assert await reconciliacion.ejecutar(ctx_waha, job_de(esc, "reconciliacion")) == reconciliacion.CONTINUAR_S
    link = await _link(ctx_waha, esc)
    assert len(link["recon_pendientes"]) == 2 and link["observado_hasta"] is None and link["cobertura"] == "provisoria"
    monkeypatch.setattr(reconciliacion, "PRESUPUESTO_PAGINAS", 200)
    await reconciliacion.ejecutar(ctx_waha, job_de(esc, "reconciliacion"))
    link = await _link(ctx_waha, esc)
    assert link["recon_pendientes"] is None and link["observado_hasta"] is not None


async def test_silencio_reinicia_una_vez_avisa_y_se_recupera(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta="al_dia")
    s = esc["session_name"]
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET conectado_at = now() - interval '3 days', "
                          "observado_hasta = now() - interval '3 days' WHERE id = $1", esc["link_id"])
    await reconciliacion.ejecutar(ctx_waha, job_de(esc, "reconciliacion"))
    link = await _link(ctx_waha, esc)
    assert link["silencio_desde"] is not None and link["reinicio_silencio_at"] is not None
    assert link["observado_hasta"] <= link["conectado_at"]                      # no avanza sin tráfico
    assert waha.llamadas.count(f"POST /api/sessions/{s}/restart") == 1
    await reconciliacion.ejecutar(ctx_waha, job_de(esc, "reconciliacion"))
    assert waha.llamadas.count(f"POST /api/sessions/{s}/restart") == 1        # como máximo uno cada 24 h
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET silencio_desde = now() - interval '3 hours', "
                          "reinicio_silencio_at = now() - interval '2 hours' WHERE id = $1", esc["link_id"])
    antes = len(ctx_waha.mailer.enviados)
    await reconciliacion.ejecutar(ctx_waha, job_de(esc, "reconciliacion"))
    assert len(ctx_waha.mailer.enviados) == antes + 1
    assert "No recibimos mensajes desde el" in ctx_waha.mailer.enviados[-1].texto
    await chat_de_prueba(ctx_waha, esc, A)
    ahora = int(time.time())
    waha.chats[s] = [chat_crudo(A, ts=ahora - 30)]
    waha.mensajes[s] = [msg_crudo(A, 1, ts=ahora - 30, body="hola")]
    await reconciliacion.ejecutar(ctx_waha, job_de(esc, "reconciliacion"))
    link = await _link(ctx_waha, esc)
    assert link["silencio_desde"] is None and link["observado_hasta"] == link["reconciliado_at"]


async def test_con_restriccion_activa_no_reinicia(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta="al_dia")
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET conectado_at = now() - interval '3 days', "
                          "restriccion_hasta = now() + interval '1 day' WHERE id = $1", esc["link_id"])
    await reconciliacion.ejecutar(ctx_waha, job_de(esc, "reconciliacion"))
    assert not any(x.endswith("/restart") for x in waha.llamadas)
    assert (await _link(ctx_waha, esc))["silencio_desde"] is not None


async def test_solo_corre_con_el_vinculo_importando(ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta="pendiente_de_seleccion")
    assert await reconciliacion.ejecutar(ctx_waha, job_de(esc, "reconciliacion")) == "hecho"
    assert waha.params == []
```

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_ingesta_reconciliacion.py -q`
Expected: FAIL con `ImportError: cannot import name 'reconciliacion' from 'app.radar.ingesta'`.

- [ ] **Step 3: Implementación**

Crear `app/radar/ingesta/reconciliacion.py`:

```python
"""
Reconciliación en dos pasos mientras dure el vínculo (§6.3 punto 3). Nunca usa
chats/all/messages: traería a nuestro proceso el cuerpo de los chats excluidos.

- Paso 1: GET /api/{s}/chats por último mensaje descendente, hasta el primer chat
  anterior a observado_hasta − 10 min. Solo id, nombre y fecha.
- Paso 2: para los chats incluidos y los pendientes de decisión (nunca excluidos ni
  suprimidos), GET /chats/{id}/messages con filter.timestamp.gte, downloadMedia=false
  y limit fijo, hasta una página vacía.
- Presupuesto de páginas por corrida; si se agota, los chats que faltan quedan en
  links.recon_pendientes, la cobertura en 'provisoria' y la siguiente sigue en 1 min.
- Cadencia 15 min en horario / 60 fuera, con el desfase fijo por línea que puso
  radar_jobs_programar_ingesta(). Cubre solo lo posterior a t_vínculo.
- observado_hasta = inicio de la última corrida completa con la sesión WORKING y
  con tráfico. Con sospecha de silencio deja de avanzar y vuelve al último evento
  real (Estados especiales); un reinicio automático cada 24 h como máximo y nunca
  con restricción activa; si a la hora sigue mudo, un email al dueño.
"""

import logging
import uuid
from datetime import timedelta
from typing import Literal, Union

from app.radar import fin_vinculo
from app.radar.contexto import RadarContexto
from app.radar.ingesta import inbox, reglas
from app.radar.ingesta.backfill import importar_chat
from app.radar.ingesta.identidad import leer_chat, resolver_chat
from app.radar.ingesta.lectura import MapaLids, Presupuesto, PresupuestoAgotado, cliente_lectura, paginar_chats
from app.radar.jobs import Job
from app.radar.vinculo_estados import restriccion_activa
from app.radar.waha.cliente import WahaError
from app.radar.workers import ClaveAdminAusente, cliente_de, leer_worker

logger = logging.getLogger("app.radar.ingesta.reconciliacion")

PRESUPUESTO_PAGINAS = 200
CONTINUAR_S = 60.0
AVISO_TRAS_REINICIO = timedelta(hours=1)
ASUNTO_SILENCIO = "No recibimos mensajes de tu WhatsApp"
TEXTO_SILENCIO = (
    "No recibimos mensajes desde el {fecha}. Revisá que el teléfono tenga conexión.\n\n"
    "Mientras tanto, ese tramo figura como posible hueco en el tablero.\n\n{url}\n"
)
_LINK = ("SELECT l.id, l.line_id, l.worker_id, l.session_name, l.estado, l.waha_status, l.ingesta, l.conectado_at, "
         "l.observado_hasta, l.recon_pendientes, now() AS ahora FROM links l WHERE l.id = $1")


async def _hay_silencio(ctx: RadarContexto, tenant_id: uuid.UUID, link, ahora) -> bool:
    async with ctx.db.tenant_tx(tenant_id) as con:
        fila = await con.fetchrow("SELECT ultimo_msg_at, conectado_at FROM links WHERE id = $1", link["id"])
        entrantes = [f["provider_ts"] for f in await con.fetch(
            "SELECT provider_ts FROM wa_messages WHERE line_id = $1 AND NOT from_me "
            "AND provider_ts > $2::timestamptz - interval '28 days' ORDER BY provider_ts", link["line_id"], ahora)]
    return reglas.hay_silencio(ultimo_msg=fila["ultimo_msg_at"] or fila["conectado_at"], ahora=ahora,
                               intervalos=reglas.intervalos_en_horario(entrantes))


async def _actuar_silencio(ctx: RadarContexto, tenant_id: uuid.UUID, link, ahora) -> None:
    logger.warning("ALERTA sospecha de silencio en el vínculo %s", link["id"])
    async with ctx.db.tenant_tx(tenant_id) as con:
        f = await con.fetchrow("SELECT silencio_desde, reinicio_silencio_at, aviso_silencio_at, ultimo_msg_at, "
                               "conectado_at, restriccion_hasta, restriccion_sin_fecha FROM links WHERE id = $1",
                               link["id"])
    restringida = restriccion_activa(f["restriccion_hasta"], f["restriccion_sin_fecha"], ahora)
    if reglas.puede_reiniciar(f["reinicio_silencio_at"], ahora, restringida):
        try:
            worker = await leer_worker(ctx, link["worker_id"])
            async with cliente_de(ctx, worker) as cli:
                await cli.reiniciar_sesion(link["session_name"])
        except (WahaError, ClaveAdminAusente, LookupError) as e:
            logger.warning("reinicio por silencio del vínculo %s falló: %s", link["id"], type(e).__name__)
            return
        async with ctx.db.tenant_tx(tenant_id) as con:
            await con.execute("UPDATE links SET reinicio_silencio_at = now(), updated_at = now() WHERE id = $1",
                              link["id"])
        return
    if (f["reinicio_silencio_at"] is not None and f["aviso_silencio_at"] is None
            and f["reinicio_silencio_at"] >= f["silencio_desde"]
            and ahora - f["reinicio_silencio_at"] >= AVISO_TRAS_REINICIO):
        async with ctx.db.tenant_tx(tenant_id) as con:
            duenos = await fin_vinculo._emails_duenos(con)
        ultimo = f["ultimo_msg_at"] or f["conectado_at"]
        fecha = ultimo.astimezone(reglas.HORA_ARGENTINA).strftime("%d/%m %H:%M")
        url = ctx.settings.public_base_url.rstrip("/") + "/radar/"
        await fin_vinculo._avisar(ctx, duenos, ASUNTO_SILENCIO, TEXTO_SILENCIO.format(fecha=fecha, url=url),
                                  link["id"])
        async with ctx.db.tenant_tx(tenant_id) as con:
            await con.execute("UPDATE links SET aviso_silencio_at = now(), updated_at = now() WHERE id = $1",
                              link["id"])


async def ejecutar(ctx: RadarContexto, job: Job) -> Union[Literal["hecho"], float]:
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        link = await con.fetchrow(_LINK, job.link_id)
    if link is None or link["estado"] != "vinculado" or link["ingesta"] not in ("importando", "al_dia"):
        return "hecho"
    t0 = link["ahora"]
    base = max(link["observado_hasta"] or link["conectado_at"], link["conectado_at"])
    desde_ts = int((base - reglas.MARGEN).timestamp())
    presupuesto = Presupuesto(PRESUPUESTO_PAGINAS)
    cola_chats: list[uuid.UUID] = list(link["recon_pendientes"] or [])
    hechos = 0
    completo = True
    async with cliente_lectura(ctx, link_id=link["id"], worker_id=link["worker_id"]) as cli:
        lids = MapaLids(cli, link["session_name"])
        try:
            async for pagina in paginar_chats(cli, link["session_name"], corte_ts=desde_ts, presupuesto=presupuesto):
                for c in pagina:
                    if c.ultimo_ts is not None and c.ultimo_ts < desde_ts:
                        continue
                    ref = await resolver_chat(ctx, tenant_id=job.tenant_id, line_id=link["line_id"], jid=c.jid,
                                              lids=lids, nombre=c.nombre)
                    if ref.estado != "excluido" and not ref.suprimido and ref.id not in cola_chats:
                        cola_chats.append(ref.id)
            for chat_id in cola_chats:
                ref = await leer_chat(ctx, job.tenant_id, chat_id)
                if ref is not None:
                    await importar_chat(ctx, cli, job.tenant_id, link, ref, [(desde_ts, None)], via="reconciliacion",
                                        presupuesto=presupuesto)
                hechos += 1
        except PresupuestoAgotado:
            completo = False
    await inbox.drenar(ctx, tenant_id=job.tenant_id, link_id=link["id"])
    if not completo:
        async with ctx.db.tenant_tx(job.tenant_id) as con:
            await con.execute("UPDATE links SET recon_pendientes = $2, cobertura = 'provisoria', updated_at = now() "
                              "WHERE id = $1", link["id"], cola_chats[hechos:])
        return CONTINUAR_S
    silencio = await _hay_silencio(ctx, job.tenant_id, link, t0)
    avanza = not silencio and link["waha_status"] == "WORKING"
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        await con.execute(
            """
            UPDATE links SET recon_pendientes = NULL, reconciliado_at = $2, updated_at = now(),
                   observado_hasta = CASE WHEN $4 THEN $2
                                          WHEN $3 THEN LEAST(COALESCE(observado_hasta, $2),
                                                             COALESCE(ultimo_msg_at, conectado_at))
                                          ELSE observado_hasta END,
                   silencio_desde = CASE WHEN $3 THEN COALESCE(silencio_desde, $2) ELSE NULL END,
                   aviso_silencio_at = CASE WHEN $3 THEN aviso_silencio_at ELSE NULL END
             WHERE id = $1
            """, link["id"], t0, silencio, avanza)
    if silencio:
        await _actuar_silencio(ctx, job.tenant_id, link, t0)
    return float(reglas.cadencia_s(t0))
```

En `app/radar/worker.py`: `from app.radar.ingesta import backfill, conteo, inbox, reconciliacion` y `"reconciliacion": reconciliacion.ejecutar,` en `HANDLERS`.

En `app/radar/salud.py`, reemplazar la línea del docstring `La "sospecha de silencio" necesita tráfico y llega en el tramo 3.` por `La "sospecha de silencio" necesita tráfico: vive en app/radar/ingesta/reconciliacion.py.`

- [ ] **Step 4: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_ingesta_reconciliacion.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/ingesta/reconciliacion.py app/radar/worker.py app/radar/salud.py tests/radar_tests/test_ingesta_reconciliacion.py
git commit -m "Radar tramo 3: reconciliación en dos pasos con observado_hasta, presupuesto y regla de silencio

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: C1 de la Consola sin "—" en último mensaje, sincronización y huecos; semáforo con tráfico

**Files:**
- Modify: `app/radar/consola.py`, `app/radar/vinculo_estados.py`, `tests/radar_tests/test_consola_api.py`, `tests/radar_tests/test_vinculo_estados.py`
- Test: `tests/radar_tests/test_consola_ingesta.py`

**Interfaces:**
- Consumes: `radar_admin_consola_lineas()` con las columnas nuevas (Task 4); `fila_consola(f, ahora)`, `listar_lineas_consola(ctx, *, estado=None, tenant_id=None)` (tramo 2).
- Produces: `PENDIENTES = {"gasto_ia_mes": "tramo 5"}`; `sincronizacion(f: dict) -> Optional[str]`; `huecos(f: dict) -> Optional[int]`; en cada fila de C1: `ultimo_mensaje` (ISO), `sincronizacion` (`"contando chats"`, `"esperando selección"`, `"provisoria 78 %"`, `"estable 100 %"`…), `huecos` (chats con importación fallida en la última pasada + 1 si hay sospecha de silencio), `chats_nuevos`, `posible_hueco_desde`; `semaforo()` amarillo con ingesta en curso, silencio o cobertura < 90 %.

- [ ] **Step 1: Tests que fallan**

Crear `tests/radar_tests/test_consola_ingesta.py`:

```python
"""C1 (§3.1) con los datos de la ingesta: último mensaje, sincronización, huecos y semáforo."""
from app.radar.constantes import TENANT_KIS

from .helpers import chat_de_prueba, crear_usuario, entrar, preparar_ingesta

A, B = "5493411111111", "5493412222222"


async def _fila(cliente, ctx):
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    [f] = (await cliente.get("/radar/admin/consola/lineas")).json()
    return f


async def test_linea_al_dia_con_trafico_en_verde(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta="al_dia")
    a = await chat_de_prueba(ctx_waha, esc, A)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET backfill_pasada = 2, cobertura = 'estable', ultimo_msg_at = now() "
                          "WHERE id = $1", esc["link_id"])
        await con.execute("INSERT INTO ingesta_conteos (link_id, chat_uuid, pasada, mensajes, estado) "
                          "VALUES ($1, $2, 2, 5, 'ok')", esc["link_id"], a)
    f = await _fila(cliente, ctx_waha)
    assert (f["sincronizacion"], f["huecos"], f["semaforo"]) == ("estable 100 %", 0, "verde")
    assert f["ultimo_mensaje"] is not None and f["pendiente"] == {"gasto_ia_mes": "tramo 5"}


async def test_huecos_y_silencio_en_amarillo(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta="al_dia")
    a = await chat_de_prueba(ctx_waha, esc, A)
    b = await chat_de_prueba(ctx_waha, esc, B)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET backfill_pasada = 1, cobertura = 'provisoria', "
                          "silencio_desde = now() WHERE id = $1", esc["link_id"])
        await con.execute("INSERT INTO ingesta_conteos (link_id, chat_uuid, pasada, mensajes, estado) "
                          "VALUES ($1, $2, 1, 3, 'ok'), ($1, $3, 1, 0, 'fallido')", esc["link_id"], a, b)
    f = await _fila(cliente, ctx_waha)
    assert (f["sincronizacion"], f["huecos"], f["semaforo"]) == ("provisoria 50 %", 2, "amarillo")
    assert f["posible_hueco_desde"] is not None


async def test_contando_y_esperando_seleccion(cliente, ctx_waha, waha):
    esc = await preparar_ingesta(ctx_waha, waha, ingesta="contando")
    f = await _fila(cliente, ctx_waha)
    assert (f["sincronizacion"], f["semaforo"]) == ("contando chats", "amarillo")
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET ingesta = 'pendiente_de_seleccion' WHERE id = $1", esc["link_id"])
    assert (await cliente.get("/radar/admin/consola/lineas")).json()[0]["sincronizacion"] == "esperando selección"
```

En `tests/radar_tests/test_consola_api.py`, en `test_lista_todas_las_lineas_con_semaforo_y_pendientes`, reemplazar

```python
    assert (por["Farmacia A"]["semaforo"], por["Farmacia A"]["numero"]) == ("verde", "…4567")
    assert (por["Farmacia B"]["semaforo"], por["Farmacia B"]["link_estado"]) == ("gris", None)
    assert por["Farmacia A"]["pendiente"] == {"ultimo_mensaje": "tramo 3", "sincronizacion": "tramo 3",
                                              "huecos": "tramo 3", "gasto_ia_mes": "tramo 5"}
```

por

```python
    # Recién vinculada: el conteo de P4 todavía no empezó (sincronizando = amarillo, §3.1).
    assert (por["Farmacia A"]["semaforo"], por["Farmacia A"]["numero"]) == ("amarillo", "…4567")
    assert por["Farmacia A"]["sincronizacion"] == "esperando conteo" and por["Farmacia A"]["huecos"] == 0
    assert (por["Farmacia B"]["semaforo"], por["Farmacia B"]["link_estado"]) == ("gris", None)
    assert por["Farmacia B"]["sincronizacion"] is None and por["Farmacia B"]["huecos"] is None
    assert por["Farmacia A"]["pendiente"] == {"gasto_ia_mes": "tramo 5"}
```

En `tests/radar_tests/test_vinculo_estados.py`, agregar a la parametrización de `test_semaforo` (antes del `])`):

```python
    (_fila(ingesta=None), "amarillo"),
    (_fila(ingesta="importando"), "amarillo"),
    (_fila(ingesta="al_dia", silencio_desde=AHORA), "amarillo"),
    (_fila(ingesta="al_dia", chats_total=10, chats_importados=8), "amarillo"),
    (_fila(ingesta="al_dia", chats_total=10, chats_importados=9), "verde"),
```

- [ ] **Step 2: Correr y ver que falla**

Run: `python -m pytest tests/radar_tests/test_consola_ingesta.py tests/radar_tests/test_consola_api.py tests/radar_tests/test_vinculo_estados.py -q`
Expected: FAIL (`sincronizacion` sigue en null con `pendiente` "tramo 3", y el semáforo da verde con ingesta en curso).

- [ ] **Step 3: Semáforo**

En `app/radar/vinculo_estados.py`, reemplazar el docstring y el bloque `if estado == "vinculado":` de `semaforo` por:

```python
    """C1 (§3.1): verde = WORKING con tráfico; amarillo = sincronizando, sospecha de
    silencio o cobertura < 90 %; rojo = caída, FAILED, restricción o worker lleno.
    Una fila sin las claves de la ingesta (tramo 2) se evalúa como al día."""
```

```python
    if estado == "vinculado":
        ws = fila.get("waha_status")
        if ws == "FAILED":
            return "rojo"
        if ws != "WORKING":
            return "amarillo"
        if fila.get("silencio_desde") or fila.get("ingesta", "al_dia") in (None, "contando", "pendiente_de_seleccion",
                                                                            "importando"):
            return "amarillo"
        total = fila.get("chats_total") or 0
        if total and (fila.get("chats_importados") or 0) < UMBRAL_COBERTURA * total:
            return "amarillo"
        return "verde"
```

y debajo de `UMBRAL_WORKER_LLENO = 0.8` agregar `UMBRAL_COBERTURA = 0.9   # §3.1 y Estados especiales: nunca "completo" con < 90 %`.

- [ ] **Step 4: C1**

En `app/radar/consola.py`:

1. Reemplazar desde el inicio del archivo hasta el cierre del dict `PENDIENTES` (inclusive) por:

```python
"""
C1 de la Consola KIS (§3.1): todas las líneas de todos los clientes, con el
último vínculo de cada una, por la función SECURITY DEFINER
radar_admin_consola_lineas(). Sin conversaciones ni teléfonos: del número solo
el sufijo. Desde el tramo 3: último mensaje, sincronización (estado de la ingesta
o cobertura con su %) y huecos (chats con importación fallida en la última
pasada, más uno si hay sospecha de silencio). El gasto de IA llega en el tramo 5.
"""

import uuid
from datetime import datetime
from typing import Optional

from app.radar.contexto import RadarContexto
from app.radar.vinculo_estados import restriccion_activa, semaforo

PENDIENTES = {"gasto_ia_mes": "tramo 5"}
_TEXTO_INGESTA = {None: "esperando conteo", "contando": "contando chats",
                  "pendiente_de_seleccion": "esperando selección"}
```

2. Agregar, antes de `fila_consola`:

```python
def sincronizacion(f: dict) -> Optional[str]:
    if f["link_id"] is None or (f["ingesta"] is None and f["link_estado"] != "vinculado"):
        return None
    if f["ingesta"] in _TEXTO_INGESTA:
        return _TEXTO_INGESTA[f["ingesta"]]
    total = f["chats_total"] or 0
    pct = round(100 * (f["chats_importados"] or 0) / total) if total else 100
    return f"{f['cobertura'] or 'provisoria'} {pct} %"


def huecos(f: dict) -> Optional[int]:
    if f["link_id"] is None:
        return None
    return (f["chats_fallidos"] or 0) + (1 if f["silencio_desde"] else 0)
```

3. En `fila_consola`, reemplazar

```python
        **{campo: None for campo in PENDIENTES},
        "pendiente": dict(PENDIENTES),
```

por

```python
        "ultimo_mensaje": _iso(f["ultimo_msg_at"]),
        "sincronizacion": sincronizacion(f),
        "huecos": huecos(f),
        "chats_nuevos": f["chats_pendientes_decision"] if f["link_id"] else None,
        "posible_hueco_desde": _iso(f["silencio_desde"]),
        "gasto_ia_mes": None,
        "pendiente": dict(PENDIENTES),
```

- [ ] **Step 5: Correr y ver que pasa**

Run: `python -m pytest tests/radar_tests/test_consola_ingesta.py tests/radar_tests/test_consola_api.py tests/radar_tests/test_vinculo_estados.py -q`
Expected: PASS.

- [ ] **Step 6: Suite completa y commit**

Run: `python -m pytest tests/radar_tests -q` → PASS.

```bash
git add app/radar/consola.py app/radar/vinculo_estados.py tests/radar_tests/test_consola_ingesta.py tests/radar_tests/test_consola_api.py tests/radar_tests/test_vinculo_estados.py
git commit -m "Radar tramo 3: C1 muestra último mensaje, sincronización y huecos; el semáforo pide tráfico

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: Despliegue y runbook de staging del tramo 3

**Files:**
- Modify: `docs/radar-despliegue.md`
- Create: `docs/radar-waha-runbook-tramo3.md`

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: documentación operativa (sin código).

- [ ] **Step 1: Despliegue**

En `docs/radar-despliegue.md`:

1. En la tabla de variables, reemplazar la fila de `RADAR_FUENTE_DATABASE_URL` por:

```markdown
| `RADAR_FUENTE_DATABASE_URL` | Postgres del almacén de fuente permanente (servidor distinto del de resultados, con backups). Desde el tramo 3 guarda texto, ids de proveedor, identidad de contactos y `webhook_inbox`. Debe conectar con el rol `radar_fuente` (ver "Tramo 3"): la app aborta con un superusuario o `BYPASSRLS`. |
```

2. En "Qué NO existe todavía", reemplazar el primer renglón `Ingesta y tablas de conversación en el almacén de fuente (tramo 3), KPI (4),` por `KPI y conversaciones (4),`.

3. Agregar al final:

```markdown
## Tramo 3: ingesta y sincronización

### Rol del almacén de fuente

El almacén de fuente tiene RLS forzada por tenant (f0002). `FORCE` alcanza al
dueño de las tablas pero no a un superusuario, así que el usuario por defecto de
Railway no sirve. Una vez, como superusuario del Postgres de fuente:

    CREATE ROLE radar_fuente LOGIN PASSWORD '<aleatoria>' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    ALTER DATABASE <base_de_fuente> OWNER TO radar_fuente;
    -- si f0001 ya corrió con otro dueño:
    REASSIGN OWNED BY <usuario_anterior> TO radar_fuente;

y `RADAR_FUENTE_DATABASE_URL` pasa a usar `radar_fuente`. Las migraciones de
fuente (f0002) corren con ese mismo rol en el arranque.

### Jobs y tareas periódicas

| Nombre | Qué hace | Cadencia |
|---|---|---|
| `ingesta_inbox` (job) | Normaliza `webhook_inbox` de un vínculo y borra el payload procesado. | Lo encola el receptor. |
| `conteo` (job) | Pasada de conteo de P4: estable a los 75 s sin cambios, tope 10 min. | Cada 20 s mientras cuenta. |
| `backfill` (job) | Pasadas +0, +30 min, +6 h y +24 h desde `WORKING`; cobertura provisoria/estable. | Se reprograma solo. |
| `reconciliacion` (job) | Dos pasos (`/chats` + mensajes de incluidos y pendientes), `observado_hasta`, silencio. | 15 min en horario, 60 fuera, desfase fijo por línea. |
| `programar_ingesta` (bucle) | `radar_jobs_programar_ingesta()`: conteo de vínculos nuevos, una reconciliación viva, adelanto al volver a `WORKING`. | 30 s. |
| `mantenimiento_fuente` (bucle) | Particiones mensuales y purga de `webhook_inbox` a 7 días. | 1 h. |

El horario de atención de P1 todavía no existe: la cadencia y la regla de
silencio usan lunes a sábado de 8 a 20 (hora argentina), `reglas.HORARIO_DEFECTO`.

### Rollback

`alembic -c alembic_radar.ini downgrade r0003` borra las tablas de conversación
de resultados; antes, como superusuario, `DELETE FROM jobs WHERE tipo IN
('ingesta_inbox', 'conteo', 'backfill', 'reconciliacion')` (con `FORCE RLS` el
migrator no ve filas). `alembic -c alembic_fuente.ini downgrade f0001` borra el
texto importado: hacer backup antes.

### Verificación en staging

`docs/radar-waha-runbook-tramo3.md`.
```

- [ ] **Step 2: Runbook**

Crear `docs/radar-waha-runbook-tramo3.md`:

```markdown
# Radar tramo 3 — verificación manual de la ingesta en staging

Solo con cuentas de prueba consentidas y el contenedor de staging del tramo 2
(`docs/radar-waha-runbook-tramo2.md`). Ningún paso copia mensajes, nombres ni
teléfonos a un archivo, un ticket o un chat: se anotan solo conteos y tiempos.

| # | Paso | Qué anotar | Resultado |
|---|---|---|---|
| 1 | Arrancar Radar con `RADAR_FUENTE_DATABASE_URL` apuntando al rol `radar_fuente`. En los logs: `Radar arrancó: almacén de fuente permanente`. Con el usuario por defecto de Railway tiene que abortar. | ¿Abortó con el superusuario? ¿Arrancó con `radar_fuente`? | |
| 2 | Vincular una cuenta de prueba (C2). Desde `WORKING`, cronometrar hasta que C1 pasa de "contando chats" a "esperando selección" (§9: hipótesis ≤ 3 min). | segundos; `conteo_tope` | |
| 3 | Mientras cuenta, mandar un mensaje desde otro teléfono. En `webhook_inbox` (como `radar_fuente`, `SET app.tenant_id = '<tenant>'`): la fila tiene `motivo = 'pendiente_de_seleccion'` y `payload` NULL. | cantidad de filas con payload no nulo (debe ser 0) | |
| 4 | Abrir `/radar/consola/seleccion?tenant=…&linea=…`: la lista aparece sin nombres; con el botón de nombres queda `seleccion_vista` en `access_audit_log`. Excluir un chat de prueba y confirmar a 30 días. | chats a analizar / excluidos | |
| 5 | Verificar en el almacén de fuente que el chat excluido no tiene filas en `wa_message_bodies`, `wa_message_provider_ids` ni `wa_contact_identities`, y en resultados ninguna en `wa_messages`. | 0 / 0 / 0 / 0 | |
| 6 | En los logs de acceso de WAHA (o `docker logs`), confirmar que la primera pasada del backfill usó `chats/all/messages` solo si no había exclusiones, y que todas las lecturas llevan `downloadMedia=false`. Nunca `chats/overview`. | rutas vistas | |
| 7 | Mandar mensajes al chat excluido y a uno incluido. El incluido aparece en `wa_messages` (vía `webhook`) en segundos; el excluido deja solo una fila de `webhook_inbox` con `motivo = 'excluido'`. | segundos hasta `wa_messages` | |
| 8 | A los +30 min y +6 h, C1 muestra la pasada; cuando dos coinciden, "estable 100 %". | hora de cada pasada; cobertura final | |
| 9 | Silencio: poner el teléfono en modo avión dentro del horario. A las ~4 h de horario sin mensajes, C1 en amarillo con posible hueco y **un** `POST /api/sessions/{s}/restart` en los logs de WAHA; a la hora siguiente, un email al dueño. Sacar el modo avión: el atraso entra por la reconciliación y el hueco se cierra. | hora de detección, de reinicio y de recuperación | |
| 10 | Muestra de logs de web y WAHA de todo el recorrido: ninguna coincidencia de `@c.us`, `@lid` ni de 10+ dígitos (§9 guardas). | coincidencias (debe ser 0) | |
| 11 | Al día 8, `SELECT count(*) FROM webhook_inbox WHERE recibido_at < now() - interval '7 days'` (como `radar_fuente` con `app.purga = 'inbox'`) da 0. | 0 | |
```

- [ ] **Step 3: Commit**

```bash
git add docs/radar-despliegue.md docs/radar-waha-runbook-tramo3.md
git commit -m "Radar tramo 3: rol del almacén de fuente, jobs de ingesta y runbook de staging

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Cierre**

Run: `python -m pytest tests/radar_tests -q` → PASS completo. Después, `superpowers:finishing-a-development-branch`.

---

## Self-Review

**Cobertura del spec (tramo 3, §8 punto 3) → task:**

| Pedido | Dónde |
|---|---|
| §6.3.1 receptor de `message.any/ack/edited/revoked`, filtro antes de persistir (pendiente de selección sin payload; excluidos/suprimidos por HMAC; pendientes solo `{id,timestamp,fromMe}`), <100 ms sin llamar a WAHA, ms/s en timestamps | Task 8 (receptor), Task 1 (`ts_segundos`, `metadatos()`) |
| `webhook_inbox` en el almacén de fuente, sin payload de descartados (CHECK), purga a 7 días (§7), particionado mensual | Task 5 (f0002, `mantenimiento()`), Task 9 (payload borrado al normalizar y tarea periódica) |
| Pasada de conteo P4: `GET /chats` paginado sin overview, mensajes con `downloadMedia=false`, body descartado antes de escribir, estabilización 60–90 s con tope 10 min | Task 11 (+ Task 2 `estado_conteo`, Task 3 cliente) |
| API y pantalla P4 en la Consola y del dueño, sugerencia local con regex de `checkout_helper`, sin IA | Tasks 10 y 12 (+ Task 1 `senales`/`sugerir_exclusion`) |
| Backfill en pasadas (+30 min, +6 h, +24 h), chat por chat con exclusiones, `chats/all` solo sin exclusiones, página vacía como fin, cobertura provisoria/estable | Task 13 (+ Task 6 paginación) |
| Reconciliación en dos pasos, nunca `chats/all`, paso 1 hasta `observado_hasta − 10 min`, paso 2 solo incluidos (y pendientes), presupuesto, cadencia 15/60 con desfase, `observado_hasta` | Task 14 (+ Task 4 `radar_jobs_programar_ingesta`) |
| Idempotencia `UNIQUE(line_id, provider_msg_id)` en fuente | Task 5 (PK), Task 7 (`guardar_lote`) |
| `wa_chats`, `wa_messages` sin texto ni id; `wa_message_bodies`, `wa_message_provider_ids`, `wa_contact_identities`, `webhook_inbox` en fuente; RLS | Tasks 4 y 5 |
| `contact_hmac`/`lid_hmac` con `k_tenant` del tramo 1; lid→teléfono vía `/lids`; `pn_resuelto` | Task 6 |
| P5 carriles de importación y cobertura | Task 12 (`progreso_linea` + pantalla) |
| C1 sin "—" en último mensaje, sincronización y huecos | Task 15 |
| Sesión conectada sin tráfico: regla de silencio, un reinicio cada 24 h, nunca con restricción, `observado_hasta` deja de avanzar, aviso | Task 14 (+ Task 2) |
| Ampliación de profundidad y re-vinculación importando lo que falta (`observado_hasta`, `fuente_purgada_hasta`) | Task 10 (`preparar_importacion`), Task 11 (re-vinculación sin P4), Task 2 (`ventanas_a_importar`) |
| Lista blanca extendida (chats, messages, all, lids) con tests y `downloadMedia=false` forzado | Task 3 |
| §6.3.5 medios: sin descarga, `has_media` y tipo | Task 1 (`tipo_de`), Task 4 (`wa_messages.tipo/has_media`) |
| §6.3.8 higiene de logs | `__repr__` sin contenido (Task 1), tests de logs (Task 8), runbook paso 10 |
| §9 guardas: 0 filas de excluidos, 0 lecturas de excluidos en la reconciliación, 0 columnas de texto/ids fuera de fuente | Tasks 7, 8, 9, 10 (filas), Task 14 (contrato de lecturas), Task 4 (test de esquema) |

**Fuera de este tramo (y dónde cae):**
- Conversaciones, hilos, ventana de observación aplicada a KPI, "no legible", respuesta automática y unificación `@lid`/`@c.us` con `merge` (§4.2), banner de P6 y carril de métricas: **tramo 4**. Este tramo deja lo que necesitan: `tipo`/`has_media`/`tiene_texto`, `from_me`, `provider_ts`, `observado_hasta`, `silencio_desde` y los HMAC de ambas formas del contacto.
- Texto redactado (`wa_message_bodies.texto_redactado`), vía rápida y carril de IA: **tramo 5**.
- "Suprimir contacto" (insertar en `suppressions`), purga de fuente con almacén purgable, fichas y `retencion_tras_desvinculo_dias`: **tramo 6**. La ingesta ya lee `suppressions` y respeta `fuente_purgada_hasta`.

**Placeholders:** ninguno. Todo paso tiene código o texto completo. Los únicos valores "a validar" son del spec (duración del sync, tope de `limit`), y el plan fija constantes concretas (`LIMITE_MENSAJES = 100`, `PRESUPUESTO_PAGINAS = 200`, `ESTABLE = 75 s`) que el runbook mide.

**Consistencia de nombres contra el código real** (verificado leyendo el worktree `feature/radar-tramo2`):
- `RadarDB.tenant_tx/sin_tenant`, `FuenteStore` (se le agregan `pool`, `tenant_tx`, `mantenimiento`), `RadarContexto(settings, db, fuente, secretos, mailer, waha_transport)`.
- `jobs.encolar(con, *, tipo, link_id, causa=None, en_segundos=0)`, `reclamar`, `completar`, `reprogramar(db, job, en_segundos)`, `fallar`, `programar_salud`; `Job(id, tenant_id, tipo, link_id, causa, intentos)`; `worker.HANDLERS`, `correr_una_vez`, `bucle`; índice `jobs_uno_vivo_por_link_y_tipo`; CHECK `jobs_tipo_check`.
- `rls_sql.politica_por_tenant`, `grants_app`, `definir_funcion_admin`; patrón `GRANTS` + `UPDATES_POR_COLUMNA` de r0003; `radar_tenant_actual()`; `radar_admin_consola_lineas()` recreada con las 18 columnas originales más 9.
- `WahaCliente._request`, `verificar_ruta` con `fullmatch`, `RUTAS_PERMITIDAS`, `PATRON_SESION`, `WahaError`/`WahaHttpError`/`RutaNoPermitida`, `reiniciar_sesion`; `workers.leer_worker`, `cliente_de`, `nombre_clave_lectura`, `ClaveAdminAusente`; `waha.sesion.EVENTOS_WEBHOOK` ya incluye los cuatro `message.*`.
- `secrets.Seudonimizador`, `contact_hmac`, `lid_hmac`, `obtener_k_tenant`, `crear_k_tenant`, `destruir_k_tenant`, `KTenantAusente`; `telefonos.normalizar_e164`, `TelefonoNoSoportado`.
- `vinculo_estados.semaforo`, `restriccion_activa`, `UMBRAL_WORKER_LLENO`; `consola.fila_consola`, `listar_lineas_consola`, `PENDIENTES`; `auditoria.ACCIONES`/`registrar`; `eventos_producto.EVENTOS`/`registrar_evento`; `fin_vinculo._emails_duenos`, `_avisar`, `HORA_ARGENTINA`.
- Routers: `routers.vinculo._linea_visible`, `routers.consola._tenant_cliente`, `auth.requiere_rol`, `sesion_actual`, `ip_de`, `paginas._html`, `_TIPOS`, `CSP`.
- Tests: fixtures `radar_urls`, `radar_db`, `radar_ctx`, `cliente`, `waha`, `ctx_waha`; helpers `escenario_vinculable`, `vincular_de_prueba`, `crear_link_directo`, `como_superusuario`, `entrar`, `crear_usuario`, `HMAC_TEST`; `WahaFalso` con `llamadas`, `sesiones`, `claves`.
- Columnas nuevas que evitan la guarda de §9: `chat_uuid` (no `chat_id`), `ultimo_msg_at` (no `mensaje`), `conteo_n_msgs`; la guarda se amplía solo para `contact_hmac` y `lid_hmac` y agrega `provider_msg_id` a lo prohibido.

**Verificación fuera del repo:** `reduccion.py` y `reglas.py` con sus tests (Tasks 1 y 2) se extrajeron a un árbol temporal fuera del repo, importando `app.services.checkout_helper` del worktree, y pasan (24 tests). La única diferencia con el plan es que allí `TIPOS` y `SUGERENCIAS` estaban definidos en el módulo en lugar de importarse de `app.radar.constantes`. El resto (SQL, RLS, jobs, routers) no se ejecutó: depende de pgserver y del esquema completo, y queda para la ejecución task por task.

**Riesgos que vigilar al ejecutar:**
- `webhook_inbox` usa `BIGSERIAL` (no `IDENTITY`) porque las columnas identity en tablas particionadas recién existen en Postgres 17.
- Que `app/radar/waha/cliente.py` importe `app.radar.ingesta.reduccion` arrastra `app.services.checkout_helper` (y `rapidfuzz`) al proceso de Radar: ya están en la imagen; si molestara, las señales se mueven a un módulo que se importe solo desde `conteo.py`.
