# Radar — Spike de validación de WAHA (Semana 0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir un arnés de medición descartable (`scripts/radar_spike/`) que cierre los 16 puntos [VALIDAR] de la sección 8 del spec de Radar contra WAHA, sin leer ni guardar nunca el contenido de un chat, y dejar los resultados en un documento con la rúbrica de elección de motor.

**Architecture:** Un paquete Python aislado (no importa `app/`, `app/` no lo importa) con cuatro piezas: un cliente HTTP de WAHA con lista blanca de rutas y prefijo de sesión obligatorio `spike_`; un receptor de webhooks FastAPI que verifica HMAC sha512 y registra solo hechos sin contenido; funciones puras de reducción y agregación (`privacy.py`, `measures.py`); y un CLI (`runner.py`) con un comando por medición que escribe JSON de agregados en `scripts/radar_spike/results/` (ignorado por git). La fase 1 corre sobre la instancia WAHA existente (NOWEB 2026.8.2, tier CORE) con una sesión nueva al lado de `MaroSession`; la fase 2 corre sobre un segundo contenedor con `WHATSAPP_DEFAULT_ENGINE=GOWS`, porque el motor es una propiedad del servidor, no de la sesión (spec §6.2).

**Tech Stack:** Python 3.12, httpx 0.27 (`httpx.MockTransport` / `httpx.ASGITransport` en tests), FastAPI + uvicorn (receptor), python-dotenv, pytest + pytest-asyncio (`asyncio_mode=auto`). Sin base de datos: los resultados son archivos JSON/JSONL.

**Spec:** `docs/superpowers/specs/2026-09-21-onboarding-radar-whatsapp-design.md` (v5.1), sección 8 "Semana 0", §6.2 (criterios de motor y endurecimiento), §6.3 (reconciliación y fin de vínculo), §3 P3 (cuerpo de creación de sesión).

## Global Constraints

- Servidor WAHA de fase 1: versión **2026.8.2**, motor **NOWEB**, tier **CORE**; la sesión personal del dueño se llama `MaroSession` y **el arnés nunca la toca**: el cliente rechaza cualquier nombre de sesión que no empiece con `spike_` (test unitario).
- La fase 1 **no cambia variables de entorno del servidor** ni reinicia el contenedor. Solo crea, lee y borra sesiones `spike_*` y sus claves.
- Configuración **solo por variables de entorno** leídas en tiempo de ejecución: `WAHA_BASE_URL`, `WAHA_ADMIN_KEY`, `SPIKE_WEBHOOK_PUBLIC_URL`, `SPIKE_WEBHOOK_HMAC_KEY`, `SPIKE_RUN_ID` (+ opcionales `SPIKE_RESULTS_DIR`, `SPIKE_RECEIVER_MODE`, `SPIKE_PAIRING_PHONE`, `SPIKE_COMPARE_CHATS_FILE`, y `SPIKE_ENV_FILE` solo para el receptor). El arnés nunca las imprime, nunca las escribe a disco y nunca las pone en una URL. Precedencia del archivo de entorno (`config.load_env_file`, usado por el runner con `--env-file` y por el receptor con `.env.spike`/`SPIKE_ENV_FILE`): **un valor no vacío del archivo pisa la shell; una línea vacía no toca la shell**.
- Logs: los loggers `httpx` y `httpcore` quedan en `WARNING` (httpx imprime la URL completa, con JID, a nivel INFO) y todo handler del logger raíz lleva `RedactingFilter`. Ninguna salida del runner ni del receptor se redirige a un archivo dentro del repo.
- `.env.spike.example` lleva **solo nombres** de variables; `.env.spike` y `scripts/radar_spike/results/` quedan ignorados por git (el `.gitignore` del repo hoy ignora solo `.env`).
- Privacidad, en código y en tests: **nunca se persiste ni se loguea** un cuerpo de mensaje, un teléfono, un JID, un nombre de contacto ni un valor de QR. Toda función lectora reduce un mensaje de WAHA a `{chat_ref, timestamp, from_me, has_text, has_media}` (más un `text_fp` opcional, hash salado del texto, solo para la heurística de respuesta automática; `text_fp` nunca llega a un archivo de resultados) **antes** de devolverlo. `chat_ref` es un HMAC con sal por corrida codificado **solo con letras a-z** (16 caracteres), así ningún hash puede parecerse a un teléfono ni disparar `assert_clean`.
- Los resultados son **agregados**, escritos en `scripts/radar_spike/results/<run_id>/` y pasan por `assert_clean()` (regex `@c\.us|@lid|@s\.whatsapp\.net|@g\.us|\d{8,}`) antes de tocar el disco.
- Lista blanca de rutas del cliente HTTP: `POST /api/sessions`, `GET|DELETE /api/sessions/{s}`, `POST /api/sessions/{s}/(start|stop|restart)`, `GET /api/{s}/auth/qr`, `POST /api/{s}/auth/request-code`, `GET /api/{s}/chats`, `GET /api/{s}/chats/{id}/messages` (incluido `chatId=all`), `GET /api/{s}/lids`, `GET /api/{s}/lids/count`, `POST|GET /api/keys`, `DELETE /api/keys/{id}`, `GET /api/server/(version|status)`. **Cualquier otra ruta lanza** (`send*`, marcar leído, presencia, typing, archivar, borrar chat, `logout`, `chats/overview`, `server/environment` están testeados como rechazados).
- `downloadMedia=false` se fuerza en **toda** lectura de mensajes (spec §6.2: el valor por defecto del servidor es `true`).
- Cuerpo de creación de sesión: el de spec §3/P3 con prefijo `spike_` en vez de `v_`: `ignore {status, groups, channels, broadcast: true}`, `noweb {markOnline: false, store {enabled, fullSync}}`, webhooks con `events ["message", "message.any", "message.ack", "message.edited", "message.revoked", "session.status"]`, `hmac.key`, `retries {policy: exponential, delaySeconds: 2, attempts: 15}`. Tras crear se relee `GET /api/sessions/{name}` y se **aborta** (borrando la sesión) si `config.noweb.markOnline !== false` o si `store` o `ignore` difieren.
- Fin de sesión: **un solo** `DELETE /api/sessions/{name}`, nunca `POST …/logout` (spec §6.3 punto 6: sobre una sesión en marcha, logout la vuelve a arrancar con QR nuevo). Antes del DELETE se registra el estado (`status_before`: el dispositivo solo se desvincula del teléfono si la sesión estaba WORKING). Después se borran las claves de API de esa sesión (`DELETE /api/keys/{id}`, WAHA no las borra solo) y se verifica 404; un error en el paso de claves se registra (`keys_error`) y **no** bloquea la verificación ni el olvido de la sal (spec §6.3 punto 6: un paso fallido no bloquea los siguientes).
- Receptor de webhooks: verifica `X-Webhook-Hmac` (sha512 sobre el cuerpo crudo), **fail-closed** (sin clave configurada responde 401 siempre), registra solo `{event_ref, event, session, ts_ms, received_ms, payload_ts, from_me, has_text, has_media, chat_ref, is_lid, is_history, status}` y responde 200 rápido.
- Timestamps: el del sobre del webhook viene en **ms**, `payload.timestamp` en **segundos** (spec §6.3 punto 1).
- Fase 2 (GOWS): segundo contenedor con las variables de endurecimiento de spec §6.2 (`WAHA_PRINT_QR=false`, `WAHA_PRESENCE_AUTO_ONLINE=false`, `WAHA_SESSION_CONFIG_IGNORE_*=true`, medios apagados, `WAHA_APPS_ENABLED=false`, dashboard y Swagger deshabilitados, clave hasheada) y las de historial `WAHA_GOWS_DEVICE_REQUIRE_FULL_SYNC`, `WAHA_GOWS_DEVICE_HISTORY_SYNC_FULL_SYNC_DAYS_LIMIT`, `WAHA_GOWS_DEVICE_HISTORY_SYNC_RECENT_SYNC_DAYS_LIMIT`, `WAHA_GOWS_DEVICE_HISTORY_SYNC_INITIAL_SYNC_MAX_MESSAGES_PER_CHAT`.
- Código de producto: **ninguno**. Todo vive en `scripts/radar_spike/` y `tests/radar_spike/`. Rama sugerida: `spike/radar-waha`.
- Tests: `python -m pytest tests/radar_spike -q` desde la raíz del repo (`python -m` pone la raíz en `sys.path`; `tests/radar_spike/conftest.py` lo garantiza además para `pytest` a secas).
- Prohibido en la ejecución de este plan: llamar herramientas MCP de WAHA, leer chats reales, imprimir valores de secretos.

---

## Prerrequisitos humanos (antes de la Tarea 8)

Los cumple el dueño del proyecto; el arnés no puede hacerlos.

1. **Una línea de WhatsApp Business de prueba con consentimiento** y con historial real de atención (spec §8: "cuentas de prueba consentidas"; §7: no se desarrolla contra `MaroSession`). Se necesita conocer la fecha aproximada de vinculación anterior y tener el teléfono a mano para contar mensajes por mes en 20 chats (punto 2).
2. **Un segundo teléfono** con otra línea, que nunca haya escrito a la línea de prueba, para los chequeos manuales de notificaciones (punto 5), tildes/presencia (punto 6), primer mensaje de contacto `@lid` (punto 10) y mensajes de bienvenida/ausencia (punto 12).
3. **Variables de entorno**: el dueño exporta `WAHA_BASE_URL`, `WAHA_ADMIN_KEY` (clave admin de la instancia de staging), `SPIKE_WEBHOOK_PUBLIC_URL` (URL pública de un túnel hacia el receptor local, por ejemplo `cloudflared tunnel --url http://localhost:8787`), `SPIKE_WEBHOOK_HMAC_KEY` (32 bytes aleatorios en hex) y `SPIKE_RUN_ID`. Las copia a `.env.spike` (ignorado por git), pasa `--env-file .env.spike` al runner y cambia `SPIKE_RUN_ID` en ese archivo antes de cada corrida (el receptor lo lee al arrancar y hay que reiniciarlo).
4. **Vincular el dispositivo lo hace el dueño**: escanea el QR (archivo temporal que el comando `qr --png` escribe fuera del repo y borra al llegar a `WORKING`) o tipea el código de vinculación en el teléfono. El arnés nunca escanea nada por sí mismo.
5. Para la fase 2, **Docker local o un servicio Railway nuevo** para el contenedor GOWS (Tarea 9). No se toca el contenedor de staging.
6. Para el punto 11 (coexistencia) y el 16 (retención de proveedores de IA) no hay código: son preguntas a Kapso/Meta y una lista de URLs que el dueño consulta (Tarea 10).

## Por qué dos fases sobre dos instancias

Respuesta a la pregunta del dueño ("¿no podemos usar la misma instancia de staging, separada de la línea personal?"): **sí, para todo lo que es por sesión**. WAHA CORE desde 2026.6.1 admite varias sesiones (spec §0.3, es justamente el punto 1), y la separación dentro de la misma instancia es por nombre de sesión: el arnés solo puede crear, leer y borrar sesiones `spike_*`, y `MaroSession` queda fuera de su alcance por construcción. Lo que **no** se puede separar dentro de la misma implementación es el motor: `WHATSAPP_DEFAULT_ENGINE` es una variable del servidor (spec §6.2, "el motor es una propiedad de cada servidor WAHA, no de cada sesión"). Por eso los puntos que comparan NOWEB con GOWS (2, 10, 13, 14, 15) necesitan un segundo contenedor, y ese contenedor es además la única forma de probar las variables de endurecimiento sin reiniciar el de staging.

---

## File Structure

```
.gitignore                                   # + .env.spike, + scripts/radar_spike/results/* (con .gitkeep)
.env.spike.example                           # solo NOMBRES de variables
scripts/radar_spike/
├── __init__.py                              # docstring; marca el paquete como descartable
├── privacy.py                               # sal por corrida, ref(), reduce_message/reduce_chat, scan_for_pii/assert_clean, RedactingFilter
├── config.py                                # SpikeConfig.from_env(): nombres de variables, repr redactado; load_env_file()
├── client.py                                # WahaClient: lista blanca, prefijo spike_, downloadMedia=false, lecturas reducidas, refs_for_phone (alias @lid)
├── session_spec.py                          # build_session_body() (spec P3) y verify_session_config()
├── lifecycle.py                             # ensure_session() (crea + relee + aborta), wait_for_status(), teardown_session()
├── receiver.py                              # FastAPI: verify_hmac(), extract_record(), build_app(), app_from_env()
├── results.py                               # ResultsStore: results/<run_id>/, salt.bin, link.json, write() con assert_clean
├── measures.py                              # funciones puras: count_summary, monthly_by_label, stable_since, autoreply_summary, events_summary, qr_timeline
├── runner.py                                # CLI: un subcomando por medición
├── README.md                                # runbook: orden de comandos por punto y fase 2 (docker run)
└── results/.gitkeep
tests/radar_spike/
├── conftest.py                              # raíz del repo en sys.path
├── test_privacy.py
├── test_config.py
├── test_client.py
├── test_session_spec.py
├── test_lifecycle.py
├── test_receiver.py
├── test_results.py
├── test_measures.py
└── test_runner.py
docs/superpowers/specs/2026-09-21-radar-spike-resultados.md   # plantilla de resultados + rúbrica de motor
```

---

### Task 1: Paquete, ignorados de git y reglas de privacidad

**Files:**
- Create: `scripts/radar_spike/__init__.py`, `scripts/radar_spike/privacy.py`, `scripts/radar_spike/results/.gitkeep`, `.env.spike.example`, `tests/radar_spike/conftest.py`
- Modify: `.gitignore`
- Test: `tests/radar_spike/test_privacy.py`

**Interfaces:**
- Produces: `new_salt() -> bytes`; `ref(salt: bytes, value: str) -> str` (16 letras a-z, nunca dígitos); `reduce_message(msg: dict, salt: bytes, *, with_text_fp: bool = False) -> MessageFacts`; `MessageFacts(chat_ref: str, timestamp: int, from_me: bool, has_text: bool, has_media: bool, text_fp: str | None)` con `.as_dict()`; `chat_kind(chat_id: str) -> str`; `reduce_chat(chat: dict, salt: bytes) -> ChatFacts(chat_ref: str, kind: str, last_ts: int | None)`; `scan_for_pii(obj) -> list[str]`; `assert_clean(obj) -> None` (lanza `PrivacyViolation`); `redact(text: str, secrets=()) -> str`; `RedactingFilter(secrets)`; `get_logger(secrets=()) -> logging.Logger`.

- [ ] **Step 1: Ignorados de git y ejemplo de entorno**

Agregar al final de `.gitignore`:

```
# Spike de WAHA (Radar): secretos y resultados nunca se versionan
.env.spike
scripts/radar_spike/results/*
!scripts/radar_spike/results/.gitkeep
```

Crear `scripts/radar_spike/results/.gitkeep` vacío y `.env.spike.example`:

```
# Spike de WAHA (Radar). Solo NOMBRES: nunca poner valores en este archivo.
# Copiar a .env.spike (ignorado por git) y pasar --env-file .env.spike al runner.
WAHA_BASE_URL=
WAHA_ADMIN_KEY=
SPIKE_WEBHOOK_PUBLIC_URL=
SPIKE_WEBHOOK_HMAC_KEY=
SPIKE_RUN_ID=
# Opcionales
SPIKE_RESULTS_DIR=
SPIKE_RECEIVER_MODE=
SPIKE_PAIRING_PHONE=
SPIKE_COMPARE_CHATS_FILE=
```

- [ ] **Step 2: Verificar los ignorados**

Run: `git check-ignore .env.spike scripts/radar_spike/results/x.json; git check-ignore .env.spike.example; echo "codigo=$LASTEXITCODE"`
Expected: imprime `.env.spike` y `scripts/radar_spike/results/x.json`; para `.env.spike.example` no imprime nada y `codigo=1`.

- [ ] **Step 3: Paquete y conftest**

`scripts/radar_spike/__init__.py`:

```python
"""Arnés descartable del spike de WAHA para Radar (spec §8, Semana 0).

No es código de producto: no importa nada de app/ y app/ no lo importa.
"""
```

`tests/radar_spike/conftest.py`:

```python
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

- [ ] **Step 4: Test de privacidad (falla)**

`tests/radar_spike/test_privacy.py`:

```python
import json
import logging
import re

import pytest

from scripts.radar_spike import privacy

SALT = b"\x01" * 32
FAKE_MSG = {
    "id": "false_5491155551234@c.us_3EB0ABCDEF12",
    "timestamp": 1758400000,
    "from": "5491155551234@c.us",
    "fromMe": False,
    "to": "5491166667777@c.us",
    "body": "Hola, tienen ibuprofeno 600? soy Marta",
    "hasMedia": False,
    "_data": {"pushName": "Marta Gomez"},
}
LEAKS = ("5491155551234", "5491166667777", "ibuprofeno", "Marta", "@c.us", "3EB0ABCDEF12")


def test_reduce_message_keeps_only_facts():
    d = privacy.reduce_message(FAKE_MSG, SALT).as_dict()
    assert set(d) == {"chat_ref", "timestamp", "from_me", "has_text", "has_media", "text_fp"}
    assert d["has_text"] is True and d["from_me"] is False and d["has_media"] is False
    assert d["timestamp"] == 1758400000 and d["text_fp"] is None
    dumped = json.dumps(d)
    for leak in LEAKS:
        assert leak not in dumped
    assert privacy.scan_for_pii(d) == []


def test_chat_ref_uses_chat_id_and_salt():
    a = privacy.reduce_message(FAKE_MSG, SALT)
    b = privacy.reduce_message({**FAKE_MSG, "body": "otro"}, SALT)
    c = privacy.reduce_message(FAKE_MSG, b"\x02" * 32)
    assert a.chat_ref == b.chat_ref and a.chat_ref != c.chat_ref
    mine = privacy.reduce_message({**FAKE_MSG, "fromMe": True}, SALT)
    assert mine.chat_ref != a.chat_ref  # sin chatId, fromMe usa 'to'
    with_chat_id = privacy.reduce_message({**FAKE_MSG, "fromMe": True, "chatId": FAKE_MSG["from"]}, SALT)
    assert with_chat_id.chat_ref == a.chat_ref


def test_text_fp_only_when_requested_and_normalized():
    f1 = privacy.reduce_message(FAKE_MSG, SALT, with_text_fp=True)
    f2 = privacy.reduce_message({**FAKE_MSG, "body": "  HOLA,   tienen ibuprofeno 600? soy marta "}, SALT, with_text_fp=True)
    f3 = privacy.reduce_message({**FAKE_MSG, "body": "gracias"}, SALT, with_text_fp=True)
    assert f1.text_fp == f2.text_fp and f1.text_fp != f3.text_fp
    assert len(f1.text_fp) == 16
    assert privacy.reduce_message({**FAKE_MSG, "body": None}, SALT, with_text_fp=True).text_fp is None


def test_refs_are_sixteen_lowercase_letters():
    """Solo letras: un hash nunca puede parecerse a un teléfono ni disparar los patrones de PII."""
    seen = set()
    for i in range(3000):
        r = privacy.ref(SALT, f"549{i:010d}@c.us")
        assert len(r) == 16 and r.isalpha() and r.islower() and not re.search(r"\d", r)
        seen.add(r)
    assert len(seen) == 3000
    assert privacy.scan_for_pii({"chat_ref": privacy.ref(SALT, "5491155551234@c.us")}) == []


@pytest.mark.parametrize("cid,kind", [
    ("5491155551234@c.us", "individual"),
    ("123456789012345@lid", "individual_lid"),
    ("120363012345678901@g.us", "group"),
    ("120363012345678901@newsletter", "channel"),
    ("status@broadcast", "status"),
    ("123@broadcast", "broadcast"),
    ("raro", "other"),
])
def test_chat_kind(cid, kind):
    assert privacy.chat_kind(cid) == kind


def test_reduce_chat_hides_id_and_name():
    chat = {"id": "5491155551234@c.us", "name": "Marta Gomez", "conversationTimestamp": 1758400000}
    c = privacy.reduce_chat(chat, SALT)
    assert c.kind == "individual" and c.last_ts == 1758400000
    assert "Marta" not in json.dumps(c.__dict__) and "5491155551234" not in json.dumps(c.__dict__)
    c2 = privacy.reduce_chat({"id": {"_serialized": "5491155551234@c.us"}}, SALT)
    assert c2.chat_ref == c.chat_ref and c2.last_ts is None


def test_assert_clean_raises_on_phone_jid_and_qr_like_strings():
    for bad in ({"x": "5491155551234"}, ["a", {"y": ["b", "1@c.us"]}], {"k": "abc@lid"}, {"1122334455": 1}):
        with pytest.raises(privacy.PrivacyViolation):
            privacy.assert_clean(bad)
    privacy.assert_clean({"chat_ref": "abcdefghijklmnop", "timestamp": 1758400000, "written_at": "2026-09-21T10:00:00"})


def test_logger_redacts_secrets_and_pii(caplog):
    log = privacy.get_logger(secrets=("ADMINKEY-xyz", "HMACKEY-abc"))
    with caplog.at_level(logging.INFO, logger="radar_spike"):
        log.info("clave %s hmac %s chat %s id %s", "ADMINKEY-xyz", "HMACKEY-abc", "5491155551234@c.us", "999@lid")
    out = caplog.text
    for leak in ("ADMINKEY-xyz", "HMACKEY-abc", "5491155551234", "@c.us", "@lid"):
        assert leak not in out
    assert "[secreto]" in out and "[redactado]" in out
```

- [ ] **Step 5: Correr el test (falla)**

Run: `python -m pytest tests/radar_spike/test_privacy.py -q`
Expected: `ImportError: cannot import name 'privacy' from 'scripts.radar_spike'` (error de colección; el paquete existe desde el Step 3, el módulo todavía no).

- [ ] **Step 6: Implementar `privacy.py`**

```python
"""Reglas de privacidad del spike: reducción de mensajes, hashes por corrida y redacción de logs.

Nada de lo que sale de este módulo contiene texto de mensajes, teléfonos, JID, nombres ni QR.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

_ALPHA = "abcdefghijklmnopqrstuvwxyz"
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "jid_cus": re.compile(r"@c\.us"),
    "jid_lid": re.compile(r"@lid"),
    "jid_wa": re.compile(r"@s\.whatsapp\.net"),
    "jid_group": re.compile(r"@g\.us"),
    "digits8": re.compile(r"\d{8,}"),
}
REDACTED = "[redactado]"
SECRET = "[secreto]"


class PrivacyViolation(RuntimeError):
    """Un objeto destinado a resultados o logs contiene teléfono, JID o texto sospechoso."""


def new_salt() -> bytes:
    return os.urandom(32)


def ref(salt: bytes, value: str) -> str:
    """HMAC-SHA256 con sal por corrida, codificado solo con letras a-z: nunca dispara los patrones de PII."""
    digest = hmac.new(salt, value.encode("utf-8"), hashlib.sha256).digest()
    return "".join(_ALPHA[b % 26] for b in digest[:16])


def _norm_text(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip().lower()


@dataclass(frozen=True)
class MessageFacts:
    chat_ref: str
    timestamp: int
    from_me: bool
    has_text: bool
    has_media: bool
    text_fp: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChatFacts:
    chat_ref: str
    kind: str
    last_ts: int | None


def _serialized(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("_serialized") or ""
    return str(value or "")


def chat_id_of(msg: dict[str, Any]) -> str:
    cid = msg.get("chatId")
    if not cid:
        cid = msg.get("to") if msg.get("fromMe") else msg.get("from")
    return _serialized(cid)


def reduce_message(msg: dict[str, Any], salt: bytes, *, with_text_fp: bool = False) -> MessageFacts:
    body = msg.get("body")
    has_text = isinstance(body, str) and bool(body.strip())
    fp = ref(salt, _norm_text(body)) if (with_text_fp and has_text) else None
    return MessageFacts(
        chat_ref=ref(salt, chat_id_of(msg)),
        timestamp=int(msg.get("timestamp") or 0),
        from_me=bool(msg.get("fromMe")),
        has_text=has_text,
        has_media=bool(msg.get("hasMedia")),
        text_fp=fp,
    )


def chat_kind(chat_id: str) -> str:
    if chat_id == "status@broadcast":
        return "status"
    if chat_id.endswith("@g.us"):
        return "group"
    if chat_id.endswith("@newsletter"):
        return "channel"
    if chat_id.endswith("@broadcast"):
        return "broadcast"
    if chat_id.endswith("@c.us") or chat_id.endswith("@s.whatsapp.net"):
        return "individual"
    if chat_id.endswith("@lid"):
        return "individual_lid"
    return "other"


def reduce_chat(chat: dict[str, Any], salt: bytes) -> ChatFacts:
    cid = _serialized(chat.get("id"))
    ts = chat.get("conversationTimestamp") or chat.get("timestamp")
    return ChatFacts(chat_ref=ref(salt, cid), kind=chat_kind(cid), last_ts=int(ts) if ts else None)


def scan_for_pii(obj: Any, _path: str = "$") -> list[str]:
    """Recorre strings (claves y valores) y devuelve rutas + patrón encontrado. Nunca devuelve el valor."""
    hits: list[str] = []
    if isinstance(obj, str):
        for name, pat in PII_PATTERNS.items():
            if pat.search(obj):
                hits.append(f"{_path}:{name}")
    elif isinstance(obj, dict):
        for i, (k, v) in enumerate(obj.items()):
            hits += scan_for_pii(str(k), f"{_path}.key{i}")
            hits += scan_for_pii(v, f"{_path}.val{i}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits += scan_for_pii(v, f"{_path}[{i}]")
    return hits


def assert_clean(obj: Any) -> None:
    hits = scan_for_pii(obj)
    if hits:
        raise PrivacyViolation(f"{len(hits)} coincidencias de PII, por ejemplo {hits[:5]}")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, SECRET)
    for pat in PII_PATTERNS.values():
        text = pat.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = ()
        return True


def get_logger(secrets: Iterable[str] = ()) -> logging.Logger:
    log = logging.getLogger("radar_spike")
    for f in list(log.filters):
        log.removeFilter(f)
    log.addFilter(RedactingFilter(secrets))
    return log
```

- [ ] **Step 7: Correr el test (pasa)**

Run: `python -m pytest tests/radar_spike/test_privacy.py -q`
Expected: `14 passed` (7 funciones, una de ellas parametrizada en 7 casos).

- [ ] **Step 8: Commit**

```
git checkout -b spike/radar-waha
git add .gitignore .env.spike.example scripts/radar_spike/__init__.py scripts/radar_spike/privacy.py scripts/radar_spike/results/.gitkeep tests/radar_spike/conftest.py tests/radar_spike/test_privacy.py
git commit -m "Spike WAHA: paquete descartable y reglas de privacidad"
```

---

### Task 2: Configuración por variables de entorno

**Files:**
- Create: `scripts/radar_spike/config.py`
- Test: `tests/radar_spike/test_config.py`

**Interfaces:**
- Produces: `REQUIRED: tuple[str, ...]`; `ConfigError(RuntimeError)`; `load_env_file(path: str | os.PathLike[str]) -> None` (los valores no vacíos del archivo pisan `os.environ`; las líneas vacías y un archivo inexistente no hacen nada); `SpikeConfig(waha_base_url: str, waha_admin_key: str, webhook_public_url: str, webhook_hmac_key: str, run_id: str, results_dir: Path)` con `SpikeConfig.from_env(env: Mapping[str, str] | None = None) -> SpikeConfig`, propiedades `session_name -> str` (`spike_<run_id>`), `secrets -> tuple[str, str]`, `run_dir -> Path`, y `__repr__` redactado.

- [ ] **Step 1: Test (falla)**

`tests/radar_spike/test_config.py`:

```python
import os

import pytest

from scripts.radar_spike.config import ConfigError, SpikeConfig, load_env_file

ENV = {
    "WAHA_BASE_URL": "https://waha.example/",
    "WAHA_ADMIN_KEY": "ADMIN-SECRET",
    "SPIKE_WEBHOOK_PUBLIC_URL": "https://tunel.example/",
    "SPIKE_WEBHOOK_HMAC_KEY": "HMAC-SECRET",
    "SPIKE_RUN_ID": "nw0",
}


def test_from_env_reads_everything_and_strips_slashes():
    cfg = SpikeConfig.from_env(ENV)
    assert cfg.waha_base_url == "https://waha.example"
    assert cfg.webhook_public_url == "https://tunel.example"
    assert cfg.session_name == "spike_nw0"
    assert cfg.secrets == ("ADMIN-SECRET", "HMAC-SECRET")
    assert cfg.run_dir.name == "nw0" and cfg.results_dir.name == "results"


def test_missing_vars_are_listed_by_name_only():
    with pytest.raises(ConfigError) as e:
        SpikeConfig.from_env({"WAHA_BASE_URL": "x"})
    msg = str(e.value)
    for name in ("WAHA_ADMIN_KEY", "SPIKE_WEBHOOK_PUBLIC_URL", "SPIKE_WEBHOOK_HMAC_KEY", "SPIKE_RUN_ID"):
        assert name in msg
    assert msg.startswith("faltan variables de entorno") and "WAHA_BASE_URL" not in msg


def test_repr_and_str_hide_secrets():
    cfg = SpikeConfig.from_env(ENV)
    for text in (repr(cfg), str(cfg), f"{cfg}"):
        assert "ADMIN-SECRET" not in text and "HMAC-SECRET" not in text
        assert "spike_nw0" in text or "nw0" in text


@pytest.mark.parametrize("bad", ["", "Con Espacios", "MAYUS", "a" * 25, "x-y"])
def test_run_id_is_restricted(bad):
    with pytest.raises(ConfigError):
        SpikeConfig.from_env({**ENV, "SPIKE_RUN_ID": bad})


def test_results_dir_override(tmp_path):
    cfg = SpikeConfig.from_env({**ENV, "SPIKE_RESULTS_DIR": str(tmp_path)})
    assert cfg.results_dir == tmp_path and cfg.run_dir == tmp_path / "nw0"


def test_load_env_file_nonempty_values_win_and_empty_lines_keep_shell(tmp_path, monkeypatch):
    f = tmp_path / ".env.spike"
    f.write_text("SPIKE_RUN_ID=t1\nSPIKE_PAIRING_PHONE=\nSPIKE_RECEIVER_MODE=\n", encoding="utf-8")
    monkeypatch.setenv("SPIKE_RUN_ID", "otro")          # valor viejo en la shell: el archivo gana
    monkeypatch.setenv("SPIKE_PAIRING_PHONE", "549")    # línea vacía en el archivo: la shell queda
    monkeypatch.delenv("SPIKE_RECEIVER_MODE", raising=False)
    load_env_file(f)
    assert os.environ["SPIKE_RUN_ID"] == "t1" and os.environ["SPIKE_PAIRING_PHONE"] == "549"
    assert "SPIKE_RECEIVER_MODE" not in os.environ
    load_env_file(tmp_path / "no-existe")  # silencioso
    assert os.environ["SPIKE_RUN_ID"] == "t1"
```

- [ ] **Step 2: Correr (falla)**

Run: `python -m pytest tests/radar_spike/test_config.py -q`
Expected: `ModuleNotFoundError: No module named 'scripts.radar_spike.config'`.

- [ ] **Step 3: Implementar `config.py`**

```python
"""Configuración del spike: solo variables de entorno, leídas en tiempo de ejecución."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

REQUIRED: tuple[str, ...] = (
    "WAHA_BASE_URL",
    "WAHA_ADMIN_KEY",
    "SPIKE_WEBHOOK_PUBLIC_URL",
    "SPIKE_WEBHOOK_HMAC_KEY",
    "SPIKE_RUN_ID",
)
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"
RUN_ID_RE = re.compile(r"[a-z0-9_]{1,24}")


class ConfigError(RuntimeError):
    pass


def load_env_file(path: str | os.PathLike[str]) -> None:
    """Carga un archivo .env: los valores NO vacíos pisan la shell; las líneas vacías no tocan nada.

    Así `.env.spike` (con SPIKE_RUN_ID por corrida) es la fuente de verdad, y lo que el archivo deja vacío
    (SPIKE_PAIRING_PHONE, SPIKE_RECEIVER_MODE) se puede dar en la terminal sin escribirlo a disco.
    Un archivo inexistente no es error (python-dotenv devuelve {} en silencio).
    """
    for key, value in dotenv_values(path).items():
        if value:
            os.environ[key] = value


@dataclass(frozen=True, repr=False)
class SpikeConfig:
    waha_base_url: str
    waha_admin_key: str
    webhook_public_url: str
    webhook_hmac_key: str
    run_id: str
    results_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SpikeConfig":
        env = os.environ if env is None else env
        missing = [k for k in REQUIRED if not env.get(k)]
        if missing:
            raise ConfigError("faltan variables de entorno: " + ", ".join(missing))
        run_id = env["SPIKE_RUN_ID"]
        if not RUN_ID_RE.fullmatch(run_id):
            raise ConfigError("SPIKE_RUN_ID: solo [a-z0-9_], entre 1 y 24 caracteres")
        return cls(
            waha_base_url=env["WAHA_BASE_URL"].rstrip("/"),
            waha_admin_key=env["WAHA_ADMIN_KEY"],
            webhook_public_url=env["SPIKE_WEBHOOK_PUBLIC_URL"].rstrip("/"),
            webhook_hmac_key=env["SPIKE_WEBHOOK_HMAC_KEY"],
            run_id=run_id,
            results_dir=Path(env.get("SPIKE_RESULTS_DIR") or DEFAULT_RESULTS_DIR),
        )

    @property
    def session_name(self) -> str:
        return f"spike_{self.run_id}"

    @property
    def secrets(self) -> tuple[str, str]:
        return (self.waha_admin_key, self.webhook_hmac_key)

    @property
    def run_dir(self) -> Path:
        return self.results_dir / self.run_id

    def __repr__(self) -> str:
        return (
            f"SpikeConfig(waha_base_url={self.waha_base_url!r}, run_id={self.run_id!r}, "
            f"session={self.session_name!r}, admin_key='***', hmac_key='***')"
        )

    __str__ = __repr__
```

- [ ] **Step 4: Correr (pasa)**

Run: `python -m pytest tests/radar_spike/test_config.py -q`
Expected: `10 passed` (5 funciones + `test_run_id_is_restricted` en 5 casos).

- [ ] **Step 5: Commit**

```
git add scripts/radar_spike/config.py tests/radar_spike/test_config.py
git commit -m "Spike WAHA: configuracion por variables de entorno, archivo .env con precedencia y repr redactado"
```

---

### Task 3: Cliente HTTP de WAHA con lista blanca y prefijo de sesión

**Files:**
- Create: `scripts/radar_spike/client.py`
- Test: `tests/radar_spike/test_client.py`

**Interfaces:**
- Consumes: `privacy.reduce_message`, `privacy.reduce_chat`, `privacy.ref`, `privacy.MessageFacts`, `privacy.ChatFacts`.
- Produces: `SESSION_PREFIX = "spike_"`; `check_route(method: str, path: str) -> str | None`; `check_session_name(name) -> str`; excepciones `WahaClientError`, `RouteNotAllowed`, `ForbiddenSession`, `WahaHttpError(method, path, status)`; `Page(items: list, raw_count: int, elapsed_ms: int, status: int)`; `WahaClient(base_url: str, admin_key: str, salt: bytes, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 600.0)` (async context manager; al construirse baja los loggers `httpx` y `httpcore` a WARNING) con `server_version() -> dict`, `server_status() -> dict`, `create_session(body: dict) -> dict`, `get_session(name) -> dict | None`, `delete_session(name) -> int`, `start_session/stop_session/restart_session(name) -> dict`, `get_qr_raw(name) -> tuple[int, str | None]`, `get_qr_png(name) -> bytes | None`, `request_code(name, phone: str) -> tuple[int, str | None]`, `list_chats(name, *, limit=500, offset=0, sort_by="conversationTimestamp", sort_order="desc") -> Page[ChatFacts]`, `list_messages(name, *, chat_ref="all", limit=1000, offset=0, ts_gte: int | None = None, ts_lte: int | None = None, with_text_fp=False) -> Page[MessageFacts]`, `lids_count(name) -> int`, `lids_sample(name, limit=500) -> dict`, `refs_for_phone(name, digits: str) -> list[str]` (chat_refs de un teléfono: `@c.us`, `@s.whatsapp.net` y, si `GET /lids` lo conoce, su `@lid`; el mapeo crudo pn→lid vive solo en memoria del cliente), `create_key(session, *, actions: dict) -> dict`, `list_keys() -> list[dict]`, `delete_key(key_id) -> int`.

- [ ] **Step 1: Test (falla)**

`tests/radar_spike/test_client.py`:

```python
import json
import logging

import httpx
import pytest

from scripts.radar_spike.client import (
    ForbiddenSession, RouteNotAllowed, WahaClient, WahaHttpError, check_route,
)
from scripts.radar_spike.privacy import ChatFacts, MessageFacts, ref

SALT = b"\x03" * 32
FAKE_MSG = {
    "id": "false_5491155551234@c.us_3EB0ABCDEF12",
    "timestamp": 1758400000,
    "from": "5491155551234@c.us",
    "fromMe": False,
    "to": "5491166667777@c.us",
    "body": "Hola, tienen ibuprofeno 600? soy Marta",
    "hasMedia": True,
}
FAKE_CHAT = {"id": "5491155551234@c.us", "name": "Marta Gomez", "conversationTimestamp": 1758400000}


class Recorder:
    def __init__(self, responder=None):
        self.requests: list[httpx.Request] = []
        self.responder = responder or (lambda req: httpx.Response(200, json={}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def client(rec: Recorder) -> WahaClient:
    return WahaClient("http://waha", "ADMIN-SECRET", SALT, transport=httpx.MockTransport(rec))


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/sendText"),
    ("POST", "/api/spike_x/chats/1@c.us/messages/read"),
    ("POST", "/api/spike_x/presence"),
    ("POST", "/api/startTyping"),
    ("POST", "/api/spike_x/chats/1@c.us/archive"),
    ("DELETE", "/api/spike_x/chats/1@c.us"),
    ("POST", "/api/sessions/spike_x/logout"),
    ("GET", "/api/spike_x/chats/overview"),
    ("GET", "/api/server/environment"),
    ("GET", "/api/sessions"),
    ("PUT", "/api/sessions/spike_x"),
    ("POST", "/api/spike_x/lids"),
    ("GET", "/api/spike_x/contacts/all"),
])
async def test_rejects_routes_outside_allowlist(method, path):
    rec = Recorder()
    async with client(rec) as c:
        with pytest.raises(RouteNotAllowed):
            await c._request(method, path)
    assert rec.requests == []


def test_check_route_extracts_session():
    assert check_route("GET", "/api/sessions/spike_a") == "spike_a"
    assert check_route("GET", "/api/spike_a/chats/all/messages") == "spike_a"
    assert check_route("GET", "/api/server/version") is None
    assert check_route("POST", "/api/keys") is None


async def test_refuses_sessions_without_prefix():
    rec = Recorder()
    async with client(rec) as c:
        with pytest.raises(ForbiddenSession):
            await c.get_session("MaroSession")
        with pytest.raises(ForbiddenSession):
            await c.create_session({"name": "MaroSession", "start": True, "config": {}})
        with pytest.raises(ForbiddenSession):
            await c.list_chats("MaroSession")
        with pytest.raises(ForbiddenSession):
            await c.create_key("MaroSession", actions={"read": True})
        with pytest.raises(ForbiddenSession):
            await c.delete_session("default")
    assert rec.requests == []


async def test_forces_download_media_false_and_reduces_messages():
    rec = Recorder(lambda req: httpx.Response(200, json=[FAKE_MSG, {**FAKE_MSG, "fromMe": True, "body": ""}]))
    async with client(rec) as c:
        page = await c.list_messages("spike_x", limit=50, offset=100, ts_gte=1700000000)
    req = rec.requests[0]
    assert req.url.path == "/api/spike_x/chats/all/messages"
    assert req.url.params["downloadMedia"] == "false"
    assert req.url.params["limit"] == "50" and req.url.params["offset"] == "100"
    assert req.url.params["filter.timestamp.gte"] == "1700000000"
    assert req.headers["X-Api-Key"] == "ADMIN-SECRET"
    assert page.raw_count == 2 and page.status == 200 and page.elapsed_ms >= 0
    assert all(isinstance(m, MessageFacts) for m in page.items)
    dumped = json.dumps([m.as_dict() for m in page.items])
    for leak in ("5491155551234", "ibuprofeno", "Marta", "@c.us"):
        assert leak not in dumped


async def test_list_messages_non_200_returns_empty_page_with_status():
    rec = Recorder(lambda req: httpx.Response(422, json={"message": "store disabled"}))
    async with client(rec) as c:
        page = await c.list_messages("spike_x")
    assert page.items == [] and page.raw_count == 0 and page.status == 422


async def test_list_chats_reduces_and_resolves_ref_for_messages():
    def responder(req):
        if req.url.path.endswith("/chats"):
            return httpx.Response(200, json=[FAKE_CHAT])
        return httpx.Response(200, json=[FAKE_MSG])
    rec = Recorder(responder)
    async with client(rec) as c:
        chats = await c.list_chats("spike_x", limit=10)
        assert isinstance(chats.items[0], ChatFacts) and chats.items[0].kind == "individual"
        assert rec.requests[0].url.params["sortBy"] == "conversationTimestamp"
        page = await c.list_messages("spike_x", chat_ref=chats.items[0].chat_ref, limit=5)
    assert rec.requests[1].url.path == "/api/spike_x/chats/5491155551234@c.us/messages"
    assert page.raw_count == 1
    assert "5491155551234" not in json.dumps([m.as_dict() for m in page.items])


async def test_per_chat_read_never_logs_jid(caplog):
    """httpx loguea 'HTTP Request: GET <url completa>' a nivel INFO: el cliente lo baja a WARNING."""
    def responder(req):
        if req.url.path.endswith("/chats"):
            return httpx.Response(200, json=[FAKE_CHAT])
        return httpx.Response(200, json=[FAKE_MSG])
    rec = Recorder(responder)
    with caplog.at_level(logging.INFO):
        async with client(rec) as c:
            chats = await c.list_chats("spike_x")
            await c.list_messages("spike_x", chat_ref=chats.items[0].chat_ref)
    assert len(rec.requests) == 2
    assert "5491155551234" not in caplog.text and "@c.us" not in caplog.text
    assert logging.getLogger("httpx").level == logging.WARNING


async def test_refs_for_phone_adds_lid_alias_without_leaking_ids():
    rec = Recorder(lambda req: httpx.Response(200, json=[{"lid": "111@lid", "pn": "5491155551234@c.us"},
                                                         {"lid": "222@lid", "pn": None}]))
    async with client(rec) as c:
        refs = await c.refs_for_phone("spike_x", "5491155551234")
        again = await c.refs_for_phone("spike_x", "5491100000000")
    assert refs == [ref(SALT, "5491155551234@c.us"), ref(SALT, "5491155551234@s.whatsapp.net"), ref(SALT, "111@lid")]
    assert again == [ref(SALT, "5491100000000@c.us"), ref(SALT, "5491100000000@s.whatsapp.net")]  # sin alias conocido
    assert len(rec.requests) == 1 and rec.requests[0].url.path == "/api/spike_x/lids"  # el mapeo se pide una vez
    assert all(r.isalpha() for r in refs) and "5491155551234" not in json.dumps(refs)


async def test_session_views_drop_me_and_qr_is_returned_raw_only():
    def responder(req):
        if req.url.path == "/api/sessions/spike_x":
            return httpx.Response(200, json={"name": "spike_x", "status": "WORKING", "engine": "NOWEB",
                                             "me": {"id": "5491166667777@c.us", "pushName": "Farmacia"},
                                             "config": {"noweb": {"markOnline": False}}})
        if req.url.path == "/api/spike_x/auth/qr":
            return httpx.Response(200, json={"value": "2@QRVALUE,abc"})
        return httpx.Response(404, json={})
    rec = Recorder(responder)
    async with client(rec) as c:
        s = await c.get_session("spike_x")
        assert set(s) == {"name", "status", "engine", "config", "me_present"} and s["me_present"] is True
        assert "5491166667777" not in json.dumps(s)
        status, value = await c.get_qr_raw("spike_x")
        assert status == 200 and value == "2@QRVALUE,abc"
        assert rec.requests[-1].url.params["format"] == "raw"
        assert await c.get_session("spike_zzz") is None


async def test_create_key_drops_value_and_delete_key_refuses_other_sessions():
    def responder(req):
        if req.method == "POST" and req.url.path == "/api/keys":
            return httpx.Response(201, json={"id": "k1", "key": "SECRET-KEY-VALUE", "session": "spike_x"})
        if req.method == "GET" and req.url.path == "/api/keys":
            return httpx.Response(200, json=[{"id": "k1", "session": "spike_x", "isAdmin": False, "key": "hash"},
                                             {"id": "k9", "session": "MaroSession", "isAdmin": False}])
        return httpx.Response(200, json={})
    rec = Recorder(responder)
    async with client(rec) as c:
        k = await c.create_key("spike_x", actions={"read": True, "send": False})
        assert k == {"id": "k1", "session": "spike_x"}
        body = json.loads(rec.requests[0].content)
        assert body["session"] == "spike_x" and body["actions"] == {"read": True, "send": False} and body["isAdmin"] is False
        keys = await c.list_keys()
        assert all(set(x) == {"id", "session", "isAdmin"} for x in keys)
        with pytest.raises(ForbiddenSession):
            await c.delete_key("k9")
        assert await c.delete_key("k1") == 200
    assert [r.method for r in rec.requests] == ["POST", "GET", "GET", "GET", "DELETE"]
    assert rec.requests[-1].url.path == "/api/keys/k1"


async def test_http_error_on_unexpected_status():
    rec = Recorder(lambda req: httpx.Response(500, json={}))
    async with client(rec) as c:
        with pytest.raises(WahaHttpError) as e:
            await c.server_version()
    assert e.value.status == 500


async def test_lids_endpoints_return_counts_only():
    def responder(req):
        if req.url.path.endswith("/lids/count"):
            return httpx.Response(200, json=3)
        return httpx.Response(200, json=[{"lid": "111@lid", "pn": "5491155551234@c.us"}, {"lid": "222@lid", "pn": None}])
    rec = Recorder(responder)
    async with client(rec) as c:
        assert await c.lids_count("spike_x") == 3
        sample = await c.lids_sample("spike_x", limit=100)
    assert sample == {"sampled": 2, "with_pn": 1}
```

- [ ] **Step 2: Correr (falla)**

Run: `python -m pytest tests/radar_spike/test_client.py -q`
Expected: `ModuleNotFoundError: No module named 'scripts.radar_spike.client'`.

- [ ] **Step 3: Implementar `client.py`**

```python
"""Cliente HTTP de WAHA para el spike: lista blanca de rutas, prefijo de sesión y lecturas reducidas.

Ningún método devuelve texto de mensajes, teléfonos, JID, nombres, QR ni claves.
Los ids crudos de chat viven solo en memoria del cliente (chat_ref -> id) para pedir mensajes por chat.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .privacy import ChatFacts, MessageFacts, reduce_chat, reduce_message, ref

SESSION_PREFIX = "spike_"
_S = r"(?P<session>[^/]+)"
ALLOWED_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/api/server/(version|status)$")),
    ("POST", re.compile(r"^/api/sessions$")),
    ("GET", re.compile(rf"^/api/sessions/{_S}$")),
    ("DELETE", re.compile(rf"^/api/sessions/{_S}$")),
    ("POST", re.compile(rf"^/api/sessions/{_S}/(start|stop|restart)$")),
    ("GET", re.compile(rf"^/api/{_S}/auth/qr$")),
    ("POST", re.compile(rf"^/api/{_S}/auth/request-code$")),
    ("GET", re.compile(rf"^/api/{_S}/chats$")),
    ("GET", re.compile(rf"^/api/{_S}/chats/[^/]+/messages$")),
    ("GET", re.compile(rf"^/api/{_S}/lids$")),
    ("GET", re.compile(rf"^/api/{_S}/lids/count$")),
    ("POST", re.compile(r"^/api/keys$")),
    ("GET", re.compile(r"^/api/keys$")),
    ("DELETE", re.compile(r"^/api/keys/[^/]+$")),
)


class WahaClientError(RuntimeError):
    pass


class RouteNotAllowed(WahaClientError):
    pass


class ForbiddenSession(WahaClientError):
    pass


class WahaHttpError(WahaClientError):
    def __init__(self, method: str, path: str, status: int) -> None:
        super().__init__(f"{method} {path} -> HTTP {status}")
        self.status = status


def check_route(method: str, path: str) -> str | None:
    """Devuelve el nombre de sesión embebido en la ruta (o None) si está permitida; si no, lanza."""
    for m, pat in ALLOWED_ROUTES:
        if m == method:
            mt = pat.match(path)
            if mt:
                return mt.groupdict().get("session")
    raise RouteNotAllowed(f"{method} {path}")


def check_session_name(name: Any) -> str:
    if not isinstance(name, str) or not name.startswith(SESSION_PREFIX):
        raise ForbiddenSession(f"solo sesiones con prefijo '{SESSION_PREFIX}'")
    return name


@dataclass(frozen=True)
class Page:
    items: list = field(default_factory=list)
    raw_count: int = 0
    elapsed_ms: int = 0
    status: int = 0


def _session_view(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": d.get("name"),
        "status": d.get("status"),
        "engine": d.get("engine"),
        "config": d.get("config") or {},
        "me_present": bool(d.get("me")),
    }


class WahaClient:
    def __init__(self, base_url: str, admin_key: str, salt: bytes, *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 600.0) -> None:
        # httpx loguea "HTTP Request: GET <url completa>" a INFO: con chats/{jid}/messages eso es un teléfono.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self._salt = salt
        self._ids: dict[str, str] = {}
        self._pn_to_lid: dict[str, str] | None = None
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Api-Key": admin_key, "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "WahaClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, *, params: dict | None = None, json: Any = None,
                       expect: tuple[int, ...] | None = (200, 201), headers: dict | None = None
                       ) -> tuple[httpx.Response, int]:
        session = check_route(method, path)
        if session is not None:
            check_session_name(session)
        if method == "POST" and path == "/api/sessions":
            check_session_name((json or {}).get("name"))
        if method == "POST" and path == "/api/keys":
            check_session_name((json or {}).get("session"))
        if method == "GET" and path.endswith("/messages"):
            params = {**(params or {}), "downloadMedia": "false"}
        t0 = time.perf_counter()
        resp = await self._http.request(method, path, params=params, json=json, headers=headers)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if expect and resp.status_code not in expect:
            raise WahaHttpError(method, path, resp.status_code)
        return resp, elapsed_ms

    # --- servidor -----------------------------------------------------------------
    async def server_version(self) -> dict[str, Any]:
        r, _ = await self._request("GET", "/api/server/version")
        d = r.json()
        return {k: d.get(k) for k in ("version", "engine", "tier")}

    async def server_status(self) -> dict[str, Any]:
        r, _ = await self._request("GET", "/api/server/status")
        d = r.json()
        return {k: d.get(k) for k in ("startTimestamp", "uptime")}

    # --- sesiones -----------------------------------------------------------------
    async def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        r, _ = await self._request("POST", "/api/sessions", json=body)
        return _session_view(r.json())

    async def get_session(self, name: str) -> dict[str, Any] | None:
        r, _ = await self._request("GET", f"/api/sessions/{name}", expect=None)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise WahaHttpError("GET", f"/api/sessions/{name}", r.status_code)
        return _session_view(r.json())

    async def delete_session(self, name: str) -> int:
        r, _ = await self._request("DELETE", f"/api/sessions/{name}", expect=None)
        return r.status_code

    async def _action(self, name: str, action: str) -> dict[str, Any]:
        r, _ = await self._request("POST", f"/api/sessions/{name}/{action}")
        return _session_view(r.json())

    async def start_session(self, name: str) -> dict[str, Any]:
        return await self._action(name, "start")

    async def stop_session(self, name: str) -> dict[str, Any]:
        return await self._action(name, "stop")

    async def restart_session(self, name: str) -> dict[str, Any]:
        return await self._action(name, "restart")

    # --- autenticación ------------------------------------------------------------
    async def get_qr_raw(self, name: str) -> tuple[int, str | None]:
        r, _ = await self._request("GET", f"/api/{name}/auth/qr", params={"format": "raw"}, expect=None)
        if r.status_code != 200:
            return r.status_code, None
        return 200, (r.json() or {}).get("value")

    async def get_qr_png(self, name: str) -> bytes | None:
        r, _ = await self._request("GET", f"/api/{name}/auth/qr", params={"format": "image"},
                                   expect=None, headers={"Accept": "image/png"})
        return r.content if r.status_code == 200 else None

    async def request_code(self, name: str, phone: str) -> tuple[int, str | None]:
        r, _ = await self._request("POST", f"/api/{name}/auth/request-code",
                                   json={"phoneNumber": phone}, expect=None)
        if r.status_code not in (200, 201):
            return r.status_code, None
        try:
            return r.status_code, (r.json() or {}).get("code")
        except ValueError:
            return r.status_code, None

    # --- lecturas reducidas -------------------------------------------------------
    async def list_chats(self, name: str, *, limit: int = 500, offset: int = 0,
                         sort_by: str = "conversationTimestamp", sort_order: str = "desc") -> Page:
        params = {"limit": limit, "offset": offset, "sortBy": sort_by, "sortOrder": sort_order}
        r, ms = await self._request("GET", f"/api/{name}/chats", params=params)
        raw = r.json()
        raw = raw if isinstance(raw, list) else []
        items: list[ChatFacts] = []
        for chat in raw:
            facts = reduce_chat(chat, self._salt)
            cid = chat.get("id")
            self._ids[facts.chat_ref] = cid.get("_serialized") if isinstance(cid, dict) else str(cid or "")
            items.append(facts)
        return Page(items, len(raw), ms, r.status_code)

    async def list_messages(self, name: str, *, chat_ref: str = "all", limit: int = 1000, offset: int = 0,
                            ts_gte: int | None = None, ts_lte: int | None = None,
                            with_text_fp: bool = False) -> Page:
        if chat_ref == "all":
            chat_id = "all"
        elif chat_ref in self._ids:
            chat_id = self._ids[chat_ref]
        else:
            raise WahaClientError("chat_ref desconocido: primero llamá list_chats en este mismo cliente")
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if ts_gte is not None:
            params["filter.timestamp.gte"] = int(ts_gte)
        if ts_lte is not None:
            params["filter.timestamp.lte"] = int(ts_lte)
        r, ms = await self._request("GET", f"/api/{name}/chats/{chat_id}/messages", params=params, expect=None)
        if r.status_code != 200:
            return Page([], 0, ms, r.status_code)
        raw = r.json()
        raw = raw if isinstance(raw, list) else []
        items = [reduce_message(m, self._salt, with_text_fp=with_text_fp) for m in raw]
        return Page(items, len(raw), ms, 200)

    async def lids_count(self, name: str) -> int:
        r, _ = await self._request("GET", f"/api/{name}/lids/count")
        d = r.json()
        return int(d.get("count", 0)) if isinstance(d, dict) else int(d or 0)

    async def lids_sample(self, name: str, limit: int = 500) -> dict[str, int]:
        r, _ = await self._request("GET", f"/api/{name}/lids", params={"limit": limit, "offset": 0})
        raw = r.json()
        raw = raw if isinstance(raw, list) else []
        return {"sampled": len(raw), "with_pn": sum(1 for x in raw if x.get("pn"))}

    async def refs_for_phone(self, name: str, digits: str) -> list[str]:
        """chat_refs de un teléfono: su @c.us, su @s.whatsapp.net y, si el store lo conoce, su @lid.

        El mapeo crudo pn -> lid se pide una sola vez (GET /lids paginado) y vive solo en memoria: salen hashes.
        """
        if self._pn_to_lid is None:
            self._pn_to_lid = {}
            offset = 0
            while True:
                r, _ = await self._request("GET", f"/api/{name}/lids", params={"limit": 500, "offset": offset})
                raw = r.json()
                raw = raw if isinstance(raw, list) else []
                for x in raw:
                    pn, lid = str(x.get("pn") or "").split("@")[0], x.get("lid")
                    if pn and lid:
                        self._pn_to_lid[pn] = str(lid)
                if len(raw) < 500:
                    break
                offset += 500
        refs = [ref(self._salt, f"{digits}@c.us"), ref(self._salt, f"{digits}@s.whatsapp.net")]
        if digits in self._pn_to_lid:
            refs.append(ref(self._salt, self._pn_to_lid[digits]))
        return refs

    # --- claves -------------------------------------------------------------------
    async def create_key(self, session: str, *, actions: dict[str, bool]) -> dict[str, Any]:
        body = {"isAdmin": False, "session": session, "isActive": True, "actions": actions}
        r, _ = await self._request("POST", "/api/keys", json=body)
        return {"id": (r.json() or {}).get("id"), "session": session}

    async def list_keys(self) -> list[dict[str, Any]]:
        r, _ = await self._request("GET", "/api/keys")
        raw = r.json()
        raw = raw if isinstance(raw, list) else []
        return [{"id": k.get("id"), "session": k.get("session"), "isAdmin": bool(k.get("isAdmin"))} for k in raw]

    async def delete_key(self, key_id: str) -> int:
        match = next((k for k in await self.list_keys() if k["id"] == key_id), None)
        if match is None:
            raise WahaClientError("clave inexistente")
        check_session_name(match["session"])
        r, _ = await self._request("DELETE", f"/api/keys/{key_id}", expect=None)
        return r.status_code
```

- [ ] **Step 4: Correr (pasa)**

Run: `python -m pytest tests/radar_spike/test_client.py -q`
Expected: `24 passed` (13 rutas rechazadas + 11 tests).

- [ ] **Step 5: Commit**

```
git add scripts/radar_spike/client.py tests/radar_spike/test_client.py
git commit -m "Spike WAHA: cliente HTTP con lista blanca, prefijo spike_ y lecturas sin contenido"
```

---

### Task 4: Cuerpo de sesión (spec P3), verificación posterior y fin de sesión

**Files:**
- Create: `scripts/radar_spike/session_spec.py`, `scripts/radar_spike/lifecycle.py`
- Test: `tests/radar_spike/test_session_spec.py`, `tests/radar_spike/test_lifecycle.py`

**Interfaces:**
- Consumes: `client.WahaClient`, `client.check_session_name`.
- Produces (`session_spec`): `EVENTS`, `IGNORE`, `DEFAULT_RETRIES`, `GOWS_STORAGE_FULL`; `build_session_body(name: str, *, webhook_url: str, hmac_key: str, run_id: str, engine: str = "NOWEB", store_enabled: bool = True, full_sync: bool = False, retries: dict | None = DEFAULT_RETRIES) -> dict`; `verify_session_config(session: dict, body: dict) -> list[str]` (lista de campos que no coinciden; vacía = OK).
- Produces (`lifecycle`): `ConfigMismatch(RuntimeError)`; `ensure_session(client, body) -> dict` (`{name, status, engine, retries_effective}`); `wait_for_status(client, name, *, targets=("WORKING",), fail_on=("FAILED",), timeout_s=600, interval_s=2.0) -> dict` (`{status, seconds, transitions, timed_out}`); `teardown_session(client, name) -> dict` (`{status_before, delete_status, session_gone, keys_deleted, keys_remaining, keys_error, ok}`; `keys_remaining` es `None` y `ok` es `False` cuando el paso de claves falló).

- [ ] **Step 1: Test del cuerpo y la verificación (falla)**

`tests/radar_spike/test_session_spec.py`:

```python
import copy

import pytest

from scripts.radar_spike.client import ForbiddenSession
from scripts.radar_spike.session_spec import (
    DEFAULT_RETRIES, EVENTS, build_session_body, verify_session_config,
)

ARGS = dict(webhook_url="https://tunel.example/webhook/waha", hmac_key="HMAC-SECRET", run_id="nw0")


def test_body_matches_spec_p3_for_noweb():
    body = build_session_body("spike_nw0", **ARGS)
    assert body["name"] == "spike_nw0" and body["start"] is True
    cfg = body["config"]
    assert cfg["metadata"] == {"spike_run": "nw0"}
    assert cfg["ignore"] == {"status": True, "groups": True, "channels": True, "broadcast": True}
    assert cfg["noweb"] == {"markOnline": False, "store": {"enabled": True, "fullSync": False}}
    hook = cfg["webhooks"][0]
    assert hook["url"] == ARGS["webhook_url"] and hook["hmac"] == {"key": "HMAC-SECRET"}
    assert hook["events"] == ["message", "message.any", "message.ack", "message.edited", "message.revoked", "session.status"]
    assert hook["retries"] == {"policy": "exponential", "delaySeconds": 2, "attempts": 15} == DEFAULT_RETRIES
    assert "gows" not in cfg


def test_body_variants():
    full = build_session_body("spike_nw1", full_sync=True, **ARGS)
    assert full["config"]["noweb"]["store"] == {"enabled": True, "fullSync": True}
    nostore = build_session_body("spike_ns0", store_enabled=False, **ARGS)
    assert nostore["config"]["noweb"]["store"]["enabled"] is False
    noretry = build_session_body("spike_qr0", retries=None, **ARGS)
    assert "retries" not in noretry["config"]["webhooks"][0]
    gows = build_session_body("spike_gw0", engine="GOWS", **ARGS)
    assert "noweb" not in gows["config"]
    assert gows["config"]["gows"]["storage"] == {"messages": True, "chats": True, "groups": False,
                                                 "labels": False, "contacts": True, "messageSecrets": True}
    gows_nomsg = build_session_body("spike_gw1", engine="GOWS", store_enabled=False, **ARGS)
    assert gows_nomsg["config"]["gows"]["storage"]["messages"] is False
    with pytest.raises(ForbiddenSession):
        build_session_body("v_abc", **ARGS)
    with pytest.raises(ValueError):
        build_session_body("spike_x", engine="WEBJS", **ARGS)


def _echo(body):
    return {"name": body["name"], "status": "STARTING", "engine": "NOWEB", "config": copy.deepcopy(body["config"])}


def test_verify_accepts_exact_echo_and_extra_keys():
    body = build_session_body("spike_nw0", **ARGS)
    echo = _echo(body)
    assert verify_session_config(echo, body) == []
    echo["config"]["noweb"]["store"]["extra"] = 1
    echo["config"]["ignore"]["extra"] = True
    assert verify_session_config(echo, body) == []


@pytest.mark.parametrize("mutate,field", [
    (lambda c: c["noweb"].__setitem__("markOnline", True), "noweb.markOnline"),
    (lambda c: c["noweb"].pop("markOnline"), "noweb.markOnline"),
    (lambda c: c["noweb"]["store"].__setitem__("enabled", False), "noweb.store"),
    (lambda c: c["noweb"]["store"].__setitem__("fullSync", True), "noweb.store"),
    (lambda c: c["ignore"].__setitem__("groups", False), "ignore"),
    (lambda c: c.pop("ignore"), "ignore"),
    (lambda c: c["webhooks"][0].__setitem__("url", "https://otro"), "webhooks.url"),
    (lambda c: c["webhooks"][0].__setitem__("events", ["message"]), "webhooks.events"),
    (lambda c: c.__setitem__("webhooks", []), "webhooks.url"),
])
def test_verify_reports_each_mismatch(mutate, field):
    body = build_session_body("spike_nw0", **ARGS)
    echo = _echo(body)
    mutate(echo["config"])
    assert field in verify_session_config(echo, body)


def test_verify_gows_compares_storage():
    body = build_session_body("spike_gw0", engine="GOWS", **ARGS)
    echo = _echo(body)
    assert verify_session_config(echo, body) == []
    echo["config"]["gows"]["storage"]["messages"] = False
    assert verify_session_config(echo, body) == ["gows.storage"]
    assert "ignore" in verify_session_config({"config": {}}, body)
```

- [ ] **Step 2: Correr (falla)**

Run: `python -m pytest tests/radar_spike/test_session_spec.py -q`
Expected: `ModuleNotFoundError: No module named 'scripts.radar_spike.session_spec'`.

- [ ] **Step 3: Implementar `session_spec.py`**

```python
"""Cuerpo de creación de sesión (spec §3 P3, con prefijo spike_) y verificación posterior."""
from __future__ import annotations

from typing import Any

from .client import check_session_name

EVENTS: list[str] = ["message", "message.any", "message.ack", "message.edited", "message.revoked", "session.status"]
IGNORE: dict[str, bool] = {"status": True, "groups": True, "channels": True, "broadcast": True}
DEFAULT_RETRIES: dict[str, Any] = {"policy": "exponential", "delaySeconds": 2, "attempts": 15}
GOWS_STORAGE_FULL: dict[str, bool] = {
    "messages": True, "chats": True, "groups": False, "labels": False, "contacts": True, "messageSecrets": True,
}


def build_session_body(name: str, *, webhook_url: str, hmac_key: str, run_id: str, engine: str = "NOWEB",
                       store_enabled: bool = True, full_sync: bool = False,
                       retries: dict[str, Any] | None = DEFAULT_RETRIES) -> dict[str, Any]:
    check_session_name(name)
    webhook: dict[str, Any] = {"url": webhook_url, "events": list(EVENTS), "hmac": {"key": hmac_key}}
    if retries is not None:
        webhook["retries"] = dict(retries)
    config: dict[str, Any] = {"metadata": {"spike_run": run_id}, "ignore": dict(IGNORE), "webhooks": [webhook]}
    if engine == "NOWEB":
        config["noweb"] = {"markOnline": False, "store": {"enabled": store_enabled, "fullSync": full_sync}}
    elif engine == "GOWS":
        config["gows"] = {"storage": {**GOWS_STORAGE_FULL, "messages": store_enabled}}
    else:
        raise ValueError(f"motor no soportado por el spike: {engine}")
    return {"name": name, "start": True, "config": config}


def _subset_matches(got: Any, want: dict[str, Any]) -> bool:
    return isinstance(got, dict) and all(got.get(k) == v for k, v in want.items())


def verify_session_config(session: dict[str, Any], body: dict[str, Any]) -> list[str]:
    """Compara lo que WAHA devolvió en GET /api/sessions/{name} con lo pedido. Lista vacía = OK."""
    got = (session or {}).get("config") or {}
    want = body["config"]
    problems: list[str] = []
    if not _subset_matches(got.get("ignore"), want["ignore"]):
        problems.append("ignore")
    if "noweb" in want:
        gn = got.get("noweb") or {}
        if gn.get("markOnline") is not False:
            problems.append("noweb.markOnline")
        if not _subset_matches(gn.get("store"), want["noweb"]["store"]):
            problems.append("noweb.store")
    if "gows" in want:
        gg = (got.get("gows") or {}).get("storage")
        if not _subset_matches(gg, want["gows"]["storage"]):
            problems.append("gows.storage")
    hooks = got.get("webhooks") or []
    if not hooks or hooks[0].get("url") != want["webhooks"][0]["url"]:
        problems.append("webhooks.url")
    elif set(hooks[0].get("events") or []) != set(want["webhooks"][0]["events"]):
        problems.append("webhooks.events")
    return problems
```

- [ ] **Step 4: Correr (pasa)**

Run: `python -m pytest tests/radar_spike/test_session_spec.py -q`
Expected: `13 passed` (4 funciones + `test_verify_reports_each_mismatch` en 9 casos).

- [ ] **Step 5: Test del ciclo de vida (falla)**

`tests/radar_spike/test_lifecycle.py`:

```python
import copy

import httpx
import pytest

from scripts.radar_spike.client import WahaClient
from scripts.radar_spike.lifecycle import ConfigMismatch, ensure_session, teardown_session, wait_for_status
from scripts.radar_spike.session_spec import build_session_body

SALT = b"\x04" * 32
BODY = build_session_body("spike_nw0", webhook_url="https://t/webhook/waha", hmac_key="H", run_id="nw0")


class FakeWaha:
    """Servidor WAHA mínimo con estado para sesiones y claves."""

    def __init__(self, *, echo_mutation=None, statuses=None, keys_status=200):
        self.sessions: dict[str, dict] = {}
        self.keys = [{"id": "k1", "session": "spike_nw0", "isAdmin": False},
                     {"id": "k2", "session": "spike_nw0", "isAdmin": False},
                     {"id": "k9", "session": "MaroSession", "isAdmin": False}]
        self.calls: list[str] = []
        self.echo_mutation = echo_mutation
        self.statuses = list(statuses or [])
        self.keys_status = keys_status  # 403: el endpoint de claves falla (p. ej. tier sin claves por sesión)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(f"{req.method} {req.url.path}")
        p = req.url.path
        if req.method == "POST" and p == "/api/sessions":
            import json
            body = json.loads(req.content)
            cfg = copy.deepcopy(body["config"])
            if self.echo_mutation:
                self.echo_mutation(cfg)
            self.sessions[body["name"]] = {"name": body["name"], "status": "STARTING", "engine": "NOWEB", "config": cfg}
            return httpx.Response(201, json=self.sessions[body["name"]])
        if req.method == "GET" and p.startswith("/api/sessions/"):
            s = self.sessions.get(p.rsplit("/", 1)[1])
            if s is None:
                return httpx.Response(404, json={})
            if self.statuses:
                s = {**s, "status": self.statuses.pop(0)}
            return httpx.Response(200, json=s)
        if req.method == "DELETE" and p.startswith("/api/sessions/"):
            self.sessions.pop(p.rsplit("/", 1)[1], None)
            return httpx.Response(200, json={})
        if req.method == "GET" and p == "/api/keys":
            if self.keys_status != 200:
                return httpx.Response(self.keys_status, json={})
            return httpx.Response(200, json=self.keys)
        if req.method == "DELETE" and p.startswith("/api/keys/"):
            kid = p.rsplit("/", 1)[1]
            self.keys = [k for k in self.keys if k["id"] != kid]
            return httpx.Response(200, json={})
        return httpx.Response(500, json={})


def client(fake):
    return WahaClient("http://waha", "K", SALT, transport=httpx.MockTransport(fake))


async def test_ensure_session_ok_returns_effective_retries():
    fake = FakeWaha()
    async with client(fake) as c:
        info = await ensure_session(c, BODY)
    assert info == {"name": "spike_nw0", "status": "STARTING", "engine": "NOWEB",
                    "retries_effective": {"policy": "exponential", "delaySeconds": 2, "attempts": 15}}
    assert fake.calls == ["POST /api/sessions", "GET /api/sessions/spike_nw0"]


async def test_ensure_session_aborts_and_deletes_on_mark_online_true():
    fake = FakeWaha(echo_mutation=lambda c: c["noweb"].__setitem__("markOnline", True))
    async with client(fake) as c:
        with pytest.raises(ConfigMismatch) as e:
            await ensure_session(c, BODY)
    assert "noweb.markOnline" in str(e.value)
    assert fake.calls[-1] == "DELETE /api/sessions/spike_nw0" and fake.sessions == {}


async def test_ensure_session_aborts_on_store_or_ignore_mismatch():
    fake = FakeWaha(echo_mutation=lambda c: (c["noweb"]["store"].__setitem__("fullSync", True), c.pop("ignore")))
    async with client(fake) as c:
        with pytest.raises(ConfigMismatch) as e:
            await ensure_session(c, BODY)
    assert "noweb.store" in str(e.value) and "ignore" in str(e.value)


async def test_wait_for_status_records_transitions():
    fake = FakeWaha(statuses=["STARTING", "SCAN_QR_CODE", "SCAN_QR_CODE", "WORKING"])
    async with client(fake) as c:
        await c.create_session(BODY)
        out = await wait_for_status(c, "spike_nw0", timeout_s=10, interval_s=0)
    assert out["status"] == "WORKING" and out["timed_out"] is False
    assert [t["status"] for t in out["transitions"]] == ["STARTING", "SCAN_QR_CODE", "WORKING"]


async def test_wait_for_status_stops_on_failed_and_times_out():
    fake = FakeWaha(statuses=["SCAN_QR_CODE", "FAILED"])
    async with client(fake) as c:
        await c.create_session(BODY)
        out = await wait_for_status(c, "spike_nw0", timeout_s=10, interval_s=0)
        assert out["status"] == "FAILED"
        fake.statuses = ["STARTING"] * 50
        out = await wait_for_status(c, "spike_nw0", timeout_s=0.05, interval_s=0.01)
    assert out["timed_out"] is True and out["status"] == "STARTING"


async def test_teardown_uses_single_delete_then_keys_then_verifies_404():
    fake = FakeWaha()
    async with client(fake) as c:
        await c.create_session(BODY)
        fake.calls.clear()
        out = await teardown_session(c, "spike_nw0")
    assert out == {"status_before": "STARTING", "delete_status": 200, "session_gone": True,
                   "keys_deleted": 2, "keys_remaining": 0, "keys_error": None, "ok": True}
    assert fake.calls[:2] == ["GET /api/sessions/spike_nw0", "DELETE /api/sessions/spike_nw0"]
    assert fake.calls.count("DELETE /api/sessions/spike_nw0") == 1
    assert not any("logout" in call for call in fake.calls)
    assert "DELETE /api/keys/k1" in fake.calls and "DELETE /api/keys/k2" in fake.calls
    assert "DELETE /api/keys/k9" not in fake.calls
    assert [k["id"] for k in fake.keys] == ["k9"]
    assert fake.calls[-2:] == ["GET /api/sessions/spike_nw0", "GET /api/keys"]


async def test_teardown_keys_error_does_not_block_verification():
    """spec §6.3 punto 6: un paso fallido no bloquea los siguientes. El DELETE ya es irreversible."""
    fake = FakeWaha(keys_status=403)
    async with client(fake) as c:
        await c.create_session(BODY)
        out = await teardown_session(c, "spike_nw0")
    assert out["session_gone"] is True and out["delete_status"] == 200
    assert out["keys_error"] == "WahaHttpError" and out["keys_deleted"] == 0
    assert out["keys_remaining"] is None and out["ok"] is False
    assert fake.sessions == {} and fake.calls[-1] == "GET /api/sessions/spike_nw0"
```

- [ ] **Step 6: Correr (falla)**

Run: `python -m pytest tests/radar_spike/test_lifecycle.py -q`
Expected: `ModuleNotFoundError: No module named 'scripts.radar_spike.lifecycle'`.

- [ ] **Step 7: Implementar `lifecycle.py`**

```python
"""Crear (con verificación y aborto), esperar estado y terminar sesiones spike_."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .client import WahaClient, WahaClientError, check_session_name
from .session_spec import verify_session_config


class ConfigMismatch(RuntimeError):
    pass


async def ensure_session(client: WahaClient, body: dict[str, Any]) -> dict[str, Any]:
    """POST /api/sessions, relee GET /api/sessions/{name} y aborta (borrando) si la config no coincide."""
    name = check_session_name(body["name"])
    await client.create_session(body)
    session = await client.get_session(name) or {}
    problems = verify_session_config(session, body)
    if problems:
        await client.delete_session(name)
        raise ConfigMismatch("la sesión no quedó como se pidió: " + ", ".join(problems) + " (sesión borrada)")
    hooks = (session.get("config") or {}).get("webhooks") or [{}]
    return {
        "name": name,
        "status": session.get("status"),
        "engine": session.get("engine"),
        "retries_effective": hooks[0].get("retries"),
    }


async def wait_for_status(client: WahaClient, name: str, *, targets: tuple[str, ...] = ("WORKING",),
                          fail_on: tuple[str, ...] = ("FAILED",), timeout_s: float = 600,
                          interval_s: float = 2.0) -> dict[str, Any]:
    t0 = time.monotonic()
    transitions: list[dict[str, Any]] = []
    last: str | None = None
    while True:
        session = await client.get_session(name)
        status = (session or {}).get("status")
        if status != last:
            transitions.append({"t": round(time.monotonic() - t0, 1), "status": status})
            last = status
        if status in targets or status in fail_on:
            return {"status": status, "seconds": round(time.monotonic() - t0, 1),
                    "transitions": transitions, "timed_out": False}
        if time.monotonic() - t0 >= timeout_s:
            return {"status": status, "seconds": round(time.monotonic() - t0, 1),
                    "transitions": transitions, "timed_out": True}
        await asyncio.sleep(interval_s)


async def teardown_session(client: WahaClient, name: str) -> dict[str, Any]:
    """Un solo DELETE de la sesión (nunca logout), después las claves, después verificación.

    El estado previo se registra porque el dispositivo solo se desvincula del teléfono si la sesión estaba
    WORKING (punto 14). Un error en el paso de claves se anota y no impide verificar el 404.
    """
    check_session_name(name)
    status_before = ((await client.get_session(name)) or {}).get("status")
    delete_status = await client.delete_session(name)
    deleted = 0
    keys_error: str | None = None
    try:
        mine = [k for k in await client.list_keys() if k.get("session") == name]
        for k in mine:
            await client.delete_key(k["id"])
            deleted += 1
    except WahaClientError as e:
        keys_error = type(e).__name__
    gone = await client.get_session(name) is None
    remaining: int | None = None
    if keys_error is None:
        remaining = len([k for k in await client.list_keys() if k.get("session") == name])
    return {
        "status_before": status_before,
        "delete_status": delete_status,
        "session_gone": gone,
        "keys_deleted": deleted,
        "keys_remaining": remaining,
        "keys_error": keys_error,
        "ok": gone and remaining == 0 and keys_error is None,
    }
```

- [ ] **Step 8: Correr (pasa)**

Run: `python -m pytest tests/radar_spike/test_lifecycle.py tests/radar_spike/test_session_spec.py -q`
Expected: `20 passed` (lifecycle 7 + session_spec 13).

- [ ] **Step 9: Commit**

```
git add scripts/radar_spike/session_spec.py scripts/radar_spike/lifecycle.py tests/radar_spike/test_session_spec.py tests/radar_spike/test_lifecycle.py
git commit -m "Spike WAHA: cuerpo de sesion del spec, verificacion posterior y fin con un solo DELETE"
```

---

### Task 5: Almacén de resultados (agregados, sal por corrida, hora de vínculo)

**Files:**
- Create: `scripts/radar_spike/results.py`
- Test: `tests/radar_spike/test_results.py`

**Interfaces:**
- Consumes: `privacy.assert_clean`, `privacy.new_salt`, `privacy.PrivacyViolation`.
- Produces: `ResultsStore(root: Path, run_id: str)` con `.dir: Path`, `init() -> None`, `salt() -> bytes`, `forget_salt() -> None`, `path(name) -> Path`, `write(name: str, data: dict) -> Path`, `read(name) -> dict`, `append_jsonl(name, rec: dict) -> None`, `read_jsonl(name) -> list[dict]`, `set_link_ts(ts: float) -> None`, `link_ts() -> float | None`.

- [ ] **Step 1: Test (falla)**

`tests/radar_spike/test_results.py`:

```python
import json

import pytest

from scripts.radar_spike.privacy import PrivacyViolation
from scripts.radar_spike.results import ResultsStore


def test_init_creates_dir_salt_and_run_json(tmp_path):
    s = ResultsStore(tmp_path, "nw0")
    with pytest.raises(FileNotFoundError):
        s.salt()
    s.init()
    assert s.dir == tmp_path / "nw0" and (s.dir / "salt.bin").exists()
    assert len(s.salt()) == 32 and s.read("run.json")["run_id"] == "nw0"
    salt = s.salt()
    s.init()  # idempotente: no cambia la sal
    assert s.salt() == salt
    s.forget_salt()
    assert not (s.dir / "salt.bin").exists()


def test_write_refuses_pii_and_leaves_no_file(tmp_path):
    s = ResultsStore(tmp_path, "nw0")
    s.init()
    with pytest.raises(PrivacyViolation):
        s.write("bad.json", {"chat": "5491155551234@c.us"})
    assert not (s.dir / "bad.json").exists()
    with pytest.raises(PrivacyViolation):
        s.append_jsonl("bad.jsonl", {"x": "999@lid"})
    assert not (s.dir / "bad.jsonl").exists()


def test_write_read_roundtrip_and_jsonl(tmp_path):
    s = ResultsStore(tmp_path, "nw0")
    s.init()
    p = s.write("a.json", {"total": 3, "by_month": {"2026-08": 2}})
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["total"] == 3 and "written_at" in data and s.read("a.json") == data
    s.append_jsonl("ev.jsonl", {"event": "message.any", "from_me": False})
    s.append_jsonl("ev.jsonl", {"event": "session.status", "status": "WORKING"})
    assert s.read_jsonl("ev.jsonl") == [{"event": "message.any", "from_me": False},
                                        {"event": "session.status", "status": "WORKING"}]
    assert s.read_jsonl("nada.jsonl") == []


def test_link_ts(tmp_path):
    s = ResultsStore(tmp_path, "nw0")
    s.init()
    assert s.link_ts() is None
    s.set_link_ts(1758400000.5)
    assert s.link_ts() == 1758400000.5
```

- [ ] **Step 2: Correr (falla)**

Run: `python -m pytest tests/radar_spike/test_results.py -q`
Expected: `ModuleNotFoundError: No module named 'scripts.radar_spike.results'`.

- [ ] **Step 3: Implementar `results.py`**

```python
"""Resultados del spike: solo agregados, en results/<run_id>/, con assert_clean antes de escribir."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import assert_clean, new_salt


class ResultsStore:
    def __init__(self, root: Path, run_id: str) -> None:
        self.dir = Path(root) / run_id

    def init(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        salt_path = self.dir / "salt.bin"
        if not salt_path.exists():
            salt_path.write_bytes(new_salt())
        if not (self.dir / "run.json").exists():
            self.write("run.json", {"run_id": self.dir.name})

    def salt(self) -> bytes:
        salt_path = self.dir / "salt.bin"
        if not salt_path.exists():
            raise FileNotFoundError(f"sin sal en {salt_path}: corré 'init-run' primero")
        return salt_path.read_bytes()

    def forget_salt(self) -> None:
        (self.dir / "salt.bin").unlink(missing_ok=True)

    def path(self, name: str) -> Path:
        return self.dir / name

    def write(self, name: str, data: dict[str, Any]) -> Path:
        payload = {**data, "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        assert_clean(payload)
        p = self.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    def read(self, name: str) -> dict[str, Any]:
        return json.loads(self.path(name).read_text(encoding="utf-8"))

    def append_jsonl(self, name: str, rec: dict[str, Any]) -> None:
        assert_clean(rec)
        with self.path(name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read_jsonl(self, name: str) -> list[dict[str, Any]]:
        p = self.path(name)
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def set_link_ts(self, ts: float) -> None:
        self.write("link.json", {"link_ts": ts})

    def link_ts(self) -> float | None:
        p = self.path("link.json")
        if not p.exists():
            return None
        return self.read("link.json").get("link_ts")
```

- [ ] **Step 4: Correr (pasa)**

Run: `python -m pytest tests/radar_spike/test_results.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```
git add scripts/radar_spike/results.py tests/radar_spike/test_results.py
git commit -m "Spike WAHA: almacen de resultados con sal por corrida y guarda de PII"
```

---

### Task 6: Receptor de webhooks (HMAC sha512 fail-closed, registro sin contenido)

**Files:**
- Create: `scripts/radar_spike/receiver.py`
- Test: `tests/radar_spike/test_receiver.py`

**Interfaces:**
- Consumes: `privacy.ref`, `privacy.reduce_message`, `privacy.chat_id_of`, `results.ResultsStore`, `config.DEFAULT_RESULTS_DIR`, `config.load_env_file`.
- Produces: `verify_hmac(raw: bytes, header: str | None, key: str) -> bool`; `extract_record(envelope: dict, *, salt: bytes, link_ts: float | None, received_ms: int) -> dict`; `build_app(store: ResultsStore, hmac_key: str, mode: str = "ok") -> FastAPI` (rutas `POST /webhook/waha`, `GET /health`); `app_from_env() -> FastAPI` (factory para `uvicorn --factory`; primero carga `SPIKE_ENV_FILE` o `.env.spike` con `load_env_file`, así el `SPIKE_RUN_ID` de la corrida es el del archivo; el `run_id` queda fijado al arrancar).

- [ ] **Step 1: Test (falla)**

`tests/radar_spike/test_receiver.py`:

```python
import hashlib
import hmac
import json

import httpx
import pytest

from scripts.radar_spike.receiver import build_app, extract_record, verify_hmac
from scripts.radar_spike.results import ResultsStore

KEY = "HMAC-SECRET"
ENVELOPE = {
    "id": "evt_01HZY8Q4N9K3Z1",
    "timestamp": 1758400010123,
    "event": "message.any",
    "session": "spike_nw0",
    "metadata": {"spike_run": "nw0"},
    "me": {"id": "5491166667777@c.us", "pushName": "Farmacia"},
    "payload": {
        "id": "false_5491155551234@c.us_3EB0ABCDEF12",
        "timestamp": 1758400009,
        "from": "5491155551234@c.us",
        "fromMe": False,
        "to": "5491166667777@c.us",
        "body": "Hola, tienen ibuprofeno 600? soy Marta",
        "hasMedia": False,
        "_data": {"pushName": "Marta Gomez"},
    },
}
LEAKS = ("5491155551234", "5491166667777", "ibuprofeno", "Marta", "@c.us", "3EB0ABCDEF12", "evt_01HZY8Q4N9K3Z1")


def sign(raw: bytes, key: str = KEY) -> str:
    return hmac.new(key.encode(), raw, hashlib.sha512).hexdigest()


def make(tmp_path, mode="ok", key=KEY):
    store = ResultsStore(tmp_path, "nw0")
    store.init()
    app = build_app(store, key, mode)
    return store, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_verify_hmac():
    raw = b'{"a":1}'
    assert verify_hmac(raw, sign(raw), KEY) is True
    assert verify_hmac(raw, sign(raw).upper(), KEY) is True
    assert verify_hmac(raw, sign(raw, "otra"), KEY) is False
    assert verify_hmac(raw, None, KEY) is False
    assert verify_hmac(raw, sign(raw), "") is False


def test_extract_record_has_only_facts():
    rec = extract_record(ENVELOPE, salt=b"\x05" * 32, link_ts=1758400100.0, received_ms=1758400010500)
    assert set(rec) == {"event_ref", "event", "session", "ts_ms", "received_ms", "payload_ts", "from_me",
                        "has_text", "has_media", "chat_ref", "is_lid", "is_history"}
    assert rec["event"] == "message.any" and rec["ts_ms"] == 1758400010123 and rec["payload_ts"] == 1758400009
    assert rec["from_me"] is False and rec["has_text"] is True and rec["is_lid"] is False
    assert rec["is_history"] is True  # payload anterior a la hora de vínculo
    dumped = json.dumps(rec)
    for leak in LEAKS:
        assert leak not in dumped
    later = extract_record(ENVELOPE, salt=b"\x05" * 32, link_ts=1758400000.0, received_ms=0)
    assert later["is_history"] is False
    unknown = extract_record(ENVELOPE, salt=b"\x05" * 32, link_ts=None, received_ms=0)
    assert unknown["is_history"] is None
    lid = extract_record({**ENVELOPE, "payload": {**ENVELOPE["payload"], "from": "111@lid"}}, salt=b"\x05" * 32, link_ts=None, received_ms=0)
    assert lid["is_lid"] is True
    status = extract_record({**ENVELOPE, "event": "session.status", "payload": {"name": "spike_nw0", "status": "WORKING"}},
                            salt=b"\x05" * 32, link_ts=None, received_ms=0)
    assert status["status"] == "WORKING" and "chat_ref" not in status


async def test_signed_request_is_recorded_without_content(tmp_path):
    store, http = make(tmp_path)
    raw = json.dumps(ENVELOPE).encode()
    async with http:
        r = await http.post("/webhook/waha", content=raw, headers={"X-Webhook-Hmac": sign(raw), "Content-Type": "application/json"})
    assert r.status_code == 200
    text = store.path("events.jsonl").read_text(encoding="utf-8")
    for leak in LEAKS:
        assert leak not in text
    recs = store.read_jsonl("events.jsonl")
    assert len(recs) == 1 and recs[0]["event"] == "message.any" and recs[0]["is_history"] is None


async def test_bad_signature_and_missing_key_are_rejected(tmp_path):
    store, http = make(tmp_path)
    raw = json.dumps(ENVELOPE).encode()
    async with http:
        r1 = await http.post("/webhook/waha", content=raw, headers={"X-Webhook-Hmac": sign(raw, "otra")})
        r2 = await http.post("/webhook/waha", content=raw)
    assert r1.status_code == 401 and r2.status_code == 401
    assert not store.path("events.jsonl").exists()
    store2, http2 = make(tmp_path / "b", key="")
    async with http2:
        r3 = await http2.post("/webhook/waha", content=raw, headers={"X-Webhook-Hmac": sign(raw, "")})
    assert r3.status_code == 401 and not store2.path("events.jsonl").exists()


async def test_fail_mode_records_then_returns_500_and_history_uses_link(tmp_path):
    store, http = make(tmp_path, mode="fail")
    store.set_link_ts(1758400100.0)
    raw = json.dumps(ENVELOPE).encode()
    async with http:
        r = await http.post("/webhook/waha", content=raw, headers={"X-Webhook-Hmac": sign(raw)})
        h = await http.get("/health")
    assert r.status_code == 500 and h.status_code == 200
    recs = store.read_jsonl("events.jsonl")
    assert len(recs) == 1 and recs[0]["is_history"] is True
```

- [ ] **Step 2: Correr (falla)**

Run: `python -m pytest tests/radar_spike/test_receiver.py -q`
Expected: `ModuleNotFoundError: No module named 'scripts.radar_spike.receiver'`.

- [ ] **Step 3: Implementar `receiver.py`**

```python
"""Receptor de webhooks del spike: HMAC sha512 fail-closed y registro de hechos sin contenido.

Arranque: uvicorn --factory scripts.radar_spike.receiver:app_from_env --port 8787
Lee primero el archivo SPIKE_ENV_FILE (por defecto .env.spike del directorio actual) con la precedencia de
config.load_env_file. Variables: SPIKE_WEBHOOK_HMAC_KEY, SPIKE_RUN_ID, SPIKE_RESULTS_DIR (opcional),
SPIKE_RECEIVER_MODE=ok|fail (opcional; en la shell si el archivo lo deja vacío).
El run_id se fija al arrancar: reiniciar el receptor cada vez que cambia SPIKE_RUN_ID.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response

from .config import DEFAULT_RESULTS_DIR, load_env_file
from .privacy import chat_id_of, reduce_message, ref
from .results import ResultsStore


def verify_hmac(raw: bytes, header: str | None, key: str) -> bool:
    if not header or not key:
        return False
    expected = hmac.new(key.encode("utf-8"), raw, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, header.strip().lower())


def extract_record(envelope: dict[str, Any], *, salt: bytes, link_ts: float | None, received_ms: int) -> dict[str, Any]:
    payload = envelope.get("payload") or {}
    event = envelope.get("event")
    rec: dict[str, Any] = {
        "event_ref": ref(salt, str(envelope.get("id") or "")),
        "event": event,
        "session": envelope.get("session"),
        "ts_ms": envelope.get("timestamp"),
        "received_ms": received_ms,
    }
    if isinstance(event, str) and event.startswith("message"):
        facts = reduce_message(payload, salt)
        is_history: bool | None = None
        if link_ts is not None and facts.timestamp > 0:
            is_history = facts.timestamp < link_ts
        rec.update({
            "payload_ts": facts.timestamp or None,
            "from_me": facts.from_me,
            "has_text": facts.has_text,
            "has_media": facts.has_media,
            "chat_ref": facts.chat_ref,
            "is_lid": chat_id_of(payload).endswith("@lid"),
            "is_history": is_history,
        })
    elif event == "session.status":
        rec["status"] = payload.get("status")
    return rec


def build_app(store: ResultsStore, hmac_key: str, mode: str = "ok") -> FastAPI:
    app = FastAPI(title="radar_spike receiver", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"ok": "1", "mode": mode}

    @app.post("/webhook/waha")
    async def hook(request: Request) -> Response:
        raw = await request.body()
        if not verify_hmac(raw, request.headers.get("X-Webhook-Hmac"), hmac_key):
            return Response(status_code=401)
        try:
            envelope = json.loads(raw)
        except ValueError:
            return Response(status_code=400)
        rec = extract_record(envelope, salt=store.salt(), link_ts=store.link_ts(),
                             received_ms=int(time.time() * 1000))
        store.append_jsonl("events.jsonl", rec)
        if mode == "fail":
            return Response(status_code=500)
        return Response(content=b'{"ok":true}', media_type="application/json")

    return app


def app_from_env() -> FastAPI:
    load_env_file(os.environ.get("SPIKE_ENV_FILE") or ".env.spike")
    key = os.environ.get("SPIKE_WEBHOOK_HMAC_KEY", "")  # vacío => 401 siempre (fail-closed)
    run_id = os.environ.get("SPIKE_RUN_ID")
    if not run_id:
        raise RuntimeError("falta SPIKE_RUN_ID (en .env.spike, en SPIKE_ENV_FILE o en el entorno)")
    store = ResultsStore(Path(os.environ.get("SPIKE_RESULTS_DIR") or DEFAULT_RESULTS_DIR), run_id)
    store.init()
    return build_app(store, key, os.environ.get("SPIKE_RECEIVER_MODE") or "ok")
```

- [ ] **Step 4: Correr (pasa)**

Run: `python -m pytest tests/radar_spike/test_receiver.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Arranque manual del receptor (sin WAHA)**

Run (PowerShell, en una terminal aparte; en este punto `.env.spike` todavía no existe, así que el receptor toma las variables de la shell; si ya existiera, su `SPIKE_RUN_ID` no vacío ganaría):
```
$env:SPIKE_RUN_ID="local"; $env:SPIKE_WEBHOOK_HMAC_KEY="prueba"; python -m uvicorn --factory scripts.radar_spike.receiver:app_from_env --port 8787
```
En otra terminal: `curl.exe -s -o NUL -w "%{http_code}" -X POST http://localhost:8787/webhook/waha -d "{}"`
Expected: `401` (sin firma). Después, Ctrl+C en el receptor y en esa misma terminal:
```
Remove-Item Env:SPIKE_RUN_ID, Env:SPIKE_WEBHOOK_HMAC_KEY
Remove-Item -Recurse scripts/radar_spike/results/local
```
(Las variables se quitan para que no queden colgadas en la shell: el runner y el receptor de las corridas reales las toman de `.env.spike`.)

- [ ] **Step 6: Commit**

```
git add scripts/radar_spike/receiver.py tests/radar_spike/test_receiver.py
git commit -m "Spike WAHA: receptor de webhooks con HMAC sha512 fail-closed y registro sin contenido"
```

---

### Task 7: Funciones de medición puras

**Files:**
- Create: `scripts/radar_spike/measures.py`
- Test: `tests/radar_spike/test_measures.py`

**Interfaces:**
- Consumes: `privacy.MessageFacts`.
- Produces: `month_key(ts: int) -> str`; `count_summary(facts: Iterable[MessageFacts]) -> dict`; `monthly_by_label(facts, labels: dict[str, list[str]]) -> dict[str, dict[str, dict[str, int]]]` (una etiqueta puede tener varios `chat_ref`: el `@c.us` y el `@lid` del mismo contacto); `stable_since(samples: list[dict], window_s: float) -> float | None`; `autoreply_summary(facts, *, fast_s: int = 10, min_chats: int = 3, fast_share: float = 0.8) -> dict`; `events_summary(records: list[dict], link_ts: float | None) -> dict` (`is_history` se recalcula post hoc con `payload_ts < link_ts` cuando el receptor lo dejó en `None`; `history_unknown` cuenta los que siguen sin poder decidirse); `qr_timeline(samples: list[dict]) -> dict`.

- [ ] **Step 1: Test (falla)**

`tests/radar_spike/test_measures.py`:

```python
from scripts.radar_spike import measures
from scripts.radar_spike.privacy import MessageFacts

AUG = 1755000000  # 2025-08-12 UTC
SEP = 1757000000  # 2025-09-04 UTC


def m(chat, ts, from_me=False, text=True, media=False, fp=None):
    return MessageFacts(chat_ref=chat, timestamp=ts, from_me=from_me, has_text=text, has_media=media, text_fp=fp)


def test_count_summary():
    facts = [m("a", AUG), m("a", AUG + 5, from_me=True), m("b", SEP, text=False, media=True), m("b", 0)]
    s = measures.count_summary(facts)
    assert s["total"] == 4 and s["from_me"] == 1 and s["inbound"] == 3
    assert s["with_text"] == 3 and s["with_media"] == 1 and s["chats"] == 2
    assert s["oldest_ts"] == AUG and s["newest_ts"] == SEP and s["without_timestamp"] == 1
    assert s["by_month"] == {"2025-08": 2, "2025-09": 1}
    assert s["oldest_iso"].startswith("2025-08-12") and s["newest_iso"].startswith("2025-09-04")
    assert measures.count_summary([])["total"] == 0 and measures.count_summary([])["oldest_ts"] is None


def test_monthly_by_label_only_counts_labelled_chats_and_merges_aliases():
    facts = [m("a", AUG), m("a", AUG + 1, from_me=True), m("a", SEP), m("a_lid", SEP + 1), m("z", SEP)]
    out = measures.monthly_by_label(facts, {"A": ["a", "a_lid"], "B": ["b"]})
    assert out == {"A": {"2025-08": {"inbound": 1, "from_me": 1}, "2025-09": {"inbound": 2, "from_me": 0}}, "B": {}}


def test_stable_since():
    samples = [{"t": 0, "chats": 10, "msgs": 100}, {"t": 30, "chats": 12, "msgs": 150},
               {"t": 60, "chats": 12, "msgs": 150}, {"t": 90, "chats": 12, "msgs": 150}]
    assert measures.stable_since(samples, 60) == 30
    assert measures.stable_since(samples, 90) is None
    assert measures.stable_since(samples[:2], 0) == 30
    assert measures.stable_since([], 10) is None


def test_autoreply_summary_flags_repeated_fast_texts():
    facts = []
    for i, chat in enumerate(("c1", "c2", "c3", "c4")):
        t = AUG + i * 1000
        facts += [m(chat, t), m(chat, t + 3, from_me=True, fp="saludo"), m(chat, t + 600, from_me=True, fp=f"humano{i}")]
    facts += [m("c5", SEP), m("c5", SEP + 1, from_me=True, fp="saludo")]          # rápido, 5.º chat
    facts += [m("c6", SEP), m("c6", SEP + 3600, from_me=True, fp="repetido")]      # repetido pero lento
    facts += [m("c7", SEP), m("c7", SEP + 3600, from_me=True, fp="repetido")]
    facts += [m("c8", SEP), m("c8", SEP + 3600, from_me=True, fp="repetido")]
    out = measures.autoreply_summary(facts)
    assert out["from_me_total"] == 12
    assert [c["text_fp"] for c in out["candidates"]] == ["saludo"]
    assert out["candidates"][0] == {"text_fp": "saludo", "chats": 5, "messages": 5, "fast_share": 1.0}
    assert out["flagged_messages"] == 5 and out["flagged_share"] == round(5 / 12, 3)
    assert out["repeated_texts_not_fast"] == 1


def test_events_summary_detects_duplication_retries_and_history():
    recs = [
        {"event_ref": "e1", "event": "message", "session": "s", "ts_ms": 1000, "received_ms": 1200, "payload_ts": 1, "from_me": False, "chat_ref": "a", "is_lid": True, "is_history": True},
        {"event_ref": "e2", "event": "message.any", "session": "s", "ts_ms": 1000, "received_ms": 1300, "payload_ts": 1, "from_me": False, "chat_ref": "a", "is_lid": True, "is_history": True},
        {"event_ref": "e3", "event": "message.any", "session": "s", "ts_ms": 2000, "received_ms": 2100, "payload_ts": 2, "from_me": True, "chat_ref": "a", "is_lid": True, "is_history": False},
        {"event_ref": "e3", "event": "message.any", "session": "s", "ts_ms": 2000, "received_ms": 4100, "payload_ts": 2, "from_me": True, "chat_ref": "a", "is_lid": True, "is_history": False},
        {"event_ref": "e3", "event": "message.any", "session": "s", "ts_ms": 2000, "received_ms": 8100, "payload_ts": 2, "from_me": True, "chat_ref": "a", "is_lid": True, "is_history": False},
        {"event_ref": "e4", "event": "session.status", "session": "s", "ts_ms": 500, "received_ms": 600, "status": "WORKING"},
        {"event_ref": "e5", "event": "message.ack", "session": "s", "ts_ms": 3000, "received_ms": 3050, "payload_ts": None, "from_me": True, "chat_ref": "b", "is_lid": False, "is_history": None},
        # llegó antes de que existiera link.json (is_history None): se decide post hoc con payload_ts < link_ts
        {"event_ref": "e6", "event": "message.any", "session": "s", "ts_ms": 900, "received_ms": 1100, "payload_ts": 1, "from_me": True, "chat_ref": "a", "is_lid": True, "is_history": None},
    ]
    out = measures.events_summary(recs, link_ts=1.5)
    assert out["by_event"] == {"message": 1, "message.any": 5, "session.status": 1, "message.ack": 1}
    assert out["message_vs_any_inbound"] == {"message": 1, "message_any_inbound": 1, "duplicated": True}
    assert out["history_events"] == 3 and out["history_unknown"] == 0
    assert out["distinct_chats"] == 2 and out["lid_chats"] == 1
    assert out["delivery"] == {"distinct_events": 6, "events_retried": 1, "max_attempts": 3, "retry_gaps_s": [2.0, 4.0]}
    assert out["status_timeline"] == [{"t_ms": 500, "status": "WORKING"}]
    assert out["lag_ms_p50"] == 200 and out["link_ts"] == 1.5
    no_link = measures.events_summary(recs, link_ts=None)
    assert no_link["history_events"] == 2 and no_link["history_unknown"] == 1  # e6 no se puede decidir sin link_ts
    assert measures.events_summary([], None)["delivery"]["max_attempts"] == 0


def test_qr_timeline():
    samples = [{"t": 0.0, "status": "STARTING", "qr_ref": None},
               {"t": 2.0, "status": "SCAN_QR_CODE", "qr_ref": "q1"},
               {"t": 30.0, "status": "SCAN_QR_CODE", "qr_ref": "q1"},
               {"t": 62.0, "status": "SCAN_QR_CODE", "qr_ref": "q2"},
               {"t": 82.0, "status": "SCAN_QR_CODE", "qr_ref": "q3"},
               {"t": 102.0, "status": "FAILED", "qr_ref": None}]
    out = measures.qr_timeline(samples)
    assert out["first_qr_at"] == 2.0 and out["qr_codes"] == 3
    assert out["rotations_at"] == [62.0, 82.0] and out["qr_lifetimes_s"] == [60.0, 20.0]
    assert out["final_status"] == "FAILED" and out["final_at"] == 102.0 and out["samples"] == 6
    assert measures.qr_timeline([])["qr_codes"] == 0
```

- [ ] **Step 2: Correr (falla)**

Run: `python -m pytest tests/radar_spike/test_measures.py -q`
Expected: `ImportError: cannot import name 'measures' from 'scripts.radar_spike'`.

- [ ] **Step 3: Implementar `measures.py`**

```python
"""Agregaciones puras del spike. Entran hechos reducidos; salen solo conteos y tiempos."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable

from .privacy import MessageFacts


def month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds") if ts else None


def count_summary(facts: Iterable[MessageFacts]) -> dict[str, Any]:
    total = from_me = with_text = with_media = without_ts = 0
    chats: set[str] = set()
    oldest: int | None = None
    newest: int | None = None
    by_month: Counter[str] = Counter()
    for f in facts:
        total += 1
        from_me += f.from_me
        with_text += f.has_text
        with_media += f.has_media
        chats.add(f.chat_ref)
        if f.timestamp <= 0:
            without_ts += 1
            continue
        oldest = f.timestamp if oldest is None or f.timestamp < oldest else oldest
        newest = f.timestamp if newest is None or f.timestamp > newest else newest
        by_month[month_key(f.timestamp)] += 1
    return {
        "total": total, "from_me": from_me, "inbound": total - from_me,
        "with_text": with_text, "with_media": with_media, "chats": len(chats),
        "without_timestamp": without_ts,
        "oldest_ts": oldest, "newest_ts": newest, "oldest_iso": _iso(oldest), "newest_iso": _iso(newest),
        "by_month": dict(sorted(by_month.items())),
    }


def monthly_by_label(facts: Iterable[MessageFacts], labels: dict[str, list[str]]) -> dict[str, dict[str, dict[str, int]]]:
    """labels: etiqueta elegida por el humano -> chat_refs del mismo contacto (@c.us y @lid se suman).

    Devuelve etiqueta -> mes -> {inbound, from_me}.
    """
    inverse = {chat_ref: label for label, refs in labels.items() for chat_ref in refs}
    out: dict[str, dict[str, dict[str, int]]] = {label: {} for label in labels}
    for f in facts:
        label = inverse.get(f.chat_ref)
        if label is None or f.timestamp <= 0:
            continue
        month = out[label].setdefault(month_key(f.timestamp), {"inbound": 0, "from_me": 0})
        month["from_me" if f.from_me else "inbound"] += 1
    return {label: dict(sorted(months.items())) for label, months in out.items()}


def stable_since(samples: list[dict[str, Any]], window_s: float) -> float | None:
    """samples ordenados: [{t, chats, msgs}]. t del primer sample desde el cual nada cambió por >= window_s."""
    if not samples:
        return None
    last = samples[-1]
    start = None
    for s in reversed(samples):
        if s["chats"] == last["chats"] and s["msgs"] == last["msgs"]:
            start = s
        else:
            break
    if start is None or last["t"] - start["t"] < window_s:
        return None
    return start["t"]


def autoreply_summary(facts: Iterable[MessageFacts], *, fast_s: int = 10, min_chats: int = 3,
                      fast_share: float = 0.8) -> dict[str, Any]:
    """Heurística de spec §4.2: texto propio repetido literal en muchos chats y enviado a segundos del entrante."""
    by_chat: dict[str, list[MessageFacts]] = defaultdict(list)
    from_me_total = 0
    for f in facts:
        by_chat[f.chat_ref].append(f)
        from_me_total += f.from_me
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"chats": set(), "n": 0, "fast": 0})
    for chat, msgs in by_chat.items():
        last_in: int | None = None
        for f in sorted(msgs, key=lambda x: x.timestamp):
            if not f.from_me:
                last_in = f.timestamp
                continue
            if not f.text_fp:
                continue
            st = stats[f.text_fp]
            st["chats"].add(chat)
            st["n"] += 1
            if last_in is not None and 0 <= f.timestamp - last_in <= fast_s:
                st["fast"] += 1
    candidates = []
    repeated = 0
    for fp, st in stats.items():
        if len(st["chats"]) < min_chats:
            continue
        repeated += 1
        share = st["fast"] / st["n"]
        if share >= fast_share:
            candidates.append({"text_fp": fp, "chats": len(st["chats"]), "messages": st["n"], "fast_share": round(share, 2)})
    candidates.sort(key=lambda c: -c["messages"])
    flagged = sum(c["messages"] for c in candidates)
    return {
        "from_me_total": from_me_total,
        "candidates": candidates,
        "flagged_messages": flagged,
        "flagged_share": round(flagged / from_me_total, 3) if from_me_total else 0.0,
        "repeated_texts_not_fast": repeated - len(candidates),
    }


def events_summary(records: list[dict[str, Any]], link_ts: float | None) -> dict[str, Any]:
    def _is_history(r: dict[str, Any]) -> bool:
        # El receptor decide en el momento; si link.json no existía todavía (is_history None), se decide acá.
        if r.get("is_history") is not None:
            return bool(r["is_history"])
        return bool(link_ts and r.get("payload_ts") and r["payload_ts"] < link_ts)

    by_event: Counter[str] = Counter(r.get("event") for r in records)
    msgs = [r for r in records if r.get("event") in ("message", "message.any")]
    n_message = by_event.get("message", 0)
    n_any_inbound = sum(1 for r in msgs if r["event"] == "message.any" and not r.get("from_me"))
    attempts: Counter[str] = Counter(r.get("event_ref") for r in records)
    retry_gaps: list[float] = []
    if attempts:
        worst, count = attempts.most_common(1)[0]
        if count > 1:
            times = sorted(r["received_ms"] for r in records if r.get("event_ref") == worst)
            retry_gaps = [round((b - a) / 1000, 1) for a, b in zip(times, times[1:])]
    lags = [r["received_ms"] - r["ts_ms"] for r in records if r.get("ts_ms") and r.get("received_ms")]
    return {
        "by_event": dict(by_event),
        "message_vs_any_inbound": {"message": n_message, "message_any_inbound": n_any_inbound,
                                   "duplicated": n_message > 0 and n_message == n_any_inbound},
        "history_events": sum(1 for r in msgs if _is_history(r)),
        "history_unknown": sum(1 for r in msgs if r.get("is_history") is None and not link_ts),
        "distinct_chats": len({r["chat_ref"] for r in records if r.get("chat_ref")}),
        "lid_chats": len({r["chat_ref"] for r in records if r.get("chat_ref") and r.get("is_lid")}),
        "delivery": {
            "distinct_events": len(attempts),
            "events_retried": sum(1 for v in attempts.values() if v > 1),
            "max_attempts": max(attempts.values()) if attempts else 0,
            "retry_gaps_s": retry_gaps,
        },
        "status_timeline": [{"t_ms": r.get("ts_ms"), "status": r.get("status")}
                            for r in records if r.get("event") == "session.status"],
        "lag_ms_p50": int(median(lags)) if lags else None,
        "link_ts": link_ts,
    }


def qr_timeline(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """samples: [{t, status, qr_ref}] cada ~2 s. Cuenta códigos distintos y su vida útil."""
    seen: list[tuple[float, str]] = []
    for s in samples:
        q = s.get("qr_ref")
        if q and (not seen or seen[-1][1] != q):
            seen.append((s["t"], q))
    rotations = [t for t, _ in seen[1:]]
    lifetimes = [round(b - a, 1) for (a, _), (b, _) in zip(seen, seen[1:])]
    last = samples[-1] if samples else {}
    return {
        "samples": len(samples),
        "first_qr_at": seen[0][0] if seen else None,
        "qr_codes": len(seen),
        "rotations_at": rotations,
        "qr_lifetimes_s": lifetimes,
        "final_status": last.get("status"),
        "final_at": last.get("t"),
    }
```

- [ ] **Step 4: Correr (pasa)**

Run: `python -m pytest tests/radar_spike/test_measures.py -q`
Expected: `6 passed`.

- [ ] **Step 5: Commit**

```
git add scripts/radar_spike/measures.py tests/radar_spike/test_measures.py
git commit -m "Spike WAHA: agregaciones puras (conteos, estabilidad, respuesta automatica, eventos, QR)"
```

---

### Task 8: CLI de mediciones (`runner.py`)

**Files:**
- Create: `scripts/radar_spike/runner.py`
- Test: `tests/radar_spike/test_runner.py`

**Interfaces:**
- Consumes: todo lo anterior. `TRANSPORT_FACTORY: Callable[[], httpx.AsyncBaseTransport | None]` (gancho para tests).
- Produces: `build_parser() -> argparse.ArgumentParser`; `main(argv: list[str] | None = None) -> None`; corrutinas `cmd_<nombre>(args)`; helpers `full_pass(client, name, *, msg_limit, with_text_fp=False) -> tuple[list[MessageFacts], dict]` y `count_chats(client, name, *, limit=500) -> dict`. Subcomandos: `init-run`, `check-server`, `create-session`, `status`, `restart`, `qr`, `pairing-code`, `wait-working`, `stability`, `count-pass`, `chat-kinds`, `monthly-counts`, `autoreply`, `events-summary`, `reconcile-probe`, `lids`, `keys-create`, `no-store-probe`, `watch`, `note`, `teardown`.

- [ ] **Step 1: Test (falla)**

`tests/radar_spike/test_runner.py`:

```python
import argparse
import json
import logging
import time

import httpx
import pytest

from scripts.radar_spike import runner
from scripts.radar_spike.lifecycle import ConfigMismatch
from scripts.radar_spike.results import ResultsStore

ENV = {
    "WAHA_BASE_URL": "http://waha",
    "WAHA_ADMIN_KEY": "ADMIN-SECRET",
    "SPIKE_WEBHOOK_PUBLIC_URL": "https://tunel.example",
    "SPIKE_WEBHOOK_HMAC_KEY": "HMAC-SECRET",
    "SPIKE_RUN_ID": "t1",
}
FAKE_MSG = {"id": "false_5491155551234@c.us_AB", "timestamp": 1758400000, "from": "5491155551234@c.us",
            "fromMe": False, "to": "5491166667777@c.us", "body": "hola soy Marta", "hasMedia": False}
# El mismo contacto escribiendo desde su @lid (el servidor falso mapea 1@lid -> 5491155551234@c.us en /lids).
LID_MSG = {**FAKE_MSG, "id": "false_1@lid_CD", "from": "1@lid", "timestamp": 1758400001}
MESSAGES = [FAKE_MSG, FAKE_MSG, FAKE_MSG, LID_MSG]


class Server:
    def __init__(self):
        self.calls = []
        self.sessions = {}
        self.keys = []
        self.mark_online = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(f"{req.method} {req.url.path}")
        p, m = req.url.path, req.method
        if p == "/api/server/version":
            return httpx.Response(200, json={"version": "2026.8.2", "engine": "NOWEB", "tier": "CORE", "browser": "x"})
        if p == "/api/server/status":
            return httpx.Response(200, json={"startTimestamp": 1, "uptime": 2})
        if m == "POST" and p == "/api/sessions":
            body = json.loads(req.content)
            cfg = body["config"]
            cfg["noweb"]["markOnline"] = self.mark_online
            self.sessions[body["name"]] = {"name": body["name"], "status": "SCAN_QR_CODE", "engine": "NOWEB", "config": cfg}
            return httpx.Response(201, json=self.sessions[body["name"]])
        if m == "GET" and p.startswith("/api/sessions/"):
            s = self.sessions.get(p.rsplit("/", 1)[1])
            return httpx.Response(200, json=s) if s else httpx.Response(404, json={})
        if m == "DELETE" and p.startswith("/api/sessions/"):
            self.sessions.pop(p.rsplit("/", 1)[1], None)
            return httpx.Response(200, json={})
        if p == "/api/keys" and m == "POST":
            self.keys.append({"id": "k1", "session": json.loads(req.content)["session"], "isAdmin": False})
            return httpx.Response(201, json={"id": "k1", "key": "SECRET-KEY"})
        if p == "/api/keys" and m == "GET":
            return httpx.Response(200, json=self.keys)
        if m == "DELETE" and p.startswith("/api/keys/"):
            self.keys = [k for k in self.keys if k["id"] != p.rsplit("/", 1)[1]]
            return httpx.Response(200, json={})
        if p.endswith("/chats"):
            offset = int(req.url.params.get("offset", 0))
            # conversationTimestamp reciente: reconcile-probe (ventana de 10 min) tiene que entrar al paso 2 por chat
            return httpx.Response(200, json=[{"id": "5491155551234@c.us", "name": "Marta", "conversationTimestamp": int(time.time())},
                                             {"id": "1203630@g.us", "name": "Grupo"}] if offset == 0 else [])
        if p.endswith("/messages"):
            offset = int(req.url.params.get("offset", 0))
            limit = int(req.url.params.get("limit", 10))
            if limit >= 5000:  # punto 3: un limit enorme puede colgar al servidor; el runner lo registra como 'timeout'
                raise httpx.ReadTimeout("lento", request=req)
            return httpx.Response(200, json=MESSAGES[:limit] if offset == 0 else [])
        if p.endswith("/lids/count"):
            return httpx.Response(200, json={"count": 2})
        if p.endswith("/lids"):
            return httpx.Response(200, json=[{"lid": "1@lid", "pn": "5491155551234@c.us"}, {"lid": "2@lid"}])
        return httpx.Response(500, json={})


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SPIKE_RESULTS_DIR", str(tmp_path))
    server = Server()
    monkeypatch.setattr(runner, "TRANSPORT_FACTORY", lambda: httpx.MockTransport(server))
    store = ResultsStore(tmp_path, "t1")
    return server, store


def ns(**kw):
    base = dict(env_file=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _no_pii(path):
    text = path.read_text(encoding="utf-8")
    for leak in ("5491155551234", "5491166667777", "Marta", "@c.us", "@g.us", "@lid", "ADMIN-SECRET", "HMAC-SECRET", "SECRET-KEY"):
        assert leak not in text


async def test_init_and_check_server(env, capsys):
    server, store = env
    await runner.cmd_init_run(ns())
    assert (store.dir / "salt.bin").exists()
    await runner.cmd_check_server(ns())
    data = store.read("server.json")
    assert data["version"] == {"version": "2026.8.2", "engine": "NOWEB", "tier": "CORE"}
    out = capsys.readouterr().out
    assert "2026.8.2" in out and "ADMIN-SECRET" not in out


def test_env_file_values_win_over_stale_shell(env, tmp_path, monkeypatch):
    f = tmp_path / "corrida.env"
    f.write_text("SPIKE_RUN_ID=nw9\n", encoding="utf-8")
    monkeypatch.setenv("SPIKE_RUN_ID", "viejo")
    cfg, store, _ = runner._ctx(ns(env_file=str(f)))
    assert cfg.run_id == "nw9" and store.dir.name == "nw9"


async def test_http_request_logs_never_show_jids(env, caplog):
    """reconcile-probe lee por chat (URL con el JID): ni httpx ni el logger del spike pueden imprimirlo."""
    server, store = env
    await runner.cmd_init_run(ns())
    with caplog.at_level(logging.INFO):
        await runner.cmd_reconcile_probe(ns(window_minutes=10))
    assert any("/messages" in c and "@c.us" in c for c in server.calls)  # sí se pidió por chat
    assert "5491155551234" not in caplog.text and "@c.us" not in caplog.text and "@g.us" not in caplog.text
    data = store.read("reconcile.json")
    assert data["chats_touched"] == 2 and data["step2_requests"] == 4
    _no_pii(store.path("reconcile.json"))


async def test_create_session_ok_then_abort_on_bad_echo(env):
    server, store = env
    await runner.cmd_init_run(ns())
    await runner.cmd_create_session(ns(engine="NOWEB", no_store=False, full_sync=True, no_retries=False))
    data = store.read("session_create.json")
    assert data["name"] == "spike_t1" and data["requested"]["full_sync"] is True
    assert data["retries_effective"]["attempts"] == 15
    _no_pii(store.path("session_create.json"))
    server.mark_online = True
    with pytest.raises(ConfigMismatch):
        await runner.cmd_create_session(ns(engine="NOWEB", no_store=False, full_sync=False, no_retries=True))
    assert "spike_t1" not in server.sessions


async def test_count_pass_chat_kinds_and_autoreply_write_clean_aggregates(env):
    server, store = env
    await runner.cmd_init_run(ns())
    await runner.cmd_count_pass(ns(limits="2,5,5000", msg_limit=1000))
    data = store.read("count_pass.json")
    assert data["limit_probes"][0]["limit"] == 2 and data["limit_probes"][0]["returned"] == 2
    assert data["limit_probes"][1]["returned"] == 4
    assert data["limit_probes"][2] == {"limit": 5000, "returned": None, "elapsed_ms": None, "status": "timeout"}
    assert data["full_pass"]["total"] == 4 and data["summary"]["chats"] == 2  # @c.us y @lid son chat_refs distintos
    assert data["chats"]["total"] == 2
    _no_pii(store.path("count_pass.json"))
    await runner.cmd_chat_kinds(ns())
    kinds = store.read("chat_kinds.json")
    assert kinds["by_kind"] == {"individual": 1, "group": 1} and kinds["total"] == 2
    _no_pii(store.path("chat_kinds.json"))
    await runner.cmd_autoreply(ns(msg_limit=1000))
    auto = store.read("autoreply.json")
    assert auto["from_me_total"] == 0 and auto["candidates"] == []
    assert "text_fp" not in store.path("autoreply.json").read_text(encoding="utf-8")
    _no_pii(store.path("autoreply.json"))


async def test_monthly_counts_uses_labels_from_owner_file_and_merges_lid(env, tmp_path, monkeypatch):
    server, store = env
    f = tmp_path / "compara.csv"
    f.write_text("A,5491155551234\nB,5491100000000\n", encoding="utf-8")
    monkeypatch.setenv("SPIKE_COMPARE_CHATS_FILE", str(f))
    await runner.cmd_init_run(ns())
    await runner.cmd_monthly_counts(ns(msg_limit=1000))
    data = store.read("monthly_counts.json")
    # 3 mensajes por @c.us + 1 por el @lid del mismo contacto (mapeado en /lids) = 4 bajo la etiqueta A
    assert data["by_label"]["A"] == {"2025-09": {"inbound": 4, "from_me": 0}} and data["by_label"]["B"] == {}
    assert data["labels_without_messages"] == ["B"]
    csv_text = store.path("monthly_counts.csv").read_text(encoding="utf-8")
    assert "A,2025-09,4,0" in csv_text
    _no_pii(store.path("monthly_counts.json"))
    _no_pii(store.path("monthly_counts.csv"))
    f.write_text("5491155551234,5491155551234\n", encoding="utf-8")  # una etiqueta que es un teléfono
    with pytest.raises(SystemExit):
        await runner.cmd_monthly_counts(ns(msg_limit=1000))


async def test_events_summary_lids_keys_and_teardown(env):
    server, store = env
    await runner.cmd_init_run(ns())
    await runner.cmd_create_session(ns(engine="NOWEB", no_store=False, full_sync=False, no_retries=False))
    with pytest.raises(SystemExit):  # sin events.jsonl: el receptor no corre con este SPIKE_RUN_ID, no es 'cero eventos'
        await runner.cmd_events_summary(ns())
    store.append_jsonl("events.jsonl", {"event_ref": "e1", "event": "message", "session": "spike_t1", "ts_ms": 10, "received_ms": 20,
                                        "payload_ts": 1, "from_me": False, "chat_ref": "a", "is_lid": False, "is_history": None})
    store.append_jsonl("events.jsonl", {"event_ref": "e2", "event": "session.status", "session": "spike_otra", "ts_ms": 10,
                                        "received_ms": 20, "status": "WORKING"})
    await runner.cmd_events_summary(ns())
    ev = store.read("events_summary.json")
    assert ev["by_event"] == {"message": 1, "session.status": 1} and ev["records_other_sessions"] == 1
    await runner.cmd_lids(ns())
    lids = store.read("lids.json")
    assert lids["count"] == 2 and lids["sample"] == {"sampled": 2, "with_pn": 1}
    await runner.cmd_keys_create(ns())
    keys = store.read("keys.json")
    assert keys["created"] is True and keys["session"] == "spike_t1" and "id" not in keys
    assert keys["id_ref"].isalpha() and server.keys[0]["session"] == "spike_t1"
    _no_pii(store.path("keys.json"))
    await runner.cmd_teardown(ns())
    t = store.read("teardown.json")
    assert t["ok"] is True and t["keys_deleted"] == 1 and t["status_before"] == "SCAN_QR_CODE"
    assert server.sessions == {} and server.keys == []
    assert not (store.dir / "salt.bin").exists()
    assert not any("logout" in c for c in server.calls)


async def test_note_and_no_store_probe(env):
    server, store = env
    await runner.cmd_init_run(ns())
    await runner.cmd_note(ns(key="ram_mb", value="512"))
    await runner.cmd_note(ns(key="disk_bytes", value="2500000000"))  # 2,5 GB: el punto 13 mide stores grandes
    notes = store.read_jsonl("notes.jsonl")
    assert [n["key"] for n in notes] == ["ram_mb", "disk_bytes"] and notes[-1]["value"] == 2500000000.0
    with pytest.raises(SystemExit):
        await runner.cmd_note(ns(key="Clave Mala", value="1"))
    with pytest.raises(SystemExit):
        await runner.cmd_note(ns(key="ram_mb", value="5491155551234@c.us"))
    await runner.cmd_no_store_probe(ns())
    d = store.read("no_store.json")
    assert d["messages"]["status"] == 200 and d["messages"]["returned"] == 4 and d["lids_count"] == 2


def test_parser_has_all_commands():
    p = runner.build_parser()
    names = {"init-run", "check-server", "create-session", "status", "restart", "qr", "pairing-code", "wait-working",
             "stability", "count-pass", "chat-kinds", "monthly-counts", "autoreply", "events-summary",
             "reconcile-probe", "lids", "keys-create", "no-store-probe", "watch", "note", "teardown"}
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    assert names <= set(sub.choices)
    args = p.parse_args(["--env-file", ".env.spike", "count-pass", "--limits", "100,1000"])
    assert args.env_file == ".env.spike" and args.limits == "100,1000" and args.func is runner.cmd_count_pass
```

- [ ] **Step 2: Correr (falla)**

Run: `python -m pytest tests/radar_spike/test_runner.py -q`
Expected: `ImportError: cannot import name 'runner' from 'scripts.radar_spike'`.

- [ ] **Step 3: Implementar `runner.py`**

```python
"""CLI del spike de WAHA. Uso: python -m scripts.radar_spike.runner [--env-file .env.spike] <comando> [opciones]

Cada comando escribe un JSON de agregados en results/<SPIKE_RUN_ID>/ y lo imprime. Nunca imprime secretos,
teléfonos, JID, nombres, texto ni QR (salvo el código de vinculación, que el dueño tiene que tipear).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import httpx

from .client import WahaClient
from .config import SpikeConfig, load_env_file
from .lifecycle import ensure_session, teardown_session, wait_for_status
from .measures import (autoreply_summary, count_summary, events_summary, monthly_by_label,
                       qr_timeline, stable_since)
from .privacy import MessageFacts, RedactingFilter, assert_clean, get_logger, ref
from .results import ResultsStore
from .session_spec import DEFAULT_RETRIES, build_session_body

TRANSPORT_FACTORY: Callable[[], httpx.AsyncBaseTransport | None] = lambda: None
READ_ONLY_ACTIONS = {"read": True, "send": False, "control": False, "setting": False, "app": False, "delete": False}


def _ctx(args: argparse.Namespace) -> tuple[SpikeConfig, ResultsStore, Any]:
    if getattr(args, "env_file", None):
        load_env_file(args.env_file)  # el archivo explícito es la fuente de verdad (pisa valores viejos de la shell)
    cfg = SpikeConfig.from_env()
    # httpx/httpcore imprimen URLs completas (con JID) a INFO; y todo lo que llegue al handler raíz se redacta.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter(cfg.secrets))
    store = ResultsStore(cfg.results_dir, cfg.run_id)
    return cfg, store, get_logger(cfg.secrets)


def _client(cfg: SpikeConfig, store: ResultsStore) -> WahaClient:
    return WahaClient(cfg.waha_base_url, cfg.waha_admin_key, store.salt(), transport=TRANSPORT_FACTORY())


def _emit(store: ResultsStore, name: str, data: dict[str, Any]) -> None:
    path = store.write(name, data)
    print(json.dumps({"archivo": str(path), **data}, ensure_ascii=False, indent=2))


async def count_chats(client: WahaClient, name: str, *, limit: int = 500) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    total = offset = requests = 0
    while True:
        page = await client.list_chats(name, limit=limit, offset=offset)
        requests += 1
        for ch in page.items:
            kinds[ch.kind] += 1
        total += page.raw_count
        offset += limit
        if page.raw_count == 0:
            break
    return {"total": total, "by_kind": dict(kinds), "requests": requests}


async def full_pass(client: WahaClient, name: str, *, msg_limit: int,
                    with_text_fp: bool = False) -> tuple[list[MessageFacts], dict[str, Any]]:
    """Recorre chats/all/messages con offset += limit hasta una página vacía (spec §6.3 punto 2)."""
    facts: list[MessageFacts] = []
    offset = requests = 0
    t0 = time.perf_counter()
    last_status = 0
    while True:
        page = await client.list_messages(name, limit=msg_limit, offset=offset, with_text_fp=with_text_fp)
        requests += 1
        last_status = page.status
        facts.extend(page.items)
        offset += msg_limit
        if page.raw_count == 0 or page.status != 200:
            break
    elapsed = time.perf_counter() - t0
    return facts, {"total": len(facts), "requests": requests, "elapsed_s": round(elapsed, 1),
                   "msgs_per_min": round(len(facts) / (elapsed / 60), 1) if elapsed > 0 else None,
                   "last_status": last_status}


# --- comandos -------------------------------------------------------------------------
async def cmd_init_run(args: argparse.Namespace) -> None:
    cfg, store, log = _ctx(args)
    store.init()
    log.info("corrida %s: sesion %s, resultados en %s", cfg.run_id, cfg.session_name, store.dir)
    print(json.dumps({"run_id": cfg.run_id, "session": cfg.session_name, "dir": str(store.dir)}))


async def cmd_check_server(args: argparse.Namespace) -> None:
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        out = {"version": await c.server_version(), "status": await c.server_status()}
    _emit(store, "server.json", out)


async def cmd_create_session(args: argparse.Namespace) -> None:
    cfg, store, log = _ctx(args)
    body = build_session_body(
        cfg.session_name, webhook_url=f"{cfg.webhook_public_url}/webhook/waha", hmac_key=cfg.webhook_hmac_key,
        run_id=cfg.run_id, engine=args.engine, store_enabled=not args.no_store, full_sync=args.full_sync,
        retries=None if args.no_retries else DEFAULT_RETRIES,
    )
    async with _client(cfg, store) as c:
        info = await ensure_session(c, body)
    log.info("sesion %s creada y verificada", cfg.session_name)
    _emit(store, "session_create.json", {**info, "requested": {
        "engine": args.engine, "store_enabled": not args.no_store, "full_sync": args.full_sync,
        "retries_requested": None if args.no_retries else DEFAULT_RETRIES}})


async def cmd_status(args: argparse.Namespace) -> None:
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        s = await c.get_session(cfg.session_name)
    print(json.dumps({"session": cfg.session_name, "status": (s or {}).get("status"), "engine": (s or {}).get("engine"),
                      "exists": s is not None, "me_present": (s or {}).get("me_present")}))


async def cmd_restart(args: argparse.Namespace) -> None:
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        s = await c.restart_session(cfg.session_name)
    print(json.dumps({"session": cfg.session_name, "status": s.get("status")}))


def _write_temp_png(data: bytes, previous: str | None) -> str:
    if previous:
        Path(previous).unlink(missing_ok=True)
    fd, path = tempfile.mkstemp(prefix="spike_qr_", suffix=".png")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


async def cmd_qr(args: argparse.Namespace) -> None:
    """Punto 8: rotaciones del QR hasta WORKING o FAILED. --png escribe el QR en un archivo temporal fuera del repo."""
    cfg, store, log = _ctx(args)
    samples: list[dict[str, Any]] = []
    png_path: str | None = None
    last_qr: str | None = None
    t0 = time.monotonic()
    async with _client(cfg, store) as c:
        while time.monotonic() - t0 < args.max_seconds:
            s = await c.get_session(cfg.session_name)
            status = (s or {}).get("status")
            qr_ref: str | None = None
            if status == "SCAN_QR_CODE":
                _, value = await c.get_qr_raw(cfg.session_name)
                if value:
                    qr_ref = ref(store.salt(), value)
                if args.png and qr_ref and qr_ref != last_qr:
                    data = await c.get_qr_png(cfg.session_name)
                    if data:
                        png_path = _write_temp_png(data, png_path)
                        print(f"QR nuevo: abrilo y escanealo -> {png_path}")
                last_qr = qr_ref
            samples.append({"t": round(time.monotonic() - t0, 1), "status": status, "qr_ref": qr_ref})
            if status in ("WORKING", "FAILED", "STOPPED"):
                break
            await asyncio.sleep(args.interval)
    if png_path:
        Path(png_path).unlink(missing_ok=True)
    if samples and samples[-1]["status"] == "WORKING":
        store.set_link_ts(time.time())
        log.info("WORKING a los %s s; hora de vinculo guardada", samples[-1]["t"])
    _emit(store, "qr_timeline.json", qr_timeline(samples))


async def cmd_pairing_code(args: argparse.Namespace) -> None:
    """Punto 8: código de vinculación. El teléfono viene de SPIKE_PAIRING_PHONE y no se escribe a disco."""
    cfg, store, _ = _ctx(args)
    phone = re.sub(r"\D", "", os.environ.get("SPIKE_PAIRING_PHONE", ""))
    if not phone:
        raise SystemExit("falta SPIKE_PAIRING_PHONE (formato internacional, solo dígitos)")
    async with _client(cfg, store) as c:
        before = await c.get_session(cfg.session_name)
        t0 = time.perf_counter()
        status, code = await c.request_code(cfg.session_name, phone)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        after = await c.get_session(cfg.session_name)
    if code:
        print(f"CODIGO DE VINCULACION (tipealo en el telefono, no se guarda): {code}")
    _emit(store, "pairing_code.json", {"status_before": (before or {}).get("status"), "http_status": status,
                                        "code_received": bool(code), "elapsed_ms": elapsed_ms,
                                        "status_after": (after or {}).get("status")})


async def cmd_wait_working(args: argparse.Namespace) -> None:
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        out = await wait_for_status(c, cfg.session_name, timeout_s=args.max_seconds, interval_s=2.0)
    if out["status"] == "WORKING":
        store.set_link_ts(time.time())
    _emit(store, "link_wait.json", out)


async def cmd_stability(args: argparse.Namespace) -> None:
    """Punto 4: desde WORKING hasta que chats y mensajes no cambian por --window s (tope --max-seconds)."""
    cfg, store, _ = _ctx(args)
    link_ts = store.link_ts()
    samples: list[dict[str, Any]] = []
    wall_start = time.time()
    t0 = time.monotonic()
    async with _client(cfg, store) as c:
        while True:
            chats = await count_chats(c, cfg.session_name)
            facts, pass_info = await full_pass(c, cfg.session_name, msg_limit=args.msg_limit)
            summ = count_summary(facts)
            samples.append({"t": round(time.monotonic() - t0, 1), "chats": chats["total"], "msgs": summ["total"],
                            "oldest_ts": summ["oldest_ts"], "oldest_iso": summ["oldest_iso"],
                            "pass_s": pass_info["elapsed_s"]})
            print(json.dumps(samples[-1]))
            if stable_since(samples, args.window) is not None or time.monotonic() - t0 > args.max_seconds:
                break
            await asyncio.sleep(args.interval)
    first, last = samples[0], samples[-1]
    span_min = (last["t"] - first["t"]) / 60
    stable_at = stable_since(samples, args.window)
    _emit(store, "stability.json", {
        "samples": samples,
        "stable_since_s": stable_at,
        "seconds_from_link": round(wall_start + stable_at - link_ts, 1) if (link_ts and stable_at is not None) else None,
        "msgs_growth_per_min": round((last["msgs"] - first["msgs"]) / span_min, 1) if span_min > 0 else None,
        "final": {"chats": last["chats"], "msgs": last["msgs"], "oldest_ts": last["oldest_ts"], "oldest_iso": last["oldest_iso"]},
    })


async def cmd_count_pass(args: argparse.Namespace) -> None:
    """Punto 3: tope de limit y latencia de la pasada de conteo completa."""
    cfg, store, _ = _ctx(args)
    limits = [int(x) for x in args.limits.split(",") if x.strip()]
    async with _client(cfg, store) as c:
        probes = []
        for lim in limits:
            try:
                page = await c.list_messages(cfg.session_name, limit=lim, offset=0)
            except httpx.TimeoutException:
                # un limit enorme que cuelga al servidor es un dato del punto 3, no un crash del comando
                probes.append({"limit": lim, "returned": None, "elapsed_ms": None, "status": "timeout"})
                continue
            probes.append({"limit": lim, "returned": page.raw_count, "elapsed_ms": page.elapsed_ms, "status": page.status})
        chats = await count_chats(c, cfg.session_name)
        facts, pass_info = await full_pass(c, cfg.session_name, msg_limit=args.msg_limit)
    _emit(store, "count_pass.json", {"limit_probes": probes, "chats": chats, "full_pass": pass_info,
                                     "summary": count_summary(facts)})


async def cmd_chat_kinds(args: argparse.Namespace) -> None:
    """Punto 7: qué tipos de chat llegaron al store con ignore activo."""
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        out = await count_chats(c, cfg.session_name)
    _emit(store, "chat_kinds.json", out)


async def _labels_from_owner_file(client: WahaClient, name: str) -> dict[str, list[str]]:
    """Archivo del dueño (fuera del repo) con líneas 'ETIQUETA,telefono' -> etiqueta -> chat_refs.

    Cada teléfono se resuelve a sus refs @c.us, @s.whatsapp.net y, vía GET /lids, @lid: en una cuenta Business
    actual buena parte del historial de un contacto vive bajo su @lid (spec §4.5) y sin esto se subcontaría.
    """
    path = os.environ.get("SPIKE_COMPARE_CHATS_FILE")
    if not path or not Path(path).exists():
        raise SystemExit("falta SPIKE_COMPARE_CHATS_FILE: archivo fuera del repo con lineas 'ETIQUETA,telefono'")
    labels: dict[str, list[str]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        label, phone = [x.strip() for x in line.split(",", 1)]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,11}", label):
            raise SystemExit("etiqueta invalida: empieza con letra, hasta 12 caracteres [A-Za-z0-9_-]; nunca nombres ni telefonos")
        labels[label] = await client.refs_for_phone(name, re.sub(r"[^0-9]", "", phone))
    return labels


async def cmd_monthly_counts(args: argparse.Namespace) -> None:
    """Punto 2: conteos mensuales inbound/fromMe de los chats etiquetados por el dueño (hoja de comparación)."""
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        labels = await _labels_from_owner_file(c, cfg.session_name)
        facts, pass_info = await full_pass(c, cfg.session_name, msg_limit=args.msg_limit)
    by_label = monthly_by_label(facts, labels)
    rows = [[label, month, n["inbound"], n["from_me"], "", ""]
            for label, months in by_label.items() for month, n in months.items()]
    assert_clean(rows)  # el CSV no pasa por ResultsStore.write: se valida igual antes de tocar el disco
    with store.path("monthly_counts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["etiqueta", "mes", "entrantes_waha", "propios_waha", "entrantes_telefono", "propios_telefono"])
        w.writerows(rows)
    _emit(store, "monthly_counts.json", {"by_label": by_label, "labels_without_messages": [l for l, m in by_label.items() if not m],
                                         "full_pass": pass_info, "summary": count_summary(facts)})


async def cmd_autoreply(args: argparse.Namespace) -> None:
    """Punto 12: candidatos a respuesta automática (texto propio repetido y rápido), solo agregados."""
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        facts, pass_info = await full_pass(c, cfg.session_name, msg_limit=args.msg_limit, with_text_fp=True)
    summ = autoreply_summary(facts)
    # text_fp es un derivado del texto: sirve para agrupar en memoria, no hace falta en el archivo de agregados
    summ["candidates"] = [{k: v for k, v in cand.items() if k != "text_fp"} | {"rank": i}
                          for i, cand in enumerate(summ["candidates"], 1)]
    _emit(store, "autoreply.json", {**summ, "full_pass": pass_info})


async def cmd_events_summary(args: argparse.Namespace) -> None:
    """Puntos 4, 9, 10: resumen del events.jsonl del receptor."""
    cfg, store, _ = _ctx(args)
    if not store.path("events.jsonl").exists():
        raise SystemExit("no hay events.jsonl en esta corrida: ¿el receptor corre con este SPIKE_RUN_ID?")
    recs = store.read_jsonl("events.jsonl")
    foreign = sum(1 for r in recs if r.get("session") != cfg.session_name)
    _emit(store, "events_summary.json", {**events_summary(recs, store.link_ts()), "records_other_sessions": foreign})


async def cmd_reconcile_probe(args: argparse.Namespace) -> None:
    """Punto 13: latencia de la reconciliación en dos pasos de spec §6.3 punto 3."""
    cfg, store, _ = _ctx(args)
    since = int(time.time()) - args.window_minutes * 60
    async with _client(cfg, store) as c:
        t0 = time.perf_counter()
        refs: list[str] = []
        offset = step1_requests = 0
        stop = False
        while not stop:
            page = await c.list_chats(cfg.session_name, limit=100, offset=offset)
            step1_requests += 1
            for ch in page.items:
                if ch.last_ts is not None and ch.last_ts < since:
                    stop = True
                    break
                refs.append(ch.chat_ref)
            if page.raw_count == 0:
                break
            offset += 100
        t1 = time.perf_counter()
        seen = step2_requests = 0
        for r in refs:
            offset = 0
            while True:
                page = await c.list_messages(cfg.session_name, chat_ref=r, limit=100, offset=offset, ts_gte=since)
                step2_requests += 1
                seen += page.raw_count
                offset += 100
                if page.raw_count == 0:
                    break
        t2 = time.perf_counter()
    _emit(store, "reconcile.json", {"window_minutes": args.window_minutes, "chats_touched": len(refs),
                                    "step1_ms": int((t1 - t0) * 1000), "step1_requests": step1_requests,
                                    "step2_ms": int((t2 - t1) * 1000), "step2_requests": step2_requests,
                                    "messages_seen": seen})


async def cmd_lids(args: argparse.Namespace) -> None:
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        out = {"count": await c.lids_count(cfg.session_name), "sample": await c.lids_sample(cfg.session_name)}
    _emit(store, "lids.json", out)


async def cmd_keys_create(args: argparse.Namespace) -> None:
    """Punto 14: una clave de solo lectura de la sesión, para verificar después que el teardown la borra."""
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        out = await c.create_key(cfg.session_name, actions=READ_ONLY_ACTIONS)
    # el id crudo no se persiste (puede ser un UUID con corridas de dígitos); el teardown localiza las claves por sesión
    _emit(store, "keys.json", {"session": out["session"], "created": bool(out["id"]),
                               "id_ref": ref(store.salt(), str(out["id"]))})


async def cmd_no_store_probe(args: argparse.Namespace) -> None:
    """Punto 15: sesión sin store: ¿chats/all/messages devuelve vacío o error? ¿hay mapeo de lids?"""
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        page = await c.list_messages(cfg.session_name, limit=10, offset=0)
        try:
            lids = await c.lids_count(cfg.session_name)
        except Exception as e:  # noqa: BLE001 - se registra solo el tipo
            lids = f"error:{type(e).__name__}"
    _emit(store, "no_store.json", {"messages": {"status": page.status, "returned": page.raw_count,
                                                "elapsed_ms": page.elapsed_ms}, "lids_count": lids})


async def cmd_watch(args: argparse.Namespace) -> None:
    """Punto 13: estado de la sesión cada --interval s (largo plazo). Ctrl+C para cortar."""
    cfg, store, _ = _ctx(args)
    async with _client(cfg, store) as c:
        while True:
            s = await c.get_session(cfg.session_name)
            rec = {"t": int(time.time()), "status": (s or {}).get("status"), "exists": s is not None}
            store.append_jsonl("watch.jsonl", rec)
            print(json.dumps(rec))
            await asyncio.sleep(args.interval)


async def cmd_note(args: argparse.Namespace) -> None:
    """Números medidos a mano (RAM, disco, conteos del teléfono): solo clave y valor numérico."""
    cfg, store, _ = _ctx(args)
    # el valor se guarda como float (assert_clean no lo escanea): 15 dígitos alcanzan para bytes de un volumen
    if not re.fullmatch(r"[a-z0-9_]{1,40}", args.key) or not re.fullmatch(r"-?\d{1,15}(\.\d+)?", args.value):
        raise SystemExit("note: clave [a-z0-9_] y valor numérico (hasta 15 dígitos enteros)")
    rec = {"t": int(time.time()), "key": args.key, "value": float(args.value)}
    store.append_jsonl("notes.jsonl", rec)
    print(json.dumps(rec))


async def cmd_teardown(args: argparse.Namespace) -> None:
    """Punto 14: un solo DELETE, claves, verificación 404. Olvida la sal para que los chat_ref no se puedan cruzar."""
    cfg, store, log = _ctx(args)
    try:
        async with _client(cfg, store) as c:
            out = await teardown_session(c, cfg.session_name)
        _emit(store, "teardown.json", out)
        log.info("sesion %s terminada: ok=%s keys_error=%s", cfg.session_name, out["ok"], out["keys_error"])
    finally:
        store.forget_salt()  # pase lo que pase: después del DELETE, los chat_ref de esta corrida no se cruzan más


# --- parser ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="radar_spike", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=None, help="archivo .env.spike (ignorado por git) con las variables")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, func: Callable, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=func)
        return sp

    add("init-run", cmd_init_run, "crea results/<run>/ y la sal por corrida")
    add("check-server", cmd_check_server, "versión, motor y tier del servidor")
    sp = add("create-session", cmd_create_session, "crea spike_<run> con el cuerpo del spec y verifica")
    sp.add_argument("--engine", choices=["NOWEB", "GOWS"], default="NOWEB")
    sp.add_argument("--no-store", action="store_true")
    sp.add_argument("--full-sync", action="store_true")
    sp.add_argument("--no-retries", action="store_true", help="omite retries para leer los defaults (punto 9)")
    add("status", cmd_status, "estado actual de la sesión")
    add("restart", cmd_restart, "POST restart (tras FAILED por QR vencido)")
    sp = add("qr", cmd_qr, "punto 8: rotaciones del QR; --png para escanear")
    sp.add_argument("--png", action="store_true")
    sp.add_argument("--interval", type=float, default=2.0)
    sp.add_argument("--max-seconds", type=float, default=600)
    add("pairing-code", cmd_pairing_code, "punto 8: código de vinculación (SPIKE_PAIRING_PHONE)")
    sp = add("wait-working", cmd_wait_working, "espera WORKING y guarda la hora de vínculo")
    sp.add_argument("--max-seconds", type=float, default=600)
    sp = add("stability", cmd_stability, "punto 4: hasta que chats y mensajes se estabilizan")
    sp.add_argument("--interval", type=float, default=30)
    sp.add_argument("--window", type=float, default=90)
    sp.add_argument("--max-seconds", type=float, default=600)
    sp.add_argument("--msg-limit", type=int, default=1000)
    sp = add("count-pass", cmd_count_pass, "punto 3: tope de limit y pasada de conteo")
    sp.add_argument("--limits", default="100,1000,5000,10000")
    sp.add_argument("--msg-limit", type=int, default=1000)
    add("chat-kinds", cmd_chat_kinds, "punto 7: tipos de chat en el store")
    sp = add("monthly-counts", cmd_monthly_counts, "punto 2: conteos mensuales por etiqueta del dueño")
    sp.add_argument("--msg-limit", type=int, default=1000)
    sp = add("autoreply", cmd_autoreply, "punto 12: heurística de respuesta automática")
    sp.add_argument("--msg-limit", type=int, default=1000)
    add("events-summary", cmd_events_summary, "puntos 4/9/10: resumen de events.jsonl")
    sp = add("reconcile-probe", cmd_reconcile_probe, "punto 13: latencia de reconciliación en dos pasos")
    sp.add_argument("--window-minutes", type=int, default=10)
    add("lids", cmd_lids, "puntos 10/15: conteo y mapeo de lids (solo agregados)")
    add("keys-create", cmd_keys_create, "punto 14: clave de solo lectura de la sesión")
    add("no-store-probe", cmd_no_store_probe, "punto 15: lecturas sobre una sesión sin store")
    sp = add("watch", cmd_watch, "punto 13: estado periódico a watch.jsonl")
    sp.add_argument("--interval", type=float, default=600)
    sp = add("note", cmd_note, "anota un número medido a mano (clave, valor)")
    sp.add_argument("--key", required=True)
    sp.add_argument("--value", required=True)
    add("teardown", cmd_teardown, "punto 14: DELETE de la sesión, claves y verificación")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # el handler raíz se crea acá; _ctx le cuelga el RedactingFilter con los secretos apenas los conoce
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 4: Correr (pasa)**

Run: `python -m pytest tests/radar_spike/test_runner.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Suite completa y ayuda del CLI**

Run: `python -m pytest tests/radar_spike -q`
Expected: `92 passed` (privacy 14 + config 10 + client 24 + session_spec 13 + lifecycle 7 + results 4 + receiver 5 + measures 6 + runner 9).

Run: `python -m scripts.radar_spike.runner --help`
Expected: la lista de subcomandos, sin error de importación.

- [ ] **Step 6: Commit**

```
git add scripts/radar_spike/runner.py tests/radar_spike/test_runner.py
git commit -m "Spike WAHA: CLI con un comando por medicion y resultados agregados"
```

---

### Task 9: Runbook de las corridas (fase 1 y fase 2) y contenedor GOWS

**Files:**
- Create: `scripts/radar_spike/README.md`
- Test: ninguno automatizado (es documentación de operación); verificación manual de que cada comando citado existe en `runner.py --help`.

**Interfaces:**
- Consumes: los subcomandos de la Tarea 8 y las variables de entorno de la Tarea 2.
- Produces: el orden de comandos por corrida y por punto, y el `docker run` del contenedor GOWS con las variables de spec §6.2.

- [ ] **Step 1: Escribir `scripts/radar_spike/README.md`**

````markdown
# Spike de WAHA para Radar — runbook

Arnés descartable (spec §8, "Semana 0"). No es código de producto. Todo lo que escribe son agregados en
`results/<run>/` (ignorado por git). Nunca guarda texto de mensajes, teléfonos, JID, nombres ni QR.

## Preparación (una vez)

1. `Copy-Item .env.spike.example .env.spike` y completar los valores (el archivo está ignorado por git).
   - `WAHA_BASE_URL`: la instancia de staging (fase 1) o el contenedor GOWS (fase 2).
   - `WAHA_ADMIN_KEY`: clave admin. Nunca se imprime ni se escribe.
   - `SPIKE_WEBHOOK_HMAC_KEY`: `python -c "import secrets; print(secrets.token_hex(32))"`.
   - `SPIKE_WEBHOOK_PUBLIC_URL`: URL pública del túnel al receptor local.
2. Túnel, en una terminal que queda abierta durante todo el spike:
   `cloudflared tunnel --url http://localhost:8787` y copiar la URL pública a `SPIKE_WEBHOOK_PUBLIC_URL`.
3. Receptor, en otra terminal, **uno por corrida**:
   `python -m uvicorn --factory scripts.radar_spike.receiver:app_from_env --port 8787`
   Lee `.env.spike` del directorio actual (o el archivo que diga `SPIKE_ENV_FILE`): de ahí toma `SPIKE_RUN_ID`,
   `SPIKE_WEBHOOK_HMAC_KEY` y `SPIKE_RESULTS_DIR`. **El `run_id` se fija al arrancar: cada vez que cambia
   `SPIKE_RUN_ID` en `.env.spike`, Ctrl+C y volver a lanzarlo; pararlo antes de `spike teardown`** (el teardown borra
   la sal de la corrida y el receptor pasaría a responder 500 a todo lo que llegue después).
   Precedencia: un valor no vacío en `.env.spike` pisa la shell; una línea vacía deja lo que haya en la shell. Por eso
   `SPIKE_RECEIVER_MODE` y `SPIKE_PAIRING_PHONE` se dan en la terminal (`$env:SPIKE_RECEIVER_MODE="fail"`) sin escribirlos.
4. `python -m pytest tests/radar_spike -q` → `0 failed`.
5. Nunca redirigir la salida del runner ni del receptor a un archivo dentro del repo (`> algo.txt`): los JSON de
   `results/` son los únicos artefactos y la consola es efímera.

Todos los comandos siguientes son `python -m scripts.radar_spike.runner --env-file .env.spike <comando>`;
abajo se abrevia como `spike <comando>`. Cada corrida tiene su `SPIKE_RUN_ID` (sesión `spike_<run>`), y cada bloque
empieza por editar `SPIKE_RUN_ID` en `.env.spike` y reiniciar el receptor.

## Corrida `qr0` — QR sin escanear y defaults de reintentos (puntos 8 y 9, sin teléfono)

Los eventos `session.status` (STARTING → SCAN_QR_CODE → FAILED) salen sin vincular nada; con el receptor en modo `fail`
WAHA los reintenta con **su política por defecto**, porque la sesión se crea sin `retries` en el cuerpo. Eso es lo que
mide el punto 9 (`retries_effective` en `session_create.json` es solo el eco de lo enviado: queda `null`).

```
# .env.spike: SPIKE_RUN_ID=qr0
# terminal del receptor: $env:SPIKE_RECEIVER_MODE="fail"; python -m uvicorn --factory scripts.radar_spike.receiver:app_from_env --port 8787
spike init-run
spike check-server                      # server.json: version 2026.8.2, engine NOWEB, tier CORE
spike create-session --no-retries       # sin retries en el cuerpo: el servidor aplica su política por defecto
spike qr                                # NO escanear: corre hasta FAILED -> qr_timeline.json (punto 8)
spike events-summary                    # punto 9: delivery.max_attempts y retry_gaps_s = defaults reales del servidor
spike restart
spike qr --max-seconds 30               # confirma que tras restart vuelve a SCAN_QR_CODE
# receptor: Ctrl+C; Remove-Item Env:SPIKE_RECEIVER_MODE
spike teardown                          # teardown.json ok=true
```

## Corrida `nw0` — NOWEB, `fullSync:false` (puntos 1, 3, 4, 5, 6, 7, 9, 10, 12, 14)

```
# .env.spike: SPIKE_RUN_ID=nw0; reiniciar el receptor (modo ok)
spike init-run
spike check-server
spike create-session                    # punto 1: se crea al lado de MaroSession sin error de tier
spike qr --png                          # el dueño escanea el PNG temporal; al llegar a WORKING se borra y se guarda link.json
   # alternativa desde el mismo teléfono: $env:SPIKE_PAIRING_PHONE="549..." ; spike pairing-code ; spike wait-working ; Remove-Item Env:SPIKE_PAIRING_PHONE
spike stability --interval 30 --window 90 --max-seconds 600   # punto 4: stability.json (tiempo hasta lista estable, msgs/min)
spike count-pass                        # punto 3: count_pass.json (tope de limit, latencia de la pasada; 'timeout' es un resultado válido)
spike chat-kinds                        # punto 7: chat_kinds.json (by_kind sin group/channel/status/broadcast)
spike events-summary                    # punto 4 (history_events; history_unknown debe ser 0) y 9 (message vs message.any)
```
Chequeos manuales durante `nw0` (checklists en el documento de resultados):
- punto 5: con la sesión WORKING, el segundo teléfono escribe a la línea; el teléfono de la línea tiene que sonar.
- punto 6: correr `spike count-pass` mientras el segundo teléfono mira el chat: sin tildes azules ni "en línea".
- punto 10: el segundo teléfono (que nunca escribió) manda un primer mensaje por link `wa.me`; después `spike events-summary`
  y comparar `distinct_chats`/`lid_chats` con lo que se envió; repetir 5 veces con líneas distintas si es posible.
- punto 12: activar mensaje de bienvenida y de ausencia en WhatsApp Business, provocar ambos desde el segundo teléfono,
  después `spike autoreply` → `autoreply.json`. Falsos positivos = candidatos − mensajes automáticos configurados.
- punto 9 (política configurada, no defaults): reiniciar el receptor con `$env:SPIKE_RECEIVER_MODE="fail"`, provocar 2
  mensajes, esperar 5 min, volver a `ok`, `spike events-summary` → `delivery` tiene que reflejar `exponential/2 s/15`.
Cierre:
```
spike keys-create                       # punto 14: keys.json (created=true)
spike status                            # tiene que decir WORKING: solo así el DELETE desvincula el dispositivo del teléfono
# receptor: Ctrl+C
spike teardown                          # teardown.json: ok, status_before=WORKING, keys_deleted, session_gone
```
Después del teardown: en el teléfono, Dispositivos vinculados → el dispositivo tiene que haber desaparecido (punto 14).

## Corrida `nw1` — NOWEB, `fullSync:true` (punto 2, y punto 13 en NOWEB)

```
# .env.spike: SPIKE_RUN_ID=nw1; reiniciar el receptor
spike init-run
spike create-session --full-sync
spike qr --png
spike stability --max-seconds 900
spike count-pass                        # summary.by_month y oldest_iso: profundidad real
spike monthly-counts                    # punto 2: SPIKE_COMPARE_CHATS_FILE con lineas 'A,549...' (20 chats), fuera del repo
```
`monthly_counts.csv` tiene columnas vacías `entrantes_telefono` y `propios_telefono` que el dueño completa contando en el
teléfono, mes por mes, para las etiquetas A..T. Esta sesión **queda viva durante todo el spike** (punto 13):
```
spike watch --interval 600              # en una terminal aparte; Ctrl+C al final
spike reconcile-probe --window-minutes 10   # una vez por día: reconcile.json
spike note --key ram_mb --value <RAM del contenedor en MB>       # del panel de Railway, una vez por día
spike note --key disk_bytes --value <bytes del volumen>          # entero, hasta 15 dígitos (un store de GB entra)
```
Al final: `spike status` (WORKING), Ctrl+C en el receptor, `spike teardown`.

## Corrida `ns0` — NOWEB sin store (punto 15, opcional, no bloquea el MVP)

```
# .env.spike: SPIKE_RUN_ID=ns0; reiniciar el receptor
spike init-run
spike create-session --no-store
spike qr --png
spike no-store-probe                    # no_store.json: status/returned de chats/all/messages, lids_count
   # el segundo teléfono manda 3 mensajes
spike events-summary                    # by_event: ¿message.any sigue llegando sin store?
spike lids
spike teardown
```

## Fase 2 — contenedor GOWS (puntos 2, 10, 13, 14, 15)

El motor es por servidor (spec §6.2), así que GOWS necesita otro contenedor. Nunca se toca el de staging.

Clave hasheada (WAHA acepta `sha512:<hex>`), sin dejar la clave en el historial de la shell. Snippet para
Windows PowerShell 5.1 (la shell de esta máquina; `ConvertFrom-SecureString -AsPlainText` existe solo en PowerShell 7):
```
$sec = Read-Host "Clave admin del contenedor GOWS" -AsSecureString
$env:WAHA_ADMIN_KEY = [System.Net.NetworkCredential]::new("", $sec).Password
python -c "import hashlib,os; print('sha512:'+hashlib.sha512(os.environ['WAHA_ADMIN_KEY'].encode()).hexdigest())"
```
El hash va en `docker run` (abajo); el mismo valor en claro va en `.env.spike` como `WAHA_ADMIN_KEY` para estas corridas.
Después: `Remove-Item Env:WAHA_ADMIN_KEY` (el runner lo toma del archivo).

Docker local. Para Railway, seguir `docs/superpowers/plans/2026-09-21-radar-gows-staging-runbook.md`, que tiene las mismas variables verificadas contra la documentación y el código de WAHA, más la clave hasheada, el volumen en `/app/.sessions` y el healthcheck en `/ping`. La imagen es `devlikeapro/waha:gows-2026.8.2` (no existe el tag `2026.8.2` a secas; `gows-` no trae Chromium).
`WAHA_EVENTS_DOWNLOAD_MEDIA` y `WAHA_API_DOWNLOAD_MEDIA` son los nombres documentados en WAHA → Config → "Files"
(verificados en la documentación 2026.8 y en el código de configuración del servidor); WAHA ignora en silencio variables
desconocidas, por eso además se comprueba en `gw0` que no aparezcan archivos de medios:
```
docker run -d --name waha-gows -p 3001:3000 `
  -v waha_gows_sessions:/app/.sessions `
  -e WHATSAPP_DEFAULT_ENGINE=GOWS `
  -e WAHA_API_KEY=sha512:<hash de arriba> `
  -e WAHA_DASHBOARD_ENABLED=false `
  -e WHATSAPP_SWAGGER_ENABLED=false `
  -e WAHA_PRINT_QR=false `
  -e WAHA_PRESENCE_AUTO_ONLINE=false `
  -e WAHA_SESSION_CONFIG_IGNORE_STATUS=true `
  -e WAHA_SESSION_CONFIG_IGNORE_GROUPS=true `
  -e WAHA_SESSION_CONFIG_IGNORE_CHANNELS=true `
  -e WAHA_SESSION_CONFIG_IGNORE_BROADCAST=true `
  -e WAHA_EVENTS_DOWNLOAD_MEDIA=false `
  -e WAHA_API_DOWNLOAD_MEDIA=false `
  -e WAHA_APPS_ENABLED=false `
  -e WAHA_LOG_LEVEL=info `
  -e WAHA_GOWS_DEVICE_REQUIRE_FULL_SYNC=false `
  -e WAHA_GOWS_DEVICE_HISTORY_SYNC_FULL_SYNC_DAYS_LIMIT=90 `
  -e WAHA_GOWS_DEVICE_HISTORY_SYNC_RECENT_SYNC_DAYS_LIMIT=90 `
  -e WAHA_GOWS_DEVICE_HISTORY_SYNC_INITIAL_SYNC_MAX_MESSAGES_PER_CHAT=5000 `
  devlikeapro/waha:gows-2026.8.2
```
`WAHA_BASE_URL=http://localhost:3001` en `.env.spike` para estas corridas. Verificar con `spike check-server` que
`engine` es `GOWS`. Si `docker logs waha-gows` muestra un QR en ASCII, `WAHA_PRINT_QR` no tomó: corregir antes de vincular.

Corrida `gw0` (GOWS con límites de historial):
```
# .env.spike: SPIKE_RUN_ID=gw0 y WAHA_BASE_URL=http://localhost:3001; reiniciar el receptor
spike init-run
spike create-session --engine GOWS       # verifica config.gows.storage e ignore
spike qr --png
spike stability --max-seconds 1800       # el sync de GOWS puede tardar más
spike count-pass                         # summary.by_month, oldest_iso: ¿respetó los 90 días? (spec §6.2 [VALIDAR])
spike monthly-counts                     # punto 2, mismas etiquetas que nw1
spike events-summary                     # punto 10 con el mismo protocolo del segundo teléfono
spike watch --interval 600               # punto 13, aparte
docker stats --no-stream waha-gows       # RAM: spike note --key ram_mb --value N
docker exec waha-gows du -sb /app/.sessions   # bytes: spike note --key disk_bytes --value N
docker exec waha-gows sh -c "ls -A /tmp/whatsapp-files 2>/dev/null | wc -l"   # medios descargados: esperado 0 -> spike note --key media_files --value N
spike reconcile-probe
```
Cierre `gw0` (punto 14 con verificación en el volumen, y guarda de logs de spec §9):
```
spike keys-create
spike status                             # WORKING antes del teardown
# receptor: Ctrl+C
spike teardown
docker exec waha-gows ls -la /app/.sessions/gows/       # no debe quedar nada de spike_gw0
docker exec waha-gows sh -c "grep -rl spike_gw0 /app/.sessions || echo limpio"   # esperado: limpio
cmd /c "docker logs waha-gows 2>&1" | Select-String -Pattern "@c\.us|@lid|\d{10,}" | Measure-Object   # esperado: Count 0
spike note --key waha_log_pii_hits --value <Count>     # spec §6.2 [VALIDAR] nivel de log sin contenido; guarda de §9
```
(`cmd /c "... 2>&1"` porque en PowerShell 5.1 redirigir stderr de un ejecutable nativo envuelve cada línea en un
ErrorRecord; `Select-String` sobre eso da conteos raros.)

Corrida `gw1` (GOWS sin store de mensajes, punto 15): igual que `ns0` con `create-session --engine GOWS --no-store`.

Corrida opcional `gw2` (GOWS sin las variables `WAHA_GOWS_DEVICE_*`, es decir historial completo): solo si `gw0` **sí**
respetó los 90 días y se quiere conocer el techo real del historial de una línea Business (si no los respetó, `gw0` ya
sincronizó todo y `gw2` no aporta nada). Requiere reiniciar el contenedor sin esas variables y volver a escanear.

## Qué archivo responde cada punto

| Punto | Corrida | Archivo(s) |
|---|---|---|
| 1 | nw0 | `session_create.json` (creada sin error con `MaroSession` viva), `server.json` |
| 2 | nw0, nw1, gw0 | `count_pass.json` (`summary.by_month`, `oldest_iso`), `monthly_counts.csv` completado a mano |
| 3 | nw0 | `count_pass.json` (`limit_probes` con `status: "timeout"` como resultado válido, `full_pass`) |
| 4 | nw0 | `stability.json`, `events_summary.json` (`history_events`, `history_unknown`) |
| 5, 6 | nw0 | checklist manual en el documento de resultados |
| 7 | nw0 | `chat_kinds.json` |
| 8 | qr0, nw0 | `qr_timeline.json`, `pairing_code.json` |
| 9 | qr0, nw0 | `events_summary.json` de qr0 (`delivery` = defaults del servidor, receptor en modo `fail`), de nw0 (`message_vs_any_inbound`; `delivery` = política configurada); `session_create.json` (`retries_effective`, solo secundario) |
| 10 | nw0, gw0 | `events_summary.json` (`distinct_chats`, `lid_chats`) vs. mensajes enviados |
| 11 | — | preguntas a Kapso/Meta (documento de resultados) |
| 12 | nw0 | `autoreply.json` |
| 13 | nw1, gw0 | `watch.jsonl`, `notes.jsonl` (`ram_mb`, `disk_bytes`, `media_files`, `waha_log_pii_hits`), `reconcile.json`, `count_pass.json` (bytes por 1.000 mensajes = disk_bytes / total × 1000) |
| 14 | nw0, gw0 | `teardown.json` (`status_before` = WORKING), `ls`/`grep` del volumen, dispositivo ausente en el teléfono |
| 15 | ns0, gw1 | `no_store.json`, `events_summary.json`, `lids.json` |
| 16 | — | checklist de URLs (documento de resultados) |
````

- [ ] **Step 2: Verificar que cada comando del runbook existe**

Run: `python -m scripts.radar_spike.runner --help`
Expected: aparecen `init-run, check-server, create-session, status, restart, qr, pairing-code, wait-working, stability, count-pass, chat-kinds, monthly-counts, autoreply, events-summary, reconcile-probe, lids, keys-create, no-store-probe, watch, note, teardown`, que son exactamente los usados en el README.

- [ ] **Step 3: Commit**

```
git add scripts/radar_spike/README.md
git commit -m "Spike WAHA: runbook de corridas y contenedor GOWS endurecido"
```

---

### Task 10: Documento de resultados (plantilla) y rúbrica de motor

**Files:**
- Create: `docs/superpowers/specs/2026-09-21-radar-spike-resultados.md`
- Test: ninguno automatizado; es el documento que se completa con los JSON de `results/`. Las celdas "Resultado" vacías son datos a recolectar, no placeholders del plan.

**Interfaces:**
- Consumes: los archivos de `results/<run>/` listados en el README (Tarea 9).
- Produces: tabla de 16 puntos (qué se mide, comando, criterio de pass/fail, dónde se escribe, resultado), checklists manuales (5, 6, 14), preguntas a Kapso/Meta (11), URLs de retención (16), hoja de comparación (2) y rúbrica del motor con los cinco criterios ordenados de spec §6.2.

- [ ] **Step 1: Escribir el documento**

````markdown
# Radar — Resultados del spike de WAHA (Semana 0)

**Estado:** plantilla a completar · **Spec:** `2026-09-21-onboarding-radar-whatsapp-design.md` v5.1, §8 · **Plan:** `plans/2026-09-21-radar-spike-waha.md`
**Servidor fase 1:** WAHA 2026.8.2 / NOWEB / CORE (staging, sesiones `spike_*` al lado de `MaroSession`) · **Servidor fase 2:** contenedor GOWS 2026.8.2 propio.
**Corridas:** `qr0`, `nw0`, `nw1`, `ns0` (fase 1) · `gw0`, `gw1`, `gw2` opcional (fase 2). Fechas: ____ a ____.

Regla del documento: acá van **solo agregados** copiados de `scripts/radar_spike/results/<run>/*.json`. Nunca un teléfono, un nombre, un JID ni un texto de mensaje.

## 1. Tabla de los 16 puntos

| # | Punto (spec §8) | Qué se mide | Comando | Criterio de pass | Archivo | Resultado |
|---|---|---|---|---|---|---|
| 1 | Multi-sesión en CORE | `create-session` en `nw0` responde 201 con `MaroSession` en WORKING; ambas conviven hasta el teardown. | `spike create-session` (nw0) + dashboard/MCP del dueño para ver `MaroSession` | PASS si la sesión se crea, llega a WORKING y `MaroSession` sigue WORKING durante toda la corrida; FAIL si hay 4xx de licencia o si `MaroSession` cae. | `session_create.json`, `server.json` | |
| 2 | Profundidad, completitud y duración del sync | `summary.by_month`, `oldest_iso`, `stable_since_s`, y la hoja §4 (20 chats × mes, entrantes y propios, WAHA vs. teléfono) en NOWEB `fullSync:false`, `fullSync:true` y GOWS. | `spike count-pass`, `spike stability`, `spike monthly-counts` (nw0, nw1, gw0) | Por motor/config: % de meses de la hoja con \|WAHA − teléfono\| ≤ 5 % en entrantes y propios. PASS de "denso" para un mes si ≥ 95 % de sus chats cumplen. Se reporta el último mes denso (profundidad útil). | `count_pass.json`, `stability.json`, `monthly_counts.csv` | nw0: ____ · nw1: ____ · gw0: ____ |
| 3 | Tope de `limit` y latencia de la pasada de conteo | `limit_probes[].returned` vs. `limit`; `full_pass.elapsed_s`, `msgs_per_min`, `requests`. | `spike count-pass --limits 100,1000,5000,10000` (nw0) | Se registra el tope efectivo (primer `limit` con `returned < limit` teniendo más mensajes, o el primer `status: "timeout"`, que también es un resultado: ese `limit` no es usable). PASS operativo si la pasada completa tarda ≤ 3 min para el volumen de la línea (spec §9: WORKING → lista ≤ 3 min). | `count_pass.json` | tope: ____ · timeout en: ____ · pasada: ____ s · ____ msgs/min |
| 4 | ¿El historial llega por webhook? Tiempo hasta lista estable; msgs/min del backfill | `events_summary.history_events` (eventos con `payload_ts < link_ts`, decidido post hoc para los que llegaron antes de que existiera `link.json`; `history_unknown` tiene que ser 0); `stability.seconds_from_link`, `msgs_growth_per_min`. | `spike stability`, `spike events-summary` (nw0, nw1, gw0) | Se responde sí/no: `history_events > 0` = el historial sí se emite (cambia §6.1 del spec). Tiempo estable ≤ 10 min = PASS para la ventana de P4. | `stability.json`, `events_summary.json` | por webhook: ____ (`history_unknown`: ____) · estable a los ____ s · ____ msgs/min |
| 5 | `markOnline:false` no afecta notificaciones | Checklist §3.1, con la sesión WORKING y el teléfono bloqueado. | manual (nw0) | PASS si las 5 pruebas de §3.1 suenan en el teléfono. | este documento | |
| 6 | Las lecturas no generan tildes ni presencia | Checklist §3.2 mientras corre `count-pass`. | `spike count-pass` + manual (nw0) | PASS si en el segundo teléfono no aparecen tildes azules ni "en línea" durante ni después de la lectura. | este documento | |
| 7 | `ignore` excluye del store | `chat_kinds.by_kind`. | `spike chat-kinds` (nw0) | PASS si `group`, `channel`, `status` y `broadcast` valen 0 con una línea que tiene grupos en el teléfono. | `chat_kinds.json` | |
| 8 | Tiempos reales del QR y código de vinculación | `qr_lifetimes_s`, `qr_codes`, `final_status`, `final_at` sin escanear; `pairing_code.json` (`status_before`, `http_status`, `code_received`). | `spike qr` (qr0), `spike pairing-code` (nw0 o ns0) | Se registran los valores; docs dicen 60 s + 5×20 s, 6 códigos, luego FAILED. Código: PASS si `code_received` en estado SCAN_QR_CODE y el teléfono lo acepta. | `qr_timeline.json`, `pairing_code.json` | vidas: ____ · códigos: ____ · FAILED a los ____ s · código: ____ |
| 9 | `message` vs `message.any`; defaults de reintentos | `message_vs_any_inbound.duplicated` (nw0); defaults: `delivery.max_attempts` y `retry_gaps_s` de **qr0** (sesión creada sin `retries`, receptor en modo `fail` desde antes de crearla: los `session.status` se reintentan con la política por defecto del servidor); política configurada: `delivery` de nw0 en modo `fail`. `retries_effective` de `session_create.json` es solo el eco de lo enviado (dato secundario). | `spike create-session --no-retries` + `spike events-summary` (qr0), `spike events-summary` (nw0) | Se registra. Decisión: si `duplicated` es true, el producto suscribe solo `message.any`. Defaults: los observados en qr0 (cierra la contradicción entre páginas de docs: 15 exponencial vs 4 lineal). | `events_summary.json` (qr0 y nw0), `session_create.json` (qr0, secundario) | duplicado: ____ · defaults observados (intentos, gaps s): ____ · configurada observada: ____ |
| 10 | Reproducir #2267 (primer mensaje `@lid` perdido) | Primeros mensajes enviados desde líneas que nunca escribieron (idealmente por link `wa.me`) vs. `distinct_chats` y `lid_chats` en el resumen, por motor. | manual + `spike events-summary` (nw0, gw0) | Enviados N, recibidos M por webhook y M' en `count-pass`. PASS del motor si M = M' = N en ≥ 5 intentos; FAIL si falta alguno. | `events_summary.json`, `count_pass.json` | NOWEB: __/__ · GOWS: __/__ |
| 11 | Coexistencia con la API oficial | Respuestas de Kapso/Meta a las preguntas de §5. | ninguno (correo/ticket) | Resuelto cuando hay respuesta escrita a las 4 preguntas. | este documento | |
| 12 | Mensajes de bienvenida/ausencia en el historial; falsos positivos de la heurística | `autoreply.candidates`, `flagged_share`, `repeated_texts_not_fast`; además si los automáticos figuran como `from_me` con `has_text`. | `spike autoreply` (nw0) tras provocar bienvenida y ausencia desde el segundo teléfono | Falsos positivos = candidatos − automáticos configurados (K). PASS si FP/candidatos ≤ 10 % y los K automáticos están entre los candidatos. | `autoreply.json` | candidatos: ____ · K: ____ · FP: ____ |
| 13 | Estabilidad de un vínculo largo; RAM, disco, bytes/1.000 msgs; latencia de reconciliación; logs sin contenido | `watch.jsonl` (caídas = estados ≠ WORKING), `notes.jsonl` (`ram_mb`, `disk_bytes` diarios; `media_files`; `waha_log_pii_hits` en gw0), `reconcile.json` (`step1_ms`, `step2_ms`, `chats_touched`). | `spike watch`, `spike note`, `spike reconcile-probe` (nw1 y gw0, ≥ 7 días); en gw0 además `docker logs … \| Select-String "@c\.us\|@lid\|\d{10,}"` | Se registran. PASS de estabilidad si 0 caídas no explicadas en 7 días. bytes/1.000 msgs = `disk_bytes / summary.total × 1000` (`media_files` debe ser 0; si no, la cifra está inflada por medios). Reconciliación: PASS si `step1_ms + step2_ms` ≤ 30 s con ventana de 10 min. Logs: PASS si `waha_log_pii_hits` = 0 con `WAHA_LOG_LEVEL=info` (spec §9). | `watch.jsonl`, `notes.jsonl`, `reconcile.json` | NOWEB: caídas __, RAM __ MB, __ B/1k, reconc __ ms · GOWS: __ · log PII hits: __ |
| 14 | Borrado verificable | `teardown.json` (`status_before` = WORKING, `ok`, `keys_deleted`, `keys_remaining`, `keys_error`, `session_gone`); volumen sin rastros (`grep -rl spike_<run>` → limpio); dispositivo ausente en el teléfono. | `spike keys-create`, `spike status`, `spike teardown`, `docker exec … grep` (gw0), checklist §3.3 | PASS si `status_before=WORKING`, `ok=true` (`keys_error=null`), el volumen queda limpio (fase 2) y el dispositivo desaparece del teléfono sin intervención manual (ambas fases). Si `status_before` no era WORKING, el chequeo del teléfono no cuenta: repetir. | `teardown.json`, este documento | NOWEB: ____ · GOWS: ____ |
| 15 | Sesión sin store de mensajes | `no_store.messages.status/returned`, `by_event["message.any"]` tras enviar 3 mensajes, `lids.count`. | `spike no-store-probe`, `spike events-summary`, `spike lids` (ns0, gw1) | Viable si `message.any` llega (3/3), `chats/all/messages` devuelve vacío o error (prueba positiva de que no guarda) y `lids.count` > 0 con contactos `@lid` presentes. | `no_store.json`, `events_summary.json`, `lids.json` | NOWEB: ____ · GOWS: ____ |
| 16 | Retención de Anthropic y OpenAI en llamadas sincrónicas; trámite de retención cero | Checklist §6. | ninguno | Resuelto cuando hay plazo documentado por proveedor y estado del trámite ZDR. | este documento | |

## 2. Corridas y versiones

| Corrida | Servidor | Motor | Config | Vinculada | Terminada | `teardown.ok` |
|---|---|---|---|---|---|---|
| qr0 | staging | NOWEB | sin retries, sin escaneo, receptor en modo `fail` | — | | |
| nw0 | staging | NOWEB | store on, fullSync false | | | |
| nw1 | staging | NOWEB | store on, fullSync true | | | |
| ns0 | staging | NOWEB | store off | | | |
| gw0 | propio | GOWS | límites 90 días | | | |
| gw1 | propio | GOWS | storage.messages false | | | |
| gw2 | propio | GOWS | sin límites (opcional) | | | |

## 3. Checklists manuales

### 3.1 Punto 5 — notificaciones con `markOnline:false` (sesión WORKING, teléfono de la línea bloqueado)

| # | Prueba | Suena / aparece la notificación |
|---|---|---|
| a | Texto desde el segundo teléfono, app de la línea cerrada | |
| b | Texto con la app de la línea en segundo plano | |
| c | Foto (has_media) | |
| d | Texto durante `spike count-pass` en curso | |
| e | Texto 10 minutos después de terminar la lectura | |

### 3.2 Punto 6 — tildes y presencia (observado desde el segundo teléfono)

| # | Momento | Tildes azules en lo enviado | "en línea" / "escribiendo" visible |
|---|---|---|---|
| a | Antes de vincular (línea base) | | |
| b | Sesión WORKING, sin lecturas | | |
| c | Durante `spike count-pass` | | |
| d | Durante `spike reconcile-probe` | | |
| e | 30 minutos después | | |

### 3.3 Punto 14 — dispositivo en el teléfono

| Corrida | Dispositivo visible antes del teardown | Desapareció solo tras el teardown | Hubo que quitarlo a mano |
|---|---|---|---|
| nw0 | | | |
| gw0 | | | |

## 4. Hoja de comparación del punto 2

Etiquetas A..T (nunca nombres). El dueño cuenta en el teléfono, por mes, entrantes y propios de cada chat. Se pega el `monthly_counts.csv` completado, uno por corrida (nw0, nw1, gw0):

| Etiqueta | Mes | Entrantes WAHA | Propios WAHA | Entrantes teléfono | Propios teléfono | Δ entrantes | Δ propios |
|---|---|---|---|---|---|---|---|
| A | | | | | | | |
| … | | | | | | | |

Resumen por corrida: meses densos (≥ 95 % de chats con Δ ≤ 5 %): ____ · último mes denso: ____ · `oldest_iso`: ____ · `stable_since_s`: ____.

## 5. Punto 11 — preguntas a Kapso y a Meta (por escrito)

1. Los números que hoy operan por Cloud API a través de Kapso, ¿están en **modo coexistencia** (la misma línea sigue activa en la app de WhatsApp Business del teléfono)? ¿Es la configuración por defecto de Kapso o una opción?
2. En coexistencia, ¿la línea admite **dispositivos vinculados** adicionales (Web/Desktop y, por lo tanto, un cliente tipo WAHA)? ¿Hay un límite distinto de los 4 dispositivos habituales?
3. Los mensajes que el operador responde **desde la app** en coexistencia, ¿llegan a Cloud API como eventos (`messages` con `from` = el número del negocio) o solo quedan en el teléfono?
4. Si un número con Cloud API se vincula además por un dispositivo no oficial, ¿Meta lo considera una violación distinta a la de una línea sin API? ¿Kapso tiene reportes de bloqueos por esa combinación?

Respuestas: ____ (fecha, quién respondió, cita textual).

## 6. Punto 16 — retención de proveedores de IA (URLs a consultar el día de la revisión)

| Proveedor | Qué buscar | Dónde | Plazo vigente | ZDR: estado del trámite |
|---|---|---|---|---|
| Anthropic | Retención de entradas y salidas de la API por defecto (llamadas sincrónicas) y condiciones de zero data retention; retención de Message Batches (spec §6.5 dice 29 días y no elegible para ZDR) | https://privacy.anthropic.com/ · https://www.anthropic.com/legal/commercial-terms · https://docs.anthropic.com/en/docs/build-with-claude/batch-processing · https://support.anthropic.com/ (búsqueda "zero data retention") | | |
| OpenAI | Retención por defecto de la API (30 días por abuso) y programa de zero data retention para embeddings/chat | https://platform.openai.com/docs/guides/your-data · https://openai.com/enterprise-privacy/ | | |

Copiar acá la frase exacta que fija el plazo, con fecha de consulta. Sin ZDR verificado, el perfil `sensible` no puede habilitar IA (spec §7).

## 7. Rúbrica de elección del motor (spec §6.2, cinco criterios en orden)

Se decide en orden: el criterio 1 decide salvo empate (diferencia ≤ 5 puntos porcentuales o ambos PASS/FAIL); en empate pasa al 2, y así sucesivamente.

| Orden | Criterio | Evidencia | NOWEB | GOWS | Gana |
|---|---|---|---|---|---|
| 1 | Completitud del historial denso contra el teléfono (punto 2) | meses densos y último mes denso (§4) | | | |
| 2 | ¿Se reproduce #2267? (punto 10) | recibidos/enviados | | | |
| 3 | Estabilidad de un vínculo largo (punto 13) | caídas en 7 días, `watch.jsonl` | | | |
| 4 | Soporte de passkey (hoy solo GOWS, spec §6.2) | documentación; `PASSKEY_REQUIRED` observado en alguna vinculación: sí/no | no soporta | soporta | GOWS (fijo) |
| 5 | Consumo por sesión (punto 13) | RAM MB, bytes/1.000 msgs | | | |

**Decisión:** motor ____ · fecha ____ · firmado por ____.

Consecuencias para el spec (marcar las que aplican): control "Profundidad" de P2 (existe solo con NOWEB) ____ · "Ampliar a 12 meses" de P4 ____ · verificación posterior a la creación (`config.noweb` o `config.gows`) ____ · `waha_workers.engine` inicial ____.

## 8. [VALIDAR] del spec que cierra cada punto

| [VALIDAR] en el spec | Punto | Cierre |
|---|---|---|
| §0.3 / §1 multi-sesión en CORE | 1 | |
| §0.4 historial por webhook; §1 "si el historial se emite como eventos" | 4 | |
| §1 tope de `limit`; §9 WORKING → lista ≤ 3 min | 3, 4 | |
| §1 tildes/presencia; §0.5 notificaciones | 5, 6 | |
| §1 `message` vs `message.any`; defaults de reintentos | 9 | |
| §3 P3 estado en que se puede pedir el código; duración | 8 | |
| §3 P4 duración real del sync | 4 | |
| §4.2 tasa de falsos positivos de respuesta automática | 12 | |
| §6.2 límites `WAHA_GOWS_DEVICE_*` respetados | 2 (gw0: `oldest_iso`) | |
| §6.2 nivel de log sin contenido (`WAHA_LOG_LEVEL=info`); guarda de §9 (0 coincidencias de `@c.us`, `@lid`, 10+ dígitos) | 13 (gw0: `waha_log_pii_hits`) | |
| §6.3 punto 6 desvínculo del lado de WhatsApp tras `DELETE` | 14 | |
| §2.3 / §6.2 sesión sin store (`message.any`, lids, lectura vacía) | 15 | |
| §0 consecuencia de producto: coexistencia | 11 | |
| §3 P2 / §6.5 / §7 retención de proveedores y ZDR | 16 | |
| §4.5 cobertura del mapeo lid → teléfono | 10, 15 (`lids.json`) | |
````

- [ ] **Step 2: Verificación de consistencia con el README**

Run: `Select-String -Path docs/superpowers/specs/2026-09-21-radar-spike-resultados.md -Pattern "spike [a-z-]+" -AllMatches | ForEach-Object { $_.Matches.Value } | Sort-Object -Unique`
Expected: solo comandos que existen en `runner.py --help` (`spike autoreply, spike chat-kinds, spike count-pass, spike create-session, spike events-summary, spike keys-create, spike lids, spike monthly-counts, spike no-store-probe, spike note, spike pairing-code, spike qr, spike reconcile-probe, spike stability, spike teardown, spike watch`).

- [ ] **Step 3: Commit**

```
git add docs/superpowers/specs/2026-09-21-radar-spike-resultados.md
git commit -m "Radar: plantilla de resultados del spike de WAHA y rubrica de motor"
```

---

## Self-Review

### (a) Cobertura del spec (§8, 16 puntos) y decisiones del dueño

| Requisito | Dónde se cumple |
|---|---|
| Punto 1 multi-sesión en CORE | Tarea 3 (cliente), Tarea 4 (`ensure_session`), Tarea 8 (`create-session`, `check-server`), corrida `nw0` (Tarea 9), fila 1 (Tarea 10). |
| Punto 2 profundidad/completitud/duración NOWEB on/off vs GOWS | Tarea 3 (`refs_for_phone`: el `@lid` del contacto se suma a su `@c.us` vía `GET /lids`, sin ruta nueva), Tarea 7 (`count_summary`, `monthly_by_label` con varios refs por etiqueta), Tarea 8 (`count-pass`, `stability`, `monthly-counts` con etiquetas del dueño, CSV validado con `assert_clean`), corridas `nw0/nw1/gw0`, hoja §4 (Tarea 10). |
| Punto 3 tope de `limit` y latencia de la pasada | Tarea 3 (`timeout=600`), Tarea 8 (`count-pass --limits`, un `limit` que agota el timeout se registra como `status: "timeout"` sin perder las sondas anteriores; `full_pass` con `offset += limit` hasta página vacía). |
| Punto 4 historial por webhook, tiempo a lista estable, msgs/min | Tarea 6 (`is_history` al recibir), Tarea 7 (`stable_since`; `events_summary.history_events` decidido post hoc con `payload_ts < link_ts` para lo que llegó antes de `link.json`, más `history_unknown`), Tarea 8 (`stability`, `events-summary`, que aborta si no hay `events.jsonl` y cuenta `records_other_sessions`). |
| Punto 5 `markOnline:false` y notificaciones | Tarea 4 (verificación aborta si `markOnline !== false`), checklist §3.1 (Tarea 10). |
| Punto 6 lecturas sin tildes/presencia | Lista blanca sin `read/presence/typing` (Tarea 3), checklist §3.2 (Tarea 10). |
| Punto 7 `ignore` excluye del store | Tarea 1 (`chat_kind`), Tarea 8 (`chat-kinds`). |
| Punto 8 tiempos del QR y código de vinculación | Tarea 7 (`qr_timeline`), Tarea 8 (`qr`, `pairing-code` con teléfono desde `SPIKE_PAIRING_PHONE`), corrida `qr0`. |
| Punto 9 `message` vs `message.any`; defaults de reintentos | Tarea 4 (`retries=None` en el cuerpo), Tarea 6 (modo `fail`), Tarea 7 (`message_vs_any_inbound`, `delivery`), Tarea 9 (corrida `qr0`: receptor en modo `fail` **antes** de crear la sesión sin `retries`, así los `session.status` se reintentan con la política por defecto del servidor y `delivery` la mide sin teléfono; `retries_effective` queda como dato secundario porque WAHA solo devuelve el eco de lo enviado). |
| Punto 10 #2267 | Tarea 6 (`is_lid`, `chat_ref`), Tarea 7 (`distinct_chats`, `lid_chats`), protocolo en README y fila 10. |
| Punto 11 coexistencia | §5 del documento de resultados: cuatro preguntas exactas a Kapso/Meta. |
| Punto 12 bienvenida/ausencia y falsos positivos | Tarea 1 (`text_fp`), Tarea 7 (`autoreply_summary`), Tarea 8 (`autoreply`), fila 12. |
| Punto 13 vínculo largo: RAM, disco, bytes/1.000, reconciliación en dos pasos; logs sin contenido | Tarea 8 (`watch`, `note` con valores de hasta 15 dígitos para bytes de volumen, `reconcile-probe` con paso 1 por fecha de último mensaje y paso 2 por chat incluido, spec §6.3 punto 3), Tarea 9 (`media_files` = 0 y `waha_log_pii_hits` = 0 sobre `docker logs` del contenedor GOWS: [VALIDAR] de §6.2 y guarda de §9). |
| Punto 14 borrado verificable incl. claves y dispositivo | Tarea 4 (`teardown_session`: `status_before`, un DELETE, claves con `keys_error` que no bloquea, 404), Tarea 8 (`keys-create` sin persistir el id crudo, `teardown` con `forget_salt` en `finally`), Tarea 9 (`spike status` = WORKING antes del teardown), `grep` del volumen en fase 2, checklist §3.3. |
| Punto 15 sesión sin store | Tarea 4 (`store_enabled=False`, `gows.storage.messages=False`), Tarea 8 (`no-store-probe`, `lids`), corridas `ns0/gw1`. |
| Punto 16 retención y ZDR | §6 del documento de resultados (URLs y tabla). |
| Fase 2 con `WHATSAPP_DEFAULT_ENGINE=GOWS` y endurecimiento §6.2 | Tarea 9 (`docker run` con las variables exactas). |
| Rúbrica de los cinco criterios de §6.2 | §7 del documento de resultados. |
| Privacidad: reducción antes de devolver, sin body/teléfono/JID/nombre/QR en objeto, log ni resultados | Tarea 1 (tests de `reduce_message`, `scan_for_pii`, filtro de logs; `ref` solo letras, así ningún hash dispara `\d{8,}` ni parece un teléfono), Tarea 3 (`list_messages`/`list_chats` devuelven hechos; ids crudos y mapeo pn→lid solo en memoria; loggers `httpx`/`httpcore` en WARNING, testeado con `caplog` en una lectura por chat), Tarea 5 (`assert_clean` antes de escribir), Tarea 6 (registro del receptor), Tarea 8 (`RedactingFilter` en los handlers raíz desde `_ctx`, test `test_http_request_logs_never_show_jids` sobre `reconcile-probe`; CSV validado con `assert_clean`; `autoreply.json` sin `text_fp`; `keys.json` sin id crudo; `_no_pii` sobre cada archivo), Tarea 9 (nada de la consola se redirige a un archivo del repo). |
| Configuración: archivo `.env.spike` como fuente de verdad por corrida | Tarea 2 (`load_env_file`: valores no vacíos pisan la shell, líneas vacías no; testeado), Tarea 6 (`app_from_env` lo carga primero), Tarea 8 (`_ctx` con `--env-file`; test con `SPIKE_RUN_ID` viejo en la shell), Tarea 9 (reiniciar el receptor por corrida; `SPIKE_RECEIVER_MODE`/`SPIKE_PAIRING_PHONE` en la terminal). |
| Lista blanca + rechazo de rutas de escritura; `downloadMedia=false` forzado | Tarea 3 (13 rutas rechazadas parametrizadas; test del parámetro). |
| Prefijo `spike_` obligatorio; `MaroSession` intocable | Tarea 3 (`check_session_name` en ruta, cuerpo de sesión y claves; `delete_key` verifica la sesión de la clave). |
| Cuerpo de spec P3 + relectura + aborto | Tarea 4. |
| Teardown con un solo DELETE, sin logout, claves borradas, 404 verificado | Tarea 4 (test cuenta un solo DELETE y ningún `logout`). |
| Receptor HMAC sha512 fail-closed, campos mínimos, 200 rápido | Tarea 6. |
| `.env.spike.example` solo nombres; `.gitignore` | Tarea 1. |

**Fuera de alcance, deliberadamente (y dónde cae):** cualquier código de producto (cliente WAHA del repo, gestor de sesiones, receptor `/webhook/waha` de Radar, tablas `links`/`waha_workers`) → tramo 2 "Vínculo" del MVP; ingesta, backfill y reconciliación reales → tramo 3; verificación de `pg_database`/`DROP DATABASE` con sesiones en PostgreSQL → tramo 2, cuando exista el servidor WAHA propio con Postgres (el spike usa volumen local); comparación "antes/después" y variación entre períodos → segunda etapa; punto 2 sobre WEBJS → no se mide (D4 restringe a NOWEB/GOWS por la documentación de historial); telemetría de la imagen (§6.2 [VALIDAR]) → se anota en la observación del contenedor GOWS pero no tiene comando; validar `hide_input_in_errors` y nivel de log del producto → tramo 3.

### (b) Barrido de placeholders

Se buscó `TBD`, `TODO`, `implementar después`, `similar a la Tarea`, `agregar manejo de errores`, `…` dentro de bloques de código: ninguno. Los únicos "____" están en el documento de resultados (Tarea 10) y son celdas de datos a recolectar, como permite el enunciado; los `<...>` del README (`<bytes del volumen>`, `<Count>`, `<hash de arriba>`) son valores que el dueño mide y tipea, no pasos sin escribir. Cada paso de código muestra el código completo; cada comando tiene su salida esperada. Los conteos esperados se verificaron extrayendo los bloques de código del plan a un árbol temporal fuera del repo y corriéndolos con Python 3.12 / pytest-asyncio 1.4.0 en modo `auto`: `92 passed` (privacy 14, config 10, client 24, session_spec 13, lifecycle 7, results 4, receiver 5, measures 6, runner 9); `runner --help` lista los 21 subcomandos; con `privacy.py` ausente el error de colección es `ImportError: cannot import name 'privacy' from 'scripts.radar_spike'` (el directorio `scripts/` del repo no tiene `__init__.py`, es paquete de espacio de nombres; `scripts/radar_spike/__init__.py` sí existe desde el Step 3). Un `main()` real (`init-run`, `reconcile-probe`, `teardown` contra el servidor falso) no imprime ningún JID, teléfono ni secreto en stdout/stderr.

### (c) Consistencia de nombres y tipos entre tareas

- `privacy.MessageFacts(chat_ref, timestamp, from_me, has_text, has_media, text_fp)` se usa igual en Tareas 3, 7 y 8; `ChatFacts(chat_ref, kind, last_ts)` en 3 y 8 (`ch.kind`, `ch.last_ts`, `ch.chat_ref`). `privacy.ref` devuelve 16 letras `a-z` (Tarea 1) y así lo asumen `refs_for_phone` (Tarea 3), `keys.json.id_ref` (Tarea 8) y los tests (`isalpha()`).
- `privacy.chat_id_of` se exporta en Tarea 1 y se consume en Tarea 6 (`is_lid`); `privacy.ref` en Tareas 3, 6 y 8; `privacy.RedactingFilter` y `privacy.assert_clean` en Tarea 8 (`_ctx`, `cmd_monthly_counts`).
- `config.load_env_file(path)` se define en Tarea 2 y se usa en Tarea 6 (`app_from_env`) y Tarea 8 (`_ctx`); ya no se usa `dotenv.load_dotenv` en ningún lado.
- `client.Page(items, raw_count, elapsed_ms, status)`: Tarea 8 usa `page.items`, `page.raw_count`, `page.elapsed_ms`, `page.status`. `client.refs_for_phone(name, digits) -> list[str]` (Tarea 3) alimenta `_labels_from_owner_file(client, name) -> dict[str, list[str]]` (Tarea 8), que es el tipo que recibe `measures.monthly_by_label(facts, labels: dict[str, list[str]])` (Tarea 7).
- `client.WahaClient(base_url, admin_key, salt, *, transport, timeout=600.0)`: la Tarea 8 lo construye con `store.salt()` y `TRANSPORT_FACTORY()`; el test de la Tarea 8 lo parchea. `httpx.TimeoutException` es lo que captura `cmd_count_pass` y lo que lanza el servidor falso (`httpx.ReadTimeout`, subclase).
- `session_spec.build_session_body(name, *, webhook_url, hmac_key, run_id, engine, store_enabled, full_sync, retries)`: mismos nombres en Tareas 4 y 8 (`create-session`).
- `lifecycle.ensure_session/wait_for_status/teardown_session`: claves de retorno (`retries_effective`, `status`, `transitions`, `timed_out`, `status_before`, `delete_status`, `session_gone`, `keys_deleted`, `keys_remaining`, `keys_error`, `ok`) coinciden entre implementación, tests, `runner`, README y fila 14 del documento de resultados. `lifecycle` importa `WahaClientError` de `client` (definido en Tarea 3).
- `results.ResultsStore`: `init/salt/forget_salt/path/write/read/append_jsonl/read_jsonl/set_link_ts/link_ts` usados con esos nombres en Tareas 6 y 8.
- `measures`: `count_summary` devuelve `total, from_me, inbound, with_text, with_media, chats, without_timestamp, oldest_ts, newest_ts, oldest_iso, newest_iso, by_month`; `stability` y `count-pass` leen `total`, `oldest_ts`, `oldest_iso`; `events_summary` devuelve `by_event, message_vs_any_inbound, history_events, history_unknown, distinct_chats, lid_chats, delivery, status_timeline, lag_ms_p50, link_ts`, y `cmd_events_summary` agrega `records_other_sessions`; son los nombres citados en README y documento de resultados. `autoreply.json` lleva `candidates[]` con `rank, chats, messages, fast_share` (sin `text_fp`).
- Nombres de archivos de resultados (`server.json`, `session_create.json`, `qr_timeline.json`, `pairing_code.json`, `link_wait.json`, `stability.json`, `count_pass.json`, `chat_kinds.json`, `monthly_counts.json/.csv`, `autoreply.json`, `events_summary.json`, `reconcile.json`, `lids.json`, `keys.json` con `session, created, id_ref`, `no_store.json`, `watch.jsonl`, `notes.jsonl` con claves `ram_mb, disk_bytes, media_files, waha_log_pii_hits`, `teardown.json`, `events.jsonl`, `link.json`, `salt.bin`, `run.json`) coinciden entre `runner.py`, `receiver.py`, README y documento de resultados.
- Variables de entorno: las cinco obligatorias, las cuatro opcionales y `SPIKE_ENV_FILE` (solo receptor, solo shell) tienen los mismos nombres en `config.py`, `receiver.py`, `runner.py`, `.env.spike.example`, Global Constraints y README. El README, el Step 5 de la Tarea 6 y el snippet de PowerShell 5.1 usan solo cmdlets que existen en 5.1 (`Read-Host -AsSecureString`, `[System.Net.NetworkCredential]`, `Remove-Item Env:`, `cmd /c` para el `2>&1` nativo).
