# Vertical petshop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que Mascotas del Oeste (MO) venda por WhatsApp con su identidad y sus reglas desde el mismo código, con `VERTICAL=petshop`, sin cambiar la farmacia (prompt idéntico byte a byte, mismos textos, los 933 tests de hoy en verde) salvo las correcciones de §5, y con la mutual registrada como perfil sin cambios de comportamiento.

**Architecture:** Un perfil de rubro (`app/services/perfil.py`) con capacidades (`recetas`, `obras_sociales`, `socios`, `cuenta_corriente`, `links_como_receta`, `sintomas`, `venta`, `catalogo_csv_base`), textos y marca, elegido por `VERTICAL` (y `COMERCIO_NOMBRE`) y leído con `get_perfil()` en el momento de usarlo. Cada gancho de farmacia (webhook, checkout, visión, pagos, arranque y catálogo) pregunta por una capacidad, nunca por el nombre del rubro. Los prompts viven en `app/services/prompts.py`, que es solo texto: el de farmacia armado por bloques e idéntico al de hoy, y la plantilla de petshop sobre los mismos bloques compartidos. La config mezcla `DEFAULTS` con los textos del perfil (`valores_base()`) y lo guardado sigue ganando.

**Tech Stack:** Python 3.12, FastAPI, Redis, Postgres (asyncpg), pydantic-settings, SDK de Anthropic y de OpenAI, httpx; tests con pytest + pytest-asyncio (`asyncio_mode = auto`) y pgserver.

**Spec:** `docs/superpowers/specs/2026-10-06-vertical-petshop-design.md`

## Global Constraints

- Todo se corre desde `D:/Dev/WhatsappBOTy-mercurio-pedidos`, rama `feature/vertical-petshop`, en Git Bash; los tests con `.venv/Scripts/python -m pytest`.
- Las líneas citadas son las del commit `07a1d7a`; si una tarea anterior las corrió, cada bloque se ubica por el texto que se cita.
- Línea de base: 933 tests en verde en `07a1d7a`; al cerrar cada tarea la suite completa queda en `0 failed`.
- Prompt de farmacia idéntico byte a byte: sha256 `1953a4e6815d855e635406c1da8f97ff83bb9be6a2fd040b69eabfae4c540749`, 14.680 caracteres (`intent_service.SYSTEM_PROMPT` y `perfil_por_clave("farmacia").system_prompt`).
- Prompt de la mutual intacto: `perfil.system_prompt is SYSTEM_PROMPT_MUTUAL`, sha256 `4377db47f567816d994d1824bd25adb7cb40e5bb9a96d00b74491f81258dc469`.
- `DEFAULTS` sin las tres claves nuevas (`pago_mp_manual`, `retiro_sucursal`, `retiro_info_message`): 90 claves, sha256 `655cbe78de891191bb660b42cc1d4b3b9e325bf93205f139a6f4bfcefa910e1d` de `json.dumps(..., sort_keys=True, ensure_ascii=False)`.
- Los goldens van como hash dentro del test, nunca como `.txt` (el repo tiene `core.autocrlf=true` sin `.gitattributes`).
- `get_perfil()` se llama en el momento de usarlo, adentro de cada función: nunca en una variable de módulo, en un `__init__` ni en un singleton (`IntentService`, `PaymentService`, `PaywayService`, `ConfigService`).
- Cada gancho pregunta por una capacidad del perfil (`perfil.recetas`, `perfil.venta`...), nunca por el nombre del rubro.
- `app/services/prompts.py` es solo texto y no importa nada de `app`; `perfil.py` importa solo stdlib, `app.config`, `prompts` y `mutual_helper`, y `config_service.DEFAULTS` adentro de `_registro()`.
- Los textos de petshop (perfil y prompt) no dicen farmacia, receta, socio, obra social ni mutual, y no llevan 💊: el emoji del perfil petshop es 🐾.
- `VERTICAL` se normaliza con `strip().lower()`; vacío o sin setear es farmacia; un valor desconocido corta el arranque con `ValueError` ("Valores válidos: farmacia, mutual, petshop") antes de tocar Redis, el catálogo o Postgres.
- Las correcciones de §5 (pregunta en `esperando_entrega` y `esperando_confirmacion`, horario eligiendo la entrega, pesos y presentaciones) van marcadas como **CAMBIO QUE TAMBIÉN AFECTA A LA FARMACIA** y son lo único que cambia a la farmacia.
- TDD: cada test nuevo se escribe antes que el código y se ve fallar; los goldens, las guardas "igual que hoy" y los tests de Review Focus pasan en verde de entrada.
- Los tests eligen perfil solo con la fixture `usar_perfil` (Task 1), que pisa `vertical` y `comercio_nombre` en el `Settings` cacheado; cualquier otro setting se pisa igual (`monkeypatch.setattr(get_settings(), ...)`). Nunca `get_settings.cache_clear()`, y un `setenv`/`delenv` no llega al app (un `Settings` nuevo levantaría el `DATABASE_URL` que deja `pg_dsn`).
- Ningún test existente cambia de expectativa (spec §6.3).
- Mensajes de commit en español, terminados con la línea `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

Cinco clases de entrada o modos de falla que el spec implica y que ninguna tarea ejercitaba, de la más probable a la menos probable. Cada una quedó fijada con un test en su tarea dueña (en el estilo de esa tarea); pasan de entrada porque fijan lo que la tarea ya implementó, y se verificaron con el plan completo aplicado sobre `07a1d7a` (suite entera: 1373 passed).

1. **Mensajes en mayúsculas, sin tildes o transcriptos de audio sin signos.** La corrección de §5 decide con regex sobre texto libre, y los 31 casos de §6.2 van casi todos en minúscula y con "?". En WhatsApp y en los audios transcriptos llegan "EN QUE SUCURSAL PUEDE SER", "Donde Retiro" o "RETIRO EN SUCURSAL". Test: `test_es_pregunta_entrega_en_mayusculas_sin_tildes_ni_signos` (11 casos) y `test_pregunta_en_mayusculas_y_sin_signos_al_elegir_entrega_se_responde`, en **Task 13, Step 26b** (`tests/test_entrega_sucursal.py`, con los dos perfiles). Riesgo residual que NO se fija con test, porque fijarlo sería afirmar un bug: sin "?", con el interrogativo lejos del principio o sin tilde ("y la sucursal donde queda", "cuando puedo pasar", "cuanto sale el envio"), `es_pregunta_entrega` da `False` y el mensaje sigue contando como elección de retiro o envío, igual que hoy. Es una decisión del regex del spec (`_INTERROGATIVO_INICIO` exige tilde en "cómo", "cuándo", "cuánto" y "qué" para que "como siempre, retiro" siga siendo elección). Revisarlo con mensajes reales de MO antes de abrir el número.
2. **Config guardada en Redis o Postgres de MO que pisa los textos del perfil.** MO corre hoy como farmacia, así que su Redis y su Postgres pueden tener claves guardadas (§9.2). Los tests de la Task 3 prueban "lo guardado gana" con una clave neutra y los fallbacks con un `cfg` armado a mano, no el caso real: un texto vaciado desde el panel y un texto viejo con 💊, leídos de Redis y de Postgres. Test: `test_config_guardada_de_mo_vaciada_cae_al_perfil_y_con_pildora_gana[redis|postgres]`, en **Task 3, Step 16b** (`tests/test_perfil_config.py`). El vacío cae al texto del perfil al usarlo; el de 💊 gana, y por eso el Step 10.3 de la Task 17 (borrar esas claves) es obligatorio.
3. **Primer arranque de MO con el catálogo vacío.** Hasta que termina el primer sync de Mercurio, `get_sku_service()` devuelve un catálogo vacío (§9.2). Los tests de la Task 12 miran `total == 0`, pero ninguno pasa un mensaje por el webhook en ese estado. Test: `test_petshop_con_catalogo_vacio_no_inventa_ni_deja_pendiente`, en **Task 12, Step 17b** (`tests/test_perfil_arranque.py`). Aunque el modelo invente producto y precio, sale "No lo encuentro en nuestro catálogo…", sin el precio, sin pendiente y sin "farmacia".
4. **`VERTICAL` con espacios y mayúsculas, y `COMERCIO_NOMBRE` con ñ, tildes y espacios.** Hay cobertura parcial: la normalización de `VERTICAL` en `get_perfil` (Task 2), el descriptor ASCII con "Piñata … Ñuñoa" y `/pay` con "Ñandú & Cía <MO>" (Task 11). Ningún test cobraba por Payway con los dos valores tal como se cargan a mano en Railway, donde el `device_unique_identifier` de respaldo tiene que salir ASCII y la descripción tiene que llevar el nombre recortado. Test: `test_payway_con_vertical_en_mayusculas_y_comercio_con_enie_y_tildes`, en **Task 11, Step 13b** (`tests/test_petshop.py`).
5. **Perfil cacheado en un singleton.** §3.1 nombra cuatro singletons. `IntentService` (Task 4), `PaymentService` y `PaywayService` (Task 11) tienen un test con la misma instancia y dos perfiles; `ConfigService` no lo tenía, y `valores_base()` tiene que recalcularse en cada `get_all`/`get`, sin quedar fijado en la instancia. Test: `test_config_service_no_fija_el_perfil_en_la_instancia`, en **Task 3, Step 16b** (`tests/test_perfil_config.py`). El control estático (`get_perfil()` en una variable de módulo o en `self.`) es la Task 17, Step 6.

## File Structure

| Archivo | Acción | Tareas | Responsabilidad |
|---|---|---|---|
| `app/services/prompts.py` | Crear | 2, 4 | Solo textos, sin imports de `app`: prompt de farmacia armado por bloques (idéntico byte a byte), bloques compartidos, `matriz_intenciones`, `formato_respuesta`, plantilla de petshop (`PET_*`), `resolver_plantilla` y los prompts de visión de los dos rubros. |
| `app/services/perfil.py` | Crear | 2 | Dataclasses `VocabularioAudio`, `VisionPerfil` y `Perfil`; `CLAVES_TEXTO_RUBRO`, `MARCAS_AUDIO_FARMACIA`, `VISION_FARMACIA`, `VISION_PETSHOP`; registro de los tres perfiles; `get_perfil()` (cache, `VERTICAL`, `COMERCIO_NOMBRE`) y `perfil_por_clave()`. |
| `app/config.py` | Modificar | 2 | `comercio_nombre` (env `COMERCIO_NOMBRE`) y comentario de `vertical`. |
| `app/main.py` | Modificar | 2, 12 | Perfil validado y logueado al principio del lifespan; `_restaurar_archivos`, `_hidratar_padron`, `_init_referencia_receta` y `_avisar_horario_por_defecto`. |
| `app/services/intent_service.py` | Modificar | 2, 4 | Re-exporta `SYSTEM_PROMPT`; sin parámetro `vertical`; prompt, bloque de socio, rótulo de KB y marca de receta salen del perfil en cada llamada. |
| `app/services/image_service.py` | Modificar | 2, 10 | `_PROMPT` como alias; prompt y categorías del clasificador según `get_perfil().vision`. |
| `app/services/config_service.py` | Modificar | 3 | `valores_base()` (DEFAULTS + textos del perfil) en `get_all`/`get`; claves `pago_mp_manual`, `retiro_sucursal` y `retiro_info_message`; comentario de `sintoma_farmaceutico_message`. |
| `app/routers/backoffice.py` | Modificar | 3, 12, 16 | `ConfigUpdate` con las tres claves nuevas; `bo_tablero` con `get_perfil().clave`; `GET /bo/perfil` para el portal. |
| `app/routers/orders_api.py` | Modificar | 3 | Aviso de pedido listo con fallback a `perfil.textos`. |
| `app/services/checkout_helper.py` | Modificar | 3, 6, 7, 8, 9, 11, 13, 14 | Fallbacks a `perfil.textos`; gates de recetas, cuenta corriente y descuentos; `dominio_propio`/`contiene_link`; `pide_pago_manual` con exclusiones; `MOTIVO_CONSULTA_SALUD` y `texto_consulta_salud`; `mensaje_pago_confirmado`; detección de preguntas de entrega y textos con la sucursal; `_NO_DIR` con pesos y `presentaciones_de`. |
| `app/routers/webhook.py` | Modificar | 3, 4, 6, 7, 8, 9, 10, 12, 13 | `perfil = get_perfil()` por lote y gates por capacidad (socios, cuenta corriente, obras sociales, recetas, links, venta), compuertas de salud A-D, ramas de imagen por perfil, pregunta en la elección de entrega y dato de la sucursal para el modelo. |
| `app/routers/simulate.py` | Modificar | 8 | Contexto de socio solo con `perfil.socios`. |
| `app/services/sku_service.py` | Modificar | 5, 12 | `vocabulario_audio` según el perfil (`MARCAS_AUDIO_BASE` por compatibilidad); `_csv_permitido` en `get_sku_service`/`reload_sku_service`. |
| `app/services/catalog_rules.py` | Modificar | 6 | `explicar_receta` → `("no", "sin_recetas")` sin recetas; `ORIGENES["sin_recetas"]`. |
| `app/services/catalog_source.py` | Modificar | 12 | `CSV_FARMACIA` y `csv_de_arranque`: un perfil sin `catalogo_csv_base` nunca carga el CSV de la farmacia. |
| `app/services/metrics_store.py` | Modificar | 9 | `derivado_consulta_salud` e `imagen_indicacion_veterinaria` cuentan como derivación. |
| `app/static/dashboard.html` | Modificar | 9 | Las mismas dos intenciones en `DERIV_INTENTS`. |
| `app/routers/mp_webhook.py` | Modificar | 11 | Confirmación de pago con `mensaje_pago_confirmado` (emoji del perfil y sucursal). |
| `app/routers/payway.py` | Modificar | 11 | Confirmación de pago compartida; `/pay` y páginas de estado con la marca del perfil (`_con_marca`). |
| `app/services/payment_service.py` | Modificar | 11 | `statement_descriptor()` del perfil, leído en cada link. |
| `app/services/payway_service.py` | Modificar | 11 | Descripción, antifraude y device de respaldo con el comercio del perfil (`_slug`). |
| `scripts/probar_prompt_petshop.py` | Crear | 15 | Prueba manual del prompt de petshop con el LLM real (§6.5). |
| `tests/conftest.py` | Modificar | 1 | Fixture `usar_perfil`. |
| `tests/test_goldens_farmacia.py` | Crear | 1, 2 | Goldens de farmacia: prompts, visión, `DEFAULTS`, `/pay` y páginas de estado. |
| `tests/test_perfil.py` | Crear | 2, 4, 5 | Registro de perfiles, prompts, textos, `get_perfil`, arranque, `IntentService` y audio. |
| `tests/test_perfil_config.py` | Crear | 3 | `valores_base`, claves nuevas, los 12 fallbacks y Review Focus de config guardada y singleton. |
| `tests/test_perfil_arranque.py` | Crear | 12 | Arranque, CSV de la farmacia, tablero, desvío sin venta y Review Focus de catálogo vacío. |
| `tests/test_petshop.py` | Crear | 4, 6, 7, 8, 9, 10, 11 | Unitarios por capacidad, cada uno con su par de farmacia "igual que hoy". |
| `tests/test_petshop_conversaciones.py` | Crear | 6, 7, 8, 9, 10, 15 | Conversaciones por el webhook completo con dependencias falsas, incluidas las punta a punta. |
| `tests/test_perfil_bo.py` | Crear | 16 | `GET /bo/perfil`: identidad y capacidades por rubro, autenticación y que no expone prompt ni textos. |
| `tests/test_entrega_sucursal.py` | Crear | 13 | Corrección de §5 (pregunta en la elección de entrega), con farmacia y con petshop. |
| `tests/test_pesos.py` | Crear | 14 | Pesos y presentaciones de §5, con farmacia y con petshop. |
| `tests/test_probar_prompt_petshop.py` | Crear | 15 | El script de prueba del prompt, sin red. |

---

### Task 1: Goldens de farmacia y fixture `usar_perfil`

Antes de cualquier refactor se fotografía lo que la farmacia NO puede cambiar: el prompt de farmacia, el de la mutual, el prompt de visión, `DEFAULTS` y el HTML de `/pay` y de las páginas de estado de Payway. Son goldens: **pasan en verde sobre el código de hoy** (es la única excepción al "ver fallar" de TDD, spec §6) y tienen que seguir en verde después de cada tarea. Van como hash dentro del test, no como `.txt` (el repo tiene `core.autocrlf=true` sin `.gitattributes`).

Todos los hashes se calcularon sobre el código de hoy (`07a1d7a`, idéntico a la rama) y coinciden con los de la spec: prompt de farmacia `1953a4e6…0749` (14.680 caracteres), mutual `4377db47…c469`, `DEFAULTS` `655cbe78de891191bb660b42cc1d4b3b9e325bf93205f139a6f4bfcefa910e1d` (90 claves). Los de visión y `/pay` son nuevos de esta tarea.

**Files:**
- Create: `tests/test_goldens_farmacia.py`
- Modify: `tests/conftest.py` (se agrega al final, después de `_adjuntos_en_tmp`, líneas 77-82)
- Test: `tests/test_goldens_farmacia.py`

**Interfaces:**
- Consumes: `app.services.intent_service.SYSTEM_PROMPT`, `app.services.mutual_helper.SYSTEM_PROMPT_MUTUAL`, `app.services.image_service._PROMPT`, `app.services.config_service.DEFAULTS`, `app.routers.payway.pay_page(pid: str)`, `app.routers.payway.payway_return(r: str = "")`, `app.routers.payway._get_pending`, `app.routers.payway.get_payway_service`, `app.config.get_settings()`.
- Produces: fixture `usar_perfil(clave: str, comercio: str | None = None) -> Perfil` en `tests/conftest.py` (usable desde la Task 2: importa `app.services.perfil` de forma diferida). Fixture local `payway_falso` en `tests/test_goldens_farmacia.py`.

- [ ] **Step 1: Escribir los goldens**

Crear `tests/test_goldens_farmacia.py`:

```python
"""
Goldens de la farmacia, tomados sobre 07a1d7a ANTES del refactor del perfil de
rubro (spec 2026-10-06-vertical-petshop-design.md §2.3 y §6). Pasan en verde
con el código de hoy y tienen que seguir en verde después de cada tarea: la
farmacia no cambia ni un byte.

Van como hash adentro del test y no como .txt: el repo tiene
core.autocrlf=true sin .gitattributes, y un .txt cambiaría de bytes al hacer
checkout en Windows.
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

PROMPT_FARMACIA_SHA256 = "1953a4e6815d855e635406c1da8f97ff83bb9be6a2fd040b69eabfae4c540749"
PROMPT_FARMACIA_LEN = 14680
PROMPT_MUTUAL_SHA256 = "4377db47f567816d994d1824bd25adb7cb40e5bb9a96d00b74491f81258dc469"
VISION_FARMACIA_SHA256 = "84fec1f627d68084101c2afe62d8c6c15dc7deb0ae238b30a16cc0e8bf423132"
DEFAULTS_SHA256 = "655cbe78de891191bb660b42cc1d4b3b9e325bf93205f139a6f4bfcefa910e1d"
DEFAULTS_CLAVES = 90
# Claves que la spec suma a DEFAULTS (§4.4 y §5): quedan fuera del golden.
CLAVES_NUEVAS = {"pago_mp_manual", "retiro_sucursal", "retiro_info_message"}

PAY_SHA256 = "85c4271036760ed8f4185d0d2c860bf6c7bfb926d2d36e4a882283a22f84ad52"
PAY_VENCIDO_SHA256 = "a23c99347472476c32a97742e46978de209ec2c2a8d4165ed33386df16f47283"
RETURN_OK_SHA256 = "0b8cb84ff34ec20836cd88e807afa23f41e8e0b2e9b3b5fdba629ac1d0e44eea"
RETURN_ERR_SHA256 = "a975ad681e7c07c62cf5ef0c9260c8cd109c578870b0d820376e98fa31225994"


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def test_prompt_farmacia_golden():
    from app.services.intent_service import SYSTEM_PROMPT
    assert len(SYSTEM_PROMPT) == PROMPT_FARMACIA_LEN
    assert _sha(SYSTEM_PROMPT) == PROMPT_FARMACIA_SHA256


def test_prompt_mutual_golden():
    from app.services.mutual_helper import SYSTEM_PROMPT_MUTUAL
    assert _sha(SYSTEM_PROMPT_MUTUAL) == PROMPT_MUTUAL_SHA256


def test_prompt_vision_farmacia_golden():
    from app.services.image_service import _PROMPT
    assert _sha(_PROMPT) == VISION_FARMACIA_SHA256


def test_defaults_golden():
    from app.services.config_service import DEFAULTS
    base = {k: v for k, v in DEFAULTS.items() if k not in CLAVES_NUEVAS}
    assert len(base) == DEFAULTS_CLAVES
    assert _sha(json.dumps(base, sort_keys=True, ensure_ascii=False)) == DEFAULTS_SHA256


# ── /pay y páginas de estado de Payway (spec §4.7: "en farmacia, el body es
# idéntico a un snapshot tomado antes del cambio") ─────────────────────────────

@pytest.fixture
def payway_falso(monkeypatch):
    """pay_page sin Redis ni Payway: pago pendiente fijo y servicio falso."""
    from app.config import get_settings
    from app.routers import payway as pw

    async def _pending(pid):
        if pid == "abc123":
            return {"total": "4770.0", "sku_nombre": "IBUPROFENO 600 MG X 10",
                    "estado": "pendiente"}
        return None

    monkeypatch.setattr(pw, "_get_pending", _pending)
    monkeypatch.setattr(pw, "get_payway_service", lambda *a, **k: SimpleNamespace(
        base_url="https://pw.test/api/v2", public_key="PUBKEY"))
    monkeypatch.setattr(get_settings(), "payway_cs_org_id", "ORG")
    monkeypatch.setattr(get_settings(), "payway_cs_merchant_id", "MERCH")
    return pw


async def test_pay_page_farmacia_snapshot(payway_falso):
    r = await payway_falso.pay_page("abc123")
    assert r.status_code == 200
    assert hashlib.sha256(r.body).hexdigest() == PAY_SHA256


async def test_pay_page_vencido_farmacia_snapshot(payway_falso):
    r = await payway_falso.pay_page("vencido")
    assert r.status_code == 404
    assert hashlib.sha256(r.body).hexdigest() == PAY_VENCIDO_SHA256


async def test_payway_return_farmacia_snapshot(payway_falso):
    ok = await payway_falso.payway_return("ok")
    err = await payway_falso.payway_return("")
    assert hashlib.sha256(ok.body).hexdigest() == RETURN_OK_SHA256
    assert hashlib.sha256(err.body).hexdigest() == RETURN_ERR_SHA256
```

- [ ] **Step 2: Correr los goldens y ver que pasan sobre el código de hoy**

```bash
.venv/Scripts/python -m pytest tests/test_goldens_farmacia.py -v
```

Esperado: `7 passed`. Si alguno falla, NO se sigue: el código de partida no es `07a1d7a` y los goldens hay que recalcularlos antes de tocar nada.

- [ ] **Step 3: Agregar la fixture `usar_perfil` a `tests/conftest.py`**

Al final de `tests/conftest.py` (después de la fixture `_adjuntos_en_tmp`, línea 82):

```python
@pytest.fixture
def usar_perfil():
    """Cambia el perfil de rubro durante el test y devuelve el Perfil activo:
    usar_perfil("petshop") o usar_perfil("petshop", comercio="MO Prueba").

    Pisa `vertical` y `comercio_nombre` en el Settings cacheado (no setea
    variables de entorno ni recrea Settings: pg_dsn deja DATABASE_URL en
    os.environ para toda la sesión y un Settings nuevo lo levantaría) y limpia
    el cache de get_perfil antes y en el teardown, DESPUÉS de restaurar los
    atributos. Sin ese cache_clear el perfil se filtra al resto de la suite.
    Como no recrea Settings, convive con monkeypatch.setattr(get_settings(), ...)
    en cualquier orden.

    Requiere app/services/perfil.py (Task 2 del plan): los imports son
    diferidos para que este conftest cargue antes de que exista.
    """
    from app.config import get_settings
    from app.services.perfil import get_perfil

    mp = pytest.MonkeyPatch()

    def _usar(clave: str, comercio: str | None = None):
        s = get_settings()
        mp.setattr(s, "vertical", clave)
        mp.setattr(s, "comercio_nombre", comercio or "")
        get_perfil.cache_clear()
        return get_perfil()

    yield _usar
    mp.undo()
    get_perfil.cache_clear()
```

Nota de diseño (difiere a propósito del código de la spec §6.1, misma interfaz): la spec hacía `setenv` + `get_settings.cache_clear()`. Eso recrea `Settings` desde el entorno, y la fixture de sesión `pg_dsn` (`tests/conftest.py:59`) deja `DATABASE_URL` en `os.environ`: después del primer teardown, el resto de la suite correría con un `database_url` que hoy no tiene (verificado: corriendo `test_agent_ws.py` y `test_alias.py`, `get_settings().database_url` sigue `''` pero un `Settings()` nuevo ya trae el DSN del pgserver). Cualquier otra fixture que haga `get_settings.cache_clear()` tiene el mismo problema: para cambiar de perfil se usa `usar_perfil`. Además se perdían los `monkeypatch.setattr(get_settings(), ...)` hechos antes de llamar a la fixture. Pisar los dos atributos del `Settings` cacheado evita las dos cosas; el `get_perfil.cache_clear()` antes y en el teardown (después de restaurar) se mantiene. Nadie usa la fixture todavía: hasta la Task 2 no existe `app/services/perfil.py`.

- [ ] **Step 4: Correr la suite completa**

```bash
.venv/Scripts/python -m pytest -q
```

Esperado: `940 passed` (933 de hoy + 7 goldens).

- [ ] **Step 5: Commit**

```bash
git add tests/test_goldens_farmacia.py tests/conftest.py
git commit -F - <<'EOF'
Goldens de farmacia y fixture usar_perfil antes del perfil de rubro

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 2: `prompts.py`, `perfil.py`, `COMERCIO_NOMBRE` y corte del arranque

Se crea el perfil de rubro (spec §3.1-§3.5): `prompts.py` con el prompt de farmacia **armado por bloques** (spec §4.1) y el de visión movido; `perfil.py` con las dataclasses, los tres perfiles, `CLAVES_TEXTO_RUBRO`, `MARCAS_AUDIO_FARMACIA`, `get_perfil()` y `perfil_por_clave()`; `comercio_nombre` en `config.py`; y el log/corte del arranque en el lifespan. Ningún gancho del webhook, checkout ni pagos se toca acá: los consumidores de `get_perfil()` son de otras tareas.

La composición de `SYSTEM_PROMPT` se verificó con un prototipo sobre `07a1d7a`: reproduce el sha256 `1953a4e6…0749` y los 14.680 caracteres. Respecto de la fórmula de la spec (`36-45 + SEGUIMIENTO + 50-73 + DERIVACION + 77-99 + RESERVAS + 103-109 + matriz(114, 119) + CONFIRMACIONES + RESPUESTA_DIRECTA + 131-140 + formato(145, 156, 160, 164, 168-169)`), los tramos 50-73 y 131-140 se cortan un poco más fino para dejar como constantes públicas las líneas que petshop copia "= farmacia NN" (59-64, 68-70, 72-73, 133-134 y 138). Los bytes son los mismos y lo controla el golden.

**Coordinación con la Task 4 (prompt de petshop):** en esta tarea `SYSTEM_PROMPT_PETSHOP_PLANTILLA` es una **plantilla mínima provisoria**, entre los comentarios `# >>> PETSHOP: PLANTILLA PROVISORIA (Task 2)` y `# <<< PETSHOP` de `app/services/prompts.py`, después de `resolver_plantilla` y antes de la sección de visión. La Task 4 reemplaza **todo lo que está entre esos dos marcadores** por los bloques `PET_*` y la composición de §4.1, usando los bloques de este archivo: `SEGUIMIENTO`, `DERIVACION`, `RESERVAS`, `CONFIRMACIONES`, `RESPUESTA_DIRECTA`, `matriz_intenciones(...)`, `formato_respuesta(...)` y las líneas reusables `CATALOGO_BUSQUEDA` (= farmacia 59-64), `PAGO_SIN_LINKS` (68-70), `PAGO_CORRECCION_CANTIDAD` (72-73), `VARIOS_PRIMERO_Y_DEMAS` (133-134) y `VARIOS_NUNCA_JUNTES` (138). Las líneas 139 y 156 que petshop cambia están en `_FARMACIA_VARIOS_SOLO_PRINCIPAL` y `_FARMACIA_AGREGAR` (privadas, mismo módulo). El perfil petshop resuelve la plantilla adentro de `_registro()`, así que toma la versión completa sin tocar `perfil.py`. El test `test_perfil_petshop` de esta tarea exige solo "Soy el asistente virtual de Mascotas del Oeste" y que no queden `{comercio}`/`{emoji}`, cosa que el prompt completo también cumple. `VISION_PROMPT_PETSHOP` queda completo acá (texto literal de §4.6, verificado byte a byte contra la spec).

**Files:**
- Create: `app/services/prompts.py`, `app/services/perfil.py`, `tests/test_perfil.py`
- Modify: `app/services/intent_service.py:21-22` (import) y `:36-172` (se borra el literal y la línea en blanco que lo sigue); `app/services/image_service.py:20-21` (import) y `:27-50` (`_PROMPT`); `app/config.py:25-27`; `app/main.py:18` (import) y `:26-28` (lifespan); `tests/test_goldens_farmacia.py` (fixture `payway_falso`, sin cambiar expectativas)
- Test: `tests/test_perfil.py`, `tests/test_goldens_farmacia.py`

**Interfaces:**
- Consumes: `app.config.get_settings()` (`vertical`, `comercio_nombre`), `app.services.mutual_helper.SYSTEM_PROMPT_MUTUAL`, `app.services.config_service.DEFAULTS` (import diferido adentro de `_registro()`), fixture `usar_perfil` (Task 1).
- Produces (contrato):
  - `app/services/prompts.py` (sin imports de `app`): `SYSTEM_PROMPT: str`, `SYSTEM_PROMPT_PETSHOP_PLANTILLA: str` (provisoria, ver arriba), `VISION_PROMPT_FARMACIA: str`, `VISION_PROMPT_PETSHOP: str`, `def resolver_plantilla(plantilla: str, comercio: str, emoji: str) -> str`.
  - `app/services/perfil.py`: `VocabularioAudio`, `VisionPerfil`, `Perfil` (frozen, campos y orden del contrato), `CLAVES_TEXTO_RUBRO: frozenset[str]`, `MARCAS_AUDIO_FARMACIA: tuple[str, ...]`, `VISION_FARMACIA`, `VISION_PETSHOP`, `def get_perfil() -> Perfil` (`@lru_cache`, `ValueError` con `VERTICAL` desconocido), `def perfil_por_clave(clave: str) -> Perfil`.
  - `app/config.py`: `Settings.comercio_nombre: str = ""`.
  - `app/services/intent_service.py`: re-export `SYSTEM_PROMPT` (mismo objeto que `prompts.SYSTEM_PROMPT`); `app/services/image_service.py`: `_PROMPT` es alias de `prompts.VISION_PROMPT_FARMACIA`.
- Produces (agregado fuera del contrato, para la Task 4): en `prompts.py`, `SEGUIMIENTO`, `DERIVACION`, `RESERVAS`, `CONFIRMACIONES`, `RESPUESTA_DIRECTA`, `CATALOGO_BUSQUEDA`, `PAGO_SIN_LINKS`, `PAGO_CORRECCION_CANTIDAD`, `VARIOS_PRIMERO_Y_DEMAS`, `VARIOS_NUNCA_JUNTES: str`; `def matriz_intenciones(fila_saludo: str, fila_consulta_abierta: str) -> str`; `def formato_respuesta(linea_entidad: str, parrafo_agregar: str, linea_por_sintoma: str, linea_rechazo: str, lineas_cambio: str) -> str`. Cada hueco es una o más líneas completas con su `"\n"` final.

- [ ] **Step 1: Escribir `tests/test_perfil.py`**

Crear `tests/test_perfil.py` (las tareas siguientes agregan sus secciones al final de este archivo):

```python
"""
Perfil de rubro (spec 2026-10-06-vertical-petshop-design.md §3, §4.1 y §6.2):
registro de perfiles, get_perfil, prompts armados por bloques, textos del
perfil y corte del arranque con un VERTICAL desconocido.

Los tests que cambian de perfil usan la fixture `usar_perfil` (conftest.py).
"""
import hashlib
import inspect
import logging
import os
import re

import pytest

from app.services import prompts
from app.services.config_service import DEFAULTS
from app.services.mutual_helper import SYSTEM_PROMPT_MUTUAL
from app.services.perfil import (
    CLAVES_TEXTO_RUBRO, MARCAS_AUDIO_FARMACIA, VISION_FARMACIA, VISION_PETSHOP,
    get_perfil, perfil_por_clave,
)

SHA_FARMACIA = "1953a4e6815d855e635406c1da8f97ff83bb9be6a2fd040b69eabfae4c540749"
SHA_MUTUAL = "4377db47f567816d994d1824bd25adb7cb40e5bb9a96d00b74491f81258dc469"
SHA_VISION_FARMACIA = "84fec1f627d68084101c2afe62d8c6c15dc7deb0ae238b30a16cc0e8bf423132"
CAPACIDADES = ("recetas", "obras_sociales", "socios", "cuenta_corriente", "links_como_receta")
BLOQUES_COMPARTIDOS = ("SEGUIMIENTO", "DERIVACION", "RESERVAS", "CONFIRMACIONES",
                       "RESPUESTA_DIRECTA")
LINEAS_REUSABLES = ("CATALOGO_BUSQUEDA", "PAGO_SIN_LINKS", "PAGO_CORRECCION_CANTIDAD",
                    "VARIOS_PRIMERO_Y_DEMAS", "VARIOS_NUNCA_JUNTES")


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ── Prompts ───────────────────────────────────────────────────────────────────

def test_prompt_farmacia_identico_byte_a_byte():
    from app.services import intent_service
    for texto in (perfil_por_clave("farmacia").system_prompt, intent_service.SYSTEM_PROMPT):
        assert len(texto) == 14680
        assert _sha(texto) == SHA_FARMACIA
    # intent_service lo re-exporta: es el mismo objeto, no una copia.
    assert intent_service.SYSTEM_PROMPT is prompts.SYSTEM_PROMPT


def test_bloques_del_prompt_de_farmacia():
    for nombre in BLOQUES_COMPARTIDOS + LINEAS_REUSABLES:
        bloque = getattr(prompts, nombre)
        assert bloque.endswith("\n"), nombre
        assert bloque in prompts.SYSTEM_PROMPT, nombre


def test_prompts_no_importa_nada_de_app():
    fuente = inspect.getsource(prompts)
    assert not re.search(r"^\s*(from|import)\s+app\b", fuente, re.MULTILINE)


def test_resolver_plantilla_usa_replace_y_respeta_las_llaves_del_json():
    plantilla = 'Sos {comercio} {emoji}. Respondé {"intencion": "saludo"} {otra}'
    assert (prompts.resolver_plantilla(plantilla, "MO", "🐾")
            == 'Sos MO 🐾. Respondé {"intencion": "saludo"} {otra}')


def test_prompt_de_vision_de_farmacia_movido_byte_a_byte():
    from app.services import image_service
    assert image_service._PROMPT is prompts.VISION_PROMPT_FARMACIA
    assert _sha(prompts.VISION_PROMPT_FARMACIA) == SHA_VISION_FARMACIA
    assert VISION_FARMACIA.prompt is prompts.VISION_PROMPT_FARMACIA
    assert VISION_FARMACIA.categorias == ("receta", "bono", "credencial", "comprobante",
                                          "producto", "otro")


def test_vision_petshop():
    assert VISION_PETSHOP.prompt is prompts.VISION_PROMPT_PETSHOP
    assert VISION_PETSHOP.categorias == ("producto", "comprobante", "indicacion_veterinaria", "otro")
    # El esquema que se le pide al modelo lista exactamente las categorías.
    assert '"tipo": "producto|comprobante|indicacion_veterinaria|otro"' in VISION_PETSHOP.prompt
    bajo = VISION_PETSHOP.prompt.lower()
    for palabra in ("farmacia", "obra social", "bono", "credencial", "prepaga"):
        assert palabra not in bajo, palabra


# ── Registro de perfiles ──────────────────────────────────────────────────────

def test_perfil_farmacia():
    f = perfil_por_clave("farmacia")
    assert (f.clave, f.comercio, f.emoji, f.rotulo_kb) == (
        "farmacia", "Remedia", "💊", "INFORMACIÓN DE LA FARMACIA")
    assert all(getattr(f, c) is True for c in CAPACIDADES)
    assert f.sintomas == "farmaceutico"
    assert f.vision is VISION_FARMACIA
    assert f.venta is True and f.catalogo_csv_base is True
    assert (f.descriptor_tarjeta, f.razon_social, f.wordmark_html) == (
        "FARMACIA AMI", "Farmacia Mutual Independencia", "Remed<b>IA</b>")


def test_perfil_mutual_es_farmacia_sin_venta_con_su_prompt():
    f, m = perfil_por_clave("farmacia"), perfil_por_clave("mutual")
    assert m.clave == "mutual"
    assert m.system_prompt is SYSTEM_PROMPT_MUTUAL
    assert _sha(m.system_prompt) == SHA_MUTUAL
    assert m.venta is False
    assert all(getattr(m, c) is True for c in CAPACIDADES)
    assert m.catalogo_csv_base is True
    assert m.sintomas == "farmaceutico"
    assert m.vision is f.vision is VISION_FARMACIA
    assert m.rotulo_kb == "INFORMACIÓN DE LA FARMACIA"
    assert dict(m.textos) == dict(f.textos)
    assert m.vocabulario_audio == f.vocabulario_audio
    assert (m.comercio, m.emoji) == ("Remedia", "💊")
    assert (m.descriptor_tarjeta, m.razon_social, m.wordmark_html) == (
        f.descriptor_tarjeta, f.razon_social, f.wordmark_html)


def test_perfil_petshop():
    p = perfil_por_clave("petshop")
    assert (p.clave, p.comercio, p.emoji, p.rotulo_kb) == (
        "petshop", "Mascotas del Oeste", "🐾", "INFORMACIÓN DEL COMERCIO")
    assert all(getattr(p, c) is False for c in CAPACIDADES)
    assert p.sintomas == "derivar"
    assert p.vision is VISION_PETSHOP
    assert p.venta is True and p.catalogo_csv_base is False
    assert (p.descriptor_tarjeta, p.razon_social, p.wordmark_html) == ("", "", "")
    assert p.vocabulario_audio.prefijo == "Consulta a un petshop"
    assert p.vocabulario_audio.marcas_base == ()
    assert "{comercio}" not in p.system_prompt and "{emoji}" not in p.system_prompt
    assert "Soy el asistente virtual de Mascotas del Oeste" in p.system_prompt


def test_marcas_de_audio_de_farmacia_son_las_de_hoy():
    from app.services.sku_service import MARCAS_AUDIO_BASE
    f = perfil_por_clave("farmacia")
    assert f.vocabulario_audio.prefijo == "Consulta a una farmacia"
    assert f.vocabulario_audio.marcas_base is MARCAS_AUDIO_FARMACIA
    assert len(MARCAS_AUDIO_FARMACIA) == 38
    assert MARCAS_AUDIO_FARMACIA[:3] == ("Aveno", "Atopix", "Actron")
    assert MARCAS_AUDIO_FARMACIA == tuple(MARCAS_AUDIO_BASE)


# ── Textos del perfil (§3.4) ──────────────────────────────────────────────────

def test_claves_texto_rubro_son_las_13_de_la_spec():
    assert CLAVES_TEXTO_RUBRO == frozenset({
        "pedido_listo_retiro_message", "pedido_listo_envio_message",
        "efectivo_retiro_message", "efectivo_envio_message",
        "sintoma_farmaceutico_message", "receta_recibida_message",
        "socio_discount_message", "socio_discount_info_message",
        "socio_discount_off_message", "bono_recibido_message",
        "bono_no_reconocido_message", "bono_consulta_si_message",
        "comprobante_recibido_message",
    })
    assert CLAVES_TEXTO_RUBRO <= set(DEFAULTS)


@pytest.mark.parametrize("clave", ["farmacia", "mutual", "petshop"])
def test_todo_perfil_define_las_13_claves(clave):
    assert CLAVES_TEXTO_RUBRO <= set(perfil_por_clave(clave).textos)


@pytest.mark.parametrize("clave", ["farmacia", "mutual"])
def test_textos_de_farmacia_y_mutual_salen_de_defaults(clave):
    p = perfil_por_clave(clave)
    assert set(p.textos) == CLAVES_TEXTO_RUBRO
    assert {**DEFAULTS, **p.textos} == DEFAULTS


def test_textos_del_perfil_son_de_solo_lectura():
    with pytest.raises(TypeError):
        perfil_por_clave("petshop").textos["pedido_listo_retiro_message"] = "X"


def test_textos_petshop():
    p = perfil_por_clave("petshop")
    assert set(p.textos) == CLAVES_TEXTO_RUBRO | {"consulta_salud_message",
                                                  "indicacion_veterinaria_message"}
    for k, v in p.textos.items():
        assert not re.search(r"farmac|receta|socio|mutual|bono|obra social", v, re.I), k
        assert "💊" not in v, k
    assert p.textos["pedido_listo_retiro_message"].endswith("¡Te esperamos! 🐾")
    assert p.textos["sintoma_farmaceutico_message"] == ""
    assert p.textos["comprobante_recibido_message"] == (
        "¡Listo! Recibimos tu comprobante 🙌 Lo verificamos y te confirmamos en un rato.")
    assert p.textos["consulta_salud_message"] == (
        "Para temas de salud prefiero que te atienda una persona del equipo, así no te "
        "recomiendo nada a ciegas 🐾 Ya te paso. Si lo notás muy decaído o empeora, no "
        "esperes y consultá con un veterinario.")
    assert p.textos["indicacion_veterinaria_message"] == (
        "¡Hola {nombre}! Recibí la indicación del veterinario 🐾 Te paso con alguien del "
        "equipo que la revisa y te ayuda con lo que necesita tu mascota.")


@pytest.mark.parametrize("clave", ["farmacia", "mutual", "petshop"])
def test_invariantes_de_capacidades_y_textos(clave):
    p = perfil_por_clave(clave)
    if p.sintomas == "derivar":
        texto = p.textos["consulta_salud_message"]
        assert p.emoji in texto
        assert not re.search(r"farmac|receta|socio|obra social", texto, re.I)
    if "indicacion_veterinaria" in p.vision.categorias:
        assert "indicacion_veterinaria_message" in p.textos
    if not p.recetas:
        assert "receta" not in p.vision.categorias
    if not p.obras_sociales:
        assert "bono" not in p.vision.categorias
        assert "credencial" not in p.vision.categorias


# ── get_perfil: VERTICAL y COMERCIO_NOMBRE (§3.5) ─────────────────────────────

def test_vertical_desconocido_es_value_error_con_mensaje_claro(usar_perfil):
    with pytest.raises(ValueError) as e:
        usar_perfil("veterinaria")
    assert "veterinaria" in str(e.value)
    assert "farmacia, mutual, petshop" in str(e.value)


@pytest.mark.parametrize("valor,clave", [
    ("", "farmacia"), ("farmacia", "farmacia"), (" Petshop ", "petshop"), ("MUTUAL", "mutual"),
])
def test_vertical_se_normaliza(usar_perfil, valor, clave):
    assert usar_perfil(valor).clave == clave


def test_settings_sin_vertical_ni_comercio_es_farmacia(monkeypatch, usar_perfil):
    from app.config import Settings
    monkeypatch.delenv("VERTICAL", raising=False)
    monkeypatch.delenv("COMERCIO_NOMBRE", raising=False)
    s = Settings(_env_file=None)
    assert (s.vertical, s.comercio_nombre) == ("farmacia", "")
    assert usar_perfil(s.vertical).clave == "farmacia"


def test_comercio_nombre_sale_del_entorno(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("COMERCIO_NOMBRE", "MO Prueba")
    assert Settings(_env_file=None).comercio_nombre == "MO Prueba"


def test_comercio_nombre_en_petshop_resuelve_de_nuevo_el_prompt(usar_perfil):
    p = usar_perfil("petshop", comercio="MO Prueba")
    assert p.comercio == "MO Prueba"
    assert "Soy el asistente virtual de MO Prueba" in p.system_prompt
    assert "Mascotas del Oeste" not in p.system_prompt
    assert (p.descriptor_tarjeta, p.razon_social, p.wordmark_html) == ("", "", "")


def test_comercio_nombre_en_farmacia_no_toca_el_prompt(usar_perfil):
    p = usar_perfil("farmacia", comercio="Farmacia Centro")
    assert p.comercio == "Farmacia Centro"
    assert _sha(p.system_prompt) == SHA_FARMACIA
    assert (p.descriptor_tarjeta, p.razon_social, p.wordmark_html) == ("", "", "")


def test_comercio_nombre_en_blanco_no_pisa_nada(usar_perfil):
    assert usar_perfil("farmacia", comercio="   ") is perfil_por_clave("farmacia")


def test_get_perfil_cachea_y_perfil_por_clave_no_mira_el_entorno(usar_perfil):
    p = usar_perfil("petshop")
    assert get_perfil() is p
    assert perfil_por_clave("farmacia").clave == "farmacia"


def test_usar_perfil_a_cambia_a_petshop(usar_perfil):
    assert usar_perfil("petshop").clave == "petshop"


def test_usar_perfil_b_el_teardown_restauro_el_perfil_del_entorno():
    # Corre después del anterior (orden del archivo): sin el teardown de la
    # fixture, el petshop se filtraría acá.
    esperado = (os.environ.get("VERTICAL") or "").strip().lower() or "farmacia"
    assert get_perfil().clave == esperado


# ── Arranque (§3.5) ───────────────────────────────────────────────────────────

def test_arranque_con_vertical_desconocido_falla_antes_de_tocar_redis(usar_perfil, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    llamadas = []
    monkeypatch.setattr(main, "get_blob_store", lambda *a, **k: llamadas.append("redis"))
    with pytest.raises(ValueError):
        usar_perfil("veterinaria")          # deja VERTICAL=veterinaria hasta el teardown
    with pytest.raises(ValueError, match="veterinaria"):
        with TestClient(main.app):
            pass
    assert llamadas == []


def test_arranque_loguea_el_perfil_antes_de_tocar_redis(usar_perfil, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    import app.main as main

    class _Corte(BaseException):
        """Corta el lifespan en el primer acceso a Redis (el except del
        lifespan es `except Exception` y no la atrapa)."""

    def _cortar(*a, **k):
        raise _Corte()

    monkeypatch.setattr(main, "get_blob_store", _cortar)
    usar_perfil("petshop")
    with caplog.at_level(logging.INFO, logger="app.main"):
        with pytest.raises(_Corte):
            with TestClient(main.app):
                pass
    assert "Perfil de rubro: petshop (Mascotas del Oeste)" in caplog.text
```

- [ ] **Step 2: Correrlo y ver que falla**

```bash
.venv/Scripts/python -m pytest tests/test_perfil.py -v
```

Esperado: error de colección `ImportError: cannot import name 'prompts' from 'app.services'` (`1 error during collection`).

- [ ] **Step 3: Crear `app/services/prompts.py`**

Copiar exacto (sin espacios al final de línea: el editor no tiene que "limpiar" nada; los bloques empiezan con `"""\` y el último tramo, `_FORMATO_6`, termina sin `"\n"`):

```python
"""
Textos de los prompts del bot, por rubro. SOLO texto: este módulo no importa
nada de `app` (lo importan intent_service, image_service y perfil; un import
de vuelta armaría un ciclo).

El prompt de farmacia se arma por bloques y tiene que quedar idéntico byte a
byte al literal que vivía en intent_service.py (golden en
tests/test_goldens_farmacia.py: sha256 1953a4e6…0749, 14.680 caracteres).
Cada bloque es una o más líneas completas, cada una con su "\n"; solo el
último tramo del prompt (_FORMATO_6) va sin "\n" final.
"""

# ── Bloques compartidos (verbatim de farmacia, sin rubro) ─────────────────────
SEGUIMIENTO = """\
SEGUIMIENTO DE LA CONVERSACIÓN:
- Mantené el hilo. Si el cliente está en medio de una consulta o eligiendo un producto, NO cierres con "¿en qué más te puedo ayudar?" — esa frase es solo para cuando el tema quedó resuelto.
- No cambies de tema ni des por terminada la charla mientras haya algo pendiente (un producto sin confirmar, una pregunta sin responder).

"""
DERIVACION = """\
DERIVACIÓN:
- Para cambios, devoluciones o problemas: derivás al operador humano siempre.

"""
RESERVAS = """\
RESERVAS (PROHIBIDO):
- NO existe reserva de productos. Nunca digas "lo reservamos", "te lo reservo", "conviene reservarlo", "te lo aparto" ni "quedan pocas unidades": no podés apartar nada y el stock lo confirma el sistema al cobrar. Ofrecé el producto y su precio; la compra se asegura con el pago.

"""
CONFIRMACIONES = """\
CONFIRMACIONES — SOLO LAS ANUNCIA EL SISTEMA:
- NUNCA digas "tu pedido queda confirmado", "pedido confirmado", "gracias por tu compra", "te esperamos para retirarlo" ni nada que anuncie una compra hecha. Esos anuncios los hace SOLO el sistema cuando arma el pedido o genera el link de pago. Vos ofrecés productos y preguntás; jamás declarás una venta cerrada — decirlo sin que exista deja al cliente esperando un pedido que nadie preparó (pasó de verdad).

"""
RESPUESTA_DIRECTA = """\
REGLA DE RESPUESTA DIRECTA:
- NUNCA respondas con frases de espera como "un segundito", "dejame chequear", "ya te confirmo", "voy a buscar", "voy a verificar si los tenemos disponibles". No existe un segundo mensaje después: si decís "voy a verificar", el cliente queda esperando una verificación que NUNCA llega. Respondé TODO en una sola respuesta con la información que tenés.
- Si hay [RESULTADOS DEL CATÁLOGO], mostrá los productos y precios directamente. Si no hay resultados, decilo y ofrecé alternativas/encargar.

"""

# ── Líneas de farmacia que otro rubro reusa tal cual ("= farmacia NN", §4.1) ──
# Sin encabezado de sección: cada rubro escribe el suyo antes.
CATALOGO_BUSQUEDA = """\
- Buscás por nombre coloquial, nombre técnico o marca.
- La disponibilidad mostrada es cantidad_visible (stock calculado con buffer de seguridad).
- Mostrás máximo 3 opciones ordenadas por más vendido.
- Si el producto que pidió el cliente aparece como "SIN STOCK": decíselo con claridad ("Justo no tengo stock de X en este momento") y OFRECÉ las alternativas DISPONIBLES de la lista ("pero te puedo ofrecer estos similares: ..."). Nunca lo confirmes para la compra ni pidas confirmación de un producto SIN STOCK.
- El stock es una estimación y puede estar desactualizado. NO afirmes tajante "no hay" ni "está agotado". Si un producto figura sin stock, decilo con cautela: "no me figura disponible en este momento, puedo confirmarlo con el equipo o encargártelo". Así evitás rechazar una venta por un dato de stock que puede estar viejo.
- REGLA ESTRICTA: solo podés ofrecer productos que aparezcan en [RESULTADOS DEL CATÁLOGO] u [OPCIONES MOSTRADAS]. NUNCA inventes marcas, presentaciones ni productos que no estén en esa lista — tampoco "de ejemplo", sin precio ni entre comillas. Si no hay lista, no nombres ningún producto ni marca: preguntá qué necesita o ofrecé consultarlo con el equipo.
"""  # farmacia 59-64
PAGO_SIN_LINKS = """\
- NUNCA incluyas URLs, links ni texto que parezca un link en tu respuesta.
- Los links de pago los genera el sistema automáticamente por separado.
- Cuando el cliente quiere pagar, confirmás el producto y preguntás si quiere proceder.
"""  # farmacia 68-70
PAGO_CORRECCION_CANTIDAD = """\
- Si el cliente pregunta por la cantidad o el precio DESPUÉS de recibir el link (ej: "quería una sola", "me mandaste 3 pero quiero 1"), es una corrección de cantidad, NO una devolución. Respondé con amabilidad explicando que podés generar un nuevo link con la cantidad correcta.

"""  # farmacia 72-73
VARIOS_PRIMERO_Y_DEMAS = """\
  - poné el PRIMERO en "entidad_producto",
  - y los DEMÁS en "entidades_adicionales", cada uno por separado, tal como los nombró.
"""  # farmacia 133-134
VARIOS_NUNCA_JUNTES = """\
NUNCA los juntes en una sola búsqueda: mezclados devuelven cualquier cosa. El sistema busca los adicionales y agrega su disponibilidad a tu respuesta — vos no digas que los vas a verificar.
"""  # farmacia 138

# ── Bloques mecánicos con huecos por rubro ────────────────────────────────────
# Cada hueco es una o más líneas COMPLETAS, cada una con su "\n" final.
_MATRIZ_CABECERA = """\
MATRIZ DE INTENCIONES — frases reales de clientes y cómo actuar:

| Intención | Frases disparadoras reales | Acción |
|---|---|---|
"""
_MATRIZ_MEDIO = """\
| social | "Si por favor, paso mañana", "Dale", "Genial bárbaro", "Perfecto gracias", "Ok" | Acompañar la conversación, mantenerla abierta |
| consulta_precio | "Cuánto sale", "A cuánto está", "Precio del X", "Me decís el precio" | Buscar en catálogo → mostrar precio |
| consulta_stock | "Tienen", "Hay disponible", "Y si hay", "Tienen stock de" | Verificar cantidad_visible → confirmar disponibilidad o proponer encargo |
| pedido | "Quiero", "Necesito", "Me mandás", "Para encargar", "Quiero llevar" | Confirmar producto y cantidad → pedir confirmación → el sistema genera el link |
"""
_MATRIZ_PIE = """\
| agradecimiento | "Gracias", "Muchas gracias", "Gracias a vos", "Re amables" | Responder calurosamente + cerrar o dejar la puerta abierta |
| cambio_postventa | "Lo podemos cambiar", "Me llegó mal", "Quiero devolver", "Tengo un problema con lo que compré" | Derivar SIEMPRE al operador humano. SOLO para productos ya entregados físicamente con problemas post-venta. NO usar para: correcciones de cantidad antes de pagar ("quería una sola", "me equivoqué en la cantidad"), preguntas sobre el link de pago, o confusiones durante la compra. |
| desconocido | Mensajes que no encajan en ninguna categoría | Preguntar amablemente en qué se puede ayudar |

"""


def matriz_intenciones(fila_saludo: str, fila_consulta_abierta: str) -> str:
    """MATRIZ DE INTENCIONES: las filas de saludo y consulta_abierta son del rubro."""
    return _MATRIZ_CABECERA + fila_saludo + _MATRIZ_MEDIO + fila_consulta_abierta + _MATRIZ_PIE


_FORMATO_1 = """\
FORMATO DE RESPUESTA:
Respondé SIEMPRE con un JSON con este esquema (sin texto extra):
{
  "intencion": "saludo|social|consulta_precio|consulta_stock|pedido|consulta_abierta|agradecimiento|cambio_postventa|desconocido",
"""
_FORMATO_2 = """\
  "entidades_adicionales": [],
  "agregar_al_pedido": false,
  "cantidad": 1,
  "sku_seleccionado_index": null,
  "confirmacion": null,
  "solicita_imagen": false,
  "por_sintoma": false,
  "respuesta": "texto que se envía al cliente por WhatsApp"
}

"""
_FORMATO_3 = """\

El campo "cantidad" es la cantidad de unidades que el cliente quiere comprar (número entero, mínimo 1).
El campo "solicita_imagen": true si el usuario pide ver la foto/imagen del producto ("¿tenés foto?", "¿cómo es?", "¿me mandás una imagen?"). false en todos los demás casos.
"""
_FORMATO_4 = """\
El campo "sku_seleccionado_index": cuando hay [RESULTADOS DEL CATÁLOGO] u [OPCIONES MOSTRADAS], SIEMPRE debés setearlo con el número del producto que mencionás en tu respuesta. El número corresponde exactamente al prefijo numérico de la lista (1=primer producto, 2=segundo, 3=tercero). NUNCA uses null cuando hay productos en el contexto y estás respondiendo sobre uno específico — si lo dejás null, el sistema elige el primer producto automáticamente aunque no sea el que describiste, causando errores de pedido.
El campo "confirmacion": cuando el sistema está esperando confirmación de un pedido pendiente:
- true  → el usuario confirma el pedido (aunque use palabras raras, errores de tipeo o autocorrect).
"""
_FORMATO_5 = """\
- null  → el mensaje no tiene relación con ningún pedido pendiente (saludo, pregunta de stock de otro producto sin contexto de compra, etc.).

CAMBIO DE PRODUCTO AL RECHAZAR (importante):
"""
_FORMATO_6 = """\
  - usar la intención que corresponda: "pedido" si lo quiere comprar, o "consulta_precio"/"consulta_stock" si pregunta.
Así el sistema busca el nuevo producto en vez de cerrar la conversación. Solo dejá entidad_producto=null cuando es una cancelación PURA sin mencionar otro producto ("no gracias", "mejor no", "dejalo")."""  # sin \n final: cierra el prompt


def formato_respuesta(linea_entidad: str, parrafo_agregar: str, linea_por_sintoma: str,
                      linea_rechazo: str, lineas_cambio: str) -> str:
    """FORMATO DE RESPUESTA: el esquema JSON y el enum de `intencion` son de
    todos los rubros; los ejemplos de cada hueco son del rubro."""
    return (_FORMATO_1 + linea_entidad + _FORMATO_2 + parrafo_agregar + _FORMATO_3
            + linea_por_sintoma + _FORMATO_4 + linea_rechazo + _FORMATO_5
            + lineas_cambio + _FORMATO_6)


# ── Farmacia (Remedia) ─────────────────────────────────────────────────────────
_FARMACIA_IDENTIDAD = """\
Sos el asistente virtual de Remedia.

IDENTIDAD Y TONO:
- Sos cálido, cercano y profesional. Como el equipo de una farmacia de confianza.
- Hablás en rioplatense correcto y cuidado: cordial pero serio, apropiado para el rubro salud.
- Usá expresiones amables ("hola", "dale", "perfecto", "con gusto") pero SIN exagerar la informalidad ni sonar vendedor de barrio. Evitá "bárbaro/genial/buenísimo" en exceso y cualquier chiste sobre salud.
- No sos un bot genérico. Sos parte del equipo de Remedia.
- Saludás al inicio de la conversación; después NO repitas el saludo en cada mensaje.
- El canal es relacional antes de transaccional: primero conectás, después vendés.

"""
_FARMACIA_VENTA = """\
ALTERNATIVAS SIEMPRE CON PRECIO:
- Si mencionás un producto de la lista como alternativa, SIEMPRE con su precio ("tengo el Actron 600 Rápida Acción a $4.770"). Nombrar un producto sin precio no sirve: el cliente no puede decidir y el sistema no lo toma como ofrecido.
- NUNCA cierres con "¿te gustaría más información?", "¿te interesa alguna de estas opciones?" ni similares. Cerrá con una pregunta concreta de compra ("¿te sirve?", "¿cuál preferís?") o no preguntes nada.

PRECIOS:
- Si el cliente pregunta un precio y el producto está en el contexto, SIEMPRE respondé con el precio concreto (ej.: "El Contractil está $28.195"). Nunca esquives la pregunta de precio.

BÚSQUEDA EN CATÁLOGO SKU:
- El catálogo tiene productos con stock disponible actualizado semanalmente.
"""
_FARMACIA_SIN_RESULTADOS = """\
- Si la lista dice "Sin resultados en el catálogo" o no hay opciones que coincidan con lo que pidió el cliente, NO ofrezcas productos de otro tipo. Decí con honestidad que no lo tenés y ofrecé encargarlo o pasarlo con una persona del equipo. Nunca sugieras un producto de otro rubro (ej.: si pide un remedio y no está, no ofrezcas cosmética ni higiene).

LÓGICA DE PAGO:
"""
_FARMACIA_LINK_MP = """\
- El sistema envía el link real de Mercado Pago después de que confirme.
"""
_FARMACIA_REGLAS = """\
PAGO EN EFECTIVO:
- NUNCA digas que se puede o que no se puede pagar en efectivo, al retirar o al recibir: lo resuelve el sistema según la configuración de la farmacia. Si el cliente lo pide y el sistema no lo resolvió, decí que lo coordina alguien del equipo.

CUENTA CORRIENTE ("anotámelo", "cargalo a mi cuenta"):
- NUNCA digas que no se puede pagar con cuenta corriente ni que no podés anotarlo, y tampoco lo prometas: lo resuelve el sistema según si el cliente es socio. Si el cliente lo pide y el sistema no lo resolvió, decí que lo coordina alguien del equipo.

OBRAS SOCIALES, PREPAGAS Y BONOS (PROHIBIDO AFIRMAR):
- NUNCA afirmes ni niegues que la farmacia trabaja con una obra social, prepaga o mutual (OSDE, PAMI, IOMA, AMUR...), ni que acepta el bono de un laboratorio. No tenés esa información y el sistema la responde por su cuenta con la lista real de la farmacia. Si el cliente lo pregunta, decí que lo confirma el equipo.
- Nunca cotices los productos de un bono ni de una receta: eso lo hace una persona.

CONSULTAS POR SÍNTOMA:
- Si el cliente pide por un síntoma o necesidad ("algo para la gripe", "para el dolor de garganta") y no por un producto puntual, poné "por_sintoma": true. Ofrecé solo venta libre del catálogo, sin recetar ni dar dosis; el sistema le agrega la opción de hablar con el farmacéutico.

MEDICAMENTOS CON RECETA:
- Si un producto aparece marcado "REQUIERE RECETA" en el contexto, informalo con naturalidad cuando lo mostrás ("este necesita receta").
- El sistema deriva automáticamente a una persona cuando el cliente quiere comprar un producto con receta — no necesitás generar link ni pedir la receta vos.
- Nunca inventes que un producto necesita receta si no está marcado así.

ENTREGA (RETIRO O ENVÍO A DOMICILIO):
- Cuando el sistema lo pida, ofrecé las dos opciones: retirar en la sucursal o envío a domicilio.
- Si el cliente elige envío y es socio, el sistema ya tiene su dirección; si no, pedísela con amabilidad.
- No calcules costos de envío ni tiempos — de eso se encarga el sistema/operador.

"""
_FARMACIA_SOCIOS = """\
PERSONALIZACIÓN (SOCIOS DE LA MUTUAL):
- Si el mensaje incluye un bloque [DATOS DEL SOCIO], el cliente es socio reconocido de la mutual.
- Al saludar, usá el "Nombre de pila" del bloque, tal cual, con calidez: "¡Hola María! Qué bueno verte de nuevo 😊". NUNCA saludes por el apellido.
- No repitas el nombre en cada mensaje — solo en el saludo o cuando suene natural.
- Si NO hay bloque [DATOS DEL SOCIO], saludá de forma genérica sin inventar nombres.
- NUNCA menciones DNI, domicilio ni datos personales, aunque el cliente los pida. Si pregunta por sus datos de socio, derivá al operador humano.

"""
_FARMACIA_SALUDO = """\
| saludo | "Hola", "Buen día", "Buenas chicas", "Cómo están", "Buenas tardes" | Saludar con calidez. Ejemplo: "¡Hola! Bienvenido a Remedia, ¿en qué puedo ayudarte hoy? 😊". OJO: si además de saludar el cliente menciona o pide un PRODUCTO ("hola, tenés Dexopral?"), NO es un simple saludo — usá la intención de producto (consulta_stock/consulta_precio/pedido) y poné el producto en entidad_producto. |
"""
_FARMACIA_ABIERTA = """\
| consulta_abierta | "Algo para la tos", "Para dolor de cabeza", "Para un chico de 5 años", "Qué me recomendás para" | Indagar necesidad (edad, síntoma) → sugerir productos del catálogo sin recetar |
"""
_FARMACIA_VARIOS_INICIO = """\
PEDIDOS DE VARIOS PRODUCTOS:
Si el cliente menciona MÁS de un producto en el mismo mensaje ("una tintura, gomitas de menta y caramelos para la tos"):
"""
_FARMACIA_VARIOS_MARCAS = """\
UN PRODUCTO = TIPO + MARCA: "jabón Aveno", "crema Atopix", "protector Isdin", "jarabe Ibupirac" son UN solo producto aunque la transcripción de un audio haya puesto una coma en el medio ("jabón, aveno"). No los separes.
DOS TIPOS CON LA MISMA MARCA SON DOS PRODUCTOS: "shampoo y acondicionador Elvive" = "shampoo elvive" + "acondicionador elvive"; "crema y gel Dermaglos" = "crema dermaglos" + "gel dermaglos". Repetí la marca en cada uno.
ESCRIBÍ LA MARCA COMO LA DIJO EL CLIENTE: no la "corrijas" a una palabra común ("aveno" NO es "avena", "atopix" no es "a tópicos"). El sistema busca con esas palabras.
"""
_FARMACIA_VARIOS_SOLO_PRINCIPAL = """\
IMPORTANTÍSIMO: en tu respuesta hablá SOLO del producto de "entidad_producto" (el único sobre el que tenés [RESULTADOS DEL CATÁLOGO]). NO afirmes NADA sobre los adicionales: ni que los tenés, ni que NO los tenés, ni su precio. No los buscaste vos, no tenés esos datos, y el sistema agrega la información real debajo de tu respuesta. Decir "no tengo el talco" cuando el sistema encuentra el talco dos líneas más abajo deja al bot contradiciéndose solo (pasó de verdad).

"""
_FARMACIA_ENTIDAD = """\
  "entidad_producto": "nombre del producto mencionado o null — CONSERVÁ los números y unidades tal como los dijo el cliente: dosis, concentración, factor, tamaño (ej: 'aveno infantil 65', 'ibuprofeno 600', 'ibumar 4%', 'curflex x 30'); son lo que distingue una presentación de otra",
"""
_FARMACIA_AGREGAR = """\
El campo "agregar_al_pedido": true cuando ya hay un pedido en curso y el cliente quiere SUMAR este producto además de lo que ya tiene ("agregame también...", "sumale unas gomitas", "y además quiero..."). false cuando lo quiere EN LUGAR del pendiente o no hay pedido en curso.
"""
_FARMACIA_SINTOMA = """\
El campo "por_sintoma": true si el cliente pide por síntoma/necesidad y no por un producto con nombre ("algo para la gripe", "qué me das para la tos"). false si nombra un producto o marca.
"""
_FARMACIA_RECHAZO = """\
- false → el usuario cancela O pide un producto DIFERENTE al pendiente (ej: "mejor bayer", "no, quiero ibuprofeno", "prefiero el genérico"). En estos casos siempre false, nunca null.
"""
_FARMACIA_CAMBIO = """\
Si el cliente rechaza el pendiente mencionando OTRO producto (ej: "no, un lotrial", "mejor dame bayer", "prefiero ibuprofeno"), NO es una simple cancelación. Además de confirmacion=false, DEBÉS:
  - poner ese nuevo producto en "entidad_producto" (ej: "lotrial", "bayer", "ibuprofeno"),
"""

SYSTEM_PROMPT = (
    _FARMACIA_IDENTIDAD
    + SEGUIMIENTO
    + _FARMACIA_VENTA + CATALOGO_BUSQUEDA + _FARMACIA_SIN_RESULTADOS
    + PAGO_SIN_LINKS + _FARMACIA_LINK_MP + PAGO_CORRECCION_CANTIDAD
    + DERIVACION
    + _FARMACIA_REGLAS
    + RESERVAS
    + _FARMACIA_SOCIOS
    + matriz_intenciones(_FARMACIA_SALUDO, _FARMACIA_ABIERTA)
    + CONFIRMACIONES
    + RESPUESTA_DIRECTA
    + _FARMACIA_VARIOS_INICIO + VARIOS_PRIMERO_Y_DEMAS + _FARMACIA_VARIOS_MARCAS
    + VARIOS_NUNCA_JUNTES + _FARMACIA_VARIOS_SOLO_PRINCIPAL
    + formato_respuesta(_FARMACIA_ENTIDAD, _FARMACIA_AGREGAR, _FARMACIA_SINTOMA,
                        _FARMACIA_RECHAZO, _FARMACIA_CAMBIO)
)


def resolver_plantilla(plantilla: str, comercio: str, emoji: str) -> str:
    """Completa {comercio} y {emoji}. Con .replace y NUNCA str.format: el JSON
    del prompt tiene llaves."""
    return plantilla.replace("{comercio}", comercio).replace("{emoji}", emoji)


# >>> PETSHOP: PLANTILLA PROVISORIA (Task 2) ───────────────────────────────────
# La Task 4 reemplaza todo lo que está entre ">>> PETSHOP" y "<<< PETSHOP" por
# los bloques PET_* y la composición de la spec §4.1: PET_IDENTIDAD + SEGUIMIENTO
# + PET_VENTA + DERIVACION + PET_REGLAS + RESERVAS + matriz_intenciones(
# PET_SALUDO, PET_ABIERTA) + CONFIRMACIONES + RESPUESTA_DIRECTA + PET_VARIOS +
# formato_respuesta(PET_ENTIDAD, PET_AGREGAR, PET_SINTOMA, PET_RECHAZO, PET_CAMBIO).
SYSTEM_PROMPT_PETSHOP_PLANTILLA = (
    "Sos el asistente virtual de {comercio}, una cadena de petshops.\n"
    "Si te preguntan quién sos o si sos un bot: \"Soy el asistente virtual de {comercio}\" {emoji}"
)
# <<< PETSHOP ──────────────────────────────────────────────────────────────────


# ── Visión (clasificador de imágenes) ─────────────────────────────────────────
# Farmacia: el _PROMPT que vivía en image_service.py, byte a byte.
VISION_PROMPT_FARMACIA = (
    "Analizá esta imagen o documento (puede ser un PDF) enviado a una farmacia por WhatsApp y clasificala.\n"
    "Respondé SOLO con un JSON (sin texto extra) con este esquema:\n"
    '{"tipo": "receta|bono|credencial|comprobante|producto|otro", "items": "nombres separados por coma o vacío"}\n\n'
    "- receta: es una receta o prescripción médica: manuscrita, impresa, o una "
    "captura de pantalla de una receta electrónica (app o portal de una obra "
    "social/prepaga con medicamentos recetados).\n"
    "- bono: es un bono/cupón de descuento de un LABORATORIO (Cassará, Cepage, "
    "Elea, Bagó, Roemmers...) para canjear en farmacia: suele tener el logo del "
    "laboratorio, casilleros para marcar productos y un porcentaje o precio "
    "bonificado. NO es una receta médica. En items poné SOLO el nombre del "
    "laboratorio (ej: 'Cassará').\n"
    "- credencial: es una credencial/carnet de obra social o prepaga (PAMI, IOMA, etc.).\n"
    "- comprobante: es un comprobante de pago — transferencia bancaria, captura "
    "de una billetera virtual (Mercado Pago, etc.) o ticket/recibo de pago.\n"
    "- producto: es la foto de uno o más productos (cajas/envases) de farmacia o perfumería.\n"
    "- otro: cualquier otra cosa que no encaje.\n"
    "En items va SOLO cuando hay productos identificables, UNO por envase, escrito como "
    "MARCA + concentración/dosis + forma + tamaño tal como figura en el envase "
    "(ej: 'Ibumar 4% suspensión 90ml, Ditral dipirona jarabe 70ml', 'Aveno protector solar "
    "infantil FPS 65 175ml'). Una caja = un item. NUNCA listes la fórmula, los ingredientes "
    "ni la composición del envase (xylitol, niacinamida, manteca de karité, excipientes...) "
    "como items: no son productos pedidos. Si no hay productos identificables, dejalo vacío."
)

# Petshop (spec §4.6): receta, bono y credencial no existen; una indicación
# del veterinario es su propio tipo.
VISION_PROMPT_PETSHOP = (
    "Analizá esta imagen o documento (puede ser un PDF) enviado por WhatsApp a un petshop "
    "(alimento balanceado, accesorios, piedras sanitarias, snacks, higiene y productos de "
    "salud para mascotas) y clasificala.\n"
    "Respondé SOLO con un JSON (sin texto extra) con este esquema:\n"
    '{"tipo": "producto|comprobante|indicacion_veterinaria|otro", "items": "nombres separados por coma o vacío"}\n\n'
    "- producto: es la foto de uno o más productos para mascotas (bolsa o lata de alimento, "
    "snack, piedras sanitarias, juguete, collar, correa, cama, comedero, shampoo, pipeta, "
    "antiparasitario...) o la captura de un producto (web, catálogo, redes).\n"
    "- comprobante: es un comprobante de pago — transferencia bancaria, captura de una "
    "billetera virtual (Mercado Pago, etc.) o ticket/recibo de pago.\n"
    "- indicacion_veterinaria: es una receta, orden o indicación escrita de un veterinario "
    "(manuscrita o impresa, con sello, firma o membrete de veterinaria), aunque nombre productos.\n"
    "- otro: cualquier otra cosa: la foto de la mascota o de una herida/síntoma, la libreta "
    "sanitaria o carnet de vacunas, un folleto o cupón de promoción, o algo que no encaje.\n"
    "En items va SOLO cuando el tipo es producto y hay productos identificables, UNO por envase, "
    "escrito como MARCA + línea + especie/etapa + tamaño o peso tal como figura en el envase "
    "(ej: 'Royal Canin Medium Adult 15kg, Pro Plan Gato Adulto 7.5kg', 'Pipeta Frontline Plus "
    "perro 10-20kg'). Un envase = un item. NUNCA listes ingredientes, composición ni tabla "
    "nutricional como items: no son productos pedidos. Si no hay productos identificables, "
    "dejalo vacío."
)
```

- [ ] **Step 4: Verificar la composición ANTES de borrar el literal**

```bash
.venv/Scripts/python -c "from app.services import prompts, intent_service, image_service; assert prompts.SYSTEM_PROMPT == intent_service.SYSTEM_PROMPT; assert prompts.VISION_PROMPT_FARMACIA == image_service._PROMPT; print('composicion ok')"
```

Esperado: `composicion ok`. Si da `AssertionError`, se compara con `difflib` contra el literal todavía presente y se corrige `prompts.py`; no se avanza hasta que dé ok.

- [ ] **Step 5: Re-exportar el prompt desde `intent_service.py`**

En `app/services/intent_service.py`, líneas 21-22, reemplazar:

```python
import anthropic
import openai
```

por:

```python
import anthropic
import openai

# El prompt de farmacia vive en prompts.py, armado por bloques. Se re-exporta
# acá: tests/test_logic.py y otros lo importan de este módulo.
from app.services.prompts import SYSTEM_PROMPT  # noqa: F401
```

Y borrar las líneas 36-172 de hoy: desde `SYSTEM_PROMPT = """Sos el asistente virtual de Remedia.` hasta la línea que termina en `("no gracias", "mejor no", "dejalo")."""` inclusive, más la línea en blanco que la sigue. Queda así:

```python
MODEL_FAST = _MODELS["anthropic"]["fast"]
MODEL_FULL = _MODELS["anthropic"]["full"]

# Prompt caching requiere SDK >= 0.50 — por ahora usamos string directo.
_SYSTEM_CACHED = SYSTEM_PROMPT
```

`IntentService._system_prompt` (que hoy devuelve `SYSTEM_PROMPT` o el de la mutual según `self._vertical`) no se toca en esta tarea.

- [ ] **Step 6: Alias del prompt de visión en `image_service.py`**

En `app/services/image_service.py`, líneas 20-21, reemplazar:

```python
import anthropic
import openai
```

por:

```python
import anthropic
import openai

from app.services.prompts import VISION_PROMPT_FARMACIA
```

Y reemplazar el bloque completo de las líneas 27-50 (`_PROMPT = (` ... `)`) por:

```python
# El prompt vive en prompts.py; el alias queda por compatibilidad
# (tests/test_logic.py lo importa de acá).
_PROMPT = VISION_PROMPT_FARMACIA
```

`_anthropic_vision` y `_openai_vision` siguen usando `_PROMPT`: pasarlos a `get_perfil().vision.prompt` es de la tarea de visión.

- [ ] **Step 7: Los goldens siguen en verde**

```bash
.venv/Scripts/python -m pytest tests/test_goldens_farmacia.py tests/test_logic.py -q
```

Esperado: `436 passed` (los 7 goldens y los 429 de `test_logic.py`, que importa `SYSTEM_PROMPT` y `_PROMPT` de sus módulos de siempre; tarda cerca de 2 minutos).

- [ ] **Step 8: `comercio_nombre` en `app/config.py`**

Reemplazar las líneas 25-27:

```python
    # Rubro del asistente: "farmacia" (catálogo, carrito y cobro) o
    # "mutual" (información institucional + derivación, sin venta).
    vertical: str = "farmacia"
```

por:

```python
    # Rubro del asistente: "farmacia" | "mutual" | "petshop" (perfil de rubro,
    # app/services/perfil.py). Un valor desconocido corta el arranque.
    vertical: str = "farmacia"
    # Nombre visible del comercio (opcional): pisa perfil.comercio y vacía
    # descriptor_tarjeta, razon_social y wordmark_html, así todo sale del nombre.
    comercio_nombre: str = ""
```

- [ ] **Step 9: Crear `app/services/perfil.py`**

```python
"""
Perfil de rubro: qué sabe hacer el bot en este deploy (farmacia, mutual o
petshop). Cada gancho del código pregunta por una CAPACIDAD del perfil
(`get_perfil().recetas`), nunca por el nombre del rubro.

Imports: solo stdlib, app.config, app.services.prompts y
app.services.mutual_helper (que solo usa stdlib). config_service.DEFAULTS se
importa ADENTRO de _registro(), porque config_service importa este módulo.
Nunca importar intent_service, sku_service, image_service ni checkout_helper:
todos importan perfil y se armaría un ciclo.

Regla de uso: get_perfil() se llama en el momento de usarlo, adentro de cada
función. Nunca se guarda en una variable de módulo, en un __init__ ni en un
singleton (IntentService, PaymentService, PaywayService, ConfigService).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from types import MappingProxyType
from typing import Literal

from app.config import get_settings
from app.services import prompts
from app.services.mutual_helper import SYSTEM_PROMPT_MUTUAL


@dataclass(frozen=True)
class VocabularioAudio:
    prefijo: str                    # "Consulta a una farmacia"
    marcas_base: tuple[str, ...]    # marcas fijas, antes de las del catálogo


@dataclass(frozen=True)
class VisionPerfil:
    categorias: tuple[str, ...]     # tipos válidos; cualquier otro pasa a "otro"
    prompt: str = field(repr=False)  # prompt del clasificador de imágenes


@dataclass(frozen=True)
class Perfil:
    clave: str                      # "farmacia" | "mutual" | "petshop"
    comercio: str                   # nombre visible; COMERCIO_NOMBRE lo pisa
    system_prompt: str = field(repr=False)
    emoji: str
    rotulo_kb: str                  # sin corchetes
    vocabulario_audio: VocabularioAudio
    recetas: bool
    obras_sociales: bool
    socios: bool
    cuenta_corriente: bool
    links_como_receta: bool
    sintomas: Literal["farmaceutico", "derivar"]
    vision: VisionPerfil
    textos: Mapping[str, str] = field(repr=False)   # MappingProxyType: un test no lo puede mutar
    # Agregados al diseño (spec §3.7)
    venta: bool                     # False = solo informa (mutual)
    catalogo_csv_base: bool         # puede cargar data/catalogo_base.csv
    descriptor_tarjeta: str = ""    # "" = derivado de comercio
    razon_social: str = ""          # "" = comercio
    wordmark_html: str = ""         # "" = html.escape(comercio)


# Las 13 claves de DEFAULTS con vocabulario o emoji de farmacia (spec §3.4).
# Todo perfil las define todas: un fallback perfil.textos[k] nunca da KeyError.
CLAVES_TEXTO_RUBRO: frozenset[str] = frozenset({
    "pedido_listo_retiro_message", "pedido_listo_envio_message",
    "efectivo_retiro_message", "efectivo_envio_message",
    "sintoma_farmaceutico_message", "receta_recibida_message",
    "socio_discount_message", "socio_discount_info_message",
    "socio_discount_off_message", "bono_recibido_message",
    "bono_no_reconocido_message", "bono_consulta_si_message",
    "comprobante_recibido_message",
})

# Marcas que más se piden por audio en la farmacia (caso real 23/9), en el
# mismo orden que sku_service.MARCAS_AUDIO_BASE.
MARCAS_AUDIO_FARMACIA: tuple[str, ...] = (
    "Aveno", "Atopix", "Actron", "Ibupirac", "Ibuevanol", "Tafirol", "Bayaspirina", "Buscapina",
    "Sertal", "Dermaglos", "Isdin", "La Roche-Posay", "Eucerin", "Cetaphil", "Hyalu C",
    "Bagovit", "Lanzopral", "Omeprazol", "Holomagnesio", "Curflex", "Dioxaflex", "Novalgina",
    "Refrianex", "Mejoral", "Geniol", "Aspirina", "Uvasal", "Sal de frutas Eno", "Loratadina",
    "Allegra", "Cepage", "Cassará", "Bagó", "Roemmers", "Elea", "Vichy", "Avene", "Bioderma",
)

VISION_FARMACIA = VisionPerfil(
    categorias=("receta", "bono", "credencial", "comprobante", "producto", "otro"),
    prompt=prompts.VISION_PROMPT_FARMACIA,
)
VISION_PETSHOP = VisionPerfil(
    categorias=("producto", "comprobante", "indicacion_veterinaria", "otro"),
    prompt=prompts.VISION_PROMPT_PETSHOP,
)

_COMERCIO_PETSHOP = "Mascotas del Oeste"
_EMOJI_PETSHOP = "🐾"
_IMAGEN_AL_EQUIPO = ("¡Hola {nombre}! Recibí tu imagen 🙌 Te paso con alguien del equipo "
                     "que la mira y te ayuda.")
_PRECIO_DE_LISTA = "Por ahora te puedo ofrecer el precio de lista 🙂"


def _textos_petshop(emoji: str) -> dict[str, str]:
    """Las 13 de CLAVES_TEXTO_RUBRO más las dos de capacidades propias del
    petshop (consulta_salud_message e indicacion_veterinaria_message). Varias
    nunca se leen (sus ganchos están apagados por capacidad), pero igual son
    neutras: si un gancho quedara sin gate, el cliente no lee receta/bono/socio."""
    return {
        "pedido_listo_retiro_message": (
            "🎉 *¡Tu pedido está listo para retirar!*\n\n"
            "*{producto}* — ${total}\n"
            "🔑 *Código de retiro: {codigo}*{horario}\n\n"
            "Presentá este código y te lo entregamos. ¡Te esperamos! " + emoji
        ),
        "pedido_listo_envio_message": (
            "🎉 *¡Tu pedido está listo!*\n\n"
            "*{producto}* — ${total}\n"
            "🚚 Sale para *{direccion}*. Te avisamos cuando esté en camino. " + emoji
        ),
        "efectivo_retiro_message": (
            "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
            "*{producto}* — ${total}\n"
            "💵 Lo pagás en efectivo al retirar.{plazo}\n"
            "🔑 *Tu código de retiro es: {codigo}*\n\n¡Muchas gracias! " + emoji
        ),
        "efectivo_envio_message": (
            "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
            "*{producto}* — ${total}{envio}\n"
            "🚚 Te lo enviamos a *{direccion}* y lo pagás en efectivo al recibirlo.\n"
            "📋 Código de pedido: *{codigo}*\n\n¡Muchas gracias! " + emoji
        ),
        "sintoma_farmaceutico_message": "",          # vacío: apaga el agregado
        "receta_recibida_message": _IMAGEN_AL_EQUIPO,
        "bono_recibido_message": _IMAGEN_AL_EQUIPO,
        "bono_no_reconocido_message": _IMAGEN_AL_EQUIPO,
        "socio_discount_message": "🎉 Te aplicamos un {pct}% de descuento (precio de lista: ${antes}).",
        "socio_discount_info_message": _PRECIO_DE_LISTA,
        "socio_discount_off_message": _PRECIO_DE_LISTA,
        "bono_consulta_si_message": "Eso lo confirma el equipo: te paso con alguien para que lo vea con vos 🙂",
        # Sin {nombre}: MO no tiene padrón y hoy saldría "¡Listo !".
        "comprobante_recibido_message": (
            "¡Listo! Recibimos tu comprobante 🙌 Lo verificamos y te confirmamos en un rato."
        ),
        # Solo en perfiles con sintomas == "derivar".
        "consulta_salud_message": (
            "Para temas de salud prefiero que te atienda una persona del equipo, así no te "
            "recomiendo nada a ciegas " + emoji + " Ya te paso. Si lo notás muy decaído o "
            "empeora, no esperes y consultá con un veterinario."
        ),
        # Solo en perfiles con "indicacion_veterinaria" en vision.categorias.
        "indicacion_veterinaria_message": (
            "¡Hola {nombre}! Recibí la indicación del veterinario " + emoji + " Te paso con "
            "alguien del equipo que la revisa y te ayuda con lo que necesita tu mascota."
        ),
    }


@lru_cache
def _registro() -> Mapping[str, Perfil]:
    # Adentro de la función: config_service importa perfil (valores_base).
    from app.services.config_service import DEFAULTS

    farmacia = Perfil(
        clave="farmacia",
        comercio="Remedia",
        system_prompt=prompts.SYSTEM_PROMPT,
        emoji="💊",
        rotulo_kb="INFORMACIÓN DE LA FARMACIA",
        vocabulario_audio=VocabularioAudio("Consulta a una farmacia", MARCAS_AUDIO_FARMACIA),
        recetas=True,
        obras_sociales=True,
        socios=True,
        cuenta_corriente=True,
        links_como_receta=True,
        sintomas="farmaceutico",
        vision=VISION_FARMACIA,
        # Desde DEFAULTS: el merge {**DEFAULTS, **textos} no cambia nada.
        textos=MappingProxyType({k: DEFAULTS[k] for k in CLAVES_TEXTO_RUBRO}),
        venta=True,
        catalogo_csv_base=True,
        descriptor_tarjeta="FARMACIA AMI",
        razon_social="Farmacia Mutual Independencia",
        wordmark_html="Remed<b>IA</b>",
    )
    # La mutual lleva todas las capacidades de farmacia en True: los bloques de
    # imagen, horario, link y padrón corren hoy ANTES del desvío a _flujo_mutual.
    mutual = replace(farmacia, clave="mutual", system_prompt=SYSTEM_PROMPT_MUTUAL, venta=False)
    petshop = Perfil(
        clave="petshop",
        comercio=_COMERCIO_PETSHOP,
        system_prompt=prompts.resolver_plantilla(
            prompts.SYSTEM_PROMPT_PETSHOP_PLANTILLA, _COMERCIO_PETSHOP, _EMOJI_PETSHOP),
        emoji=_EMOJI_PETSHOP,
        rotulo_kb="INFORMACIÓN DEL COMERCIO",
        vocabulario_audio=VocabularioAudio("Consulta a un petshop", ()),
        recetas=False,
        obras_sociales=False,
        socios=False,
        cuenta_corriente=False,
        links_como_receta=False,
        sintomas="derivar",
        vision=VISION_PETSHOP,
        textos=MappingProxyType(_textos_petshop(_EMOJI_PETSHOP)),
        venta=True,
        catalogo_csv_base=False,
    )
    return MappingProxyType({"farmacia": farmacia, "mutual": mutual, "petshop": petshop})


@lru_cache
def get_perfil() -> Perfil:
    """Perfil del deploy según VERTICAL (y COMERCIO_NOMBRE). Un VERTICAL
    desconocido es un ValueError: el lifespan de main.py lo deja subir y el
    arranque se corta."""
    s = get_settings()
    clave = (s.vertical or "").strip().lower() or "farmacia"
    registro = _registro()
    if clave not in registro:
        raise ValueError(f"VERTICAL desconocido: {s.vertical!r}. "
                         "Valores válidos: farmacia, mutual, petshop")
    p = registro[clave]
    nombre = (s.comercio_nombre or "").strip()
    if nombre:
        # Todo sale del nombre: se vacían los tres campos de marca.
        p = replace(p, comercio=nombre, descriptor_tarjeta="",
                    razon_social="", wordmark_html="")
        if p.clave == "petshop":
            # Farmacia y mutual conservan su prompt constante (los bytes).
            p = replace(p, system_prompt=prompts.resolver_plantilla(
                prompts.SYSTEM_PROMPT_PETSHOP_PLANTILLA, nombre, p.emoji))
    return p


def perfil_por_clave(clave: str) -> Perfil:
    """Perfil del registro sin mirar el entorno ni el cache (para tests)."""
    return _registro()[clave]
```

- [ ] **Step 10: Log y corte del arranque en `app/main.py`**

En `app/main.py`, después de la línea 18 (`from app.services.db import get_db`), agregar:

```python
from app.services.perfil import get_perfil
```

Y en el `lifespan`, reemplazar:

```python
    logging.basicConfig(level=settings.log_level)
    logger = logging.getLogger(__name__)

    # Restaurar archivos subidos (catálogo/padrón) desde Redis — el filesystem
```

por:

```python
    logging.basicConfig(level=settings.log_level)
    logger = logging.getLogger(__name__)

    # Perfil de rubro (VERTICAL). Sin try/except a propósito: un VERTICAL
    # desconocido es un ValueError que corta el arranque ("Application startup
    # failed") ANTES de tocar Redis, el catálogo o Postgres.
    perfil = get_perfil()
    logger.info(f"Perfil de rubro: {perfil.clave} ({perfil.comercio})")

    # Restaurar archivos subidos (catálogo/padrón) desde Redis — el filesystem
```

La variable local `perfil` queda disponible para `_restaurar_archivos(settings, perfil, blob)` y los gates del arranque de otras tareas.

- [ ] **Step 11: Correr los tests del perfil y ver que pasan**

```bash
.venv/Scripts/python -m pytest tests/test_perfil.py -v
```

Esperado: `36 passed`.

- [ ] **Step 12: El snapshot de `/pay` siempre con el perfil farmacia**

Cuando la tarea de pagos haga que `/pay` lea `get_perfil()`, el snapshot de farmacia no puede depender del `VERTICAL` del entorno. En `tests/test_goldens_farmacia.py`, reemplazar el encabezado de la fixture:

```python
@pytest.fixture
def payway_falso(monkeypatch):
    """pay_page sin Redis ni Payway: pago pendiente fijo y servicio falso."""
    from app.config import get_settings
    from app.routers import payway as pw
```

por:

```python
@pytest.fixture
def payway_falso(monkeypatch, usar_perfil):
    """pay_page sin Redis ni Payway: pago pendiente fijo y servicio falso. El
    snapshot es de la farmacia aunque el entorno diga otro VERTICAL."""
    from app.config import get_settings
    from app.routers import payway as pw

    usar_perfil("farmacia")
```

Las expectativas (los hashes) no cambian.

- [ ] **Step 13: Correr el perfil y los goldens con `VERTICAL=petshop` (spec §6.4)**

```bash
VERTICAL=petshop .venv/Scripts/python -m pytest tests/test_perfil.py tests/test_goldens_farmacia.py -q
```

Esperado: `43 passed` (los tests de esta tarea no dependen del `VERTICAL` del proceso).

- [ ] **Step 14: Correr la suite completa**

```bash
.venv/Scripts/python -m pytest -q
```

Esperado: `976 passed` (933 de hoy + 7 goldens + 36 de `test_perfil.py`).

- [ ] **Step 15: Commit**

```bash
git add app/services/prompts.py app/services/perfil.py app/services/intent_service.py app/services/image_service.py app/config.py app/main.py tests/test_perfil.py tests/test_goldens_farmacia.py
git commit -F - <<'EOF'
Perfil de rubro: prompts por bloques, perfiles farmacia/mutual/petshop y corte del arranque

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 3: Config del perfil — `valores_base()`, claves nuevas y fallbacks a `perfil.textos`

Spec: §3.4 (merge, claves nuevas, tabla "Fallbacks"), §4.4 (`pago_mp_manual`), §4.5 (comentario de `sintoma_farmaceutico_message`), §5 (`retiro_sucursal`, `retiro_info_message`, `ConfigUpdate`), §6.2 bullet "Config" y los fallbacks de `test_petshop.py` ("Pagos": pedido listo y efectivo; "Síntomas": `agregar_oferta_farmaceutico`).

Las líneas citadas son las de `07a1d7a`. Si una tarea anterior las corrió, ubicar el bloque por el texto de "Reemplazar" (es único en el archivo).

**Files:**
- Modify: `app/services/config_service.py:17` (import), `:203-204` (comentario), `:232-235` (después de `pago_solo_tarjeta_message`), `:274` (después de `envio_costo`), `:332-344` (después de `DEFAULT_HOURS`), `:413`, `:428`, `:430` (`get_all`), `:459-461` (`get`)
- Modify: `app/routers/backoffice.py:926` y `:935` (`ConfigUpdate`)
- Modify: `app/routers/orders_api.py:23` (import), `:161-173` (`armar_mensaje_pedido_listo`)
- Modify: `app/services/checkout_helper.py:13` (import), `:1523-1535` (`responder_bono`), `:1543-1545` (`agregar_oferta_farmaceutico`), `:1694-1705` (`_cerrar_venta_efectivo`)
- Modify: `app/routers/webhook.py:25` (import), `:985-990` (receta), `:994-998` (comprobante), `:1481-1490` (descuentos)
- Create: `tests/test_perfil_config.py`

**Interfaces:**
- Consumes: `app.services.perfil.get_perfil() -> Perfil` y `Perfil.textos: Mapping[str, str]` con las 13 claves de `CLAVES_TEXTO_RUBRO` (+ `consulta_salud_message` e `indicacion_veterinaria_message` en petshop) (Task 2); `perfil.perfil_por_clave(clave) -> Perfil` y `perfil._registro()` (spec §3.5; solo en tests, para armar un perfil con textos propios); fixture `usar_perfil(clave, comercio=None) -> Perfil` de `tests/conftest.py` (Task 1); `entorno`, `_msg` y `PHONE` de `tests/test_webhook_secuencias.py`.
- Produces:
  - `app/services/config_service.py`: `def valores_base() -> dict` (`{**DEFAULTS, **get_perfil().textos}`, copia nueva en cada llamada); `DEFAULTS["pago_mp_manual"] = "true"`, `DEFAULTS["retiro_sucursal"] = ""`, `DEFAULTS["retiro_info_message"] = "Lo retirás en *{sucursal}* 🏪"`; los tres `return` de `get_all` usan `{**valores_base(), **guardado}` y `get` usa `valores_base().get(key, "")`.
  - `app/routers/backoffice.py`: `ConfigUpdate.pago_mp_manual`, `ConfigUpdate.retiro_sucursal`, `ConfigUpdate.retiro_info_message` (`str | None = None`).
  - Los 12 fallbacks de la tabla de §3.4 leen `get_perfil().textos[clave]`.
  - `from app.services.perfil import get_perfil` a nivel de módulo en `config_service.py`, `checkout_helper.py`, `orders_api.py` y `webhook.py`. Las tareas siguientes lo reusan; no se duplica el import.

- [ ] **Step 1: Escribir el golden de `DEFAULTS` (antes de tocar nada)**

Crear `tests/test_perfil_config.py` con el encabezado, los fakes y el golden. El golden se toma sobre el código de hoy y tiene que pasar en verde **antes** del cambio (spec §6). Va como hash adentro del test, no como `.txt`. (Repite a propósito el `test_defaults_golden` de la Task 1 para dejar el golden junto a los tests del merge; las Tasks 1 y 2 no tocan `DEFAULTS`, así que sigue en verde.)

```python
"""
Config del perfil de rubro (spec 3.4): DEFAULTS + perfil.textos, lo guardado
gana, y los fallbacks del código que pasan a perfil.textos. La farmacia queda
idéntica: sus textos de perfil SON los de DEFAULTS.
"""
import hashlib
import json
from dataclasses import replace
from types import MappingProxyType

import pytest

from app.routers import webhook as wh
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (fixture reusada)


# ── Golden de DEFAULTS (tomado sobre 07a1d7a, ANTES del cambio) ─────────────────
def test_defaults_farmacia_golden():
    """Los 90 DEFAULTS de hoy no cambian: solo se suman las tres claves nuevas."""
    from app.services.config_service import DEFAULTS
    nuevas = {"pago_mp_manual", "retiro_sucursal", "retiro_info_message"}
    hoy = {k: v for k, v in DEFAULTS.items() if k not in nuevas}
    assert len(hoy) == 90
    h = hashlib.sha256(json.dumps(hoy, sort_keys=True, ensure_ascii=False)
                       .encode("utf-8")).hexdigest()
    assert h == "655cbe78de891191bb660b42cc1d4b3b9e325bf93205f139a6f4bfcefa910e1d"


# ── Fakes de Redis y Postgres para ConfigService ────────────────────────────────
class _RedisFalso:
    def __init__(self, data):
        self.data = dict(data)

    async def hgetall(self, key):
        return dict(self.data)

    async def hset(self, key, field=None, value=None, mapping=None):
        self.data.update(mapping or {field: value})


class _DBFalsa:
    def __init__(self, filas=None, disponible=True):
        self.filas = dict(filas or {})
        self._disponible = disponible

    def available(self):
        return self._disponible

    async def fetch(self, query, *args):
        return [{"clave": k, "valor": v} for k, v in self.filas.items()]

    async def execute(self, query, *args):
        self.filas[args[0]] = args[1]
        return "OK"


def _config(redis_data=None, db_filas=None):
    """ConfigService con Redis caído (o con datos) y sin Postgres (o con filas)."""
    from app.services.config_service import ConfigService
    svc = ConfigService("redis://127.0.0.1:1")
    svc._ok = redis_data is not None
    if redis_data is not None:
        svc._redis = _RedisFalso(redis_data)
    svc._db = _DBFalsa(db_filas, disponible=db_filas is not None)
    return svc
```

- [ ] **Step 2: Correr el golden y verlo pasar (es la foto de hoy)**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_config.py::test_defaults_farmacia_golden -v`
Expected: `PASSED`. Si falla, no seguir: `DEFAULTS` ya no es el de `07a1d7a`.

- [ ] **Step 3: Commit del golden**

```bash
git add tests/test_perfil_config.py
git commit -F - <<'EOF'
Golden de DEFAULTS de la farmacia antes del perfil de rubro

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 4: Escribir los tests de claves nuevas, `ConfigUpdate`, `valores_base()` y el merge**

Agregar al final de `tests/test_perfil_config.py`:

```python
# ── Claves nuevas de DEFAULTS y ConfigUpdate ────────────────────────────────────
def test_defaults_claves_nuevas_dejan_todo_como_hoy():
    from app.services.config_service import DEFAULTS
    assert DEFAULTS["pago_mp_manual"] == "true"
    assert DEFAULTS["retiro_sucursal"] == ""
    assert DEFAULTS["retiro_info_message"] == "Lo retirás en *{sucursal}* 🏪"


def test_config_update_acepta_las_claves_nuevas():
    from app.routers.backoffice import ConfigUpdate
    body = ConfigUpdate(retiro_sucursal="Sucursal Piloto",
                        retiro_info_message="Lo retirás en *{sucursal}*, Calle Falsa 123",
                        pago_mp_manual="false")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    assert updates == {"retiro_sucursal": "Sucursal Piloto",
                       "retiro_info_message": "Lo retirás en *{sucursal}*, Calle Falsa 123",
                       "pago_mp_manual": "false"}


# ── valores_base() y el merge de get_all / get ──────────────────────────────────
def test_valores_base_farmacia_es_defaults_y_petshop_suma_sus_textos(usar_perfil):
    from app.services.config_service import DEFAULTS, valores_base
    usar_perfil("farmacia")
    assert valores_base() == DEFAULTS

    p = usar_perfil("petshop")
    vb = valores_base()
    assert {k: vb[k] for k in p.textos} == dict(p.textos)
    assert {k: v for k, v in vb.items() if k not in p.textos} == \
        {k: v for k, v in DEFAULTS.items() if k not in p.textos}
    vb["send_images"] = "on_request"                 # copia: no ensucia DEFAULTS
    assert valores_base()["send_images"] == DEFAULTS["send_images"]


async def test_get_all_farmacia_es_defaults(usar_perfil):
    from app.services.config_service import DEFAULTS
    usar_perfil("farmacia")
    assert await _config().get_all() == DEFAULTS


async def test_get_all_petshop_textos_del_perfil_y_lo_guardado_gana(usar_perfil):
    from app.services.config_service import DEFAULTS
    p = usar_perfil("petshop")
    svc = _config()
    cfg = await svc.get_all()
    assert cfg["pedido_listo_retiro_message"].endswith("🐾")
    assert cfg["sintoma_farmaceutico_message"] == ""
    assert cfg["send_images"] == DEFAULTS["send_images"]
    assert cfg["consulta_salud_message"] == p.textos["consulta_salud_message"]
    assert not any("💊" in cfg[k] for k in p.textos)

    await svc.set_many({"pedido_listo_retiro_message": "X"})
    assert (await svc.get_all())["pedido_listo_retiro_message"] == "X"
    assert await svc.get("pedido_listo_retiro_message") == "X"


@pytest.mark.parametrize("origen", ["redis", "postgres"])
async def test_get_all_petshop_con_config_guardada(usar_perfil, origen):
    usar_perfil("petshop")
    guardado = {"send_images": "on_request"}
    svc = _config(redis_data=guardado) if origen == "redis" else _config(db_filas=guardado)
    cfg = await svc.get_all()
    assert cfg["send_images"] == "on_request"                 # lo guardado gana
    assert cfg["pedido_listo_envio_message"].endswith("🐾")   # el resto, del perfil
    assert "💊" not in cfg["efectivo_retiro_message"]


async def test_get_usa_valores_base_como_default(usar_perfil):
    usar_perfil("petshop")
    svc = _config()
    assert (await svc.get("pedido_listo_envio_message")).endswith("🐾")
    assert await svc.get("clave_que_no_existe") == ""
```

- [ ] **Step 5: Correrlos y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_config.py -v -k "claves_nuevas or config_update or valores_base or get_all or get_usa"`
Expected: 7 FAILED y 1 PASSED.
- `test_defaults_claves_nuevas_dejan_todo_como_hoy`: `KeyError: 'pago_mp_manual'`.
- `test_config_update_acepta_las_claves_nuevas`: `AssertionError: assert {} == {'retiro_sucu...ual': 'false'}` (pydantic descarta lo no declarado).
- `test_valores_base_...`: `ImportError: cannot import name 'valores_base' from 'app.services.config_service'`.
- `test_get_all_petshop_*` (3) y `test_get_usa_valores_base_como_default`: `AssertionError` en `.endswith("🐾")` (el texto termina en 💊).
- `test_get_all_farmacia_es_defaults` PASA: es la guarda de "la farmacia no cambia".

- [ ] **Step 6: Implementar `valores_base()`, las claves nuevas y el comentario en `config_service.py`**

En `app/services/config_service.py`:

(a) Import (línea 17). Reemplazar:
```python
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)
```
por:
```python
import redis.asyncio as aioredis

from app.services.perfil import get_perfil

logger = logging.getLogger(__name__)
```
No hay ciclo: `perfil.py` importa `DEFAULTS` adentro de `_registro()`.

(b) Comentario de `sintoma_farmaceutico_message` (líneas 203-204, spec §4.5). Reemplazar:
```python
    # Fila 48: pedido por síntoma ("algo para la gripe") → tras ofrecer venta
    # libre, dejar a mano al farmacéutico. Vacío = apagado.
```
por:
```python
    # Fila 48: pedido por síntoma ("algo para la gripe") → tras ofrecer venta
    # libre, dejar a mano al farmacéutico. Un espacio lo apaga; vacío vuelve
    # al texto del perfil de rubro (en la farmacia, este mismo).
```

(c) `pago_mp_manual` (después de las líneas 232-235). Reemplazar:
```python
    "pago_solo_tarjeta_message": (
        "Por este canal aceptamos pago con tarjeta (débito o crédito) 💳. "
        "Si querés, seguimos con tu pedido y te mando el link de pago seguro."
    ),
```
por:
```python
    "pago_solo_tarjeta_message": (
        "Por este canal aceptamos pago con tarjeta (débito o crédito) 💳. "
        "Si querés, seguimos con tu pedido y te mando el link de pago seguro."
    ),
    # "mercado pago" en el mensaje cuenta como pago manual (deriva o "solo
    # tarjeta"). "false" = no: para un comercio que cobra con MP (spec 4.4).
    "pago_mp_manual": "true",
```

(d) Sucursal de retiro (línea 274). Reemplazar:
```python
    "envio_costo": "0",
```
por:
```python
    "envio_costo": "0",
    # Sucursal de retiro (spec 5): nombre corto que se muestra en "*retiro en
    # {sucursal}*". Vacío = "*retiro en sucursal*" como siempre. La respuesta a
    # "¿dónde queda la sucursal?" solo sale con la sucursal cargada; ningún
    # default trae una dirección (el bot nunca la inventa).
    "retiro_sucursal": "",
    "retiro_info_message": "Lo retirás en *{sucursal}* 🏪",
```

(e) `valores_base()` (después del cierre de `DEFAULT_HOURS`, línea 344). Reemplazar:
```python
        "sun": {"open": "09:00", "close": "13:00", "active": False},
    },
}
```
por:
```python
        "sun": {"open": "09:00", "close": "13:00", "active": False},
    },
}


def valores_base() -> dict:
    """
    DEFAULTS + los textos del perfil de rubro (spec 3.4). Lo guardado en
    Redis/Postgres se mezcla ENCIMA y siempre gana. En farmacia y mutual los
    textos del perfil se arman desde DEFAULTS: da DEFAULTS tal cual. Pública
    para que los tests armen su config falsa con los textos del perfil.
    """
    return {**DEFAULTS, **get_perfil().textos}
```

(f) Los tres `return` de `get_all` (líneas 413, 428 y 430). Reemplazar:
```python
                if data:
                    return {**DEFAULTS, **data}
```
por:
```python
                if data:
                    return {**valores_base(), **data}
```
y reemplazar:
```python
            return {**DEFAULTS, **durable}

        return {**DEFAULTS, **self._cache}
```
por:
```python
            return {**valores_base(), **durable}

        return {**valores_base(), **self._cache}
```

(g) `get` (líneas 459-461). Reemplazar:
```python
        config = await self.get_all()
        return config.get(key, DEFAULTS.get(key, ""))
```
por:
```python
        config = await self.get_all()
        return config.get(key, valores_base().get(key, ""))
```

- [ ] **Step 7: Declarar las tres claves en `ConfigUpdate` (`backoffice.py`)**

En `app/routers/backoffice.py` (lista blanca de `PATCH /bo/config`). Reemplazar (línea 926):
```python
    pago_solo_tarjeta_message: str | None = None
```
por:
```python
    pago_solo_tarjeta_message: str | None = None
    pago_mp_manual: str | None = None            # "false" = "mercado pago" no es pago manual
```
y reemplazar (línea 935):
```python
    envio_costo: str | None = None               # costo del envío a domicilio ("0" = gratis)
```
por:
```python
    envio_costo: str | None = None               # costo del envío a domicilio ("0" = gratis)
    retiro_sucursal: str | None = None           # nombre de la sucursal de retiro ("" = "sucursal")
    retiro_info_message: str | None = None       # respuesta a "¿dónde queda?" ({sucursal})
```

- [ ] **Step 8: Correr y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_config.py -v`
Expected: 9 passed (el golden sigue verde: las claves nuevas se excluyen del hash).

- [ ] **Step 9: Commit**

```bash
git add app/services/config_service.py app/routers/backoffice.py tests/test_perfil_config.py
git commit -F - <<'EOF'
Config: valores_base() suma los textos del perfil y tres claves nuevas

get_all y get mezclan DEFAULTS con perfil.textos; lo guardado sigue ganando.
Nuevas pago_mp_manual, retiro_sucursal y retiro_info_message, con defaults que
dejan todo como hoy, declaradas en ConfigUpdate. Se corrige el comentario de
sintoma_farmaceutico_message.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 10: Escribir los tests de los 12 fallbacks**

`perfil_con_textos` arma un perfil farmacia (todas las capacidades prendidas, así los ganchos se siguen alcanzando cuando las tareas de capacidades agreguen sus gates) con textos propios. Con eso se distingue un fallback que lee `perfil.textos` del literal de hoy, que en la farmacia es idéntico. Agregar al final de `tests/test_perfil_config.py`:

```python
# ── Fallbacks del código → perfil.textos ────────────────────────────────────────
@pytest.fixture
def perfil_con_textos():
    """Perfil farmacia (todas las capacidades prendidas) con textos propios:
    distingue un fallback que lee perfil.textos del literal de hoy. Igual que
    usar_perfil (Task 1), pisa atributos del Settings cacheado y nunca hace
    get_settings.cache_clear(): un Settings nuevo levantaría el DATABASE_URL
    que pg_dsn deja en el entorno."""
    from app.config import get_settings
    from app.services import perfil as perfil_mod
    mp = pytest.MonkeyPatch()

    def _usar(**textos):
        base = perfil_mod.perfil_por_clave("farmacia")
        propio = replace(base, textos=MappingProxyType({**base.textos, **textos}))
        mp.setattr(perfil_mod, "_registro", lambda: {"farmacia": propio})
        mp.setattr(get_settings(), "vertical", "farmacia")
        mp.setattr(get_settings(), "comercio_nombre", "")
        perfil_mod.get_perfil.cache_clear()
        return perfil_mod.get_perfil()

    yield _usar
    mp.undo()
    perfil_mod.get_perfil.cache_clear()


_ORDER = {"sku_nombre": "Piedras Sanicat 4 kg", "cantidad": 1, "total": 6200.0,
          "pickup_code": "445566", "tipo_entrega": "retiro", "direccion_envio": None}


def test_fallback_pedido_listo_lee_el_perfil(perfil_con_textos):
    from app.routers.orders_api import armar_mensaje_pedido_listo
    perfil_con_textos(pedido_listo_retiro_message="RETIRO {codigo}",
                      pedido_listo_envio_message="ENVIO {direccion}")
    assert armar_mensaje_pedido_listo(_ORDER, {}) == "RETIRO 445566"
    envio = dict(_ORDER, tipo_entrega="envio", direccion_envio="Mitre 100")
    assert armar_mensaje_pedido_listo(envio, {}) == "ENVIO Mitre 100"


@pytest.mark.parametrize("tipo", ["retiro", "envio"])
def test_pedido_listo_con_clave_vacia_petshop_y_farmacia(usar_perfil, tipo):
    from app.routers.orders_api import armar_mensaje_pedido_listo
    from app.services.config_service import DEFAULTS
    order = dict(_ORDER, tipo_entrega=tipo, direccion_envio="Mitre 100")
    clave = f"pedido_listo_{tipo}_message"

    usar_perfil("petshop")
    msg = armar_mensaje_pedido_listo(order, {clave: ""})
    assert msg.endswith("🐾") and "💊" not in msg

    usar_perfil("farmacia")                                   # igual que hoy
    assert armar_mensaje_pedido_listo(order, {clave: ""}) == \
        armar_mensaje_pedido_listo(order, {clave: DEFAULTS[clave]})


class _RedisOrdenes:
    def __init__(self):
        self.kv, self.idx = {}, []

    async def setex(self, k, ttl, v):
        self.kv[k] = v

    async def zadd(self, key, mapping):
        self.idx.extend(mapping.keys())

    async def zrevrange(self, key, a, b):
        return list(reversed(self.idx))[a:b + 1]

    async def mget(self, keys):
        return [self.kv.get(k) for k in keys]

    async def get(self, k):
        return self.kv.get(k)


async def _cerrar_efectivo(monkeypatch, tipo_entrega, direccion=None):
    import app.services.order_service as omod
    from app.services import checkout_helper as ch
    from app.services.session_service import SessionService
    ordenes = omod.OrderService("redis://127.0.0.1:1")
    ordenes._redis = _RedisOrdenes()
    monkeypatch.setattr(omod, "_instance", ordenes)
    ss = SessionService("redis://127.0.0.1:1")
    ph = "5493415550299"
    await ss.set_pending(ph, sku_id="S1", sku_nombre="Piedras Sanicat 4 kg", precio=6200.0,
                         cantidad=1, opciones=[])
    return await ch._cerrar_venta_efectivo(ss, ph, await ss.get(ph), tipo_entrega,
                                           direccion, 6200.0, cfg={})


async def test_fallback_efectivo_lee_el_perfil(perfil_con_textos, monkeypatch):
    perfil_con_textos(efectivo_retiro_message="EFECTIVO RETIRO {producto}",
                      efectivo_envio_message="EFECTIVO ENVIO {direccion}")
    assert await _cerrar_efectivo(monkeypatch, "retiro") == "EFECTIVO RETIRO Piedras Sanicat 4 kg"
    assert await _cerrar_efectivo(monkeypatch, "envio", "Mitre 100") == "EFECTIVO ENVIO Mitre 100"


@pytest.mark.parametrize("tipo,direccion", [("retiro", None), ("envio", "Mitre 100")])
async def test_efectivo_con_clave_vacia_petshop(usar_perfil, monkeypatch, tipo, direccion):
    usar_perfil("petshop")
    msg = await _cerrar_efectivo(monkeypatch, tipo, direccion)
    assert msg.endswith("¡Muchas gracias! 🐾") and "💊" not in msg


def test_fallback_sintoma_farmaceutico_lee_el_perfil(perfil_con_textos):
    from app.services.checkout_helper import agregar_oferta_farmaceutico
    perfil_con_textos(sintoma_farmaceutico_message="EXTRA DEL PERFIL")
    assert agregar_oferta_farmaceutico("Te ofrezco Tafirol $1.200", {}) == \
        "Te ofrezco Tafirol $1.200\n\nEXTRA DEL PERFIL"


def test_sintoma_farmaceutico_vacio_petshop_no_agrega_y_farmacia_como_hoy(usar_perfil):
    from app.services.checkout_helper import agregar_oferta_farmaceutico
    from app.services.config_service import DEFAULTS
    usar_perfil("petshop")
    assert agregar_oferta_farmaceutico(
        "Te ofrezco Pipeta X", {"sintoma_farmaceutico_message": ""}) == "Te ofrezco Pipeta X"
    usar_perfil("farmacia")
    assert agregar_oferta_farmaceutico(
        "Te ofrezco Pipeta X", {"sintoma_farmaceutico_message": ""}) == \
        "Te ofrezco Pipeta X\n\n" + DEFAULTS["sintoma_farmaceutico_message"]


def test_fallback_bonos_leen_el_perfil(perfil_con_textos):
    from app.services.checkout_helper import responder_bono
    perfil_con_textos(bono_recibido_message="RECIBIDO {laboratorio}",
                      bono_consulta_si_message="SI {laboratorio}",
                      bono_no_reconocido_message="NO RECONOCIDO")
    cfg = {"bonos_laboratorios": "Cassará"}
    assert responder_bono("cassara", cfg, por_foto=True) == ("RECIBIDO Cassará", True)
    assert responder_bono("cassara", cfg) == ("SI Cassará", True)
    assert responder_bono("Bagó", cfg, por_foto=True) == ("NO RECONOCIDO", False)


async def test_fallback_receta_recibida_lee_el_perfil(entorno, perfil_con_textos):
    perfil_con_textos(receta_recibida_message="RECETA DEL PERFIL")
    deps = entorno(img_tipo="receta", cfg={"receta_recibida_message": ""})
    await wh.procesar_mensajes([_msg("", tipo="image", media_url="https://kapso/r.jpg")])
    assert deps["wa"].enviados[-1] == "RECETA DEL PERFIL"


async def test_fallback_comprobante_lee_el_perfil(entorno, perfil_con_textos):
    perfil_con_textos(comprobante_recibido_message="COMPROBANTE DEL PERFIL")
    deps = entorno(img_tipo="comprobante", cfg={"comprobante_recibido_message": ""})
    await wh.procesar_mensajes([_msg("", tipo="image", media_url="https://kapso/c.jpg")])
    assert deps["wa"].enviados[-1] == "COMPROBANTE DEL PERFIL"


async def test_comprobante_petshop_sin_texto_guardado(entorno, usar_perfil):
    """Sin {nombre}: MO no tiene padrón y hoy saldría "¡Listo !"."""
    usar_perfil("petshop")
    deps = entorno(img_tipo="comprobante", cfg={"comprobante_recibido_message": ""})
    await wh.procesar_mensajes([_msg("", tipo="image", media_url="https://kapso/c.jpg")])
    assert deps["wa"].enviados[-1] == (
        "¡Listo! Recibimos tu comprobante 🙌 Lo verificamos y te confirmamos en un rato.")


async def test_fallback_descuentos_leen_el_perfil(entorno, perfil_con_textos):
    perfil_con_textos(socio_discount_off_message="OFF DEL PERFIL",
                      socio_discount_info_message="INFO {pct}% DEL PERFIL")
    deps = entorno(cfg={"socio_discount_off_message": ""})
    await wh.procesar_mensajes([_msg("tienen descuento?")])
    assert deps["wa"].enviados[-1] == "OFF DEL PERFIL"

    deps = entorno(cfg={"socio_discount_pct": "10", "socio_discount_info_message": ""})
    await wh.procesar_mensajes([_msg("tienen descuento?")])
    assert deps["wa"].enviados[-1] == "INFO 10% DEL PERFIL"
```

- [ ] **Step 11: Correrlos y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_config.py -v -k "fallback or clave_vacia or vacio_petshop or comprobante_petshop"`
Expected: 13 FAILED, todos con `AssertionError` porque sale el literal de farmacia. Por ejemplo:
- `test_fallback_pedido_listo_lee_el_perfil`: `assert '🎉 *¡Tu pedid... esperamos! 💊' == 'RETIRO 445566'`.
- `test_fallback_receta_recibida_lee_el_perfil`: `assert 'Recibimos tu...s 10 minutos.' == 'RECETA DEL PERFIL'`.
- `test_comprobante_petshop_sin_texto_guardado`: `assert '¡Listo ! Rec...s en un rato.' == '¡Listo! Reci...s en un rato.'`.
- `test_fallback_descuentos_leen_el_perfil`: `assert 'Por ahora te...máticamente.' == 'OFF DEL PERFIL'`.
- `test_sintoma_farmaceutico_vacio_petshop_...`: `assert 'Te ofrezco P...e te oriente.' == 'Te ofrezco Pipeta X'`.

- [ ] **Step 12: Fallbacks de `orders_api.py` (pedido listo)**

En `app/routers/orders_api.py`, import (línea 23). Reemplazar:
```python
from app.services.config_service import get_config_service
```
por:
```python
from app.services.config_service import get_config_service
from app.services.perfil import get_perfil
```
y en `armar_mensaje_pedido_listo` (líneas 161-173) reemplazar:
```python
    if (order.get("tipo_entrega") or "retiro") == "envio":
        plantilla = cfg.get("pedido_listo_envio_message") or (
            "🎉 *¡Tu pedido está listo!*\n\n"
            "*{producto}* — ${total}\n"
            "🚚 Sale para *{direccion}*. Te avisamos cuando esté en camino. 💊"
        )
    else:
        plantilla = cfg.get("pedido_listo_retiro_message") or (
            "🎉 *¡Tu pedido está listo para retirar!*\n\n"
            "*{producto}* — ${total}\n"
            "🔑 *Código de retiro: {codigo}*{horario}\n\n"
            "Presentá este código y te lo entregamos. ¡Te esperamos! 💊"
        )
```
por:
```python
    # Sin texto guardado, el del perfil de rubro (farmacia: el de DEFAULTS).
    if (order.get("tipo_entrega") or "retiro") == "envio":
        plantilla = (cfg.get("pedido_listo_envio_message")
                     or get_perfil().textos["pedido_listo_envio_message"])
    else:
        plantilla = (cfg.get("pedido_listo_retiro_message")
                     or get_perfil().textos["pedido_listo_retiro_message"])
```

- [ ] **Step 13: Fallbacks de `checkout_helper.py` (bonos, farmacéutico, efectivo)**

En `app/services/checkout_helper.py`, import (línea 13). Reemplazar:
```python
from app.services.sku_service import requiere_derivacion
```
por:
```python
from app.services.perfil import get_perfil
from app.services.sku_service import requiere_derivacion
```

En `responder_bono` (líneas 1523-1530) reemplazar:
```python
        if por_foto:
            txt = cfg.get("bono_recibido_message") or (
                "¡Hola {nombre}! Sí, trabajamos los bonos de {laboratorio} 🙌 Te paso con "
                "alguien del equipo que lo gestiona con vos.")
        else:
            txt = cfg.get("bono_consulta_si_message") or (
                "Sí, trabajamos los bonos de {laboratorio} 🙂 Mandame la foto del bono y te "
                "paso con alguien del equipo que lo gestiona.")
```
por:
```python
        if por_foto:
            txt = (cfg.get("bono_recibido_message")
                   or get_perfil().textos["bono_recibido_message"])
        else:
            txt = (cfg.get("bono_consulta_si_message")
                   or get_perfil().textos["bono_consulta_si_message"])
```
y (líneas 1532-1535) reemplazar:
```python
    if por_foto:
        txt = cfg.get("bono_no_reconocido_message") or (
            "¡Hola {nombre}! Recibí tu bono 🙌 Te paso con alguien del equipo para "
            "confirmar si lo trabajamos.")
```
por:
```python
    if por_foto:
        txt = (cfg.get("bono_no_reconocido_message")
               or get_perfil().textos["bono_no_reconocido_message"])
```
El `else` de `bono_consulta_no_message` (1536-1538) **no** cambia (§3.4: texto neutro).

En `agregar_oferta_farmaceutico` (líneas 1543-1545) reemplazar:
```python
def agregar_oferta_farmaceutico(respuesta: str, cfg: dict) -> str:
    extra = cfg.get("sintoma_farmaceutico_message") or (
        "Si preferís, decime \"farmacéutico\" y te paso con el nuestro para que te oriente.")
```
por:
```python
def agregar_oferta_farmaceutico(respuesta: str, cfg: dict) -> str:
    # Vacío en la config → el del perfil (petshop lo trae vacío: no agrega nada).
    extra = (cfg.get("sintoma_farmaceutico_message")
             or get_perfil().textos["sintoma_farmaceutico_message"])
```

En `_cerrar_venta_efectivo` (líneas 1694-1705) reemplazar:
```python
    if tipo_entrega == "envio":
        plantilla = cfg.get("efectivo_envio_message") or (
            "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
            "*{producto}* — ${total}{envio}\n"
            "🚚 Te lo enviamos a *{direccion}* y lo pagás en efectivo al recibirlo.\n"
            "📋 Código de pedido: *{codigo}*\n\n¡Muchas gracias! 💊")
    else:
        plantilla = cfg.get("efectivo_retiro_message") or (
            "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
            "*{producto}* — ${total}\n"
            "💵 Lo pagás en efectivo al retirar.{plazo}\n"
            "🔑 *Tu código de retiro es: {codigo}*\n\n¡Muchas gracias! 💊")
```
por:
```python
    if tipo_entrega == "envio":
        plantilla = (cfg.get("efectivo_envio_message")
                     or get_perfil().textos["efectivo_envio_message"])
    else:
        plantilla = (cfg.get("efectivo_retiro_message")
                     or get_perfil().textos["efectivo_retiro_message"])
```
`socio_discount_message` (línea 1121) **no** cambia: sigue con `or ''` (§3.4).

- [ ] **Step 14: Fallbacks de `webhook.py` (receta, comprobante, descuentos)**

En `app/routers/webhook.py`, import (línea 25). Reemplazar:
```python
from app.config import get_settings
```
por:
```python
from app.config import get_settings
from app.services.perfil import get_perfil
```

Foto de receta (líneas 985-990). Reemplazar:
```python
                        _cfg_rr = await deps["config"].get_all()
                        respuesta = _cfg_rr.get("receta_recibida_message") or (
                            "¡Hola {nombre}! Recibimos tu receta 🙌 Validamos la "
                            "información y volvemos con vos dentro de los próximos "
                            "10 minutos."
                        )
```
por:
```python
                        _cfg_rr = await deps["config"].get_all()
                        respuesta = (_cfg_rr.get("receta_recibida_message")
                                     or get_perfil().textos["receta_recibida_message"])
```

Comprobante (líneas 994-998). Reemplazar:
```python
                        _cfg_cp = await deps["config"].get_all()
                        respuesta = _cfg_cp.get("comprobante_recibido_message") or (
                            "¡Listo {nombre}! Recibimos tu comprobante 🙌 Lo "
                            "verificamos y te confirmamos en un rato."
                        )
```
por:
```python
                        _cfg_cp = await deps["config"].get_all()
                        respuesta = (_cfg_cp.get("comprobante_recibido_message")
                                     or get_perfil().textos["comprobante_recibido_message"])
```

Descuentos (líneas 1481-1490). Reemplazar:
```python
                elif _pct_desc > 0:
                    respuesta = (_cfg_pm.get("socio_discount_info_message") or
                                 "¡Sí! Los socios de la Mutual tienen {pct}% de descuento en "
                                 "productos sin receta — se aplica solo en el link de pago 🙂"
                                 ).replace("{pct}", f"{_pct_desc:g}")
                else:
                    respuesta = (_cfg_pm.get("socio_discount_off_message") or
                                 "Por ahora te puedo ofrecer el precio de lista 🙂 El descuento "
                                 "para socios lo estamos habilitando — cuando esté activo se "
                                 "aplica automáticamente.")
```
por:
```python
                elif _pct_desc > 0:
                    respuesta = (_cfg_pm.get("socio_discount_info_message")
                                 or get_perfil().textos["socio_discount_info_message"]
                                 ).replace("{pct}", f"{_pct_desc:g}")
                else:
                    respuesta = (_cfg_pm.get("socio_discount_off_message")
                                 or get_perfil().textos["socio_discount_off_message"])
```
No cambian (§3.4): `consulta_saldo_message` (1229-1231), `cc_no_habilitada_message` (1369-1371), `imagen_no_reconocida_message` (1026-1029, 1067-1069) ni el texto de la credencial (1000-1004).

- [ ] **Step 15: Correr y ver que pasan; verificar que no quedó ningún literal**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_config.py -v`
Expected: 22 passed.

Run: `grep -n "Te esperamos! 💊\|en camino. 💊\|efectivo al retirar.{plazo}\|efectivo al recibirlo\|Recibimos tu receta 🙌 Validamos\|Listo {nombre}! Recibimos tu comprobante\|socios de la Mutual tienen\|lo estamos habilitando\|Recibí tu bono 🙌\|trabajamos los bonos de {laboratorio}\|te paso con el nuestro para que te oriente" app/routers/orders_api.py app/routers/webhook.py app/services/checkout_helper.py`
Expected: sin resultados (los literales quedan solo en `DEFAULTS`). Los "¡Muchas gracias! 💊" de `mp_webhook.py`, `payway.py` y la confirmación de cuenta corriente (`checkout_helper.py:1007, 1013`) no son de esta tarea (§4.7 y §8).

- [ ] **Step 16: Commit**

```bash
git add app/routers/orders_api.py app/services/checkout_helper.py app/routers/webhook.py tests/test_perfil_config.py
git commit -F - <<'EOF'
Los 12 fallbacks de texto de rubro leen perfil.textos

Pedido listo, efectivo, bonos, oferta del farmaceutico, receta, comprobante y
respuestas de descuento: sin texto guardado sale el del perfil. En la farmacia
el perfil trae los mismos DEFAULTS, asi que no cambia nada.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 16b: Review Focus — config guardada de MO y singleton de config (pasan de entrada)**

Dos modos de falla que el spec implica y que los tests de arriba no ejercitan (ver "Review Focus", puntos 2 y 5): la config real de MO llega de Redis o de Postgres con claves ya guardadas (§9.2, "Config guardada en MO"), y `ConfigService` es un singleton que tiene que leer el perfil en cada llamada (§3.1). Fijan lo que ya implementó esta tarea: no hay paso rojo. Agregar al final de `tests/test_perfil_config.py`:

```python
# ── Review Focus: config guardada de MO y singleton de ConfigService ────────────
@pytest.mark.parametrize("origen", ["redis", "postgres"])
async def test_config_guardada_de_mo_vaciada_cae_al_perfil_y_con_pildora_gana(usar_perfil, origen):
    """Lo guardado gana (spec 3.4): un texto vaciado desde el panel ("") cae al
    del perfil recién al usarlo; un texto viejo con 💊 sigue saliendo, por eso
    el despliegue borra esas claves (spec 7.3, paso 7; Task 17, Step 10)."""
    from app.routers.orders_api import armar_mensaje_pedido_listo
    from app.services.config_service import DEFAULTS
    usar_perfil("petshop")
    guardado = {"pedido_listo_retiro_message": "",
                "efectivo_retiro_message": DEFAULTS["efectivo_retiro_message"]}
    svc = _config(redis_data=guardado) if origen == "redis" else _config(db_filas=guardado)
    cfg = await svc.get_all()
    assert cfg["pedido_listo_retiro_message"] == ""
    msg = armar_mensaje_pedido_listo(_ORDER, cfg)
    assert msg.endswith("¡Te esperamos! 🐾") and "💊" not in msg
    assert cfg["efectivo_retiro_message"] == DEFAULTS["efectivo_retiro_message"]
    assert "💊" in cfg["efectivo_retiro_message"]


async def test_config_service_no_fija_el_perfil_en_la_instancia(usar_perfil):
    svc = _config(redis_data={"send_images": "on_request"})
    usar_perfil("farmacia")
    assert (await svc.get_all())["pedido_listo_retiro_message"].endswith("💊")
    usar_perfil("petshop")                         # misma instancia, otro perfil
    cfg = await svc.get_all()
    assert cfg["pedido_listo_retiro_message"].endswith("🐾")
    assert cfg["send_images"] == "on_request"
    assert await svc.get("pedido_listo_envio_message") == cfg["pedido_listo_envio_message"]
```

Run: `.venv/Scripts/python -m pytest tests/test_perfil_config.py -v -k "config_guardada_de_mo or no_fija_el_perfil"`
Expected: `3 passed`.

```bash
git add tests/test_perfil_config.py
git commit -F - <<'EOF'
Review Focus: config guardada de MO y singleton de config con el perfil de rubro

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 17: Suite completa**

Run: `.venv/Scripts/python -m pytest -q`
Expected: todo en verde, sin `failed` ni `error`: los 933 de `07a1d7a`, los de las tareas anteriores y los 25 de `tests/test_perfil_config.py` (22 + 3 de Review Focus) (unos 5 minutos). Ningún test existente cambia de expectativa. Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1001 tests.

---

### Task 4: Prompt completo de petshop e IntentService que lee el perfil

Spec: §4.1 (prompt, `_con_contexto`), §3.6 (`intent_service`, `webhook._deps`, `simulate.py`), §4.2 (fila `_formatear_productos`) y §6.2 (tests de prompt, singleton y `_con_contexto`).

**Files:**
- Modify: `app/services/prompts.py` (lo crea Task 2): se reemplaza la región de la plantilla provisoria, desde la línea `# >>> PETSHOP: PLANTILLA PROVISORIA (Task 2) ...` hasta la línea `# <<< PETSHOP ...` inclusive.
- Modify: `app/services/intent_service.py` (números de línea de `07a1d7a`; si Task 2 ya sacó el literal 36-171, están corridos hacia arriba, y por eso cada edición se ubica por su texto):
  - `:21-22` imports (`import anthropic` / `import openai`)
  - `:177-192` `IntentService.__init__` (parámetro `vertical` y coerción en 181) y `_system_prompt`
  - `:332-344` `_con_contexto`
  - `:346-361` `_formatear_productos` (marca `REQUIERE RECETA` en 360-361)
  - `:388-393` `get_intent_service`
- Modify: `app/routers/webhook.py:257` (`_deps`)
- Sin cambio de código: `app/routers/simulate.py:90`. Ya llama `get_intent_service(settings.anthropic_api_key, settings.openai_api_key, settings.llm_provider)` y queda corregido porque el prompt sale de `get_perfil()`. Lo cubre un test.
- Test: `tests/test_perfil.py`, agregando al final dos bloques.
- Test: `tests/test_petshop.py`, que **se crea en esta tarea**. Las tareas siguientes de §6.2 agregan sus tests a este archivo.

**Interfaces:**
- Consumes:
  - Task 2 (`app/services/perfil.py`): `get_perfil() -> Perfil` y `perfil_por_clave(clave: str) -> Perfil`, con los campos `Perfil.clave`, `.system_prompt`, `.socios`, `.rotulo_kb` y `.recetas`. Task 1: fixture `usar_perfil(clave, comercio=None) -> Perfil` en `tests/conftest.py`.
  - Task 2 (`app/services/prompts.py`):
    - `SYSTEM_PROMPT` y `resolver_plantilla(plantilla, comercio, emoji)`.
    - Los bloques compartidos `SEGUIMIENTO`, `DERIVACION`, `RESERVAS`, `CONFIRMACIONES` y `RESPUESTA_DIRECTA`.
    - Las líneas "= farmacia NN" como constantes: `CATALOGO_BUSQUEDA` (59-64), `PAGO_SIN_LINKS` (68-70), `PAGO_CORRECCION_CANTIDAD` (72-73, termina con la línea en blanco), `VARIOS_PRIMERO_Y_DEMAS` (133-134) y `VARIOS_NUNCA_JUNTES` (138).
    - `matriz_intenciones(fila_saludo: str, fila_consulta_abierta: str) -> str` y `formato_respuesta(linea_entidad: str, parrafo_agregar: str, linea_por_sintoma: str, linea_rechazo: str, lineas_cambio: str) -> str`. Cada hueco es una o más líneas completas, cada una con su `\n`.
    - Los marcadores `# >>> PETSHOP` / `# <<< PETSHOP` alrededor de la plantilla provisoria.
    - `intent_service.SYSTEM_PROMPT` ya es el re-export de `prompts.SYSTEM_PROMPT`, y `_SYSTEM_CACHED` sigue en `intent_service`.
- Produces:
  - `prompts.SYSTEM_PROMPT_PETSHOP_PLANTILLA: str` definitiva: 14.975 caracteres; resuelta con "Mascotas del Oeste"/🐾 da 15.001 caracteres y sha256 `adf693e220c8920efa305c56ed31fbc80375eba0a0b20b0ba800ddaa08e24d28`.
  - `prompts.PET_IDENTIDAD`, `PET_VENTA`, `PET_REGLAS`, `PET_SALUDO`, `PET_ABIERTA`, `PET_VARIOS`, `PET_ENTIDAD`, `PET_AGREGAR`, `PET_SINTOMA`, `PET_RECHAZO` y `PET_CAMBIO: str`.
  - `class IntentService: def __init__(self, anthropic_key: str, openai_key: str = "", provider: str = "anthropic")`, sin `vertical`.
  - `IntentService._system_prompt(self) -> str`, que devuelve `get_perfil().system_prompt` en cada llamada.
  - `@staticmethod IntentService._con_contexto(user_content: str, contexto_cliente: Optional[str], contexto_kb: Optional[str] = None) -> str`.
  - `IntentService._formatear_productos(self, productos: list[dict]) -> str`, que marca la receta solo con `perfil.recetas`.
  - `def get_intent_service(anthropic_key: str, openai_key: str = "", provider: str = "anthropic") -> IntentService`.

- [ ] **Step 1: Escribir los tests del prompt de petshop (fallan)**

Agregar al final de `tests/test_perfil.py`. Los imports van con alias para no chocar con los del encabezado que dejó la Task 2.

```python


# ══════════════════════════════════════════════════════════════════════════════
# Task 4 — prompt de petshop (spec §4.1 y §6.2)
# ══════════════════════════════════════════════════════════════════════════════
import hashlib as _hashlib
import re as _re

_PROHIBIDAS_PROMPT_PETSHOP = (
    "farmac", "receta", "socio", "obra social", "remedia", "mercado pago", "mutual",
    "medicament", "remedio", "semanalmente", "{comercio}", "{emoji}", "ibuprofeno",
    "lotrial", "bayer", "aveno", "talco",
)
_OBLIGATORIAS_PROMPT_PETSHOP = (
    "Soy el asistente virtual de Mascotas del Oeste",
    "¡Hola! Soy el asistente virtual de Mascotas del Oeste 🐾 ¿En qué te puedo ayudar?",
    "El sistema envía el link de pago después de que confirme.",
    "SALUD DE LA MASCOTA",
    "DESCUENTOS, PROMOCIONES Y CUPONES",
    "Nunca inventes la dirección",
    "especie",
)
# Bloques de farmacia que el prompt de petshop reusa tal cual (prompts.py).
_BLOQUES_COMPARTIDOS = (
    "SEGUIMIENTO", "DERIVACION", "RESERVAS", "CONFIRMACIONES", "RESPUESTA_DIRECTA",
    "CATALOGO_BUSQUEDA", "PAGO_SIN_LINKS", "PAGO_CORRECCION_CANTIDAD",
    "VARIOS_PRIMERO_Y_DEMAS", "VARIOS_NUNCA_JUNTES",
)
_SHA_PROMPT_FARMACIA = "1953a4e6815d855e635406c1da8f97ff83bb9be6a2fd040b69eabfae4c540749"


def _prompt_petshop() -> str:
    from app.services.perfil import perfil_por_clave
    return perfil_por_clave("petshop").system_prompt


def _claves_json(prompt: str) -> list[str]:
    """Claves del esquema JSON de FORMATO DE RESPUESTA, en orden."""
    bloque = prompt[prompt.index("FORMATO DE RESPUESTA:"):]
    bloque = bloque[bloque.index("{"): bloque.index("}") + 1]
    return _re.findall(r'^\s*"(\w+)":', bloque, _re.M)


def test_intent_service_reexporta_el_prompt_de_farmacia():
    from app.services import intent_service, prompts
    assert intent_service.SYSTEM_PROMPT is prompts.SYSTEM_PROMPT
    assert intent_service._SYSTEM_CACHED is prompts.SYSTEM_PROMPT
    assert _hashlib.sha256(intent_service.SYSTEM_PROMPT.encode("utf-8")).hexdigest() == _SHA_PROMPT_FARMACIA


def test_prompt_petshop_sin_vocabulario_de_farmacia():
    low = _prompt_petshop().lower()
    assert [w for w in _PROHIBIDAS_PROMPT_PETSHOP if w in low] == []


def test_prompt_petshop_frases_obligatorias():
    p = _prompt_petshop()
    assert [f for f in _OBLIGATORIAS_PROMPT_PETSHOP if f not in p] == []


def test_prompt_petshop_contrato_con_farmacia():
    from app.services import prompts
    farm, pet = prompts.SYSTEM_PROMPT, _prompt_petshop()
    # Cada bloque compartido está, tal cual, en los dos prompts
    for nombre in _BLOQUES_COMPARTIDOS:
        bloque = getattr(prompts, nombre)
        assert bloque and bloque in farm and bloque in pet, nombre
    # El enum de `intencion` es la misma línea en los dos
    enum_farm = [x for x in farm.splitlines() if x.lstrip().startswith('"intencion":')]
    enum_pet = [x for x in pet.splitlines() if x.lstrip().startswith('"intencion":')]
    assert len(enum_farm) == 1 and enum_farm == enum_pet
    # Mismas claves del JSON, en el mismo orden
    assert _claves_json(farm) == _claves_json(pet) == [
        "intencion", "entidad_producto", "entidades_adicionales", "agregar_al_pedido", "cantidad",
        "sku_seleccionado_index", "confirmacion", "solicita_imagen", "por_sintoma", "respuesta"]
    # Reglas duras de venta, idénticas
    for frase in (
        "- REGLA ESTRICTA: solo podés ofrecer productos que aparezcan en [RESULTADOS DEL CATÁLOGO] u [OPCIONES MOSTRADAS].",
        "- NUNCA incluyas URLs, links ni texto que parezca un link en tu respuesta.",
    ):
        assert frase in farm and frase in pet
    # Las filas de la matriz que no son del rubro (todas menos saludo y consulta_abierta)
    def _filas_fijas(p):
        return [x for x in p.splitlines() if x.startswith("| ")
                and not x.startswith(("| saludo |", "| consulta_abierta |"))]
    assert len(_filas_fijas(farm)) == 8 and _filas_fijas(farm) == _filas_fijas(pet)


def test_prompt_petshop_resuelve_comercio_nombre_y_farmacia_conserva_el_hash(usar_perfil):
    p = usar_perfil("petshop", comercio="MO Prueba")
    assert p.comercio == "MO Prueba"
    assert "Soy el asistente virtual de MO Prueba" in p.system_prompt
    assert "¡Hola! Soy el asistente virtual de MO Prueba 🐾 ¿En qué te puedo ayudar?" in p.system_prompt
    assert "Mascotas del Oeste" not in p.system_prompt
    f = usar_perfil("farmacia", comercio="MO Prueba")
    assert _hashlib.sha256(f.system_prompt.encode("utf-8")).hexdigest() == _SHA_PROMPT_FARMACIA
```

- [ ] **Step 2: Correr los tests y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil.py -k "test_prompt_petshop_ or reexporta_el_prompt" -v`

Esperado: `3 failed, 2 passed`.
- `test_prompt_petshop_frases_obligatorias` falla con `AssertionError: assert ['¡Hola! Soy el asistente virtual de Mascotas del Oeste 🐾 ¿En qué te puedo ayudar?', 'El sistema envía el link de pago después de que confirme.', 'SALUD DE LA MASCOTA', ...] == []`.
- `test_prompt_petshop_contrato_con_farmacia` falla con `AssertionError: SEGUIMIENTO`.
- `test_prompt_petshop_resuelve_comercio_nombre_y_farmacia_conserva_el_hash` falla en el saludo de "MO Prueba".
- Pasan `test_intent_service_reexporta_el_prompt_de_farmacia` (el re-export es de Task 2) y `test_prompt_petshop_sin_vocabulario_de_farmacia` (con la plantilla provisoria es una guarda).

Si `test_intent_service_reexporta_el_prompt_de_farmacia` falla, Task 2 no hizo el re-export. En ese caso, en `app/services/intent_service.py`:
1. Borrar el literal desde `SYSTEM_PROMPT = """Sos el asistente virtual de Remedia.` hasta el `"""` que cierra después de `("no gracias", "mejor no", "dejalo").`, con la línea en blanco que le sigue.
2. Agregar `from app.services.prompts import SYSTEM_PROMPT  # re-export: tests/test_logic.py:1372 lo importa de acá` debajo de `import openai`.

- [ ] **Step 3: Escribir el prompt de petshop en `prompts.py`**

En `app/services/prompts.py`, reemplazar todo desde la línea que empieza con `# >>> PETSHOP` hasta la línea que empieza con `# <<< PETSHOP`, ambas incluidas, por este bloque.
- Va después de `resolver_plantilla` y antes de la sección de visión.
- Usa los bloques y las funciones que Task 2 define más arriba en el mismo módulo.
- Las líneas "= farmacia NN" salen de las constantes compartidas. Las dos que la spec cambia (139 y 156) van escritas.

```python
# ── Petshop (plantilla: {comercio} y {emoji} los completa resolver_plantilla) ──
# Spec §4.1. Reusa los bloques compartidos y las líneas "= farmacia NN" de
# arriba; todo lo demás es del rubro. No incluye cuenta corriente, obras
# sociales, receta ni personalización de socios.
PET_IDENTIDAD = """\
Sos el asistente virtual de {comercio}, una cadena de petshops.

IDENTIDAD Y TONO:
- Sos cálido, cercano y amable. Como el equipo de un petshop de confianza que conoce y quiere a las mascotas de sus clientes.
- Hablás en rioplatense correcto y cuidado: cordial y simpático, sin exagerar la informalidad ni sonar vendedor insistente. Evitá "bárbaro/genial/buenísimo" en exceso.
- Si te preguntan quién sos o si sos un bot: "Soy el asistente virtual de {comercio}". No tenés nombre propio: no te inventes uno ni digas que sos una persona.
- No sos un bot genérico. Sos parte del equipo de {comercio}.
- Saludás al inicio de la conversación; después NO repitas el saludo en cada mensaje.
- No conocés el nombre del cliente: saludá de forma genérica, sin inventar nombres. Si te cuenta cómo se llama su mascota, podés usarlo con naturalidad.
- El canal es relacional antes de transaccional: primero conectás, después vendés.

"""

PET_VENTA = (
    """\
ALTERNATIVAS SIEMPRE CON PRECIO:
- Si mencionás un producto de la lista como alternativa, SIEMPRE con su precio ("tengo el Pedigree Adulto 3 kg a $9.800"). Nombrar un producto sin precio no sirve: el cliente no puede decidir y el sistema no lo toma como ofrecido.
- NUNCA cierres con "¿te gustaría más información?", "¿te interesa alguna de estas opciones?" ni similares. Cerrá con una pregunta concreta de compra ("¿te sirve?", "¿cuál preferís?") o no preguntes nada.

PRECIOS:
- Si el cliente pregunta un precio y el producto está en el contexto, SIEMPRE respondé con el precio concreto (ej.: "Las piedras Sanicat de 4 kg están $6.200"). Nunca esquives la pregunta de precio.

BÚSQUEDA EN CATÁLOGO SKU:
- El catálogo tiene productos con stock disponible.
"""
    + CATALOGO_BUSQUEDA  # = farmacia 59-64
    + """\
- Si la lista dice "Sin resultados en el catálogo" o no hay opciones que coincidan con lo que pidió el cliente, NO ofrezcas productos de otro tipo. Decí con honestidad que no lo tenés y ofrecé encargarlo o pasarlo con una persona del equipo. Nunca sugieras un producto de otro rubro ni para otra especie (ej.: si pide alimento para gato y no está, no ofrezcas alimento para perro ni un juguete).

LÓGICA DE PAGO:
"""
    + PAGO_SIN_LINKS  # = farmacia 68-70
    + "- El sistema envía el link de pago después de que confirme.\n"
    + PAGO_CORRECCION_CANTIDAD  # = farmacia 72-73
)

PET_REGLAS = """\
PAGO EN EFECTIVO Y OTRAS FORMAS DE PAGO:
- NUNCA digas que se puede o que no se puede pagar en efectivo, al retirar o al recibir: lo resuelve el sistema según la configuración del comercio. Si el cliente lo pide y el sistema no lo resolvió, respondé "te paso con alguien del equipo para coordinarlo".
- No existe pago diferido: no prometas "anotarlo", fiado, "a la cuenta" ni pagar más adelante; se paga con el link de pago. Si el cliente insiste, respondé "te paso con alguien del equipo".

DESCUENTOS, PROMOCIONES Y CUPONES (PROHIBIDO AFIRMAR):
- No tenés información de descuentos, promociones, cuotas, cupones ni convenios. Nunca afirmes ni inventes un descuento, una promo o un precio especial: los únicos precios son los del catálogo.
- Si el cliente pregunta o insiste, decile que eso lo ve el equipo y respondé "te paso con alguien del equipo".

SALUD DE LA MASCOTA (IMPORTANTE):
- Los productos de salud sin indicación veterinaria (pipetas, antiparasitarios, collares antipulgas, etc.) se venden como cualquier otro cuando el cliente los pide por nombre, marca o tipo ("pipeta Frontline para perro de 10 a 20 kg", "Bravecto", "algo para las pulgas"): "por_sintoma": false. No expliques cómo ni cuánto darle.
- Si el cliente cuenta un síntoma o un problema de salud de su mascota ("mi perro vomita, ¿qué le doy?", "tiene diarrea", "no quiere comer", "se rasca hasta lastimarse"), pregunta qué darle o cuánto darle (aunque nombre un producto: "¿cuánto Drontal le doy?") o pide hablar con un veterinario, poné "por_sintoma": true. NO diagnostiques ni recomiendes productos, tratamientos ni dosis, y no nombres productos del catálogo: respondé con calidez y decile que lo pasás con una persona del equipo. El sistema hace la derivación.

ENTREGA (RETIRO O ENVÍO A DOMICILIO):
- Cuando el sistema lo pida, ofrecé las dos opciones: retirar en la sucursal o envío a domicilio.
- Si el cliente elige envío y el sistema no tiene su dirección, pedísela con amabilidad.
- Nunca inventes la dirección ni los horarios de la sucursal: usá solo los que aparezcan en [INFORMACIÓN DEL COMERCIO]. Si no están, respondé "te paso con alguien del equipo" para que te los confirme.
- No calcules costos de envío ni tiempos — de eso se encarga el sistema/operador.

"""

PET_SALUDO = """\
| saludo | "Hola", "Buen día", "Buenas", "Cómo están", "Buenas tardes" | Saludar con calidez. Ejemplo: "¡Hola! Soy el asistente virtual de {comercio} {emoji} ¿En qué te puedo ayudar?". OJO: si además de saludar el cliente menciona o pide un PRODUCTO ("hola, tenés Royal Canin?"), NO es un simple saludo — usá la intención de producto (consulta_stock/consulta_precio/pedido) y poné el producto en entidad_producto. |
"""

PET_ABIERTA = """\
| consulta_abierta | "Qué alimento me recomendás para un cachorro", "Algo para un gato castrado", "Qué piedras me conviene", "Un juguete para un perro grande" | Indagar lo que falte (especie, edad, tamaño o raza) → sugerir productos del catálogo. Si cuenta un síntoma o un problema de salud, no es consulta_abierta: poné "por_sintoma": true |
"""

PET_VARIOS = (
    """\
PEDIDOS DE VARIOS PRODUCTOS:
Si el cliente menciona MÁS de un producto en el mismo mensaje ("un alimento para gato, piedras sanitarias y unos snacks"):
"""
    + VARIOS_PRIMERO_Y_DEMAS  # = farmacia 133-134
    + """\
UN PRODUCTO = TIPO + MARCA: "alimento Royal Canin", "pretal Kipper", "piedras Sanicat", "correa Petnation" son UN solo producto aunque la transcripción de un audio haya puesto una coma en el medio ("pretal, kipper"). No los separes.
DOS TIPOS CON LA MISMA MARCA SON DOS PRODUCTOS: "alimento y snacks Pedigree" = "alimento pedigree" + "snacks pedigree"; "correa y pretal Kipper" = "correa kipper" + "pretal kipper". Repetí la marca en cada uno.
ESCRIBÍ LA MARCA COMO LA DIJO EL CLIENTE: no la "corrijas" a una palabra común ("excellent" NO es "excelente", "kipper" no es "kiper"). El sistema busca con esas palabras.
"""
    + VARIOS_NUNCA_JUNTES  # = farmacia 138
    # = farmacia 139, con la última oración cambiada
    + """\
IMPORTANTÍSIMO: en tu respuesta hablá SOLO del producto de "entidad_producto" (el único sobre el que tenés [RESULTADOS DEL CATÁLOGO]). NO afirmes NADA sobre los adicionales: ni que los tenés, ni que NO los tenés, ni su precio. No los buscaste vos, no tenés esos datos, y el sistema agrega la información real debajo de tu respuesta. Decir "no tengo las piedras" cuando el sistema encuentra las piedras dos líneas más abajo deja al bot contradiciéndose solo.

"""
)

PET_ENTIDAD = """\
  "entidad_producto": "nombre del producto mencionado o null — CONSERVÁ los números y unidades tal como los dijo el cliente: peso, tamaño, cantidad, talle (ej: 'royal canin mini adult 3 kg', 'piedras sanicat 4 kg', 'pretal kipper n 4', 'dentastix x 7'); son lo que distingue una presentación de otra",
"""

# = farmacia 156, con "sumale unos snacks" en lugar de "sumale unas gomitas"
PET_AGREGAR = """\
El campo "agregar_al_pedido": true cuando ya hay un pedido en curso y el cliente quiere SUMAR este producto además de lo que ya tiene ("agregame también...", "sumale unos snacks", "y además quiero..."). false cuando lo quiere EN LUGAR del pendiente o no hay pedido en curso.
"""

PET_SINTOMA = """\
El campo "por_sintoma": true si el cliente cuenta un síntoma o un problema de salud de su mascota, pregunta qué darle o qué dosis, o pide un veterinario ("mi perro vomita, ¿qué le doy?", "tiene diarrea", "cuántas gotas le pongo", "¿cuánto Drontal le doy?", "pasame con el veterinario"). false si pide un producto por nombre, marca o tipo ("una pipeta para perro de 10 kg", "algo para las pulgas", "alimento para gato castrado").
"""

PET_RECHAZO = """\
- false → el usuario cancela O pide un producto DIFERENTE al pendiente (ej: "mejor Pro Plan", "no, quiero Excellent", "prefiero otra marca"). En estos casos siempre false, nunca null.
"""

PET_CAMBIO = """\
Si el cliente rechaza el pendiente mencionando OTRO producto (ej: "no, un Excellent", "mejor dame Pro Plan", "prefiero Vitalcan"), NO es una simple cancelación. Además de confirmacion=false, DEBÉS:
  - poner ese nuevo producto en "entidad_producto" (ej: "excellent", "pro plan", "vitalcan"),
"""

SYSTEM_PROMPT_PETSHOP_PLANTILLA = (
    PET_IDENTIDAD
    + SEGUIMIENTO
    + PET_VENTA
    + DERIVACION
    + PET_REGLAS
    + RESERVAS
    + matriz_intenciones(PET_SALUDO, PET_ABIERTA)
    + CONFIRMACIONES
    + RESPUESTA_DIRECTA
    + PET_VARIOS
    + formato_respuesta(PET_ENTIDAD, PET_AGREGAR, PET_SINTOMA, PET_RECHAZO, PET_CAMBIO)
)
```

- [ ] **Step 4: Correr los tests y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil.py -k "test_prompt_petshop_ or reexporta_el_prompt" -v`
Esperado: `5 passed`.

Run: `.venv/Scripts/python -m pytest tests -k "byte_a_byte" -v`
Esperado: el golden de farmacia de la Task 2 (`test_prompt_farmacia_identico_byte_a_byte`) en `PASSED`.

Control de tamaño: `.venv/Scripts/python -c "from app.services import prompts as p; print(len(p.SYSTEM_PROMPT_PETSHOP_PLANTILLA))"` imprime `14975`.

- [ ] **Step 5: Commit**

```bash
git add app/services/prompts.py tests/test_perfil.py
git commit -m "Prompt de petshop completo: bloques PET_* sobre los bloques compartidos de farmacia

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Escribir los tests de IntentService con perfil (fallan)**

Agregar al final de `tests/test_perfil.py`:

```python


# ══════════════════════════════════════════════════════════════════════════════
# Task 4 — IntentService lee el perfil en cada llamada (spec §3.6 y §6.2)
# ══════════════════════════════════════════════════════════════════════════════
import inspect as _inspect
import logging as _logging

import pytest

_KB_INSTRUCCION = ("Usá esta información para responder si aplica. Si no alcanza, ofrecé pasar "
                   "con una persona del equipo. No inventes datos.")


def test_system_prompt_sale_del_perfil_en_los_tres_rubros(usar_perfil):
    from app.services.intent_service import IntentService, SYSTEM_PROMPT
    from app.services.mutual_helper import SYSTEM_PROMPT_MUTUAL
    usar_perfil("farmacia")
    assert IntentService("")._system_prompt() is SYSTEM_PROMPT
    usar_perfil("petshop")
    assert "Mascotas del Oeste" in IntentService("")._system_prompt()
    usar_perfil("mutual")
    assert IntentService("")._system_prompt() is SYSTEM_PROMPT_MUTUAL


def test_singleton_de_intent_sigue_al_perfil(usar_perfil, monkeypatch):
    from app.services import intent_service as isvc
    monkeypatch.setattr(isvc, "_instance", None)
    usar_perfil("farmacia")
    svc = isvc.get_intent_service("", "", "anthropic")
    assert svc._system_prompt() == isvc.SYSTEM_PROMPT
    usar_perfil("petshop")
    assert isvc.get_intent_service("", "", "anthropic") is svc        # misma instancia
    assert "Mascotas del Oeste" in svc._system_prompt()                # perfil nuevo


def test_get_intent_service_como_lo_llama_simulate(usar_perfil, monkeypatch):
    """simulate.py:90 arma el bot sin vertical: igual tiene que salir el perfil activo."""
    from app.services import intent_service as isvc
    monkeypatch.setattr(isvc, "_instance", None)
    usar_perfil("petshop")
    assert "Mascotas del Oeste" in isvc.get_intent_service("", "", "anthropic")._system_prompt()


def test_intent_service_sin_parametro_vertical_y_loguea_el_perfil(usar_perfil, caplog):
    from app.services.intent_service import IntentService, get_intent_service
    assert "vertical" not in _inspect.signature(IntentService.__init__).parameters
    assert "vertical" not in _inspect.signature(get_intent_service).parameters
    usar_perfil("petshop")
    with caplog.at_level(_logging.INFO, logger="app.services.intent_service"):
        IntentService("")
    assert "perfil 'petshop'" in caplog.text


def test_deps_del_webhook_arma_el_intent_con_el_perfil(usar_perfil, monkeypatch):
    from app.routers import webhook as wh
    from app.services import intent_service as isvc
    for nombre in ("get_whatsapp_service", "get_sku_service", "get_session_service",
                   "get_payway_link_service", "get_payment_service", "get_audio_service",
                   "get_image_service", "get_perf_service", "get_config_service",
                   "get_socio_service", "get_message_store", "get_db", "get_metrics_store",
                   "get_rag_service", "get_embedding_service"):
        monkeypatch.setattr(wh, nombre, lambda *a, **k: None)
    monkeypatch.setattr(isvc, "_instance", None)
    usar_perfil("petshop")
    deps = wh._deps()
    assert "Mascotas del Oeste" in deps["intent"]._system_prompt()


def test_con_contexto_petshop_sin_socio_y_con_rotulo_del_comercio(usar_perfil):
    from app.services.intent_service import IntentService
    usar_perfil("petshop")
    out = IntentService._con_contexto("m", "Nombre de pila (para saludar): Ana", "Horario: 9 a 18")
    assert out == "m\n\n[INFORMACIÓN DEL COMERCIO]\nHorario: 9 a 18\n" + _KB_INSTRUCCION
    assert IntentService._con_contexto("m", "Nombre de pila (para saludar): Ana") == "m"


@pytest.mark.parametrize("clave", ["farmacia", "mutual"])
def test_con_contexto_farmacia_y_mutual_igual_que_hoy(usar_perfil, clave):
    from app.services.intent_service import IntentService
    usar_perfil(clave)
    out = IntentService._con_contexto("m", "Nombre de pila (para saludar): Ana", "Horario: 9 a 18")
    assert out == ("m\n\n[DATOS DEL SOCIO]\nNombre de pila (para saludar): Ana"
                   "\n\n[INFORMACIÓN DE LA FARMACIA]\nHorario: 9 a 18\n" + _KB_INSTRUCCION)
```

Crear `tests/test_petshop.py`:

```python
"""
Petshop por capacidad (spec §6.2, `tests/test_petshop.py`): cada test de
petshop tiene su par de farmacia "igual que hoy". Los perfiles se eligen con la
fixture `usar_perfil` (tests/conftest.py).
"""

# ── Recetas: _formatear_productos (intent_service) ──────────────────────────────
_PIPETA = {"nombre": "PIPETA FRONTLINE PLUS PERRO 10-20KG", "precio": 15000.0, "estado": "disponible",
           "cantidad_visible": 4, "sku_id": "20", "requiere_receta": "si"}
_PIPETA_URGENTE = {**_PIPETA, "sku_id": "21", "requiere_receta": "ambiguo", "urgente": True}


def test_formatear_productos_petshop_no_marca_receta(usar_perfil):
    from app.services.intent_service import IntentService
    usar_perfil("petshop")
    txt = IntentService("")._formatear_productos([_PIPETA, _PIPETA_URGENTE])
    assert txt == (
        "1. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4) | ID: 20\n"
        "2. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | STOCK BAJO - ofrecer con urgencia | ID: 21")


def test_formatear_productos_farmacia_marca_receta_como_hoy(usar_perfil):
    from app.services.intent_service import IntentService
    usar_perfil("farmacia")
    txt = IntentService("")._formatear_productos([_PIPETA, _PIPETA_URGENTE])
    assert txt == (
        "1. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | REQUIERE RECETA | ID: 20\n"
        "2. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | STOCK BAJO - ofrecer con urgencia | REQUIERE RECETA | ID: 21")
```

- [ ] **Step 7: Correr los tests y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil.py tests/test_petshop.py -k "system_prompt_sale or singleton_de_intent or como_lo_llama_simulate or sin_parametro_vertical or deps_del_webhook or con_contexto_ or formatear_productos" -v`

Esperado: `7 failed, 3 passed`.
- `test_system_prompt_sale_del_perfil_en_los_tres_rubros`, `test_singleton_de_intent_sigue_al_perfil`, `test_get_intent_service_como_lo_llama_simulate` y `test_deps_del_webhook_arma_el_intent_con_el_perfil` fallan con `assert 'Mascotas del Oeste' in 'Sos el asistente virtual de Remedia.\n\nIDENTIDAD Y TONO: ...'`. El `IntentService` de hoy coacciona `petshop` a farmacia, y el que no recibe `vertical` queda en farmacia.
- `test_intent_service_sin_parametro_vertical_y_loguea_el_perfil` falla con `assert 'vertical' not in mappingproxy(...)`.
- `test_con_contexto_petshop_sin_socio_y_con_rotulo_del_comercio` falla porque sale `[DATOS DEL SOCIO]` y `[INFORMACIÓN DE LA FARMACIA]`.
- `test_formatear_productos_petshop_no_marca_receta` falla porque aparece `| REQUIERE RECETA`.
- Pasan los dos casos de `test_con_contexto_farmacia_y_mutual_igual_que_hoy` y `test_formatear_productos_farmacia_marca_receta_como_hoy`, que son las guardas de farmacia.

- [ ] **Step 8: IntentService toma el perfil (imports, `__init__`, `_system_prompt`)**

En `app/services/intent_service.py`, primero los imports. Agregar `get_perfil` arriba del comentario y del re-export que dejó la Task 2 (Step 5). Antes:

```python
# El prompt de farmacia vive en prompts.py, armado por bloques. Se re-exporta
# acá: tests/test_logic.py y otros lo importan de este módulo.
from app.services.prompts import SYSTEM_PROMPT  # noqa: F401
```

Después:

```python
from app.services.perfil import get_perfil

# El prompt de farmacia vive en prompts.py, armado por bloques. Se re-exporta
# acá: tests/test_logic.py y otros lo importan de este módulo.
from app.services.prompts import SYSTEM_PROMPT  # noqa: F401
```

Después, `__init__` y `_system_prompt` (07a1d7a:178-192). Reemplazar:

```python
    def __init__(self, anthropic_key: str, openai_key: str = "", provider: str = "anthropic",
                 vertical: str = "farmacia"):
        self._provider = provider if provider in ("anthropic", "openai") else "anthropic"
        self._vertical = vertical if vertical in ("farmacia", "mutual") else "farmacia"
        self._anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key) if anthropic_key else None
        self._openai = openai.AsyncOpenAI(api_key=openai_key) if openai_key else None
        logger.info(f"IntentService: vertical '{self._vertical}', proveedor primario '{self._provider}' "
                    f"(anthropic={'ok' if self._anthropic else 'no'}, openai={'ok' if self._openai else 'no'})")

    def _system_prompt(self) -> str:
        """Prompt según el vertical: farmacia (catálogo y venta) o mutual (información)."""
        if self._vertical == "mutual":
            from app.services.mutual_helper import SYSTEM_PROMPT_MUTUAL
            return SYSTEM_PROMPT_MUTUAL
        return SYSTEM_PROMPT
```

por:

```python
    def __init__(self, anthropic_key: str, openai_key: str = "", provider: str = "anthropic"):
        self._provider = provider if provider in ("anthropic", "openai") else "anthropic"
        self._anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key) if anthropic_key else None
        self._openai = openai.AsyncOpenAI(api_key=openai_key) if openai_key else None
        logger.info(f"IntentService: perfil '{get_perfil().clave}', proveedor primario '{self._provider}' "
                    f"(anthropic={'ok' if self._anthropic else 'no'}, openai={'ok' if self._openai else 'no'})")

    def _system_prompt(self) -> str:
        """Prompt del perfil de rubro activo. Se lee en cada llamada (nunca se
        guarda en la instancia): el singleton no queda con un perfil viejo."""
        return get_perfil().system_prompt
```

- [ ] **Step 9: `_con_contexto`, `_formatear_productos` y `get_intent_service`**

`_con_contexto` sigue siendo `@staticmethod`, porque `tests/test_degradation.py:42` la llama sobre la clase. Reemplazar (07a1d7a:335-340):

```python
        """Anexa bloques de contexto (datos del socio, base de conocimiento)."""
        if contexto_cliente:
            user_content += f"\n\n[DATOS DEL SOCIO]\n{contexto_cliente}"
        if contexto_kb:
            user_content += (
                f"\n\n[INFORMACIÓN DE LA FARMACIA]\n{contexto_kb}\n"
```

por:

```python
        """Anexa bloques de contexto (datos del socio, base de conocimiento).
        El de socio solo con la capacidad `socios`; la KB va con el rótulo del perfil."""
        p = get_perfil()
        if contexto_cliente and p.socios:
            user_content += f"\n\n[DATOS DEL SOCIO]\n{contexto_cliente}"
        if contexto_kb:
            user_content += (
                f"\n\n[{p.rotulo_kb}]\n{contexto_kb}\n"
```

La instrucción que sigue ("Usá esta información para responder si aplica...") no cambia.

En `_formatear_productos` (07a1d7a:347-361), reemplazar:

```python
        if not productos:
            return "Sin resultados en el catálogo."
        lines = []
```

por:

```python
        if not productos:
            return "Sin resultados en el catálogo."
        recetas = get_perfil().recetas
        lines = []
```

y reemplazar:

```python
            if p.get("requiere_receta") in ("si", "ambiguo"):
```

por:

```python
            if recetas and p.get("requiere_receta") in ("si", "ambiguo"):
```

En `get_intent_service` (07a1d7a:388-393), reemplazar:

```python
def get_intent_service(anthropic_key: str, openai_key: str = "", provider: str = "anthropic",
                       vertical: str = "farmacia") -> IntentService:
    global _instance
    if _instance is None:
        _instance = IntentService(anthropic_key, openai_key, provider, vertical)
    return _instance
```

por:

```python
def get_intent_service(anthropic_key: str, openai_key: str = "",
                       provider: str = "anthropic") -> IntentService:
    global _instance
    if _instance is None:
        _instance = IntentService(anthropic_key, openai_key, provider)
    return _instance
```

- [ ] **Step 10: `webhook._deps` sin `vertical`**

En `app/routers/webhook.py:257`, reemplazar:

```python
        "intent":  get_intent_service(s.anthropic_api_key, s.openai_api_key, s.llm_provider, s.vertical),
```

por:

```python
        "intent":  get_intent_service(s.anthropic_api_key, s.openai_api_key, s.llm_provider),
```

`simulate.py:90` no se toca: ya llama con tres argumentos.

Control: `grep -rn "vertical" app/services/intent_service.py app/routers/simulate.py` no devuelve nada, y `grep -n "s.vertical" app/routers/webhook.py` solo muestra la línea 1171 (`if _s.vertical == "mutual":`, que es de otra tarea, §4.8).

- [ ] **Step 11: Correr los tests y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil.py tests/test_petshop.py tests/test_degradation.py tests/test_logic.py::TestConfirmacionesFantasma::test_prompt_prohibe_reservas -v`
Esperado: todo `PASSED`. Incluye los 15 tests nuevos de esta tarea, los 8 de `test_degradation.py` (con `test_contexto_prompt_socio_y_kb`, que sigue viendo `[DATOS DEL SOCIO]` e `[INFORMACIÓN DE LA FARMACIA]` sin perfil seteado) y el import de `SYSTEM_PROMPT` desde `intent_service` de `test_logic.py:1372`.

- [ ] **Step 12: Suite completa**

Run: `.venv/Scripts/python -m pytest -q`
Esperado: `0 failed`. El total es el de la tarea anterior más los 15 tests nuevos de esta tarea (5 de prompt, 8 de `IntentService` y 2 de `test_petshop.py`). Ningún test existente cambia de expectativa.

Verificado en un sandbox con los 933 tests de base, Tasks 1-3 simuladas por contrato y sin sus tests: `948 passed`. Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1016 tests.

- [ ] **Step 13: Commit**

```bash
git add app/services/intent_service.py app/routers/webhook.py tests/test_perfil.py tests/test_petshop.py
git commit -m "IntentService lee el perfil en cada llamada: prompt, contexto de socio, rotulo de KB y marca de receta

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Vocabulario de audio por perfil

Spec: §4.1 (fila `sku_service.py:714-720, 742-755`) y §6.2 (Audio).

**Files:**
- Modify: `app/services/sku_service.py` (números de línea de `07a1d7a`; si una tarea anterior tocó `get_sku_service`/`reload_sku_service` (687-699), están corridos, y por eso cada edición se ubica por su texto):
  - `:26` imports (después de `from app.services.catalogo_enriquecido import ...`)
  - `:713-720` comentario y lista `MARCAS_AUDIO_BASE`
  - `:742-755` `vocabulario_audio`
- Sin cambio: `app/routers/webhook.py:846-850`. Sigue llamando `vocabulario_audio(deps["sku"])` y toma el perfil activo.
- Test: `tests/test_perfil.py`, agregando un bloque al final.

**Interfaces:**
- Consumes (Task 2, `app/services/perfil.py`; la fixture es de la Task 1):
  - `MARCAS_AUDIO_FARMACIA: tuple[str, ...]`, las 38 de hoy en el mismo orden.
  - `VocabularioAudio(prefijo: str, marcas_base: tuple[str, ...])`.
  - `Perfil.vocabulario_audio`, `get_perfil()` y `perfil_por_clave(clave)`.
  - Farmacia y mutual traen `("Consulta a una farmacia", MARCAS_AUDIO_FARMACIA)`; petshop trae `("Consulta a un petshop", ())`.
  - Fixture `usar_perfil`.
- Produces:
  - `def vocabulario_audio(sku_svc, max_chars: int = 650, perfil=None) -> str`. `perfil=None` usa `get_perfil()`.
  - `sku_service.MARCAS_AUDIO_BASE: list[str] = list(MARCAS_AUDIO_FARMACIA)`, por compatibilidad.

- [ ] **Step 1: Escribir los tests de audio (fallan)**

Agregar al final de `tests/test_perfil.py`:

```python


# ══════════════════════════════════════════════════════════════════════════════
# Task 5 — vocabulario de audio por perfil (spec §4.1, fila sku_service)
# ══════════════════════════════════════════════════════════════════════════════
import csv as _csv

# Lo que hoy (07a1d7a) devuelve vocabulario_audio para el catálogo de prueba de
# abajo: las 38 marcas de farmacia y después las del catálogo.
_VOCAB_FARMACIA_HOY = (
    "Consulta a una farmacia. Productos y marcas: Aveno, Atopix, Actron, Ibupirac, Ibuevanol, "
    "Tafirol, Bayaspirina, Buscapina, Sertal, Dermaglos, Isdin, La Roche-Posay, Eucerin, Cetaphil, "
    "Hyalu C, Bagovit, Lanzopral, Omeprazol, Holomagnesio, Curflex, Dioxaflex, Novalgina, Refrianex, "
    "Mejoral, Geniol, Aspirina, Uvasal, Sal de frutas Eno, Loratadina, Allegra, Cepage, Cassará, Bagó, "
    "Roemmers, Elea, Vichy, Avene, Bioderma, Royal Canin, Sanicat."
)


@pytest.fixture
def sku_mo(tmp_path):
    """SKUService de prueba con dos marcas de petshop."""
    from app.services.sku_service import SKUService
    path = tmp_path / "mo.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["SKU", "Nombre", "Precio", "Marca", "Laboratorio",
                    "Codigo_Barras_1", "Categoria", "Es_Medicamento"])
        w.writerow(["1", "ROYAL CANIN MEDIUM ADULT 15KG", "98000", "ROYAL CANIN", "ROYAL CANIN",
                    "7790001", "ALIMENTO PERROS", "false"])
        w.writerow(["2", "PIEDRAS SANICAT CLASSIC 4KG", "6200", "SANICAT", "SANICAT",
                    "7790002", "PIEDRAS SANITARIAS", "false"])
    return SKUService(str(path))


def test_marcas_de_audio_de_farmacia_en_el_perfil():
    from app.services.perfil import MARCAS_AUDIO_FARMACIA, perfil_por_clave
    from app.services.sku_service import MARCAS_AUDIO_BASE
    va = perfil_por_clave("farmacia").vocabulario_audio
    assert va.prefijo == "Consulta a una farmacia"
    assert va.marcas_base == MARCAS_AUDIO_FARMACIA
    assert len(va.marcas_base) == 38 and va.marcas_base[:3] == ("Aveno", "Atopix", "Actron")
    assert MARCAS_AUDIO_BASE == list(MARCAS_AUDIO_FARMACIA)       # compatibilidad
    assert perfil_por_clave("mutual").vocabulario_audio == va
    assert perfil_por_clave("petshop").vocabulario_audio.marcas_base == ()


def test_vocabulario_audio_farmacia_igual_que_hoy(usar_perfil, sku_mo):
    from app.services.sku_service import vocabulario_audio
    usar_perfil("farmacia")
    assert vocabulario_audio(sku_mo) == _VOCAB_FARMACIA_HOY


def test_vocabulario_audio_petshop(usar_perfil, sku_mo):
    from app.services.sku_service import vocabulario_audio
    usar_perfil("petshop")
    v = vocabulario_audio(sku_mo)
    assert v.startswith("Consulta a un petshop. Productos y marcas: ")
    assert "Royal Canin" in v and "Sanicat" in v
    assert "Atopix" not in v and "farmacia" not in v
    assert len(v) <= 650
    assert v == "Consulta a un petshop. Productos y marcas: Royal Canin, Sanicat."


def test_vocabulario_audio_con_perfil_explicito(usar_perfil, sku_mo):
    """El parámetro `perfil` gana sobre el del entorno."""
    from app.services.perfil import perfil_por_clave
    from app.services.sku_service import vocabulario_audio
    usar_perfil("petshop")
    assert vocabulario_audio(sku_mo, perfil=perfil_por_clave("farmacia")) == _VOCAB_FARMACIA_HOY


def test_vocabulario_audio_respeta_el_tope(usar_perfil, sku_mo):
    from app.services.sku_service import vocabulario_audio
    usar_perfil("farmacia")
    v = vocabulario_audio(sku_mo, max_chars=80)
    assert len(v) <= 80
    assert v == "Consulta a una farmacia. Productos y marcas: Aveno, Atopix, Actron, Ibupirac."
```

- [ ] **Step 2: Correr los tests y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil.py -k "vocabulario_audio or marcas_de_audio" -v`

Esperado: `2 failed, 3 passed`.
- `test_vocabulario_audio_petshop` falla con `AssertionError: assert False` en el `startswith`: sale `'Consulta a una farmacia. Productos y marcas: Aveno, ...'`.
- `test_vocabulario_audio_con_perfil_explicito` falla con `TypeError: vocabulario_audio() got an unexpected keyword argument 'perfil'`.
- Pasan `test_marcas_de_audio_de_farmacia_en_el_perfil`, `test_vocabulario_audio_farmacia_igual_que_hoy` y `test_vocabulario_audio_respeta_el_tope`, que son guardas de "igual que hoy".

- [ ] **Step 3: Las marcas salen del perfil**

En `app/services/sku_service.py`, imports (07a1d7a:25-26). Reemplazar:

```python
from app.models.sku import SKU
from app.services.catalogo_enriquecido import expandir_abreviaturas, tipos_mencionados
```

por:

```python
from app.models.sku import SKU
from app.services.catalogo_enriquecido import expandir_abreviaturas, tipos_mencionados
from app.services.perfil import MARCAS_AUDIO_FARMACIA, get_perfil
```

`perfil.py` no importa `sku_service`, así que no se arma un ciclo (spec §3.1).

Lista de marcas (07a1d7a:713-720). Reemplazar:

```python
# Marcas que más se piden por audio; se suman a las más frecuentes del catálogo.
MARCAS_AUDIO_BASE = [
    "Aveno", "Atopix", "Actron", "Ibupirac", "Ibuevanol", "Tafirol", "Bayaspirina", "Buscapina",
    "Sertal", "Dermaglos", "Isdin", "La Roche-Posay", "Eucerin", "Cetaphil", "Hyalu C",
    "Bagovit", "Lanzopral", "Omeprazol", "Holomagnesio", "Curflex", "Dioxaflex", "Novalgina",
    "Refrianex", "Mejoral", "Geniol", "Aspirina", "Uvasal", "Sal de frutas Eno", "Loratadina",
    "Allegra", "Cepage", "Cassará", "Bagó", "Roemmers", "Elea", "Vichy", "Avene", "Bioderma",
]
```

por:

```python
# Marcas que más se piden por audio; se suman a las más frecuentes del catálogo.
# Son del rubro: viven en el perfil (perfil.vocabulario_audio.marcas_base). Esta
# lista queda por compatibilidad y es la de farmacia.
MARCAS_AUDIO_BASE = list(MARCAS_AUDIO_FARMACIA)
```

`vocabulario_audio` (07a1d7a:742-755). Reemplazar:

```python
def vocabulario_audio(sku_svc, max_chars: int = 650) -> str:
    """Texto de guía para la transcripción: marcas que tiene que escribir bien."""
    vistas, nombres = set(), []
    for m in MARCAS_AUDIO_BASE + marcas_frecuentes(sku_svc):
        k = m.lower()
        if k not in vistas:
            vistas.add(k)
            nombres.append(m)
    out = "Consulta a una farmacia. Productos y marcas: "
```

por:

```python
def vocabulario_audio(sku_svc, max_chars: int = 650, perfil=None) -> str:
    """Texto de guía para la transcripción: marcas que tiene que escribir bien.
    El prefijo y las marcas fijas son del perfil de rubro (el activo si no se pasa)."""
    p = perfil or get_perfil()
    vistas, nombres = set(), []
    for m in list(p.vocabulario_audio.marcas_base) + marcas_frecuentes(sku_svc):
        k = m.lower()
        if k not in vistas:
            vistas.add(k)
            nombres.append(m)
    out = f"{p.vocabulario_audio.prefijo}. Productos y marcas: "
```

El resto de la función no cambia: el tope `max_chars` y el `rstrip(", ") + "."` siguen igual.

- [ ] **Step 4: Correr los tests y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil.py -k "vocabulario_audio or marcas_de_audio" -v`
Esperado: `5 passed`.

Run: `.venv/Scripts/python -m pytest tests/test_webhook_secuencias.py -k "vocabulario or audio" -v`
Esperado: todo `PASSED`. Incluye `test_vocabulario_audio_trae_marcas_del_catalogo` (farmacia: arranca con "Consulta a una farmacia", trae "Atopix" y "Vertiente") y `test_audio_kapso_se_transcribe_con_vocabulario_y_se_guarda` (el webhook sigue mandando "Atopix" y "Aveno" a Whisper).

- [ ] **Step 5: Suite completa**

Run: `.venv/Scripts/python -m pytest -q`
Esperado: `0 failed`. El total es el de Task 4 más los 5 tests nuevos de esta tarea. En el mismo sandbox: `953 passed`. Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1021 tests.

- [ ] **Step 6: Commit**

```bash
git add app/services/sku_service.py tests/test_perfil.py
git commit -m "Vocabulario de audio por perfil: prefijo y marcas fijas salen del rubro

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Recetas apagadas por capacidad (`recetas = False`)

Spec: §4.2 (menos la fila de `main.py`, que va en Task 12, y la de la foto de receta, que va en Task 10) y §6.2: los unitarios de "Recetas" de `test_petshop.py` con su par de farmacia y el grupo "Recetas" de `test_petshop_conversaciones.py`. La fila `_formatear_productos` (`intent_service.py:360-361`) ya la hace Task 4 con sus tests: acá no se repite.

Las líneas citadas son las de `07a1d7a`. Si una tarea anterior corrió el archivo, ubicar cada bloque por el texto de "Reemplazar" (es único en el archivo).

**Files:**
- Modify: `app/services/catalog_rules.py:11` (import), `:38-45` (`ORIGENES`), `:48-51` (`explicar_receta`)
- Modify: `app/services/checkout_helper.py:581-588` (`necesita_receta`), `:1795-1798` (`texto_alternativas`). El import de `get_perfil` ya lo puso Task 3 (antes de `from app.services.sku_service import requiere_derivacion`).
- Modify: `app/routers/webhook.py:1422-1425` (`pide_receta_nube`), `:2331-2347` (adicionales: `_top2` y `_top_rec`). El import de `get_perfil` ya lo puso Task 3 (después de `from app.config import get_settings`).
- Sin cambio de código (quedan apagados por el gate de `necesita_receta` y lo prueban los tests): `checkout_helper.py:1256` (`confirmar_pedido` paso 1), `:591-664` (`derivar_si_receta`), `:1809-1819` (`referencia_ambigua_bloquea`), `webhook.py:555` (`_sumar_productos_nuevos`), `:1683-1686`, `:2273` (agregar al pedido), `:2490-2497` (`quitar_receta_inventada` pasa a correr con todo producto ofrecido con precio), `simulate.py:287, 509`; y `catalog_store.py:57-65` (`_fila`), que pasa por `explicar_receta`.
- Test: Modify `tests/test_petshop.py` (lo crea Task 4): se agrega una sección al final.
- Test: Create `tests/test_petshop_conversaciones.py`: encabezado, helpers y grupo "Recetas". Las tareas siguientes agregan sus grupos al final y reusan los helpers.

**Interfaces:**
- Consumes: `get_perfil() -> Perfil` con `recetas: bool` (Task 2); fixture `usar_perfil(clave, comercio=None) -> Perfil` (Task 1); `from app.services.perfil import get_perfil` a nivel de módulo en `checkout_helper.py` y `webhook.py` (Task 3); `entorno`, `_msg` y `PHONE` de `tests/test_webhook_secuencias.py`.
- Produces:
  - `app/services/catalog_rules.py`: `ORIGENES["sin_recetas"] = "Este comercio no vende con receta"`; `explicar_receta(category, rubro, subrubro, name, barcodes=(), referencia=None) -> tuple[str, str]` devuelve `("no", "sin_recetas")` sin recetas, antes de `es_venta_libre` y del import perezoso de `receta_referencia.buscar`.
  - `app/services/checkout_helper.py`: `necesita_receta(sku_svc, sku_id: str, modo: str) -> bool` da `False` sin recetas; `texto_alternativas(alternativas: list[dict]) -> str` no agrega `" (requiere receta)"` sin recetas.
  - `tests/test_petshop_conversaciones.py`: `_catalogo_pipeta_mo() -> SKUService` (pipeta `20` en "Medicamentos Bajo Receta" con `requiere_receta="si"` y Dog Chow `30`), `_intenciones_perf(deps) -> list[str]` (la intención de cada mensaje, de `perf.record`) y `_texto_llego_al_modelo(deps, texto) -> bool`.

- [ ] **Step 1: Escribir los tests de `explicar_receta` y de la fila del catálogo (fallan)**

Agregar al final de `tests/test_petshop.py` (los nombres `_PIPETA_ERP` y `_item_medicamento` no chocan con `_PIPETA`/`_PIPETA_URGENTE` de Task 4):

```python
# ══════════════════════════════════════════════════════════════════════════════
# Recetas (recetas = False)
# ══════════════════════════════════════════════════════════════════════════════
import pytest

from app.services.sku_service import SKUService

_PIPETA_ERP = ("MEDICAMENTOS", "PERROS", "ANTIPARASITARIOS", "Pipeta Frontline 10-20kg")


def test_explicar_receta_petshop_no_vende_con_receta(usar_perfil):
    from app.services.catalog_rules import ORIGENES, explicar_receta
    usar_perfil("petshop")
    # La referencia diría "si": el perfil sin recetas gana antes de consultarla.
    assert explicar_receta(*_PIPETA_ERP, referencia=lambda b: "si") == ("no", "sin_recetas")
    assert ORIGENES["sin_recetas"] == "Este comercio no vende con receta"


def test_explicar_receta_farmacia_igual_que_hoy(usar_perfil):
    from app.services.catalog_rules import explicar_receta
    usar_perfil("farmacia")
    assert explicar_receta(*_PIPETA_ERP, referencia=lambda b: None) == ("ambiguo", "sin_referencia")
    assert explicar_receta("Medicamentos Bajo Receta", "", "", "Pipeta Frontline 10-20kg",
                           referencia=lambda b: None) == ("si", "categoria_bajo_receta")


def _item_medicamento():
    from app.models.sync import CatalogItemIn
    return CatalogItemIn(external_id="9001", hash="a" * 64,
                         name="PIPETA FRONTLINE PLUS PERRO 10-20KG", category="MEDICAMENTOS",
                         rubro="PERROS", subrubro="ANTIPARASITARIOS",
                         barcodes=["7790000000001"])


def test_fila_del_catalogo_petshop_no_requiere_receta(usar_perfil, monkeypatch):
    from app.services import receta_referencia
    from app.services.catalog_store import _fila
    usar_perfil("petshop")
    monkeypatch.setattr(receta_referencia, "_MAPA", {"7790000000001": "si"})
    fila = _fila("mascotas-oeste", _item_medicamento(), "mercurio")
    assert fila[17] == "no"          # requiere_receta ($18 del upsert)


def test_fila_del_catalogo_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    from app.services import receta_referencia
    from app.services.catalog_store import _fila
    usar_perfil("farmacia")
    monkeypatch.setattr(receta_referencia, "_MAPA", {})
    fila = _fila("farmacia-centro", _item_medicamento(), "observer-gestion")
    assert fila[17] == "ambiguo"
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py::test_explicar_receta_petshop_no_vende_con_receta tests/test_petshop.py::test_explicar_receta_farmacia_igual_que_hoy tests/test_petshop.py::test_fila_del_catalogo_petshop_no_requiere_receta tests/test_petshop.py::test_fila_del_catalogo_farmacia_igual_que_hoy -v`

Esperado: `2 failed, 2 passed`.
- `test_explicar_receta_petshop_no_vende_con_receta` falla con `AssertionError: assert ('si', 'referencia') == ('no', 'sin_recetas')`: hoy manda la referencia.
- `test_fila_del_catalogo_petshop_no_requiere_receta` falla con `AssertionError: assert 'si' == 'no'`.
- Pasan los dos pares de farmacia (`('ambiguo', 'sin_referencia')`, `('si', 'categoria_bajo_receta')` y `'ambiguo'` en la fila): son las guardas.

- [ ] **Step 3: Gate de recetas en `catalog_rules`**

En `app/services/catalog_rules.py:11`, reemplazar:

```python
from app.services.sku_service import es_venta_libre, _categoria_sin_receta
```

por:

```python
from app.services.perfil import get_perfil
from app.services.sku_service import es_venta_libre, _categoria_sin_receta
```

(`perfil.py` no importa `catalog_rules` ni `sku_service`: no hay ciclo.)

En `ORIGENES` (`:44-45`), reemplazar:

```python
    "otro": "No es medicamento",
}
```

por:

```python
    "otro": "No es medicamento",
    # Perfil sin recetas (petshop): nada se marca; el panel muestra el porqué.
    "sin_recetas": "Este comercio no vende con receta",
}
```

En `explicar_receta` (`:50-51`), reemplazar:

```python
    """(flag, origen) con el mismo orden que `derivar_requiere_receta`."""
    if es_venta_libre(name):
```

por:

```python
    """(flag, origen) con el mismo orden que `derivar_requiere_receta`."""
    # Rubro sin recetas (petshop): antes de la lista OTC y de la referencia,
    # que ni se importa. Un rubro de Mercurio con "medicament" quedaba
    # "ambiguo" y en modo conservador derivaba la pipeta.
    if not get_perfil().recetas:
        return "no", "sin_recetas"
    if es_venta_libre(name):
```

`derivar_requiere_receta` (`catalog_store._fila` en cada upsert, `recalcular_catalogo`) y `receta_marcas.py:51` pasan por acá: no se tocan.

- [ ] **Step 4: Correr y ver que pasan**

Run: el mismo comando del Step 2.
Esperado: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/services/catalog_rules.py tests/test_petshop.py
git commit -m "Recetas por perfil: explicar_receta no marca nada en un rubro sin recetas

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Escribir los tests de `necesita_receta` y del grupo "Recetas" por el webhook (fallan)**

Agregar al final de `tests/test_petshop.py`:

```python
def _catalogo_con_receta():
    base = {"hash": "a" * 64, "troquel": None, "brand": "", "drug": None, "form": None,
            "rubro": "PERROS", "subrubro": "", "therapeutic_actions": [], "stock": 5,
            "visible": True, "active": True, "source": "mercurio"}
    return SKUService.from_rows([
        {**base, "external_id": "20", "name": "PIPETA FRONTLINE PLUS PERRO 10-20KG",
         "price": 25000, "barcodes": ["7790000000020"],
         "category": "Medicamentos Bajo Receta", "requiere_receta": "si"},
        {**base, "external_id": "21", "name": "DRONTAL PLUS PERRO X 2", "price": 9000,
         "barcodes": [], "category": "MEDICAMENTOS", "requiere_receta": "ambiguo"},
    ])


@pytest.mark.parametrize("modo", ["conservador", "estricto"])
def test_necesita_receta_petshop_nunca(usar_perfil, modo):
    from app.services.checkout_helper import necesita_receta
    usar_perfil("petshop")
    sku = _catalogo_con_receta()
    assert necesita_receta(sku, "20", modo) is False
    assert necesita_receta(sku, "21", modo) is False


def test_necesita_receta_farmacia_igual_que_hoy(usar_perfil):
    from app.services.checkout_helper import necesita_receta
    usar_perfil("farmacia")
    sku = _catalogo_con_receta()
    assert necesita_receta(sku, "20", "conservador") is True
    assert necesita_receta(sku, "20", "estricto") is True
    assert necesita_receta(sku, "21", "conservador") is True
    assert necesita_receta(sku, "21", "estricto") is False
```

Crear `tests/test_petshop_conversaciones.py` (en el pendiente con estado `esperando_pago` el "agregame" va por el flujo normal, `webhook.py:2269-2291`, que es el que arma "¡Listo, lo sumé!"; con `esperando_confirmacion` iría por el paso 1c, que dice "¡Listo! Tu pedido queda así:"):

```python
"""
Vertical petshop punta a punta por el webhook, con dependencias falsas.

Reusa `entorno`, `_msg` y `PHONE` de test_webhook_secuencias.py. El perfil se
elige con `usar_perfil` ANTES de armar el entorno.
"""
import pytest

from app.routers import webhook as wh
from app.services.sku_service import SKUService
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (entorno es fixture)


def _catalogo_pipeta_mo():
    """Catálogo de MO con una pipeta marcada "Medicamentos Bajo Receta"."""
    base = {"hash": "a" * 64, "troquel": None, "brand": "", "drug": None, "form": None,
            "rubro": "PERROS", "subrubro": "", "therapeutic_actions": [], "stock": 5,
            "visible": True, "active": True, "source": "mercurio"}
    return SKUService.from_rows([
        {**base, "external_id": "20", "name": "PIPETA FRONTLINE PLUS PERRO 10-20KG",
         "price": 25000, "barcodes": ["7790000000020"],
         "category": "Medicamentos Bajo Receta", "requiere_receta": "si"},
        {**base, "external_id": "30", "name": "DOG CHOW ADULTO RAZAS MEDIANAS 3KG",
         "price": 9800, "barcodes": ["7790000000030"],
         "category": "ALIMENTOS", "requiere_receta": "no"},
    ])


def _intenciones_perf(deps):
    """Intención de cada mensaje procesado (la registra perf.record en el finally)."""
    return [a[0]["intencion"] for n, a, k in deps["perf"].llamadas if n == "record"]


def _texto_llego_al_modelo(deps, texto):
    return any(v[0] in ("rapido", "procesar") and v[1] == texto for v in deps["intent"].vistos)


# ══════════════════════════════════════════════════════════════════════════════
# Recetas
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("clave,deriva", [("petshop", False), ("farmacia", True)])
async def test_si_a_una_pipeta_bajo_receta(usar_perfil, entorno, clave, deriva):
    usar_perfil(clave)
    deps = entorno()
    deps["sku"] = _catalogo_pipeta_mo()
    await deps["session"].set_pending(PHONE, sku_id="20",
                                      sku_nombre="PIPETA FRONTLINE PLUS PERRO 10-20KG",
                                      precio=25000.0, cantidad=1, opciones=[])
    await wh.procesar_mensajes([_msg("si")])
    enviado = deps["wa"].enviados[-1].lower()
    s = await deps["session"].get(PHONE)
    if deriva:
        assert "derivado_receta" in _intenciones_perf(deps) and s["estado"] == "operador"
        return
    assert "derivado_receta" not in _intenciones_perf(deps)
    assert "receta" not in enviado and "medicamento" not in enviado
    assert s["estado"] == "esperando_entrega" and "retiro" in enviado


@pytest.mark.parametrize("clave,deriva", [("petshop", False), ("farmacia", True)])
async def test_agregame_la_pipeta_con_un_alimento_pendiente(usar_perfil, entorno, clave, deriva):
    usar_perfil(clave)
    txt = "agregame la pipeta frontline"
    deps = entorno({txt: {"intencion": "pedido", "entidad_producto": "pipeta frontline",
                          "agregar_al_pedido": True,
                          "respuesta": "Te sumo la Pipeta Frontline Plus Perro 10-20kg a $25.000."}})
    deps["sku"] = _catalogo_pipeta_mo()
    await deps["session"].set_pending(PHONE, sku_id="30",
                                      sku_nombre="DOG CHOW ADULTO RAZAS MEDIANAS 3KG",
                                      precio=9800.0, cantidad=1, opciones=[])
    await deps["session"].set_entrega(PHONE, "retiro", None)
    await deps["session"].set_estado(PHONE, "esperando_pago")      # link ya enviado
    await wh.procesar_mensajes([_msg(txt)])
    if deriva:
        assert _intenciones_perf(deps)[-1] == "derivado_receta"
        return
    enviado = deps["wa"].enviados[-1]
    assert enviado.startswith("¡Listo, lo sumé! Tu pedido queda así:")
    assert "PIPETA FRONTLINE PLUS PERRO 10-20KG" in enviado and "receta" not in enviado.lower()
    assert _intenciones_perf(deps)[-1] == "item_agregado"
    s = await deps["session"].get(PHONE)
    assert [i["sku_id"] for i in s["pending_items"]] == ["30", "20"]


async def test_receta_inventada_por_el_modelo_se_saca_en_petshop(usar_perfil, entorno):
    usar_perfil("petshop")
    txt = "tenés pipeta frontline para perro de 10 a 20 kg?"
    deps = entorno({txt: {
        "intencion": "consulta_stock", "entidad_producto": "pipeta frontline", "por_sintoma": False,
        "respuesta": ("Tengo la Pipeta Frontline Plus Perro 10-20kg a $25.000. "
                      "Ojo que va con receta del veterinario. ¿La querés?")}})
    deps["sku"] = _catalogo_pipeta_mo()
    await wh.procesar_mensajes([_msg(txt)])
    enviado = deps["wa"].enviados[-1]
    assert "receta" not in enviado.lower()
    assert "$25.000" in enviado and "¿La querés?" in enviado
    assert (await deps["session"].get(PHONE)).get("pending_sku_id") == "20"
```

- [ ] **Step 7: Correr y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py::test_necesita_receta_petshop_nunca tests/test_petshop.py::test_necesita_receta_farmacia_igual_que_hoy tests/test_petshop_conversaciones.py::test_si_a_una_pipeta_bajo_receta tests/test_petshop_conversaciones.py::test_agregame_la_pipeta_con_un_alimento_pendiente tests/test_petshop_conversaciones.py::test_receta_inventada_por_el_modelo_se_saca_en_petshop -v`

Esperado: `5 failed, 3 passed`.
- `test_necesita_receta_petshop_nunca[conservador]` y `[estricto]` fallan con `assert True is False`.
- `test_si_a_una_pipeta_bajo_receta[petshop-False]` falla con `assert 'derivado_receta' not in ['derivado_receta']`.
- `test_agregame_la_pipeta_con_un_alimento_pendiente[petshop-False]` falla en el `startswith`: lo enviado es "El PIPETA FRONTLINE PLUS PERRO 10-20KG requiere receta 🩺. Te paso con alguien del equipo con todo tu pedido…".
- `test_receta_inventada_por_el_modelo_se_saca_en_petshop` falla con `'receta' is contained here: ese producto requiere receta 🩺…` (deriva en vez de ofrecer).
- Pasan `test_necesita_receta_farmacia_igual_que_hoy` y los dos casos `[farmacia-True]`.

- [ ] **Step 8: Gate de recetas en `necesita_receta`**

En `app/services/checkout_helper.py:581-584`, reemplazar:

```python
def necesita_receta(sku_svc, sku_id: str, modo: str) -> bool:
    """True si el producto pendiente requiere derivación por receta."""
    if not sku_id:
        return False
```

por:

```python
def necesita_receta(sku_svc, sku_id: str, modo: str) -> bool:
    """True si el producto pendiente requiere derivación por receta."""
    # Llave única de la derivación por receta. Sin recetas (petshop) quedan
    # apagados sin tocarlos: confirmar_pedido, derivar_si_receta,
    # _sumar_productos_nuevos, referencia_ambigua_bloquea, "agregame" y
    # simulate; y quitar_receta_inventada corre con todo producto ofrecido.
    if not get_perfil().recetas:
        return False
    if not sku_id:
        return False
```

`get_perfil` ya está importado en este módulo (Task 3). Control: `grep -n "^from app.services.perfil import get_perfil" app/services/checkout_helper.py` devuelve una línea.

- [ ] **Step 9: Correr y ver que pasan**

Run: el mismo comando del Step 7.
Esperado: `8 passed`. `quitar_receta_inventada` (`webhook.py:2490-2497`) no se tocó: ahora corre porque ningún producto ofrecido "necesita receta", y saca "Ojo que va con receta del veterinario." dejando el precio y "¿La querés?".

- [ ] **Step 10: Commit**

```bash
git add app/services/checkout_helper.py tests/test_petshop.py tests/test_petshop_conversaciones.py
git commit -m "Recetas por perfil: necesita_receta apaga la derivacion por receta en un rubro sin recetas

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 11: Escribir los tests de `texto_alternativas` (fallan)**

Agregar al final de `tests/test_petshop.py`:

```python
_ALTS_RECETA = [
    {"sku_id": "20", "nombre": "Pipeta Frontline Plus Perro 10-20kg", "precio": 25000.0,
     "requiere_receta": "si"},
    {"sku_id": "30", "nombre": "Dog Chow Adulto 3kg", "precio": 9800.0, "requiere_receta": "no"},
]


def test_texto_alternativas_petshop_sin_requiere_receta(usar_perfil):
    from app.services.checkout_helper import texto_alternativas
    usar_perfil("petshop")
    t = texto_alternativas(_ALTS_RECETA)
    assert "receta" not in t.lower()
    assert "• Pipeta Frontline Plus Perro 10-20kg — $25,000.00\n" in t


def test_texto_alternativas_farmacia_igual_que_hoy(usar_perfil):
    from app.services.checkout_helper import texto_alternativas
    usar_perfil("farmacia")
    t = texto_alternativas(_ALTS_RECETA)
    assert "• Pipeta Frontline Plus Perro 10-20kg — $25,000.00 (requiere receta)\n" in t
    assert "• Dog Chow Adulto 3kg — $9,800.00\n" in t
```

- [ ] **Step 12: Correr y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py::test_texto_alternativas_petshop_sin_requiere_receta tests/test_petshop.py::test_texto_alternativas_farmacia_igual_que_hoy -v`

Esperado: `1 failed, 1 passed`. `test_texto_alternativas_petshop_sin_requiere_receta` falla con `AssertionError: assert 'receta' not in 'lo que tengo disponible:\n• pipeta frontline plus perro 10-20kg — $25,000.00 (requiere receta)…'`. Pasa el par de farmacia.

- [ ] **Step 13: Gate de recetas en `texto_alternativas`**

En `app/services/checkout_helper.py:1795-1798`, reemplazar:

```python
def texto_alternativas(alternativas: list[dict]) -> str:
    lineas = []
    for a in alternativas:
        receta = " (requiere receta)" if a.get("requiere_receta") == "si" else ""
```

por:

```python
def texto_alternativas(alternativas: list[dict]) -> str:
    _rec_on = get_perfil().recetas      # sin recetas, nunca "(requiere receta)"
    lineas = []
    for a in alternativas:
        receta = " (requiere receta)" if _rec_on and a.get("requiere_receta") == "si" else ""
```

- [ ] **Step 14: Correr y ver que pasan**

Run: el mismo comando del Step 12, más la guarda existente: `.venv/Scripts/python -m pytest tests/test_petshop.py::test_texto_alternativas_petshop_sin_requiere_receta tests/test_petshop.py::test_texto_alternativas_farmacia_igual_que_hoy "tests/test_logic.py::TestAlternativasSinPrecio::test_texto_alternativas_lo_reconoce_el_matcher_de_precios" -v`
Esperado: `3 passed`.

- [ ] **Step 15: Commit**

```bash
git add app/services/checkout_helper.py tests/test_petshop.py
git commit -m "Recetas por perfil: texto_alternativas sin \"(requiere receta)\" en un rubro sin recetas

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 16: Escribir el test del adicional con receta (falla)**

Agregar al final de `tests/test_petshop_conversaciones.py`:

```python
@pytest.mark.parametrize("clave,deriva", [("petshop", False), ("farmacia", True)])
async def test_adicional_con_receta(usar_perfil, entorno, clave, deriva):
    usar_perfil(clave)
    txt = "quiero el alimento Dog Chow 3kg y una pipeta frontline"
    deps = entorno({txt: {
        "intencion": "pedido", "entidad_producto": "alimento dog chow 3kg",
        "entidades_adicionales": ["pipeta frontline"],
        "respuesta": "Tengo el Dog Chow Adulto Razas Medianas 3 kg a $9.800. ¿Te lo preparo?"}})
    deps["sku"] = _catalogo_pipeta_mo()
    await wh.procesar_mensajes([_msg(txt)])
    enviado = deps["wa"].enviados[-1]
    if deriva:
        assert _intenciones_perf(deps)[-1] == "derivado_receta"
        return
    assert "Sobre lo demás que me pediste:" in enviado
    assert "• pipeta frontline: PIPETA FRONTLINE PLUS PERRO 10-20KG — $25,000.00" in enviado
    assert "receta" not in enviado.lower()
    s = await deps["session"].get(PHONE)
    assert [e["sku_id"] for e in s.get("extras_ofrecidos") or []] == ["20"]
    assert s["estado"] != "operador"
    assert _intenciones_perf(deps)[-1] != "derivado_receta"
```

- [ ] **Step 17: Correr y ver que falla**

Run: `.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py::test_adicional_con_receta -v`

Esperado: `1 failed, 1 passed`. `[petshop-False]` falla con `assert '• pipeta frontline: PIPETA FRONTLINE PLUS PERRO 10-20KG — $25,000.00' in 'Tengo el Dog Chow … Sobre lo demás que me pediste:\n\n\nSi querés sumar alguno al pedido, decime cuál 🙂'`: `_top2` descarta la pipeta por su flag, `_top_rec` la toma, `derivar_si_receta` ya no deriva y el adicional desaparece en silencio. `[farmacia-True]` pasa.

- [ ] **Step 18: Gate de recetas en los adicionales del webhook**

En `app/routers/webhook.py:2331-2333`, reemplazar:

```python
                if _extras:
                    from app.services.sku_service import nombre_coincide
                    _lineas_extra = []
```

por:

```python
                if _extras:
                    from app.services.sku_service import nombre_coincide
                    # Sin recetas (petshop), el flag "si"/"ambiguo" de una fila no
                    # aparta al adicional: si no, desaparecía en silencio.
                    _rec_on = get_perfil().recetas
                    _lineas_extra = []
```

y en `:2344-2347`, reemplazar:

```python
                        _top2 = next((r for r in _r2 if r.get("vendible")
                                      and r.get("requiere_receta") not in ("si", "ambiguo")), None)
                        _top_rec = next((r for r in _r2 if r.get("vendible")
                                         and r.get("requiere_receta") in ("si", "ambiguo")), None)
```

por:

```python
                        _top2 = next((r for r in _r2 if r.get("vendible") and not (
                            _rec_on and r.get("requiere_receta") in ("si", "ambiguo"))), None)
                        _top_rec = next((r for r in _r2 if r.get("vendible") and _rec_on
                                         and r.get("requiere_receta") in ("si", "ambiguo")), None)
```

`get_perfil` ya está importado en el webhook (Task 3). Control: `grep -n "^from app.services.perfil import get_perfil" app/routers/webhook.py` devuelve una línea.

- [ ] **Step 19: Correr y ver que pasa**

Run: `.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py::test_adicional_con_receta "tests/test_webhook_secuencias.py::test_maria_respuesta_limpia" -v`
Esperado: `3 passed`.

- [ ] **Step 20: Commit**

```bash
git add app/routers/webhook.py tests/test_petshop_conversaciones.py
git commit -m "Recetas por perfil: los adicionales no se apartan por el flag de receta en un rubro sin recetas

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 21: Escribir el test de "receta cargada en el sistema" (falla)**

Agregar al final de `tests/test_petshop_conversaciones.py`:

```python
@pytest.mark.parametrize("clave,deriva", [("petshop", False), ("farmacia", True)])
async def test_receta_cargada_en_el_sistema(usar_perfil, entorno, clave, deriva):
    usar_perfil(clave)
    txt = "tengo la receta del veterinario cargada en el sistema"
    deps = entorno({txt: {"intencion": "social",
                          "respuesta": "¡Dale! Contame qué producto necesitás y te lo busco 🐾"}})
    await wh.procesar_mensajes([_msg(txt)])
    enviado = deps["wa"].enviados[-1]
    s = await deps["session"].get(PHONE)
    if deriva:
        assert "sistema de recetas" in enviado and s["derivada_motivo"] == "receta_nube"
        return
    assert "sistema de recetas" not in enviado and "🩺" not in enviado
    assert _texto_llego_al_modelo(deps, txt)
    assert s.get("estado") != "operador"
```

- [ ] **Step 22: Correr y ver que falla**

Run: `.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py::test_receta_cargada_en_el_sistema -v`

Esperado: `1 failed, 1 passed`. `[petshop-False]` falla con `'sistema de recetas' is contained here`: sale "¡Dale! Eso lo revisa alguien del equipo en el sistema de recetas 🩺 En un momento te contactamos.". `[farmacia-True]` pasa.

- [ ] **Step 23: Gate de recetas en `pide_receta_nube`**

En `app/routers/webhook.py:1424-1425`, reemplazar:

```python
            # no volver dejó a una clienta esperando hasta el cierre (19/8).
            if pide_receta_nube(texto):
```

por:

```python
            # no volver dejó a una clienta esperando hasta el cierre (19/8).
            # Rubro sin recetas (petshop): el mensaje sigue al modelo.
            if get_perfil().recetas and pide_receta_nube(texto):
```

- [ ] **Step 24: Correr y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py tests/test_petshop_conversaciones.py -v`
Esperado: todo `PASSED`. Son los 18 tests de esta tarea (9 en `test_petshop.py` y 9 en `test_petshop_conversaciones.py`) más los que ya tenía `test_petshop.py` de tareas anteriores.

- [ ] **Step 25: Commit**

```bash
git add app/routers/webhook.py tests/test_petshop_conversaciones.py
git commit -m "Recetas por perfil: \"receta cargada en el sistema\" va al modelo en un rubro sin recetas

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 26: Suite completa**

Run: `.venv/Scripts/python -m pytest -q`
Esperado: `0 failed`. El total es el de la tarea anterior más los 18 tests nuevos de esta tarea. Ningún test existente cambia de expectativa (siguen verdes, sin edición, `test_logic.py:2321-2328` de `pide_receta_nube`, `test_logic.py:3343` de `texto_alternativas`, `test_receta_marcas.py:224, 261, 277` y los de receta de `test_webhook_secuencias.py`).

Verificado en un sandbox sobre `07a1d7a` (con `get_perfil` y `usar_perfil` simulados por contrato y sin los tests de las Tasks 1-5): la suite completa queda en verde después de esta tarea. Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1039 tests.

---

### Task 7: Links con dominio propio y `links_como_receta`

Spec: §4.3 completo y §6.2: los tests de "Links" de `test_petshop.py` (con la regresión de farmacia escrita **antes** del cambio) y la fila "Links" de `test_petshop_conversaciones.py`.

Las líneas citadas son las de `07a1d7a`. Si una tarea anterior corrió el archivo, ubicar cada bloque por el texto de "Reemplazar".

**Files:**
- Modify: `app/services/checkout_helper.py:11` (import de `urlsplit`), `:568-578` (`contiene_link`; se agregan `_SLD_GENERICOS` y `dominio_propio` justo antes)
- Modify: `app/routers/webhook.py:60` (import de `dominio_propio`), `:1155-1156` (bloque "Receta/bono enviado como LINK")
- Test: Modify `tests/test_petshop.py` y `tests/test_petshop_conversaciones.py` (se agrega una sección "Links" al final de cada uno)

**Interfaces:**
- Consumes: `get_perfil() -> Perfil` con `links_como_receta: bool` (Task 2); `get_settings().public_base_url` (`app/config.py:58`); fixture `usar_perfil` (Task 1); `_intenciones_perf(deps)` y `_texto_llego_al_modelo(deps, texto)` de `tests/test_petshop_conversaciones.py` (Task 6); `entorno`, `_msg` y `PHONE`.
- Produces:
  - `app/services/checkout_helper.py`: `def dominio_propio(base_url: str) -> str` y `def contiene_link(t: str, dominio_propio: str = "") -> bool` (contrato); además `_SLD_GENERICOS = {"com", "net", "org", "gob", "gov", "edu", "co"}` (privado). `/pay/` se excluye siempre; el dominio propio, solo si no está vacío y aparece en el link; se saca el literal `remedia.ar`.
  - `app/routers/webhook.py`: `if get_perfil().links_como_receta and contiene_link(texto, dominio_propio(get_settings().public_base_url)):`.
  - `tests/test_petshop_conversaciones.py`: fixture `farmacia_remedia` (farmacia con `PUBLIC_BASE_URL=https://farmacia.remedia.ar`).

- [ ] **Step 1: Escribir la regresión de farmacia ANTES del cambio (pasa hoy)**

Hoy no hay tests de `contiene_link`. Agregar al final de `tests/test_petshop.py` (solo casos que valen antes y después: sin `remedia.ar`, porque `contiene_link(t)` sin dominio deja de excluirlo):

```python
# ══════════════════════════════════════════════════════════════════════════════
# Links (links_como_receta = False)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("texto,esperado", [
    ("te mando la receta https://drive.google.com/file/d/abc/view", True),
    ("ahí va receta_ana.jpg", True),
    ("www.fotos.com/receta", True),
    ("el link https://pagos.ejemplo.com/pay/abc123 no me abre", False),
    ("hola, tenés pipetas?", False),
])
def test_contiene_link_regresion_farmacia(texto, esperado):
    """Regresión escrita ANTES del cambio de firma: pasa con el código de hoy
    y sigue pasando después (sin dominio propio)."""
    from app.services.checkout_helper import contiene_link
    assert contiene_link(texto) is esperado
```

Agregar al final de `tests/test_petshop_conversaciones.py` (por el webhook, con `PUBLIC_BASE_URL` como el de Railway):

```python
# ══════════════════════════════════════════════════════════════════════════════
# Links
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def farmacia_remedia(usar_perfil, monkeypatch):
    """La farmacia como está en Railway: PUBLIC_BASE_URL bajo remedia.ar (§4.3).
    Se pisa el atributo del Settings cacheado, después de elegir el perfil:
    usar_perfil (Task 1) no recrea Settings y un setenv no llegaría al webhook."""
    from app.config import get_settings
    perfil = usar_perfil("farmacia")
    monkeypatch.setattr(get_settings(), "public_base_url", "https://farmacia.remedia.ar")
    return perfil


@pytest.mark.parametrize("texto", [
    "te mando la receta https://drive.google.com/file/d/abc/view",
    "ahí va receta_ana.jpg",
    "www.fotos.com/receta",
])
async def test_farmacia_link_externo_deriva_como_receta(farmacia_remedia, entorno, texto):
    """Regresión escrita ANTES del cambio: pasa con el código de hoy."""
    deps = entorno()
    await wh.procesar_mensajes([_msg(texto)])
    assert deps["wa"].enviados[-1].startswith("Recibí tu link 🙌")
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "receta_link"


@pytest.mark.parametrize("texto", [
    "el link https://farmacia.remedia.ar/pay/abc123 no me abre",
    "vi esto en https://www.remedia.ar/promos",
    "y esto? https://cerca.remedia.ar/x",
])
async def test_farmacia_links_propios_no_derivan(farmacia_remedia, entorno, texto):
    """Regresión escrita ANTES del cambio: pasa con el código de hoy."""
    deps = entorno()
    await wh.procesar_mensajes([_msg(texto)])
    assert not any(t.startswith("Recibí tu link") for t in deps["wa"].enviados)
    assert (await deps["session"].get(PHONE)).get("derivada_motivo") != "receta_link"
```

- [ ] **Step 2: Correr la regresión y ver que pasa con el código de hoy**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py::test_contiene_link_regresion_farmacia tests/test_petshop_conversaciones.py::test_farmacia_link_externo_deriva_como_receta tests/test_petshop_conversaciones.py::test_farmacia_links_propios_no_derivan -v`
Esperado: `11 passed`. Si alguno falla acá, el problema está en el test, no en el código: corregirlo antes de seguir.

- [ ] **Step 3: Commit de la regresión**

```bash
git add tests/test_petshop.py tests/test_petshop_conversaciones.py
git commit -m "Links: regresion de farmacia antes de cambiar contiene_link

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Escribir los tests de `dominio_propio` y de `contiene_link` con dominio (fallan)**

Agregar al final de `tests/test_petshop.py`:

```python
@pytest.mark.parametrize("base_url,esperado", [
    ("https://cerca.remedia.ar", "remedia.ar"),
    ("https://farmacia.remedia.ar/", "remedia.ar"),
    ("HTTPS://Cerca.Remedia.AR:8443/bo", "remedia.ar"),
    ("https://www.cerca.remedia.ar", "remedia.ar"),
    ("https://bot.mascotasdeloeste.com.ar", "bot.mascotasdeloeste.com.ar"),
    ("https://mascotasdeloeste.com.ar", "mascotasdeloeste.com.ar"),
    ("http://localhost:8000", "localhost"),
    ("http://127.0.0.1:8000", "127.0.0.1"),
    ("https://[mal", ""),
    ("", ""),
])
def test_dominio_propio(base_url, esperado):
    from app.services.checkout_helper import dominio_propio
    assert dominio_propio(base_url) == esperado


@pytest.mark.parametrize("texto,dominio,esperado", [
    ("mirá https://cerca.remedia.ar/promos", "remedia.ar", False),
    ("pagá acá https://cerca.remedia.ar/pay/abc", "remedia.ar", False),
    ("te mando la receta https://drive.google.com/file/d/abc", "remedia.ar", True),
    ("receta.jpg", "remedia.ar", True),
    ("https://bot.mascotasdeloeste.com.ar/pay/xyz", "bot.mascotasdeloeste.com.ar", False),
    ("https://bot.mascotasdeloeste.com.ar/status", "bot.mascotasdeloeste.com.ar", False),
    ("https://otra.com.ar/x", "bot.mascotasdeloeste.com.ar", True),
    # Sin dominio propio: /pay/ se excluye igual y remedia.ar deja de ser especial.
    ("https://pagos.ejemplo.com/pay/abc", "", False),
    ("https://remedia.ar/x", "", True),
])
def test_contiene_link_con_dominio_propio(texto, dominio, esperado):
    from app.services.checkout_helper import contiene_link
    assert contiene_link(texto, dominio) is esperado
```

- [ ] **Step 5: Correr y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py::test_dominio_propio tests/test_petshop.py::test_contiene_link_con_dominio_propio -v`

Esperado: `19 failed`. Los 10 de `test_dominio_propio` con `ImportError: cannot import name 'dominio_propio' from 'app.services.checkout_helper'`, y los 9 de `test_contiene_link_con_dominio_propio` con `TypeError: contiene_link() takes 1 positional argument but 2 were given`.

- [ ] **Step 6: `dominio_propio` y `contiene_link(t, dominio_propio="")`, con el llamador del webhook en el mismo paso**

Los dos cambios van juntos: si se cambia `contiene_link` y el webhook sigue llamando `contiene_link(texto)`, los links a `remedia.ar` de la farmacia pasan a derivarse y la regresión del Step 1 se pone roja.

En `app/services/checkout_helper.py:11`, reemplazar:

```python
from typing import Optional
```

por:

```python
from typing import Optional
from urllib.parse import urlsplit
```

En `app/services/checkout_helper.py:568-578`, reemplazar la función entera:

```python
def contiene_link(t: str) -> bool:
    """
    True si el mensaje trae una URL o referencia a un archivo (receta/bono
    enviado como link en vez de foto) → se deriva a una persona, igual que
    una imagen de receta. Excluye los links de pago propios (pay/...).
    """
    m = _LINK_RE.search(t or "")
    if not m:
        return False
    link = m.group(0).lower()
    return "remedia.ar" not in link and "/pay/" not in link
```

por:

```python
# Sufijos genéricos de segundo nivel: en "bot.mascotasdeloeste.com.ar" el
# dominio propio es el host entero, nunca "com.ar".
_SLD_GENERICOS = {"com", "net", "org", "gob", "gov", "edu", "co"}


def dominio_propio(base_url: str) -> str:
    """
    Dominio de los links propios, a partir de PUBLIC_BASE_URL: el host en
    minúsculas; con 3 o más etiquetas que no terminan en un sufijo genérico
    de segundo nivel (com.ar, gob.ar, co.uk...), las dos últimas
    ("cerca.remedia.ar" → "remedia.ar"). Sin URL, "".
    """
    s = (base_url or "").strip()
    if not s:
        return ""
    try:
        host = (urlsplit(s if "://" in s else "//" + s).hostname or "").lower().rstrip(".")
    except ValueError:                       # URL mal cargada: sin dominio propio
        return ""
    if not host or host.replace(".", "").isdigit():      # IP de desarrollo: tal cual
        return host
    partes = host.split(".")
    if len(partes) >= 3 and not (partes[-2] in _SLD_GENERICOS and len(partes[-1]) == 2):
        return ".".join(partes[-2:])
    return host


def contiene_link(t: str, dominio_propio: str = "") -> bool:
    """
    True si el mensaje trae una URL o referencia a un archivo (receta/bono
    enviado como link en vez de foto) → se deriva a una persona, igual que
    una imagen de receta. Excluye los links de pago (/pay/) y, si viene, los
    del dominio propio del deploy (ver `dominio_propio`).
    """
    m = _LINK_RE.search(t or "")
    if not m:
        return False
    link = m.group(0).lower()
    if "/pay/" in link:
        return False
    dominio = (dominio_propio or "").strip().lower()
    return not (dominio and dominio in link)
```

(El parámetro `dominio_propio` tapa a la función del mismo nombre solo adentro de `contiene_link`, que no la llama. Las dos firmas son las del contrato.)

En `app/routers/webhook.py:60`, dentro de `from app.services.checkout_helper import (...)`, reemplazar:

```python
    quiere_cambiar_direccion, extraer_direccion_de, contiene_link, pide_pago_manual,
```

por:

```python
    quiere_cambiar_direccion, extraer_direccion_de, contiene_link, dominio_propio, pide_pago_manual,
```

En `app/routers/webhook.py:1156`, reemplazar:

```python
            if contiene_link(texto):
```

por:

```python
            if contiene_link(texto, dominio_propio(get_settings().public_base_url)):
```

- [ ] **Step 7: Correr y ver que pasan, con la regresión**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py::test_dominio_propio tests/test_petshop.py::test_contiene_link_con_dominio_propio tests/test_petshop.py::test_contiene_link_regresion_farmacia tests/test_petshop_conversaciones.py::test_farmacia_link_externo_deriva_como_receta tests/test_petshop_conversaciones.py::test_farmacia_links_propios_no_derivan -v`
Esperado: `30 passed`. Control: `grep -n "remedia.ar" app/services/checkout_helper.py` solo muestra el ejemplo del docstring de `dominio_propio`.

- [ ] **Step 8: Commit**

```bash
git add app/services/checkout_helper.py app/routers/webhook.py tests/test_petshop.py
git commit -m "Links: el dominio propio sale de PUBLIC_BASE_URL y reemplaza el literal remedia.ar

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 9: Escribir los tests del gate `links_como_receta` (falla petshop)**

Agregar al final de `tests/test_petshop_conversaciones.py`:

```python
_LINK_IG = "Hola, tenés este? https://www.instagram.com/p/C1abc/"


async def test_link_de_instagram_va_al_modelo_en_petshop(usar_perfil, entorno, monkeypatch):
    from app.config import get_settings
    usar_perfil("petshop")
    monkeypatch.setattr(get_settings(), "public_base_url", "https://bot.mascotasdeloeste.com.ar")
    deps = entorno({_LINK_IG: {
        "intencion": "saludo",
        "respuesta": "¡Hola! No puedo abrir links 🙏 ¿Me decís el nombre del producto? 🐾"}})
    await wh.procesar_mensajes([_msg(_LINK_IG)])
    assert deps["wa"].enviados and not any("Recibí tu link" in t for t in deps["wa"].enviados)
    assert (await deps["session"].get(PHONE)).get("estado") != "operador"
    assert _texto_llego_al_modelo(deps, _LINK_IG)
    assert "receta_link" not in _intenciones_perf(deps)


async def test_link_en_mutual_sigue_derivando(usar_perfil, entorno):
    usar_perfil("mutual")
    deps = entorno()
    await wh.procesar_mensajes([_msg(_LINK_IG)])
    assert deps["wa"].enviados[-1].startswith("Recibí tu link 🙌")
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "receta_link"
    assert not _texto_llego_al_modelo(deps, _LINK_IG)
```

- [ ] **Step 10: Correr y ver que falla**

Run: `.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py::test_link_de_instagram_va_al_modelo_en_petshop tests/test_petshop_conversaciones.py::test_link_en_mutual_sigue_derivando -v`

Esperado: `1 failed, 1 passed`. `test_link_de_instagram_va_al_modelo_en_petshop` falla con `assert (['Recibí tu link 🙌. Para gestionarlo te paso con alguien del equipo, que lo revisa y te ayuda. ¡En un momento te contactamos!'] and not True)`. La mutual ya deriva (guarda).

- [ ] **Step 11: Gate `links_como_receta` en el webhook**

En `app/routers/webhook.py:1155-1156`, reemplazar:

```python
            # ── Receta/bono enviado como LINK → derivar (igual que la foto) ──
            if contiene_link(texto, dominio_propio(get_settings().public_base_url)):
```

por:

```python
            # ── Receta/bono enviado como LINK → derivar (igual que la foto) ──
            # Rubro sin recetas ni bonos (petshop): un link (Instagram, la web de
            # una marca) es una consulta más y sigue al modelo.
            if get_perfil().links_como_receta and \
                    contiene_link(texto, dominio_propio(get_settings().public_base_url)):
```

El bloque sigue antes del desvío de la mutual (`webhook.py:1170-1180`): la mutual tiene `links_como_receta=True` y deriva igual que hoy.

- [ ] **Step 12: Correr y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py tests/test_petshop_conversaciones.py -v`
Esperado: todo `PASSED`. Incluye los 32 tests de esta tarea (24 en `test_petshop.py` y 8 en `test_petshop_conversaciones.py`) y los 18 de Task 6.

- [ ] **Step 13: Commit**

```bash
git add app/routers/webhook.py tests/test_petshop_conversaciones.py
git commit -m "Links por perfil: sin links_como_receta un link va al modelo

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 14: Suite completa**

Run: `.venv/Scripts/python -m pytest -q`
Esperado: `0 failed`. El total es el de la tarea anterior más los 32 tests nuevos de esta tarea. Ningún test de `07a1d7a` manda por el webhook links a `remedia.ar` ni a `/pay/`, y ninguno llama a `contiene_link`: nada existente cambia de expectativa.

Verificado en el mismo sandbox de Task 6: `984 passed` (933 de base, 1 de un `test_petshop.py` de Task 4 simulado, 18 de Task 6 y 32 de esta tarea). Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1071 tests.

Precondición de despliegue (spec §4.3 y §7.6): la farmacia queda idéntica **solo si** su `PUBLIC_BASE_URL` en Railway es un host bajo `remedia.ar`. Con `PUBLIC_BASE_URL` vacío, un link a `remedia.ar` pasa a derivarse como receta.

---

### Task 8: Beneficios por capacidad — socios, cuenta corriente y obras sociales (spec §4.4)

**Files:**
- Modify: `app/services/checkout_helper.py:13` (import de `get_perfil`, solo si falta), `:224-241` (`_PAGO_MANUAL` y `pide_pago_manual`), `:337-349` (`habilitado_cc`, docstring y primera línea), `:675-683` (`descuento_para`, docstring y primera línea)
- Modify: `app/services/config_service.py:227` (`DEFAULTS["pago_mp_manual"]`, solo si falta)
- Modify: `app/routers/backoffice.py:926` (`ConfigUpdate.pago_mp_manual`, solo si falta)
- Modify: `app/routers/webhook.py:25-26` (import, solo si falta), `:751` (`perfil = get_perfil()`, solo si falta), `:1095-1126` (contexto de socio), `:1223-1224` (`consulta_saldo`), `:1327` (cuenta corriente / anotar), `:1379` y `:1391` (`pide_pago_manual`), `:1456` (`dice_ser_socio`), `:1474` (`pregunta_descuento`), `:1548-1550` (obra social y bono por texto)
- Modify: `app/routers/simulate.py:26` (import), `:97-100` (contexto de socio)
- Test: `tests/test_petshop.py` (sección "Beneficios"), `tests/test_petshop_conversaciones.py` (sección "Beneficios")

Las líneas son las de `07a1d7a`. Si las tareas anteriores las corrieron, cada bloque se ubica por el texto que se cita completo en cada paso.

**Interfaces:**
- Consumes:
  - `app.services.perfil.get_perfil() -> Perfil` (campos `socios`, `cuenta_corriente`, `obras_sociales`).
  - Fixture `usar_perfil(clave, comercio=None) -> Perfil` de `tests/conftest.py` (Task 1).
  - `DEFAULTS["pago_mp_manual"] = "true"` de `app/services/config_service.py` (contrato; la agrega la Task 3). Si al llegar a esta tarea todavía no existe, la agrega esta tarea (Step 4).
  - Fixture `entorno`, `_msg` y `PHONE` de `tests/test_webhook_secuencias.py`. El fake `_Intent` de ese archivo **no** se usa: esta tarea trae su propio `_IntentBen`, que guarda los kwargs.
- Produces:
  - `def pide_pago_manual(t: str, incluir_cuenta_corriente: bool = True, incluir_mercado_pago: bool = True) -> bool` (contrato).
  - `descuento_para(phone, cfg, socio_svc=None) -> tuple[float, str]` devuelve `(0.0, "")` si `not get_perfil().socios` (también apaga el descuento de empleado).
  - `async habilitado_cc(...) -> Optional[dict]` devuelve `None` si `not get_perfil().cuenta_corriente`.
  - Constantes nuevas en `checkout_helper`: `_PM_CUENTA_CORRIENTE`, `_PM_MERCADO_PAGO`.
  - `ConfigUpdate.pago_mp_manual: str | None = None` (si no la agregó la Task 3).
  - Variable local `perfil = get_perfil()` al inicio de `procesar_mensajes`. La reusa la Task 10 (visión, §4.6).

- [ ] **Step 1: Escribir los tests unitarios de beneficios (fallan)**

Si `tests/test_petshop.py` no existe, crearlo con este encabezado (en este plan ya existe: lo crea la Task 4 y la Task 6 le suma `import pytest`):

```python
"""
Vertical petshop: unitarios por capacidad (spec 2026-10-06 §6.2). Cada test
de petshop tiene su par de farmacia "igual que hoy".
"""
import pytest
```

Agregar al final del archivo:

```python


# ══════════════════════════════════════════════════════════════════════════════
# Beneficios (§4.4): socios, empleados, cuenta corriente y pago manual
# ══════════════════════════════════════════════════════════════════════════════
class _SociosBen:
    def __init__(self, socios):
        self.s = socios

    def find_by_phone(self, phone):
        return self.s.get(phone)

    def contexto_para_prompt(self, phone):
        return "Nombre de pila (para saludar): Ana" if phone in self.s else None


_CFG_BEN = {"socio_discount_pct": "15", "empleado_discount_pct": "20",
            "socio_discount_en_catalogo": "true", "receta_mode": "conservador",
            "cc_enabled": "true"}


@pytest.fixture
def empleados_ben(monkeypatch):
    """Listado de empleados falso (mismo patrón que test_descuento_entrega.py)."""
    import sys
    import types
    padron = {}
    mod = types.ModuleType("app.services.empleado_service")

    class _Svc:
        def find_by_phone(self, phone):
            return padron.get(phone)
    svc = _Svc()
    mod.get_empleado_service = lambda *a, **k: svc
    monkeypatch.setitem(sys.modules, "app.services.empleado_service", mod)
    return padron


def test_beneficios_descuento_para_petshop_apaga_socio_y_empleado(usar_perfil, empleados_ben):
    from app.services import checkout_helper as ch
    usar_perfil("petshop")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    socios = _SociosBen({"549E": {"nombre": "Ema"}, "549S": {"nombre": "Sol"}})
    assert ch.descuento_para("549E", _CFG_BEN, socios) == (0.0, "")
    assert ch.descuento_para("549S", _CFG_BEN, socios) == (0.0, "")


def test_beneficios_descuento_para_farmacia_igual_que_hoy(usar_perfil, empleados_ben):
    from app.services import checkout_helper as ch
    usar_perfil("farmacia")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    socios = _SociosBen({"549E": {"nombre": "Ema"}, "549S": {"nombre": "Sol"}})
    assert ch.descuento_para("549E", _CFG_BEN, socios) == (20.0, "empleado")
    assert ch.descuento_para("549S", _CFG_BEN, socios) == (15.0, "socio")


def _res_ben():
    return [{"sku_id": "P1", "nombre": "DOG CHOW ADULTO 15KG", "precio": 10000.0,
             "requiere_receta": "no", "vendible": True}]


def test_beneficios_aplicar_descuento_petshop_no_toca_precios(usar_perfil, empleados_ben):
    from app.services import checkout_helper as ch
    usar_perfil("petshop")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    out, pct = ch.aplicar_descuento_socio(_res_ben(), "549E", _CFG_BEN,
                                          _SociosBen({"549E": {"nombre": "Ema"}}))
    assert pct == 0.0
    assert out[0]["precio"] == 10000.0 and "precio_lista" not in out[0]


def test_beneficios_aplicar_descuento_farmacia_igual_que_hoy(usar_perfil, empleados_ben):
    from app.services import checkout_helper as ch
    usar_perfil("farmacia")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    out, pct = ch.aplicar_descuento_socio(_res_ben(), "549E", _CFG_BEN,
                                          _SociosBen({"549E": {"nombre": "Ema"}}))
    assert pct == 20.0
    assert out[0]["precio"] == pytest.approx(8000.0) and out[0]["precio_lista"] == 10000.0


async def _link_de_empleado(monkeypatch, empleados_ben):
    """Link de un empleado con el precio pendiente ya bonificado ($8.000 de $10.000)."""
    from app.services import checkout_helper as ch
    from app.services import config_service as cs
    from app.services.session_service import SessionService
    empleados_ben["549E"] = {"nombre_pila": "Ema"}

    class _CfgBen:
        async def get_all(self):
            return dict(_CFG_BEN)

        async def get_hours(self):
            return {}

        def is_open_now(self, hours):
            return True
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: _CfgBen())

    async def _sin_freno(*a, **k):
        return None, None
    monkeypatch.setattr(ch, "_chequear_stock_vivo", _sin_freno)
    # Precio de lista mayor al cobrado: con descuento vigente sale la línea "🎉".
    monkeypatch.setattr(ch, "precio_sin_descuento", lambda items, pct, sku_svc=None: 10000.0)

    class _Pago:
        async def crear_link(self, **k):
            return "https://pago/ben", None

    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_pending("549E", sku_id="P1", sku_nombre="DOG CHOW ADULTO 15KG",
                         precio=8000.0, cantidad=1, opciones=[])
    return await ch.crear_link_y_responder(_Pago(), ss, "549E", await ss.get("549E"),
                                           "retiro", None)


async def test_beneficios_link_petshop_sin_linea_de_empleado(usar_perfil, empleados_ben,
                                                             monkeypatch):
    usar_perfil("petshop")
    resp, link = await _link_de_empleado(monkeypatch, empleados_ben)
    assert link == "https://pago/ben"
    assert "Como empleado" not in resp and "🎉" not in resp
    assert "$8,000.00" in resp


async def test_beneficios_link_farmacia_con_linea_de_empleado_igual_que_hoy(
        usar_perfil, empleados_ben, monkeypatch):
    usar_perfil("farmacia")
    resp, link = await _link_de_empleado(monkeypatch, empleados_ben)
    assert link == "https://pago/ben"
    assert "🎉 Como empleado te aplicamos un 20% de descuento (precio de lista: $10,000.00)." in resp


class _SinExcepcionesCC:
    async def es_excepcion(self, socio):
        return False


async def test_beneficios_habilitado_cc_petshop_none(usar_perfil, empleados_ben, monkeypatch):
    from app.services import checkout_helper as ch
    import app.services.cc_service as ccmod
    monkeypatch.setattr(ccmod, "_instance", _SinExcepcionesCC())
    usar_perfil("petshop")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    socios = _SociosBen({"549S": {"nombre": "Sol", "dni": "1"}})
    assert await ch.habilitado_cc("549S", _CFG_BEN, socios, 1000) is None
    assert await ch.habilitado_cc("549E", _CFG_BEN, socios, 1000) is None


async def test_beneficios_habilitado_cc_farmacia_igual_que_hoy(usar_perfil, empleados_ben,
                                                               monkeypatch):
    from app.services import checkout_helper as ch
    import app.services.cc_service as ccmod
    monkeypatch.setattr(ccmod, "_instance", _SinExcepcionesCC())
    usar_perfil("farmacia")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    socios = _SociosBen({"549S": {"nombre": "Sol", "dni": "1"}})
    assert (await ch.habilitado_cc("549S", _CFG_BEN, socios, 1000))["nombre"] == "Sol"
    assert (await ch.habilitado_cc("549E", _CFG_BEN, socios, 1000))["empleado"] is True


def test_beneficios_pide_pago_manual_sin_cc_ni_mp():
    from app.services.checkout_helper import pide_pago_manual
    assert pide_pago_manual("me lo anotás en cuenta corriente?", incluir_cuenta_corriente=False) is False
    assert pide_pago_manual("lo pago con mercado pago", incluir_mercado_pago=False) is False
    # Los demás medios manuales siguen contando con los dos apagados.
    assert pide_pago_manual("te pago por transferencia", incluir_cuenta_corriente=False,
                            incluir_mercado_pago=False) is True
    assert pide_pago_manual("lo pago en la sucursal cuando retiro", incluir_cuenta_corriente=False,
                            incluir_mercado_pago=False) is True


def test_beneficios_pide_pago_manual_farmacia_igual_que_hoy():
    from app.services.checkout_helper import pide_pago_manual
    assert pide_pago_manual("me lo anotás en cuenta corriente?") is True
    assert pide_pago_manual("lo pago con mercado pago") is True
    assert pide_pago_manual("tenés ibuprofeno?") is False


def test_beneficios_pago_mp_manual_default_y_editable():
    from app.routers.backoffice import ConfigUpdate
    from app.services.config_service import DEFAULTS
    assert DEFAULTS["pago_mp_manual"] == "true"          # default: todo igual que hoy
    assert ConfigUpdate(pago_mp_manual="false").model_dump()["pago_mp_manual"] == "false"
```

- [ ] **Step 2: Correr los tests y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py -v -k beneficios`

Expected: 5 FAILED y 6 PASSED (los pares de farmacia ya andan hoy, y `test_beneficios_pago_mp_manual_default_y_editable` pasa porque la Task 3 ya agregó la clave):
- `test_beneficios_descuento_para_petshop_apaga_socio_y_empleado` → `AssertionError: assert (20.0, 'empleado') == (0.0, '')`
- `test_beneficios_aplicar_descuento_petshop_no_toca_precios` → `assert 20.0 == 0.0`
- `test_beneficios_link_petshop_sin_linea_de_empleado` → `AssertionError: assert ('Como empleado' not in '...🎉 Como empleado te aplicamos un 20% de descuento...'`
- `test_beneficios_habilitado_cc_petshop_none` → `AssertionError: assert {'nombre': 'Sol', 'dni': '1'} is None`
- `test_beneficios_pide_pago_manual_sin_cc_ni_mp` → `TypeError: pide_pago_manual() got an unexpected keyword argument 'incluir_cuenta_corriente'`
- `test_beneficios_pago_mp_manual_default_y_editable` → `KeyError: 'pago_mp_manual'` solo si falta la clave. En este plan la Task 3 ya la agregó en `DEFAULTS` y en `ConfigUpdate`: pasa de entrada y queda como guarda.

- [ ] **Step 3: Implementar los gates de `checkout_helper.py`**

3a. Import (solo si falta; en este plan ya lo puso la Task 3). Correr `grep -n "from app.services.perfil import get_perfil" app/services/checkout_helper.py`. Si no imprime nada, reemplazar:

```python
from app.services.sku_service import requiere_derivacion
```

por:

```python
from app.services.perfil import get_perfil
from app.services.sku_service import requiere_derivacion
```

3b. `pide_pago_manual` (líneas 224-241). Reemplazar el bloque completo:

```python
_PAGO_MANUAL = [
    r"\btransferencia\b", r"\btransferir\b", r"\btransfiero\b", r"\btransferis\b",
    r"\befectivo\b", r"\bcbu\b", r"\balias\b", r"\bmercado\s*pago\b",
    # Casos 29 y 31: "lo pago en la sucursal cuando retiro" y "cuenta corriente"
    # recibían link de pago igual. Son medios que coordina una persona.
    r"\bcuenta\s+corriente\b",
    r"\bpag\w+\b.{0,30}\b(sucursal|local|farmacia|caja|retir\w+|ah[ií]|all[aá])\b",
    r"\b(retir\w+|sucursal)\b.{0,30}\bpag\w+",
]


def pide_pago_manual(t: str) -> bool:
    """
    True si el cliente pide pagar por transferencia o efectivo. Regla de negocio
    (minuta 2026-07-31): esos medios se derivan SIEMPRE a una persona — la
    transferencia requiere validar comprobante, el efectivo no se ofrece por el bot.
    """
    return any(re.search(p, t, re.IGNORECASE) for p in _PAGO_MANUAL)
```

por:

```python
# Medios que se pueden sacar de la lista (vertical petshop, §4.4): sin
# cuenta corriente en el rubro, o con un comercio que cobra con Mercado Pago.
_PM_CUENTA_CORRIENTE = r"\bcuenta\s+corriente\b"
_PM_MERCADO_PAGO = r"\bmercado\s*pago\b"

_PAGO_MANUAL = [
    r"\btransferencia\b", r"\btransferir\b", r"\btransfiero\b", r"\btransferis\b",
    r"\befectivo\b", r"\bcbu\b", r"\balias\b", _PM_MERCADO_PAGO,
    # Casos 29 y 31: "lo pago en la sucursal cuando retiro" y "cuenta corriente"
    # recibían link de pago igual. Son medios que coordina una persona.
    _PM_CUENTA_CORRIENTE,
    r"\bpag\w+\b.{0,30}\b(sucursal|local|farmacia|caja|retir\w+|ah[ií]|all[aá])\b",
    r"\b(retir\w+|sucursal)\b.{0,30}\bpag\w+",
]


def pide_pago_manual(t: str, incluir_cuenta_corriente: bool = True,
                     incluir_mercado_pago: bool = True) -> bool:
    """
    True si el cliente pide pagar por transferencia o efectivo. Regla de negocio
    (minuta 2026-07-31): esos medios se derivan SIEMPRE a una persona — la
    transferencia requiere validar comprobante, el efectivo no se ofrece por el bot.

    incluir_cuenta_corriente=False: el rubro no tiene cuenta corriente
    (perfil.cuenta_corriente) y "¿puedo pagar con cuenta corriente?" va al modelo.
    incluir_mercado_pago=False: el comercio cobra con Mercado Pago (config
    pago_mp_manual=false) y pedir pagar con MP no saca al cliente de la venta.
    """
    excluidos = set()
    if not incluir_cuenta_corriente:
        excluidos.add(_PM_CUENTA_CORRIENTE)
    if not incluir_mercado_pago:
        excluidos.add(_PM_MERCADO_PAGO)
    return any(re.search(p, t, re.IGNORECASE) for p in _PAGO_MANUAL if p not in excluidos)
```

3c. `habilitado_cc` (líneas 346-349). Reemplazar:

```python
    que no era socia no podía anotar ("lo anoto en la cuenta" se perdía).
    """
    if str(cfg.get("cc_enabled", "true")).lower() != "true":
        return None
```

por:

```python
    que no era socia no podía anotar ("lo anoto en la cuenta" se perdía).

    Un rubro sin cuenta corriente (perfil.cuenta_corriente=False) nunca la
    habilita, aunque el teléfono esté en un padrón o en el listado de empleados.
    """
    if not get_perfil().cuenta_corriente:
        return None
    if str(cfg.get("cc_enabled", "true")).lower() != "true":
        return None
```

3d. `descuento_para` (líneas 680-684). Reemplazar:

```python
    Único lugar que decide el descuento: catálogo, texto al modelo, respuesta
    a "¿tengo descuento?", link de pago y cotización de recetas.
    """
    try:
        from app.services.empleado_service import get_empleado_service
```

por:

```python
    Único lugar que decide el descuento: catálogo, texto al modelo, respuesta
    a "¿tengo descuento?", link de pago y cotización de recetas.

    Un rubro sin socios (perfil.socios=False) no tiene ningún descuento: se
    apaga también el de empleado, que es una regla de la farmacia (§4.4).
    """
    if not get_perfil().socios:
        return 0.0, ""
    try:
        from app.services.empleado_service import get_empleado_service
```

- [ ] **Step 4: Clave `pago_mp_manual` en `DEFAULTS` y en `ConfigUpdate` (solo si falta)**

Correr `grep -n "pago_mp_manual" app/services/config_service.py app/routers/backoffice.py`. En este plan aparecen los dos (Task 3): este paso no cambia nada y el `git add` del Step 6 no lleva esos dos archivos.

Si `config_service.py` no aparece, en `DEFAULTS` (línea 227) reemplazar:

```python
    "pago_manual_mode": "derivar",
```

por:

```python
    "pago_manual_mode": "derivar",
    # "false" = el comercio cobra con Mercado Pago: "¿puedo pagar con mercado
    # pago?" sigue la venta en vez de tomarse como pago manual (vertical
    # petshop, §4.4). "true" = como siempre.
    "pago_mp_manual": "true",
```

Si `backoffice.py` no aparece, en `ConfigUpdate` (línea 926) reemplazar:

```python
    pago_solo_tarjeta_message: str | None = None
```

por:

```python
    pago_solo_tarjeta_message: str | None = None
    pago_mp_manual: str | None = None            # "false" = cobra con MP: pedirlo no es pago manual
```

- [ ] **Step 5: Correr los tests unitarios y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py -v -k beneficios`
Expected: `11 passed`.

Run: `.venv/Scripts/python -m pytest tests/test_descuento_entrega.py tests/test_cotizacion_varios.py -q` y `.venv/Scripts/python -m pytest tests/test_logic.py -q -k "cc or pago_manual or descuento or efectivo"`
Expected: todo en verde, sin cambios de expectativa.

- [ ] **Step 6: Commit**

```bash
git add app/services/checkout_helper.py app/services/config_service.py app/routers/backoffice.py tests/test_petshop.py
git commit -m "Petshop: descuentos, cuenta corriente y pago manual se apagan por capacidad del perfil

descuento_para devuelve (0, \"\") sin socios (también el de empleado),
habilitado_cc devuelve None sin cuenta corriente y pide_pago_manual recibe
incluir_cuenta_corriente e incluir_mercado_pago. Clave nueva pago_mp_manual.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Si el Step 4 no cambió `config_service.py` ni `backoffice.py`, sacarlos del `git add`.)

- [ ] **Step 7: Escribir los tests de conversación de beneficios (fallan)**

Si `tests/test_petshop_conversaciones.py` no existe, crearlo con este encabezado. Si existe, verificar que tenga los imports `pytest`, `wh` y `from test_webhook_secuencias import ...` y no repetirlos (en este plan ya existe: lo crea la Task 6 con esos imports):

```python
"""
Vertical petshop: conversaciones punta a punta por el webhook completo, con
las dependencias falsas de test_webhook_secuencias.py (spec 2026-10-06 §6.2).
"""
import pytest

from app.routers import webhook as wh
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (entorno es fixture)
```

(`tests/` no tiene `__init__.py`: pytest agrega la carpeta al `sys.path`, y el import trae el mismo módulo ya recolectado. Importar `entorno` lo registra como fixture de este archivo.)

Agregar al final del archivo:

```python


# ══════════════════════════════════════════════════════════════════════════════
# Beneficios (§4.4): socios, empleados, cuenta corriente, obras sociales
# ══════════════════════════════════════════════════════════════════════════════
class _IntentBen:
    """Intent guionado que además guarda los kwargs de cada llamada."""
    def __init__(self, guion=None):
        self.guion = guion or {}
        self.vistos = []
        self.kwargs = []

    async def procesar_rapido(self, mensaje, **k):
        self.vistos.append(("rapido", mensaje))
        self.kwargs.append(k)
        return self.guion.get(mensaje, {"intencion": "saludo", "respuesta": "¡Hola!"})

    async def procesar(self, mensaje, **k):
        self.vistos.append(("procesar", mensaje))
        self.kwargs.append(k)
        return self.guion.get(mensaje, {"intencion": "desconocido", "respuesta": "¿En qué te ayudo?"})


class _PadronBen:
    """Padrón heredado de la farmacia: el teléfono de prueba es socio."""
    total = 1

    def find_by_phone(self, phone):
        return {"nombre": "Ana Pérez", "nombre_pila": "Ana", "socio": "4001"} if phone == PHONE else None

    def contexto_para_prompt(self, phone):
        return "Nombre de pila (para saludar): Ana | N° de socio: 4001" if phone == PHONE else None


class _EmpleadosBen:
    def find_by_phone(self, phone):
        return {"nombre": "Ana", "nombre_pila": "Ana", "activo": True} if phone == PHONE else None


class _NadieBen:
    total = 0

    def find_by_phone(self, phone):
        return None

    def contexto_para_prompt(self, phone):
        return None


@pytest.fixture
def con_beneficios(usar_perfil, entorno, monkeypatch):
    """Webhook con padrón y empleado cargados (lo que MO heredaría si compartiera
    datos con la farmacia). El perfil se fija ANTES de armar el entorno, porque
    la config falsa se arma con los valores del perfil."""
    from app.services import checkout_helper as chh
    from app.services import empleado_service as es

    async def _sin_freno(*a, **k):
        return None, None
    monkeypatch.setattr(chh, "_chequear_stock_vivo", _sin_freno)

    def armar(guion=None, cfg=None, padron=True, empleado=True, clave="petshop"):
        usar_perfil(clave)
        monkeypatch.setattr(es, "get_empleado_service",
                            lambda *a, **k: _EmpleadosBen() if empleado else _NadieBen())
        deps = entorno(guion, cfg=cfg)
        deps["intent"] = _IntentBen(guion)
        deps["socios"] = _PadronBen() if padron else _NadieBen()
        return deps
    return armar


async def _link_enviado_ben(ss):
    await ss.set_pending(PHONE, sku_id="P1", sku_nombre="DOG CHOW ADULTO 15KG",
                         precio=30000.0, cantidad=1, opciones=[])
    await ss.set_entrega(PHONE, "retiro", None)
    await ss.set_estado(PHONE, "esperando_pago")


def _llego_al_modelo(deps, txt):
    return ("rapido", txt) in deps["intent"].vistos


async def test_beneficios_petshop_sin_contexto_de_socio_ni_empleado(con_beneficios):
    txt = "hola, tienen alimento para gato?"
    deps = con_beneficios({txt: {"intencion": "saludo",
                                 "respuesta": "¡Hola! ¿Para qué edad es tu gato?"}})
    await wh.procesar_mensajes([_msg(txt)])
    assert deps["intent"].kwargs, "el mensaje tiene que llegar al modelo"
    assert all(k.get("contexto_cliente") is None for k in deps["intent"].kwargs)
    enviado = " ".join(deps["wa"].enviados).lower()
    assert not any(p in enviado for p in ("mutual", "socio", "empleado"))


async def test_beneficios_farmacia_contexto_de_empleado_igual_que_hoy(con_beneficios):
    txt = "hola, tienen alimento para gato?"
    deps = con_beneficios({txt: {"intencion": "saludo", "respuesta": "¡Hola!"}}, clave="farmacia")
    await wh.procesar_mensajes([_msg(txt)])
    ctx = deps["intent"].kwargs[0]["contexto_cliente"]
    assert ctx.startswith("Nombre de pila (para saludar): Ana") and "Es EMPLEADO de la mutual" in ctx


@pytest.mark.parametrize("txt", [
    "cuánto te debo?",
    "tienen saldo de piedras?",
    "anotame 2 bolsas más",
    "sumale una bolsa a la cuenta",
    "lo anoto en la cuenta",
])
async def test_beneficios_petshop_cuenta_corriente_va_al_modelo(con_beneficios, txt):
    deps = con_beneficios(cfg={"cc_enabled": "true"})
    await _link_enviado_ben(deps["session"])
    await wh.procesar_mensajes([_msg(txt)])
    assert _llego_al_modelo(deps, txt)
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "esperando_pago"            # ni derivado ni cerrado en cuenta
    assert "pago_metodo" not in s
    assert not any("cuenta" in t.lower() for t in deps["wa"].enviados)


async def test_beneficios_petshop_pagar_con_cc_no_es_pago_manual(con_beneficios):
    txt = "¿puedo pagar con cuenta corriente?"
    deps = con_beneficios(cfg={"pago_manual_mode": "derivar"}, padron=False, empleado=False)
    await wh.procesar_mensajes([_msg(txt)])
    assert _llego_al_modelo(deps, txt)
    assert (await deps["session"].get(PHONE)).get("estado") != "operador"


async def test_beneficios_petshop_transferencia_sigue_derivando(con_beneficios):
    deps = con_beneficios(cfg={"pago_manual_mode": "derivar"}, padron=False, empleado=False)
    await wh.procesar_mensajes([_msg("te pago por transferencia")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "transferencia_efectivo"


@pytest.mark.parametrize("extra,deriva", [({}, True), ({"pago_mp_manual": "false"}, False)])
async def test_beneficios_pago_mp_manual_decide_si_mp_deriva(con_beneficios, extra, deriva):
    txt = "¿puedo pagar con mercado pago?"
    deps = con_beneficios(cfg={"pago_manual_mode": "derivar", **extra}, padron=False, empleado=False)
    await wh.procesar_mensajes([_msg(txt)])
    s = await deps["session"].get(PHONE)
    assert (s.get("derivada_motivo") == "transferencia_efectivo") is deriva
    assert _llego_al_modelo(deps, txt) is not deriva


async def test_beneficios_petshop_soy_socio_no_pide_dni(con_beneficios):
    txt = "Hola, soy socio del club, tienen piedras sanitarias?"
    deps = con_beneficios(padron=False, empleado=False)
    await wh.procesar_mensajes([_msg(txt)])
    assert _llego_al_modelo(deps, txt)
    assert (await deps["session"].get(PHONE)).get("estado") != "operador"
    enviado = " ".join(deps["wa"].enviados)
    assert "padrón" not in enviado and "DNI" not in enviado


@pytest.mark.parametrize("txt,empleado", [
    ("¿tienen descuento por bolsa grande?", False),
    ("tengo descuento?", True),
])
async def test_beneficios_petshop_descuento_va_al_modelo(con_beneficios, txt, empleado):
    deps = con_beneficios(padron=False, empleado=empleado)
    await wh.procesar_mensajes([_msg(txt)])
    assert _llego_al_modelo(deps, txt)
    enviado = " ".join(deps["wa"].enviados)
    for frase in ("socios", "lo estamos habilitando", "Como empleado", "sin receta"):
        assert frase not in enviado


@pytest.mark.parametrize("txt", [
    "¿Le va a andar bien a mi perro?",
    "¿tienen cobertura de envío a Castelar?",
    "aceptan el bono de Royal Canin?",
])
async def test_beneficios_petshop_sin_obra_social_ni_bono(con_beneficios, txt):
    deps = con_beneficios(padron=False, empleado=False)
    await wh.procesar_mensajes([_msg(txt)])
    assert _llego_al_modelo(deps, txt)
    s = await deps["session"].get(PHONE)
    assert s.get("estado") != "operador" and not s.get("derivacion_ofrecida")
```

- [ ] **Step 8: Correr los tests de conversación y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py -v -k beneficios`

Expected: 13 FAILED y 4 PASSED.
- PASSED (guardas que ya andan hoy): `test_beneficios_farmacia_contexto_de_empleado_igual_que_hoy`, `test_beneficios_petshop_cuenta_corriente_va_al_modelo[cuánto te debo?]` (con un pedido en curso, `consulta_saldo` ya ignora "cuánto te debo"), `test_beneficios_petshop_transferencia_sigue_derivando` y `test_beneficios_pago_mp_manual_decide_si_mp_deriva[extra0-True]`.
- FAILED con `AssertionError` en `_llego_al_modelo(...)`: los otros 4 de cuenta corriente (derivan con `consulta_cuenta_corriente` o `cuenta_corriente_no_habilitada`), `pagar_con_cc_no_es_pago_manual` (deriva con `cuenta_corriente_no_habilitada`), `soy_socio_no_pide_dni` (`socio_no_reconocido`), los 2 de descuento (texto fijo de socios) y los 3 de obra social y bono.
- FAILED `test_beneficios_petshop_sin_contexto_de_socio_ni_empleado`: `assert False` en `all(k.get("contexto_cliente") is None ...)` (llega "Nombre de pila (para saludar): Ana | N° de socio: 4001").
- FAILED `test_beneficios_pago_mp_manual_decide_si_mp_deriva[extra1-False]`: `AssertionError: assert ('transferencia_efectivo' == 'transferencia_efectivo') is False`.

- [ ] **Step 9: Webhook — import y `perfil` al inicio de `procesar_mensajes` (solo si faltan)**

Correr `grep -n "from app.services.perfil import get_perfil\|perfil = get_perfil()" app/routers/webhook.py`. En este plan el import ya está (Task 3) y falta `perfil = get_perfil()`: se agrega solo eso.

Si falta el import, reemplazar (líneas 25-26):

```python
from app.config import get_settings
from app.models.whatsapp import WhatsAppMessage
```

por:

```python
from app.config import get_settings
from app.models.whatsapp import WhatsAppMessage
from app.services.perfil import get_perfil
```

Si falta `perfil = get_perfil()` dentro de `procesar_mensajes`, reemplazar (líneas 751-753):

```python
    _s = get_settings()
    deps = _deps(_s)
    _wa_base = deps["wa"]
```

por:

```python
    _s = get_settings()
    perfil = get_perfil()      # capacidades del rubro; se lee una vez por lote
    deps = _deps(_s)
    _wa_base = deps["wa"]
```

- [ ] **Step 10: Webhook — contexto de socio solo con `perfil.socios` (líneas 1095-1126)**

Reemplazar el bloque completo:

```python
            # Personalización: si el número está en el padrón de socios,
            # Claude recibe nombre y N° de socio para saludar por nombre.
            _ctx_socio = deps["socios"].contexto_para_prompt(phone)
            _socio_data = deps["socios"].find_by_phone(phone)
            _nombre_socio = nombre_de_pila(_socio_data)
            # Si el socio tiene descuento activo, los precios del catálogo YA
            # vienen bonificados: el modelo tiene que saberlo para aclararlo al
            # darlos, y para no volver a descontar por su cuenta.
            _cfg_socio = await deps["config"].get_all()
            _pct_cfg, _tipo_desc = descuento_para(phone, _cfg_socio, deps["socios"])
            if _tipo_desc == "empleado":
                # Empleado (20%, no acumulable): puede no estar en el padrón.
                try:
                    from app.services.empleado_service import get_empleado_service
                    _emp = get_empleado_service().find_by_phone(phone) or {}
                except Exception:
                    _emp = {}
                if not _nombre_socio:
                    _nombre_socio = _emp.get("nombre_pila") or ""
                if not _ctx_socio:
                    _ctx_socio = f"Nombre de pila (para saludar): {_nombre_socio}".strip()
                _ctx_socio += " | Es EMPLEADO de la mutual"
            _en_catalogo = str(
                _cfg_socio.get("socio_discount_en_catalogo", "true")).lower() == "true"
            if _ctx_socio and _pct_cfg > 0 and _en_catalogo:
                _ctx_socio += (
                    f" | IMPORTANTE: los precios que ves YA tienen aplicado el "
                    f"{_pct_cfg:g}% de descuento de {_tipo_desc}. Cuando digas un precio, "
                    f"aclaralo en la misma frase (ej: \"sale $8.500, ya con tu "
                    f"{_pct_cfg:g}% de {_tipo_desc}\"). No vuelvas a descontar nada vos, "
                    f"ni menciones el precio de lista."
                )
```

por (el código de hoy queda intacto, un nivel más adentro):

```python
            # Personalización: si el número está en el padrón de socios,
            # Claude recibe nombre y N° de socio para saludar por nombre.
            # Rubro sin socios (petshop): ni padrón, ni empleado, ni descuento.
            if perfil.socios:
                _ctx_socio = deps["socios"].contexto_para_prompt(phone)
                _socio_data = deps["socios"].find_by_phone(phone)
                _nombre_socio = nombre_de_pila(_socio_data)
                # Si el socio tiene descuento activo, los precios del catálogo YA
                # vienen bonificados: el modelo tiene que saberlo para aclararlo al
                # darlos, y para no volver a descontar por su cuenta.
                _cfg_socio = await deps["config"].get_all()
                _pct_cfg, _tipo_desc = descuento_para(phone, _cfg_socio, deps["socios"])
                if _tipo_desc == "empleado":
                    # Empleado (20%, no acumulable): puede no estar en el padrón.
                    try:
                        from app.services.empleado_service import get_empleado_service
                        _emp = get_empleado_service().find_by_phone(phone) or {}
                    except Exception:
                        _emp = {}
                    if not _nombre_socio:
                        _nombre_socio = _emp.get("nombre_pila") or ""
                    if not _ctx_socio:
                        _ctx_socio = f"Nombre de pila (para saludar): {_nombre_socio}".strip()
                    _ctx_socio += " | Es EMPLEADO de la mutual"
                _en_catalogo = str(
                    _cfg_socio.get("socio_discount_en_catalogo", "true")).lower() == "true"
                if _ctx_socio and _pct_cfg > 0 and _en_catalogo:
                    _ctx_socio += (
                        f" | IMPORTANTE: los precios que ves YA tienen aplicado el "
                        f"{_pct_cfg:g}% de descuento de {_tipo_desc}. Cuando digas un precio, "
                        f"aclaralo en la misma frase (ej: \"sale $8.500, ya con tu "
                        f"{_pct_cfg:g}% de {_tipo_desc}\"). No vuelvas a descontar nada vos, "
                        f"ni menciones el precio de lista."
                    )
            else:
                _ctx_socio = None
                _socio_data = None
                _nombre_socio = ""
```

- [ ] **Step 11: Webhook — saldo, cuenta corriente y pago manual**

11a. `consulta_saldo` (líneas 1223-1224). Reemplazar:

```python
            if consulta_saldo(texto, hay_pedido=bool(
                    session.get("pending_sku_id") or session.get("pending_items"))):
```

por:

```python
            if perfil.cuenta_corriente and consulta_saldo(texto, hay_pedido=bool(
                    session.get("pending_sku_id") or session.get("pending_items"))):
```

El fallback literal de `consulta_saldo_message` (1229-1231) no se toca (§3.4).

11b. Cuenta corriente / anotar (línea 1327). Reemplazar:

```python
            if pide_cuenta_corriente(texto) or (_hay_pedido_cc and pide_anotar(texto)):
```

por:

```python
            # Rubro sin cuenta corriente (petshop): "anotame 2 bolsas más" es un
            # pedido y va al modelo.
            if perfil.cuenta_corriente and (
                    pide_cuenta_corriente(texto) or (_hay_pedido_cc and pide_anotar(texto))):
```

11c. Pago manual (líneas 1379 y 1391). Reemplazar:

```python
            if pide_pago_manual(texto) and _pm_mode == "solo_tarjeta":
```

por:

```python
            # "cuenta corriente" solo es pago manual si el rubro la tiene, y
            # "mercado pago" solo si el comercio no cobra con MP (pago_mp_manual).
            _pide_pm = pide_pago_manual(
                texto,
                incluir_cuenta_corriente=perfil.cuenta_corriente,
                incluir_mercado_pago=str(_cfg_pm.get("pago_mp_manual", "true")).lower() != "false",
            )
            if _pide_pm and _pm_mode == "solo_tarjeta":
```

y reemplazar:

```python
            if pide_pago_manual(texto) and _pm_mode == "derivar":
```

por:

```python
            if _pide_pm and _pm_mode == "derivar":
```

- [ ] **Step 12: Webhook — "soy socio", descuentos y obra social/bono**

12a. `dice_ser_socio` (línea 1456). Reemplazar:

```python
            if (dice_ser_socio(texto) and not deps["socios"].find_by_phone(phone)
```

por:

```python
            if (perfil.socios and dice_ser_socio(texto) and not deps["socios"].find_by_phone(phone)
```

12b. `pregunta_descuento` (línea 1474). Reemplazar:

```python
            if pregunta_descuento(texto):
                _intencion = "consulta_descuento"
```

por:

```python
            # Rubro sin socios: va al modelo, que tiene prohibido afirmar descuentos.
            if perfil.socios and pregunta_descuento(texto):
                _intencion = "consulta_descuento"
```

Los fallbacks de `socio_discount_info_message` y `socio_discount_off_message` (1482-1490) no son de esta tarea (los pasa a `perfil.textos` la Task 3).

12c. Obra social y bono por texto (líneas 1548-1550). Reemplazar:

```python
            _cfg_os = await deps["config"].get_all()
            _os_preg = pregunta_obra_social(texto, parsear_lista(_cfg_os.get("obras_sociales", "")))
            _bono_preg = pregunta_bono(texto) if _os_preg is None else None
```

por:

```python
            _cfg_os = await deps["config"].get_all()
            if perfil.obras_sociales:
                _os_preg = pregunta_obra_social(texto, parsear_lista(_cfg_os.get("obras_sociales", "")))
                _bono_preg = pregunta_bono(texto) if _os_preg is None else None
            else:
                # Petshop: "andar", "cobertura" o "bono" no son obras sociales.
                _os_preg = _bono_preg = None
```

`pregunta_obra_social`, `pregunta_bono`, `responder_obra_social` y `responder_bono` no cambian.

- [ ] **Step 13: Correr los tests de conversación y los de webhook existentes**

Run: `.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py -v -k beneficios`
Expected: `17 passed`.

Run: `.venv/Scripts/python -m pytest tests/test_webhook_secuencias.py tests/test_descuento_entrega.py tests/test_casos_5_10.py -q`
Expected: todo en verde (incluye `test_dice_ser_socio_sin_padron_deriva`, `test_saldo_de_cuenta_corriente_deriva`, `test_empleada_anota_en_la_cuenta_con_link_enviado` y `test_cuenta_corriente_no_habilitada_deriva`, que corren con farmacia).

- [ ] **Step 14: Commit**

```bash
git add app/routers/webhook.py tests/test_petshop_conversaciones.py
git commit -m "Petshop: el webhook no consulta el padrón ni intercepta cuenta corriente, descuentos ni obras sociales

perfil = get_perfil() al inicio de procesar_mensajes. Sin socios no hay
contexto de socio ni de empleado, ni \"soy socio\" ni texto fijo de
descuentos. Sin cuenta corriente, saldo y \"anotalo\" van al modelo, y
pide_pago_manual no toma \"cuenta corriente\". pago_mp_manual=false deja
pasar \"mercado pago\". Sin obras sociales no se detectan por texto.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 15: Escribir el test del simulador (falla)**

Agregar al final de `tests/test_petshop.py` (usa `_SociosBen` del Step 1):

```python


class _IntentSimBen:
    def __init__(self):
        self.kwargs = []

    async def procesar_rapido(self, mensaje, **k):
        self.kwargs.append(k)
        return {"intencion": "saludo", "respuesta": "¡Hola!"}

    async def procesar(self, mensaje, **k):
        self.kwargs.append(k)
        return {"intencion": "desconocido", "respuesta": "¿En qué te ayudo?"}


async def _simular_hola(monkeypatch):
    """POST /simulate con "hola" desde un socio del padrón; devuelve los kwargs al modelo."""
    from app.routers import simulate as sim
    from app.services.session_service import SessionService
    intent = _IntentSimBen()
    ss = SessionService("redis://127.0.0.1:1")

    class _Perf:
        async def record(self, *a, **k):
            return None

    class _Cfg:
        async def get_all(self):
            return {}
    monkeypatch.setattr(sim, "get_sku_service", lambda *a, **k: None)
    monkeypatch.setattr(sim, "get_session_service", lambda *a, **k: ss)
    monkeypatch.setattr(sim, "get_intent_service", lambda *a, **k: intent)
    monkeypatch.setattr(sim, "get_perf_service", lambda *a, **k: _Perf())
    monkeypatch.setattr(sim, "get_socio_service",
                        lambda *a, **k: _SociosBen({"549SIM": {"nombre": "Ana"}}))
    monkeypatch.setattr(sim, "get_config_service", lambda *a, **k: _Cfg())
    monkeypatch.setattr(sim, "payment_svc_para", lambda *a, **k: None)
    r = await sim.simulate(sim.SimulateRequest(phone="549SIM", message="hola"))
    assert r.respuesta == "¡Hola!"
    return intent.kwargs


async def test_beneficios_simulate_petshop_sin_contexto_de_socio(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    kwargs = await _simular_hola(monkeypatch)
    assert kwargs and all(k.get("contexto_cliente") is None for k in kwargs)


async def test_beneficios_simulate_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    usar_perfil("farmacia")
    kwargs = await _simular_hola(monkeypatch)
    assert kwargs[0]["contexto_cliente"] == "Nombre de pila (para saludar): Ana"
```

- [ ] **Step 16: Correr y ver que falla**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py -v -k simulate`
Expected: `test_beneficios_simulate_petshop_sin_contexto_de_socio` FAILED con `AssertionError: assert ([{'history': [], 'contexto_cliente': 'Nombre de pila (para saludar): Ana'}] and False)`; `test_beneficios_simulate_farmacia_igual_que_hoy` PASSED.

- [ ] **Step 17: Implementar el gate en `simulate.py`**

Import (línea 26). Reemplazar:

```python
from app.services.config_service import get_config_service
from app.services.checkout_helper import (
```

por:

```python
from app.services.config_service import get_config_service
from app.services.perfil import get_perfil
from app.services.checkout_helper import (
```

Contexto del socio (líneas 97-100). Reemplazar:

```python
    session = await session_svc.get(req.phone)
    _ctx_socio = socio_svc.contexto_para_prompt(req.phone)
    _sd = socio_svc.find_by_phone(req.phone)
    _nombre_socio = nombre_de_pila(_sd)
```

por:

```python
    session = await session_svc.get(req.phone)
    # Mismo criterio que el webhook: sin socios en el rubro, sin padrón.
    if get_perfil().socios:
        _ctx_socio = socio_svc.contexto_para_prompt(req.phone)
        _sd = socio_svc.find_by_phone(req.phone)
        _nombre_socio = nombre_de_pila(_sd)
    else:
        _ctx_socio, _sd, _nombre_socio = None, None, ""
```

- [ ] **Step 18: Correr y ver que pasa**

Run: `.venv/Scripts/python -m pytest tests/test_petshop.py -v -k beneficios`
Expected: `13 passed`.

- [ ] **Step 19: Commit**

```bash
git add app/routers/simulate.py tests/test_petshop.py
git commit -m "Petshop: el simulador no arma el contexto de socio sin la capacidad socios

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 20: Suite completa**

Run: `.venv/Scripts/python -m pytest -q`
Expected: todo en verde. Son los 933 de base, más los de las tareas 1-7, más los 30 de esta tarea (13 en `test_petshop.py` y 17 en `test_petshop_conversaciones.py`). Ningún test existente cambia de expectativa. Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1101 tests.

---

### Task 9: Salud de la mascota: un síntoma deriva a una persona (§4.5)

> Números de línea: los del commit `07a1d7a`. Las tareas anteriores pueden haberlos corrido unas líneas; cada edición muestra el texto de contexto con el que se ubica. Todo se verificó en un sandbox (stub mínimo de perfil + estos cambios): los 34 tests nuevos de las Tasks 9 y 10 y la suite completa (966) en verde.

**Files:**
- Modify: `app/services/checkout_helper.py:13` (import de `get_perfil`, si falta), `app/services/checkout_helper.py:1542` (constante y función nuevas, antes del bloque `# ── 48b`), `app/services/checkout_helper.py:1543-1545` (solo si el fallback de §3.4 todavía no está, ver Step 6)
- Modify: `app/routers/webhook.py:25` (import de `get_perfil`, si falta), `:46-67` (lista de imports de `checkout_helper`), `:517-524` (rama `sintoma` de `_sin_precios_inventados`), `:581-582` (compuerta D en `_responder_consulta_en_flujo`), después de `:585` (función nueva `_derivar_consulta_salud`), `:1511-1512` (farmacéutico ofrecido), `:1795-1797` (compuerta C), `:2009-2012` (compuerta A), `:2160-2166` (compuerta B y `sintoma`), `:2316-2318` (oferta del farmacéutico), `:2501-2506` (`sintoma` de la respuesta directa)
- Modify: `app/services/metrics_store.py:16-19`, `app/static/dashboard.html:290-291` (el comentario de `sintoma_farmaceutico_message` en `config_service.py:203-204` ya lo corrigió la Task 3)
- Test: `tests/test_petshop.py` (sección "Salud de la mascota"), `tests/test_petshop_conversaciones.py` (sección "Salud de la mascota")

**Interfaces:**
- Consumes: `get_perfil()` y `perfil_por_clave()` con `Perfil.sintomas` y `Perfil.textos["consulta_salud_message"]` (Task 2); `config_service.valores_base() -> dict` (Task 3, §3.4); fixture `usar_perfil(clave, comercio=None) -> Perfil` (Task 1); `entorno`, `_msg` y `PHONE` de `tests/test_webhook_secuencias.py` (sin cambios).
- Produces:
  - `app/services/checkout_helper.py`: `MOTIVO_CONSULTA_SALUD = "consulta_salud"`; `def texto_consulta_salud(cfg: dict) -> str`
  - `app/routers/webhook.py`: `async def _derivar_consulta_salud(deps: dict, phone: str, texto: str) -> str` (intención `derivado_consulta_salud`)
  - `metrics_store._DERIVACIONES` y `DERIV_INTENTS` del dashboard suman `derivado_consulta_salud` e `imagen_indicacion_veterinaria` (esta última la emite la Task 10)
  - Helpers de test que reusa la Task 10 en `tests/test_petshop_conversaciones.py`: `_deps_mo_salud(entorno, usar_perfil, guion=None, img_tipo="otro", cfg=None, catalogo_mo=True) -> (deps, perfil)`, `_vistos(deps)`, `_intenciones_registradas(deps)`

- [ ] **Step 1: Escribir los tests unitarios de salud (fallan)**

Si `tests/test_petshop.py` todavía no existe, crearlo con este encabezado (en este plan ya existe: lo crea la Task 4):

```python
"""
Perfil petshop (Mascotas del Oeste): unitarios por capacidad, cada uno con su
par de farmacia "igual que hoy".
"""
```

Agregar al final de `tests/test_petshop.py`:

```python


# ══════════════════════════════════════════════════════════════════════════════
# Salud de la mascota (§4.5): con sintomas="derivar" un síntoma va a una persona
# ══════════════════════════════════════════════════════════════════════════════
from app.services.session_service import SessionService

_TEL_SALUD = "5493410000077"


class _MetricasSalud:
    def __init__(self):
        self.eventos = []

    async def evento(self, *a, **k):
        self.eventos.append((a, k))


class _WaSalud:
    def __init__(self):
        self.enviados = []

    async def send_text(self, phone, texto, **k):
        self.enviados.append(texto)
        return True


class _CfgSalud:
    def __init__(self, valores):
        self.v = dict(valores)

    async def get_all(self):
        return dict(self.v)


def _deps_salud(cfg=None):
    return {"session": SessionService("redis://127.0.0.1:1"), "metrics": _MetricasSalud(),
            "wa": _WaSalud(), "config": _CfgSalud(cfg or {})}


def test_motivo_consulta_salud():
    from app.services.checkout_helper import MOTIVO_CONSULTA_SALUD
    assert MOTIVO_CONSULTA_SALUD == "consulta_salud"


def test_texto_consulta_salud_config_gana_y_vacio_cae_al_perfil(usar_perfil):
    from app.services.checkout_helper import texto_consulta_salud
    p = usar_perfil("petshop")
    assert texto_consulta_salud({}) == p.textos["consulta_salud_message"]
    assert texto_consulta_salud({"consulta_salud_message": ""}) == p.textos["consulta_salud_message"]
    assert texto_consulta_salud({"consulta_salud_message": "Te paso con el equipo"}) == "Te paso con el equipo"


async def test_sin_precios_inventados_con_sintoma_deriva_en_petshop(usar_perfil):
    from app.routers.webhook import _sin_precios_inventados
    from app.services.config_service import valores_base
    p = usar_perfil("petshop")
    deps = _deps_salud()
    session = await deps["session"].get(_TEL_SALUD)
    r = await _sin_precios_inventados(deps, _TEL_SALUD, session, "Dale Vomitol $5.000", [],
                                      valores_base(), None, sintoma=True)
    assert r == p.textos["consulta_salud_message"]
    s = await deps["session"].get(_TEL_SALUD)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert not s.get("farmaceutico_ofrecido")


async def test_sin_precios_inventados_con_sintoma_farmacia_igual_que_hoy(usar_perfil):
    from app.routers.webhook import _sin_precios_inventados
    from app.services.config_service import DEFAULTS
    usar_perfil("farmacia")
    deps = _deps_salud()
    session = await deps["session"].get(_TEL_SALUD)
    r = await _sin_precios_inventados(deps, _TEL_SALUD, session, "Tomá Ibupirac $5.000", [],
                                      dict(DEFAULTS), None, sintoma=True)
    assert r == ("Para eso lo mejor es que te asesore el farmacéutico 🙌 "
                 "¿Querés que te pase con él?")
    s = await deps["session"].get(_TEL_SALUD)
    assert s.get("farmaceutico_ofrecido") is True
    assert s.get("estado") != "operador"


def test_agregar_oferta_farmaceutico_vacio_no_agrega_en_petshop(usar_perfil):
    from app.services.checkout_helper import agregar_oferta_farmaceutico
    usar_perfil("petshop")
    r = agregar_oferta_farmaceutico("Te ofrezco Pipeta X", {"sintoma_farmaceutico_message": ""})
    assert r == "Te ofrezco Pipeta X"


def test_agregar_oferta_farmaceutico_farmacia_igual_que_hoy(usar_perfil):
    from app.services.checkout_helper import agregar_oferta_farmaceutico
    usar_perfil("farmacia")
    r = agregar_oferta_farmaceutico("Te ofrezco Ibupirac", {"sintoma_farmaceutico_message": ""})
    assert r == ("Te ofrezco Ibupirac\n\nSi preferís, decime \"farmacéutico\" y te paso "
                 "con el nuestro para que te oriente.")


async def test_derivar_consulta_salud_envia_guarda_y_deriva(usar_perfil):
    from app.routers.webhook import _derivar_consulta_salud
    from app.services.config_service import valores_base
    p = usar_perfil("petshop")
    deps = _deps_salud(valores_base())
    r = await _derivar_consulta_salud(deps, _TEL_SALUD, "mi perro vomita")
    assert r == p.textos["consulta_salud_message"]
    assert deps["wa"].enviados == [r]
    s = await deps["session"].get(_TEL_SALUD)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert [(m["role"], m["content"]) for m in s["history"][-2:]] == [
        ("user", "mi perro vomita"), ("assistant", r)]


def test_metricas_cuentan_las_derivaciones_nuevas():
    from app.services.metrics_store import _DERIVACIONES
    assert "derivado_consulta_salud" in _DERIVACIONES
    assert "imagen_indicacion_veterinaria" in _DERIVACIONES


def test_dashboard_cuenta_las_derivaciones_nuevas():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "static" / "dashboard.html"
            ).read_text(encoding="utf-8")
    ini = html.index("const DERIV_INTENTS")
    conjunto = html[ini:html.index("]);", ini)]
    assert "'derivado_consulta_salud'" in conjunto
    assert "'imagen_indicacion_veterinaria'" in conjunto
```

- [ ] **Step 2: Correr los unitarios y ver que fallan**

Desde `D:/Dev/WhatsappBOTy-mercurio-pedidos`:

```bash
.venv/Scripts/python -m pytest tests/test_petshop.py::test_motivo_consulta_salud tests/test_petshop.py::test_texto_consulta_salud_config_gana_y_vacio_cae_al_perfil tests/test_petshop.py::test_sin_precios_inventados_con_sintoma_deriva_en_petshop tests/test_petshop.py::test_sin_precios_inventados_con_sintoma_farmacia_igual_que_hoy tests/test_petshop.py::test_agregar_oferta_farmaceutico_vacio_no_agrega_en_petshop tests/test_petshop.py::test_agregar_oferta_farmaceutico_farmacia_igual_que_hoy tests/test_petshop.py::test_derivar_consulta_salud_envia_guarda_y_deriva tests/test_petshop.py::test_metricas_cuentan_las_derivaciones_nuevas tests/test_petshop.py::test_dashboard_cuenta_las_derivaciones_nuevas -v
```

Esperado:
- `test_motivo_consulta_salud`: FAIL, `ImportError: cannot import name 'MOTIVO_CONSULTA_SALUD' from 'app.services.checkout_helper'`.
- `test_texto_consulta_salud_config_gana_y_vacio_cae_al_perfil`: FAIL, `ImportError: cannot import name 'texto_consulta_salud'`.
- `test_sin_precios_inventados_con_sintoma_deriva_en_petshop`: FAIL, `AssertionError: assert 'Para eso lo ... pase con él?' == 'Para temas d... veterinario.'` (hoy ofrece al farmacéutico).
- `test_derivar_consulta_salud_envia_guarda_y_deriva`: FAIL, `ImportError: cannot import name '_derivar_consulta_salud' from 'app.routers.webhook'`.
- `test_metricas_cuentan_las_derivaciones_nuevas` y `test_dashboard_cuenta_las_derivaciones_nuevas`: FAIL con `AssertionError` (las intenciones no están).
- `test_sin_precios_inventados_con_sintoma_farmacia_igual_que_hoy` y `test_agregar_oferta_farmaceutico_farmacia_igual_que_hoy`: PASS (guardas de farmacia, pasan antes y después).
- `test_agregar_oferta_farmaceutico_vacio_no_agrega_en_petshop`: PASS, porque la Task 3 (§3.4) ya pasó el fallback de `agregar_oferta_farmaceutico` a `perfil.textos`. Sin la Task 3 fallaría con `assert 'Te ofrezco P...e te oriente.' == 'Te ofrezco Pipeta X'` y se arreglaría en el Step 6.

- [ ] **Step 3: Implementar `MOTIVO_CONSULTA_SALUD` y `texto_consulta_salud` en `checkout_helper.py`**

Si `grep -n "from app.services.perfil import get_perfil" app/services/checkout_helper.py` no devuelve nada, reemplazar la línea 13:

```python
from app.services.sku_service import requiere_derivacion
```

por:

```python
from app.services.perfil import get_perfil
from app.services.sku_service import requiere_derivacion
```

Reemplazar (línea 1542):

```python
# ── 48b: pedido por síntoma → dejar a mano el farmacéutico ─────────────────────
```

por:

```python
# ── Salud de la mascota (perfil con sintomas="derivar") ───────────────────────
# Un síntoma, una dosis o "pasame con el veterinario" no se asesora: lo atiende
# una persona del equipo. Lo usan las compuertas del webhook y la foto de una
# indicación veterinaria.
MOTIVO_CONSULTA_SALUD = "consulta_salud"


def texto_consulta_salud(cfg: dict) -> str:
    """Texto de la derivación por salud: el de la config si está cargado, si no
    el del perfil. Solo se llama con sintomas == "derivar" (la clave existe solo
    en esos perfiles)."""
    return cfg.get("consulta_salud_message") or get_perfil().textos["consulta_salud_message"]


# ── 48b: pedido por síntoma → dejar a mano el farmacéutico ─────────────────────
```

- [ ] **Step 4: Implementar en `webhook.py` los imports, la rama de `_sin_precios_inventados` y `_derivar_consulta_salud`**

Si `grep -n "from app.services.perfil import get_perfil" app/routers/webhook.py` no devuelve nada, reemplazar la línea 25:

```python
from app.config import get_settings
```

por:

```python
from app.config import get_settings
from app.services.perfil import get_perfil
```

En la lista `from app.services.checkout_helper import (...)` (líneas 46-67), agregar como último renglón, antes del `)` que la cierra:

```python
    MOTIVO_CONSULTA_SALUD, texto_consulta_salud,
```

En `_sin_precios_inventados` (líneas 517-520), reemplazar:

```python
    if sintoma:
        # "¿Qué puedo tomar para el dolor de garganta?" sin productos en el
        # catálogo: lo asesora el farmacéutico (C-3964), no "no lo encuentro".
        _ses = await deps["session"].get(phone)
```

por:

```python
    if sintoma:
        if get_perfil().sintomas == "derivar":
            # Salud de la mascota: un síntoma no se asesora, lo atiende una
            # persona del equipo.
            await deps["session"].set_estado(phone, "operador", motivo=MOTIVO_CONSULTA_SALUD)
            return texto_consulta_salud(cfg)
        # "¿Qué puedo tomar para el dolor de garganta?" sin productos en el
        # catálogo: lo asesora el farmacéutico (C-3964), no "no lo encuentro".
        _ses = await deps["session"].get(phone)
```

Después de `_responder_consulta_en_flujo` (termina en la línea 585, `        return fallback`) y antes de `@router.get("/webhook")` (línea 588), agregar:

```python
async def _derivar_consulta_salud(deps: dict, phone: str, texto: str) -> str:
    """
    Salud de la mascota (perfil con sintomas="derivar"): un síntoma, una dosis
    o "pasame con el veterinario" no se busca ni se recomienda. La charla pasa
    a una persona con motivo consulta_salud: se envía el texto, se guarda el
    historial y se devuelve lo enviado (el llamador lo deja en `respuesta`
    para el historial permanente).
    """
    await deps["session"].set_estado(phone, "operador", motivo=MOTIVO_CONSULTA_SALUD)
    respuesta = texto_consulta_salud(await deps["config"].get_all())
    await deps["wa"].send_text(phone, respuesta)
    await deps["session"].add_message(phone, "user", texto)
    await deps["session"].add_message(phone, "assistant", respuesta)
    return respuesta


```

- [ ] **Step 5: Métricas y dashboard**

`app/services/metrics_store.py` (líneas 16-19), reemplazar:

```python
_DERIVACIONES = (
    "derivado_humano", "derivado_receta", "pago_manual", "sin_stock_derivado",
    "receta_link", "imagen_receta", "imagen_credencial", "cambio_postventa",
)
```

por:

```python
_DERIVACIONES = (
    "derivado_humano", "derivado_receta", "pago_manual", "sin_stock_derivado",
    "receta_link", "imagen_receta", "imagen_credencial", "cambio_postventa",
    # Perfil petshop (salud de la mascota). La farmacia nunca las emite.
    "derivado_consulta_salud", "imagen_indicacion_veterinaria",
)
```

`app/static/dashboard.html` (líneas 290-291), reemplazar:

```javascript
const DERIV_INTENTS = new Set(['derivado_humano','derivado_receta','pago_manual','sin_stock_derivado',
                               'receta_link','imagen_receta','imagen_credencial','cambio_postventa','operador']);
```

por:

```javascript
const DERIV_INTENTS = new Set(['derivado_humano','derivado_receta','pago_manual','sin_stock_derivado',
                               'receta_link','imagen_receta','imagen_credencial','cambio_postventa','operador',
                               'derivado_consulta_salud','imagen_indicacion_veterinaria']);
```

El comentario de `sintoma_farmaceutico_message` en `app/services/config_service.py` (líneas 203-204, spec §4.5) ya lo corrigió la Task 3 (Step 6, punto b): no se toca acá.

- [ ] **Step 6: Solo si `test_agregar_oferta_farmaceutico_vacio_no_agrega_en_petshop` falló en el Step 2**

Es el fallback de §3.4 (dueño: la Task 3; en este plan ya está hecho y este paso se saltea). Si esa tarea no lo dejó hecho, en `agregar_oferta_farmaceutico` (`checkout_helper.py:1544-1545`) reemplazar:

```python
    extra = cfg.get("sintoma_farmaceutico_message") or (
        "Si preferís, decime \"farmacéutico\" y te paso con el nuestro para que te oriente.")
```

por:

```python
    extra = cfg.get("sintoma_farmaceutico_message") or get_perfil().textos["sintoma_farmaceutico_message"]
```

(En farmacia `textos["sintoma_farmaceutico_message"]` es el mismo literal de `DEFAULTS`; en petshop es `""` y el `if not extra.strip()` de la línea 1546 apaga el agregado.)

- [ ] **Step 7: Correr los unitarios y ver que pasan**

Mismo comando del Step 2. Esperado: `9 passed`.

- [ ] **Step 8: Commit**

```bash
git add app/services/checkout_helper.py app/routers/webhook.py app/services/metrics_store.py app/static/dashboard.html tests/test_petshop.py
git commit -m "Petshop: texto y motivo de consulta de salud, derivacion por sintoma en el control de precios y metricas de las derivaciones nuevas" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 9: Escribir los tests de conversación de salud (fallan)**

Si `tests/test_petshop_conversaciones.py` todavía no existe, crearlo con este encabezado (en este plan ya existe: lo crea la Task 6):

```python
"""
Conversaciones punta a punta con el perfil petshop (Mascotas del Oeste), por el
webhook completo y con dependencias falsas. Reusa el entorno de
test_webhook_secuencias.py.
"""
```

Agregar al final de `tests/test_petshop_conversaciones.py` (el `import` de `test_webhook_secuencias` funciona porque `tests/` no tiene `__init__.py` y pytest lo pone en `sys.path`; si el archivo ya lo importa, repetirlo no molesta):

```python


# ══════════════════════════════════════════════════════════════════════════════
# Salud de la mascota (§4.5): compuertas A, B, C y D por el webhook completo
# ══════════════════════════════════════════════════════════════════════════════
from app.routers import webhook as wh
from app.services.config_service import valores_base
from app.services.sku_service import SKUService
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (entorno es fixture)


def _catalogo_mo_salud():
    base = {"hash": "c" * 64, "barcodes": [], "troquel": None, "brand": "", "drug": None,
            "form": None, "category": "ALIMENTOS", "rubro": "PERROS", "subrubro": "",
            "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
            "requiere_receta": "no", "source": "t"}
    return SKUService.from_rows([
        {**base, "external_id": "30", "name": "DOG CHOW ADULTO RAZAS MEDIANAS 15KG", "price": 52000.0},
        {**base, "external_id": "31", "name": "ROYAL CANIN MEDIUM ADULT 15KG", "price": 98000.0},
        {**base, "external_id": "32", "name": "PIPETA FRONTLINE PLUS PERRO 10-20KG", "price": 15000.0,
         "category": "SALUD ANIMAL"},
    ])


def _deps_mo_salud(entorno, usar_perfil, guion=None, img_tipo="otro", cfg=None, catalogo_mo=True):
    """Webhook con el perfil petshop. La config falsa lleva los textos del
    perfil (valores_base) más lo que pida el test. Devuelve (deps, perfil)."""
    p = usar_perfil("petshop")
    deps = entorno(guion, img_tipo, {**valores_base(), **(cfg or {})})
    if catalogo_mo:
        deps["sku"] = _catalogo_mo_salud()
    return deps, p


class _IntentPorPaso:
    """Claude 1 (procesar_rapido) y Claude 2 (procesar) con guiones distintos."""
    def __init__(self, rapido, procesar):
        self.rapido, self.proc, self.vistos = rapido, procesar, []

    async def procesar_rapido(self, mensaje, **k):
        self.vistos.append(("rapido", mensaje))
        return self.rapido.get(mensaje, {"intencion": "saludo", "respuesta": "¡Hola!"})

    async def procesar(self, mensaje, **k):
        self.vistos.append(("procesar", mensaje))
        return self.proc.get(mensaje, {"intencion": "desconocido", "respuesta": "¿En qué te ayudo?"})


def _vistos(deps):
    """(paso, mensaje) de cada llamada al modelo falso."""
    return [tuple(v[:2]) for v in deps["intent"].vistos]


def _intenciones_registradas(deps):
    """Intenciones que el webhook dejó en las métricas (deps["metrics"].record)."""
    return [a[2] for n, a, k in deps["metrics"].llamadas if n == "record"]


async def _pendiente_royal_mo(ss):
    await ss.set_pending(PHONE, sku_id="31", sku_nombre="ROYAL CANIN MEDIUM ADULT 15KG",
                         precio=98000.0, cantidad=1, opciones=[])


_VOMITA = "mi perro vomita, ¿qué le doy?"


async def test_sintoma_deriva_sin_buscar_ni_ofrecer(entorno, usar_perfil):
    guion = {_VOMITA: {"intencion": "consulta_abierta", "entidad_producto": None,
                       "por_sintoma": True, "respuesta": "Dale Reliveran $3.000"}}
    deps, p = _deps_mo_salud(entorno, usar_perfil, guion)
    await wh.procesar_mensajes([_msg(_VOMITA)])
    assert deps["wa"].enviados == [p.textos["consulta_salud_message"]]
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert _vistos(deps) == [("rapido", _VOMITA)]          # sin KB, sin búsqueda, sin Claude 2
    assert not s.get("pending_sku_id")
    assert not any("farmac" in t.lower() or "$" in t for t in deps["wa"].enviados)
    assert "derivado_consulta_salud" in _intenciones_registradas(deps)
    assert s["history"][-1]["content"] == p.textos["consulta_salud_message"]


async def test_pasame_con_el_veterinario_deriva_por_salud(entorno, usar_perfil):
    txt = "pasame con el veterinario"
    guion = {txt: {"intencion": "desconocido", "entidad_producto": None, "por_sintoma": True,
                   "respuesta": "Ya te paso con alguien del equipo."}}
    deps, p = _deps_mo_salud(entorno, usar_perfil, guion)
    await wh.procesar_mensajes([_msg(txt)])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert deps["wa"].enviados == [p.textos["consulta_salud_message"]]


async def test_producto_de_salud_pedido_por_nombre_se_vende(entorno, usar_perfil):
    """Guarda: pedir una pipeta por nombre no es un síntoma."""
    txt = "tenés pipeta Frontline para perro de 10 a 20 kg?"
    guion = {txt: {"intencion": "consulta_stock",
                   "entidad_producto": "pipeta frontline perro 10 a 20 kg",
                   "por_sintoma": False, "sku_seleccionado_index": 1,
                   "respuesta": "Sí, tengo la PIPETA FRONTLINE PLUS PERRO 10-20KG a $15.000. ¿Te sirve?"}}
    deps, _ = _deps_mo_salud(entorno, usar_perfil, guion)
    await wh.procesar_mensajes([_msg(txt)])
    s = await deps["session"].get(PHONE)
    assert s.get("estado") != "operador"
    assert s.get("pending_sku_id") == "32"


async def test_sintoma_marcado_por_claude_2_deriva_sin_pendiente(entorno, usar_perfil):
    txt = "tenés pipeta frontline? se rasca mucho"
    deps, p = _deps_mo_salud(entorno, usar_perfil)
    deps["intent"] = _IntentPorPaso(
        {txt: {"intencion": "consulta_stock", "entidad_producto": "pipeta frontline",
               "por_sintoma": False, "respuesta": ""}},
        {txt: {"intencion": "consulta_stock", "entidad_producto": "pipeta frontline",
               "por_sintoma": True, "sku_seleccionado_index": 1,
               "respuesta": "Para la picazón te sirve la PIPETA FRONTLINE PLUS PERRO 10-20KG a $15.000."}})
    await wh.procesar_mensajes([_msg(txt)])
    assert deps["wa"].enviados == [p.textos["consulta_salud_message"]]
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert not s.get("pending_sku_id")
    assert not [c for c in deps["metrics"].llamadas
                if c[0] == "evento" and c[1] and c[1][0] == "producto_ofrecido"]


async def test_sintoma_con_pedido_pendiente_deriva_sin_confirmar(entorno, usar_perfil):
    txt = "che, y mi gata está vomitando, ¿qué le doy?"
    guion = {txt: {"intencion": "consulta_abierta", "confirmacion": None, "entidad_producto": None,
                   "por_sintoma": True, "respuesta": "Dale Reliveran $3.000"}}
    deps, p = _deps_mo_salud(entorno, usar_perfil, guion)
    await _pendiente_royal_mo(deps["session"])
    await wh.procesar_mensajes([_msg(txt)])
    assert deps["wa"].enviados == [p.textos["consulta_salud_message"]]
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert s.get("pending_sku_id") == "31"          # no confirma, no cambia, no limpia
    antes = len(deps["wa"].enviados)
    await wh.procesar_mensajes([_msg("dale")])
    assert len(deps["wa"].enviados) == antes        # modo operador: el bot calla
    assert not any("http" in t for t in deps["wa"].enviados)


async def test_sintoma_eligiendo_la_entrega_deriva(entorno, usar_perfil):
    txt = "mi perro vomita, que le doy?"
    guion = {txt: {"intencion": "consulta_abierta", "entidad_producto": None,
                   "por_sintoma": True, "respuesta": "Dale Reliveran $3.000"}}
    deps, p = _deps_mo_salud(entorno, usar_perfil, guion)
    await _pendiente_royal_mo(deps["session"])
    await deps["session"].set_estado(PHONE, "esperando_entrega")
    await wh.procesar_mensajes([_msg(txt)])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert deps["wa"].enviados[-1] == p.textos["consulta_salud_message"]


async def test_sintoma_dando_la_direccion_deriva(entorno, usar_perfil):
    txt = "mi perro vomita, que le doy?"
    guion = {txt: {"intencion": "consulta_abierta", "entidad_producto": None,
                   "por_sintoma": True, "respuesta": "Dale Reliveran $3.000"}}
    deps, p = _deps_mo_salud(entorno, usar_perfil, guion)
    await _pendiente_royal_mo(deps["session"])
    await deps["session"].set_estado(PHONE, "esperando_direccion")
    await wh.procesar_mensajes([_msg(txt)])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert deps["wa"].enviados[-1] == p.textos["consulta_salud_message"]


async def test_consulta_abierta_con_dato_inventado_no_va_por_salud(entorno, usar_perfil):
    txt = "qué alimento me recomendás para un gato castrado?"
    guion = {txt: {"intencion": "consulta_abierta", "entidad_producto": None, "por_sintoma": False,
                   "respuesta": "Te recomiendo el Royal Canin Sterilised a $25.000 🐾"}}
    deps, _ = _deps_mo_salud(entorno, usar_perfil, guion, catalogo_mo=False)
    await wh.procesar_mensajes([_msg(txt)])
    enviado = deps["wa"].enviados[-1]
    assert enviado.startswith("No lo encuentro en nuestro catálogo")
    assert "farmac" not in enviado.lower() and "25.000" not in enviado
    s = await deps["session"].get(PHONE)
    assert s.get("derivacion_ofrecida")
    assert s.get("estado") != "operador"


async def test_farmaceutico_ofrecido_no_deriva_en_petshop(entorno, usar_perfil):
    deps, _ = _deps_mo_salud(entorno, usar_perfil)
    s = await deps["session"].get(PHONE)
    s["farmaceutico_ofrecido"] = True
    await deps["session"].save(PHONE, s)
    await wh.procesar_mensajes([_msg("farmacéutico")])
    s = await deps["session"].get(PHONE)
    assert s.get("derivada_motivo") != "farmaceutico"
    assert not any("farmacéutico" in t for t in deps["wa"].enviados)


async def test_sintoma_en_farmacia_sigue_ofreciendo_el_farmaceutico(entorno, usar_perfil):
    """Par de farmacia (guarda): igual que hoy, sin consulta_salud."""
    txt = "me duele la garganta, qué tomo?"
    guion = {txt: {"intencion": "consulta_abierta", "entidad_producto": None, "por_sintoma": True,
                   "respuesta": "Tomá Ibupirac a $3.000"}}
    usar_perfil("farmacia")
    deps = entorno(guion)
    await wh.procesar_mensajes([_msg(txt)])
    s = await deps["session"].get(PHONE)
    assert s.get("derivada_motivo") != "consulta_salud"
    assert "farmacéutico" in deps["wa"].enviados[-1]
```

- [ ] **Step 10: Correr los tests de conversación y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py::test_sintoma_deriva_sin_buscar_ni_ofrecer tests/test_petshop_conversaciones.py::test_pasame_con_el_veterinario_deriva_por_salud tests/test_petshop_conversaciones.py::test_producto_de_salud_pedido_por_nombre_se_vende tests/test_petshop_conversaciones.py::test_sintoma_marcado_por_claude_2_deriva_sin_pendiente tests/test_petshop_conversaciones.py::test_sintoma_con_pedido_pendiente_deriva_sin_confirmar tests/test_petshop_conversaciones.py::test_sintoma_eligiendo_la_entrega_deriva tests/test_petshop_conversaciones.py::test_sintoma_dando_la_direccion_deriva tests/test_petshop_conversaciones.py::test_consulta_abierta_con_dato_inventado_no_va_por_salud tests/test_petshop_conversaciones.py::test_farmaceutico_ofrecido_no_deriva_en_petshop tests/test_petshop_conversaciones.py::test_sintoma_en_farmacia_sigue_ofreciendo_el_farmaceutico -v
```

Esperado (tarda ~30 s por los reintentos a Redis):
- `test_sintoma_deriva_sin_buscar_ni_ofrecer`: FAIL en `deps["wa"].enviados == [...]` (hoy busca con la frase y lista alternativas con precio).
- `test_pasame_con_el_veterinario_deriva_por_salud`: FAIL, `derivada_motivo` es `'no_entendido'`.
- `test_sintoma_marcado_por_claude_2_deriva_sin_pendiente`: FAIL, sale la respuesta de Claude 2 con la pipeta y queda pendiente.
- `test_sintoma_con_pedido_pendiente_deriva_sin_confirmar`: FAIL, `assert ['Dale Reliveran $3.000'] == ['Para temas ...veterinario.']`.
- `test_sintoma_eligiendo_la_entrega_deriva`: FAIL, `assert ('esperando_entrega' == 'operador' ...`.
- `test_sintoma_dando_la_direccion_deriva`: FAIL, `assert ('esperando_direccion' == 'operador' ...`.
- `test_consulta_abierta_con_dato_inventado_no_va_por_salud`: FAIL en `startswith("No lo encuentro...")` (hoy responde "Para eso lo mejor es que te asesore el farmacéutico").
- `test_farmaceutico_ofrecido_no_deriva_en_petshop`: FAIL, `assert 'farmaceutico' != 'farmaceutico'`.
- `test_producto_de_salud_pedido_por_nombre_se_vende` y `test_sintoma_en_farmacia_sigue_ofreciendo_el_farmaceutico`: PASS (guardas).

- [ ] **Step 11: Compuerta D en `_responder_consulta_en_flujo`**

En `webhook.py`, líneas 579-582, reemplazar:

```python
            contexto_cliente=ctx_socio,
            situacion=situacion,
        )
        return (resultado.get("respuesta") or "").strip() or fallback
```

por:

```python
            contexto_cliente=ctx_socio,
            situacion=situacion,
        )
        # Compuerta D: un síntoma mientras elige la entrega o da la dirección
        # no lo contesta el modelo (prometía "te paso" y nadie derivaba). Los
        # llamadores envían el texto como hoy.
        if get_perfil().sintomas == "derivar" and resultado.get("por_sintoma"):
            await deps["session"].set_estado(phone, "operador", motivo=MOTIVO_CONSULTA_SALUD)
            return texto_consulta_salud(await deps["config"].get_all())
        return (resultado.get("respuesta") or "").strip() or fallback
```

- [ ] **Step 12: Compuertas A, B y C**

Compuerta C, en `esperando_confirmacion` (líneas 1795-1797), reemplazar:

```python
                    _entidad_nueva = intent_result.get("entidad_producto")
                    sku_index     = intent_result.get("sku_seleccionado_index")

```

por:

```python
                    _entidad_nueva = intent_result.get("entidad_producto")
                    sku_index     = intent_result.get("sku_seleccionado_index")

                    # Compuerta C: un síntoma con un pedido pendiente deriva sin
                    # confirmar, sin cambiar de producto y sin limpiar el
                    # pendiente (igual que pidio_humano).
                    if get_perfil().sintomas == "derivar" and intent_result.get("por_sintoma"):
                        _intencion = "derivado_consulta_salud"
                        respuesta = await _derivar_consulta_salud(deps, phone, texto)
                        continue

```

Compuerta A, tras Claude 1 y antes de la KB (líneas 2009-2012), reemplazar:

```python
            respuesta = quitar_confirmaciones_fantasma(
                quitar_frases_de_espera(intent_result.get("respuesta", "")))

            # Base de conocimiento (RAG): preguntas generales sin producto →
```

por:

```python
            respuesta = quitar_confirmaciones_fantasma(
                quitar_frases_de_espera(intent_result.get("respuesta", "")))

            # Compuerta A (salud de la mascota): un síntoma, una dosis o "pasame
            # con el veterinario" va a una persona. Antes que la KB, la búsqueda
            # y Claude 2, y sin dejar nada pendiente.
            if get_perfil().sintomas == "derivar" and intent_result.get("por_sintoma"):
                _intencion = "derivado_consulta_salud"
                respuesta = await _derivar_consulta_salud(deps, phone, texto)
                continue

            # Base de conocimiento (RAG): preguntas generales sin producto →
```

Compuerta B y el `sintoma` de la consulta abierta, tras Claude 2 (líneas 2160-2166), reemplazar:

```python
                respuesta = quitar_confirmaciones_fantasma(
                quitar_frases_de_espera(intent_result.get("respuesta", "")))
                # Antes de decidir qué producto se ofreció: un precio inventado
                # no llega al cliente (auditoría 2/10).
                respuesta = await _sin_precios_inventados(
                    deps, phone, session, respuesta, resultados_sku, _cfg_desc, entidad,
                    sintoma=bool(intent_result.get("por_sintoma")) or intencion == "consulta_abierta")
```

por:

```python
                respuesta = quitar_confirmaciones_fantasma(
                quitar_frases_de_espera(intent_result.get("respuesta", "")))
                # Compuerta B: Claude 2 marcó un síntoma que Claude 1 no vio.
                # Misma derivación, antes del control de precios, del pendiente,
                # la métrica y la imagen.
                if get_perfil().sintomas == "derivar" and intent_result.get("por_sintoma"):
                    _intencion = "derivado_consulta_salud"
                    respuesta = await _derivar_consulta_salud(deps, phone, texto)
                    continue
                # Antes de decidir qué producto se ofreció: un precio inventado
                # no llega al cliente (auditoría 2/10). La consulta abierta
                # cuenta como síntoma solo donde la asesora el farmacéutico.
                respuesta = await _sin_precios_inventados(
                    deps, phone, session, respuesta, resultados_sku, _cfg_desc, entidad,
                    sintoma=bool(intent_result.get("por_sintoma")) or (
                        intencion == "consulta_abierta"
                        and get_perfil().sintomas == "farmaceutico"))
```

- [ ] **Step 13: `sintoma` de la respuesta directa y gates del farmacéutico**

Respuesta directa sin búsqueda (líneas 2503-2506), reemplazar:

```python
                respuesta = await _sin_precios_inventados(
                    deps, phone, session, respuesta, None,
                    await deps["config"].get_all(), entidad,
                    sintoma=bool(intent_result.get("por_sintoma")) or _intencion == "consulta_abierta")
```

por:

```python
                respuesta = await _sin_precios_inventados(
                    deps, phone, session, respuesta, None,
                    await deps["config"].get_all(), entidad,
                    sintoma=bool(intent_result.get("por_sintoma")) or (
                        _intencion == "consulta_abierta"
                        and get_perfil().sintomas == "farmaceutico"))
```

Oferta del farmacéutico (líneas 2316-2318), reemplazar:

```python
                        # Pedido por síntoma (feedback 48): tras ofrecer venta
                        # libre, dejar a mano al farmacéutico.
                        if intent_result.get("por_sintoma"):
```

por:

```python
                        # Pedido por síntoma (feedback 48): tras ofrecer venta
                        # libre, dejar a mano al farmacéutico.
                        if get_perfil().sintomas == "farmaceutico" and intent_result.get("por_sintoma"):
```

Derivación al farmacéutico ofrecido (líneas 1511-1512), reemplazar:

```python
            # ── Aceptó hablar con el farmacéutico ofrecido (feedback 48) ─────
            if session.get("farmaceutico_ofrecido"):
```

por:

```python
            # ── Aceptó hablar con el farmacéutico ofrecido (feedback 48) ─────
            if get_perfil().sintomas == "farmaceutico" and session.get("farmaceutico_ofrecido"):
```

- [ ] **Step 14: Correr los tests de salud y ver que pasan**

Mismo comando del Step 10 y después el del Step 2. Esperado: `10 passed` y `9 passed`.

- [ ] **Step 15: Suite completa**

```bash
.venv/Scripts/python -m pytest -q
```

Esperado: `0 failed`; el total es el del cierre de la Task 8 más 19 (9 unitarios y 10 de conversación). La farmacia no cambia: ningún test existente se edita. Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1120 tests.

- [ ] **Step 16: Commit**

```bash
git add app/routers/webhook.py tests/test_petshop_conversaciones.py
git commit -m "Petshop: un sintoma de la mascota deriva a una persona con consulta_salud (compuertas A, B, C y D) y sin oferta del farmaceutico" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Visión por perfil e indicación veterinaria (§4.6)

**Files:**
- Modify: `app/services/image_service.py` (import de `get_perfil` al lado del de `prompts` que dejó la Task 2; `_PROMPT` ya es alias desde la Task 2), `:138` y `:154` (prompt del perfil), `:161-173` (`_parse` con `categorias`)
- Modify: `app/routers/webhook.py:751` (precondición: `perfil = get_perfil()`), `:929-932` (bono con `obras_sociales`), entre `:947` y `:949` (rama nueva `indicacion_veterinaria`), `:949-951` (receta con `recetas`, credencial con `obras_sociales`), `:961` (OCR con `recetas`), `:986-990` y `:995-998` (fallbacks a `perfil.textos`: ya los hizo la Task 3)
- Test: `tests/test_petshop.py` (sección "Visión"), `tests/test_petshop_conversaciones.py` (sección "Visión")

**Interfaces:**
- Consumes: `get_perfil().vision` (`VisionPerfil.categorias`, `VisionPerfil.prompt`), `perfil.VISION_PETSHOP`, `prompts.VISION_PROMPT_FARMACIA` (Task 2); `Perfil.recetas`, `obras_sociales`, `socios`, `textos` (Task 2); `MOTIVO_CONSULTA_SALUD` (Task 9); el `perfil = get_perfil()` local de `procesar_mensajes` (Task 8; el Step 6 lo verifica); `_deps_mo_salud`, `_vistos` e `_intenciones_registradas` de la sección de salud de `tests/test_petshop_conversaciones.py` (Task 9).
- Produces:
  - `ImageService._parse(raw: str, categorias: Optional[tuple] = None) -> Optional[dict]` (sigue siendo `@staticmethod` y acepta la llamada con un argumento)
  - `image_service._PROMPT` como alias de `prompts.VISION_PROMPT_FARMACIA` (mismo objeto)
  - Intención `imagen_indicacion_veterinaria` (ya contada en métricas por la Task 9)

- [ ] **Step 1: Escribir los tests unitarios de visión (fallan)**

Agregar al final de `tests/test_petshop.py`:

```python


# ══════════════════════════════════════════════════════════════════════════════
# Visión (§4.6): prompt y categorías del clasificador salen del perfil
# ══════════════════════════════════════════════════════════════════════════════
from types import SimpleNamespace


def _vision_con_clientes_falsos(provider, raw):
    """ImageService con clientes Anthropic y OpenAI falsos: guardan lo que
    reciben y devuelven `raw` como respuesta del modelo."""
    from app.services.image_service import ImageService
    llamadas = {"anthropic": [], "openai": []}

    async def _create_anthropic(**k):
        llamadas["anthropic"].append(k)
        return SimpleNamespace(content=[SimpleNamespace(text=raw)])

    async def _create_openai(**k):
        llamadas["openai"].append(k)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])

    svc = ImageService.__new__(ImageService)
    svc._provider = provider
    svc._anthropic = SimpleNamespace(messages=SimpleNamespace(create=_create_anthropic))
    svc._openai = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create_openai)))
    return svc, llamadas


def _texto_anthropic(k):
    return k["messages"][0]["content"][1]["text"]


def _texto_openai(k):
    return k["messages"][0]["content"][0]["text"]


def _json_vision(tipo, items=""):
    import json
    return json.dumps({"tipo": tipo, "items": items})


def test_parse_petshop_apaga_receta_bono_y_credencial(usar_perfil):
    from app.services.image_service import ImageService
    usar_perfil("petshop")
    for tipo in ("receta", "bono", "credencial"):
        assert ImageService._parse(_json_vision(tipo))["tipo"] == "otro", tipo
    assert ImageService._parse(_json_vision("indicacion_veterinaria"))["tipo"] == "indicacion_veterinaria"
    r = ImageService._parse(_json_vision("producto", "Royal Canin Medium Adult 15kg"))
    assert r == {"tipo": "producto", "items": "Royal Canin Medium Adult 15kg"}


def test_parse_farmacia_igual_que_hoy(usar_perfil):
    from app.services.image_service import ImageService
    usar_perfil("farmacia")
    for tipo in ("receta", "bono", "credencial", "comprobante", "producto", "otro"):
        assert ImageService._parse(_json_vision(tipo))["tipo"] == tipo
    assert ImageService._parse(_json_vision("indicacion_veterinaria"))["tipo"] == "otro"


def test_parse_con_categorias_explicitas(usar_perfil):
    from app.services.image_service import ImageService
    usar_perfil("petshop")
    assert ImageService._parse(_json_vision("receta"), categorias=("receta", "otro"))["tipo"] == "receta"
    assert ImageService._parse("sin json") is None


async def test_vision_petshop_manda_el_prompt_del_perfil(usar_perfil):
    from app.services.perfil import VISION_PETSHOP
    usar_perfil("petshop")
    svc, llamadas = _vision_con_clientes_falsos("anthropic", _json_vision("receta"))
    r = await svc.analizar(b"foto", "image/jpeg")
    assert _texto_anthropic(llamadas["anthropic"][0]) == VISION_PETSHOP.prompt
    assert r["tipo"] == "otro"                          # receta no existe en petshop
    svc, llamadas = _vision_con_clientes_falsos("openai", _json_vision("producto", "Pipeta"))
    r = await svc.analizar(b"foto", "image/jpeg")
    assert _texto_openai(llamadas["openai"][0]) == VISION_PETSHOP.prompt
    assert r == {"tipo": "producto", "items": "Pipeta"}


async def test_vision_farmacia_manda_el_prompt_de_hoy(usar_perfil):
    from app.services import image_service
    from app.services.prompts import VISION_PROMPT_FARMACIA
    usar_perfil("farmacia")
    assert image_service._PROMPT is VISION_PROMPT_FARMACIA          # alias, mismo objeto
    svc, llamadas = _vision_con_clientes_falsos("anthropic", _json_vision("receta"))
    r = await svc.analizar(b"foto", "image/jpeg")
    assert _texto_anthropic(llamadas["anthropic"][0]) == image_service._PROMPT
    assert r["tipo"] == "receta"
    svc, llamadas = _vision_con_clientes_falsos("openai", _json_vision("bono", "Cassará"))
    await svc.analizar(b"foto", "image/jpeg")
    assert _texto_openai(llamadas["openai"][0]) == image_service._PROMPT
```

- [ ] **Step 2: Correr los unitarios de visión y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_petshop.py::test_parse_petshop_apaga_receta_bono_y_credencial tests/test_petshop.py::test_parse_farmacia_igual_que_hoy tests/test_petshop.py::test_parse_con_categorias_explicitas tests/test_petshop.py::test_vision_petshop_manda_el_prompt_del_perfil tests/test_petshop.py::test_vision_farmacia_manda_el_prompt_de_hoy -v
```

Esperado:
- `test_parse_petshop_apaga_receta_bono_y_credencial`: FAIL, `AssertionError: receta` (`'receta' == 'otro'`).
- `test_parse_con_categorias_explicitas`: FAIL, `TypeError: ImageService._parse() got an unexpected keyword argument 'categorias'`.
- `test_vision_petshop_manda_el_prompt_del_perfil`: FAIL, el bloque de texto es el prompt de farmacia (`'Analizá esta...dejalo vacío.' == 'Analizá esta...'` del de petshop).
- `test_vision_farmacia_manda_el_prompt_de_hoy`: PASS (guarda). La Task 2 ya dejó `_PROMPT` como alias de `VISION_PROMPT_FARMACIA` y hoy se manda ese prompt; sin la Task 2 fallaría en el `is` (iguales pero no el mismo objeto).
- `test_parse_farmacia_igual_que_hoy`: PASS (guarda).

- [ ] **Step 3: Implementar el clasificador por perfil en `image_service.py`**

En `app/services/image_service.py`, la Task 2 (Step 6) ya dejó `from app.services.prompts import VISION_PROMPT_FARMACIA` debajo de `import openai`. Reemplazar:

```python
from app.services.prompts import VISION_PROMPT_FARMACIA
```

por:

```python
from app.services.perfil import get_perfil
from app.services.prompts import VISION_PROMPT_FARMACIA
```

El alias `_PROMPT = VISION_PROMPT_FARMACIA` también lo dejó la Task 2: no se toca. Desde esta tarea queda solo por compatibilidad (`test_logic` lo importa de acá); el prompt que se manda en cada llamada sale de `get_perfil().vision.prompt`.

En `_anthropic_vision` (línea 138), reemplazar:

```python
                    bloque_adjunto(b64, media_type),
                    {"type": "text", "text": _PROMPT},
```

por:

```python
                    bloque_adjunto(b64, media_type),
                    {"type": "text", "text": get_perfil().vision.prompt},
```

En `_openai_vision` (línea 154), reemplazar:

```python
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
```

por:

```python
                    {"type": "text", "text": get_perfil().vision.prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
```

Reemplazar `_parse` completo (líneas 161-173):

```python
    @staticmethod
    def _parse(raw: str) -> Optional[dict]:
        match = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None
        tipo = str(data.get("tipo", "otro")).lower().strip()
        if tipo not in ("receta", "bono", "credencial", "comprobante", "producto", "otro"):
            tipo = "otro"
        return {"tipo": tipo, "items": str(data.get("items", "")).strip()}
```

por:

```python
    @staticmethod
    def _parse(raw: str, categorias: Optional[tuple] = None) -> Optional[dict]:
        """JSON del modelo → {"tipo", "items"}. Un tipo fuera de las categorías
        del perfil (o de `categorias`, si se pasan) pasa a "otro": en petshop,
        receta, bono y credencial no existen."""
        match = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None
        if categorias is None:
            categorias = get_perfil().vision.categorias
        tipo = str(data.get("tipo", "otro")).lower().strip()
        if tipo not in categorias:
            tipo = "otro"
        return {"tipo": tipo, "items": str(data.get("items", "")).strip()}
```

`leer_receta` y `_PROMPT_RECETA` no se tocan.

- [ ] **Step 4: Correr los unitarios de visión y ver que pasan**

Mismo comando del Step 2. Esperado: `5 passed`. Además, la guarda de hoy:

```bash
.venv/Scripts/python -m pytest tests/test_logic.py::TestComprobanteImagen -v
```

Esperado: todos PASS (`_parse` con un argumento y `from app.services.image_service import _PROMPT` siguen funcionando).

- [ ] **Step 5: Escribir los tests de conversación de visión (fallan)**

Agregar al final de `tests/test_petshop_conversaciones.py` (usa `_deps_mo_salud`, `_vistos` e `_intenciones_registradas` de la sección de salud de la Task 9):

```python


# ══════════════════════════════════════════════════════════════════════════════
# Visión (§4.6): ramas de imagen del webhook con el perfil petshop
# ══════════════════════════════════════════════════════════════════════════════
class _ImgMO:
    """Clasificador falso: devuelve el tipo y los items pedidos y cuenta el OCR."""
    def __init__(self, tipo="otro", items=""):
        self.tipo, self.items = tipo, items
        self.mimes, self.ocr = [], 0

    async def analizar(self, data, mime):
        self.mimes.append(mime)
        return {"tipo": self.tipo, "items": self.items}

    async def leer_receta(self, *a, **k):
        self.ocr += 1
        return None


def _foto_mo():
    return _msg("", tipo="image", media_url="https://kapso/foto.jpg")


async def test_foto_indicacion_veterinaria_deriva_por_salud(entorno, usar_perfil):
    deps, p = _deps_mo_salud(entorno, usar_perfil)
    deps["image"] = _ImgMO("indicacion_veterinaria", "Drontal Plus perro")
    await wh.procesar_mensajes([_foto_mo()])
    r = deps["wa"].enviados[-1]
    assert r.startswith("Recibí la indicación del veterinario 🐾")
    assert "{nombre}" not in r and "receta" not in r.lower()
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert _vistos(deps) == []                    # no se buscó ni se cotizó
    assert "imagen_indicacion_veterinaria" in _intenciones_registradas(deps)
    antes = len(deps["wa"].enviados)
    await wh.procesar_mensajes([_msg("Hola")])
    assert len(deps["wa"].enviados) == antes              # modo operador: el bot calla


async def test_foto_comprobante_acusa_y_deriva(entorno, usar_perfil):
    deps, _ = _deps_mo_salud(entorno, usar_perfil)
    deps["image"] = _ImgMO("comprobante")
    await wh.procesar_mensajes([_foto_mo()])
    assert deps["wa"].enviados[-1].startswith("¡Listo! Recibimos tu comprobante 🙌")
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "comprobante"


async def test_foto_comprobante_con_texto_vacio_en_config_usa_el_del_perfil(entorno, usar_perfil):
    deps, p = _deps_mo_salud(entorno, usar_perfil, cfg={"comprobante_recibido_message": ""})
    deps["image"] = _ImgMO("comprobante")
    await wh.procesar_mensajes([_foto_mo()])
    assert deps["wa"].enviados[-1] == p.textos["comprobante_recibido_message"]


async def test_foto_de_producto_llega_al_modelo(entorno, usar_perfil):
    deps, _ = _deps_mo_salud(entorno, usar_perfil)
    deps["image"] = _ImgMO("producto", "Royal Canin Medium Adult 15kg")
    await wh.procesar_mensajes([_foto_mo()])
    assert ("rapido", "Royal Canin Medium Adult 15kg") in _vistos(deps)
    assert (await deps["session"].get(PHONE)).get("estado") != "operador"


async def test_foto_otro_deriva_como_no_reconocida(entorno, usar_perfil):
    deps, _ = _deps_mo_salud(entorno, usar_perfil)
    deps["image"] = _ImgMO("otro")
    await wh.procesar_mensajes([_foto_mo()])
    assert deps["wa"].enviados[-1].startswith("Recibí tu imagen 🙌")
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "imagen_no_reconocida"


async def test_foto_receta_en_petshop_no_hace_ocr_ni_habla_de_receta(entorno, usar_perfil):
    deps, _ = _deps_mo_salud(entorno, usar_perfil, cfg={"receta_ocr_enabled": "true"})
    deps["image"] = _ImgMO("receta")
    await wh.procesar_mensajes([_foto_mo()])
    assert deps["image"].ocr == 0
    assert not any("receta" in t.lower() for t in deps["wa"].enviados)
    s = await deps["session"].get(PHONE)
    assert s["derivada_motivo"] == "imagen_no_reconocida"


async def test_foto_bono_en_petshop_no_deriva_como_bono(entorno, usar_perfil):
    deps, _ = _deps_mo_salud(entorno, usar_perfil)
    deps["image"] = _ImgMO("bono", "Cassará")
    await wh.procesar_mensajes([_foto_mo()])
    s = await deps["session"].get(PHONE)
    assert s.get("derivada_motivo") != "bono_foto"
    assert ("rapido", "Cassará") in _vistos(deps)   # con items sigue el camino normal
    assert not any("bono" in t.lower() for t in deps["wa"].enviados)


async def test_foto_credencial_en_petshop_no_deriva_como_credencial(entorno, usar_perfil):
    deps, _ = _deps_mo_salud(entorno, usar_perfil)
    deps["image"] = _ImgMO("credencial")
    await wh.procesar_mensajes([_foto_mo()])
    s = await deps["session"].get(PHONE)
    assert s["derivada_motivo"] == "imagen_no_reconocida"
    assert not any("credencial" in t.lower() for t in deps["wa"].enviados)


async def test_farmacia_receta_con_ocr_igual_que_hoy(entorno, usar_perfil):
    """Par de farmacia: la receta deriva con OCR y su texto de siempre."""
    usar_perfil("farmacia")
    deps = entorno(cfg={"receta_ocr_enabled": "true"})
    deps["image"] = _ImgMO("receta")
    await wh.procesar_mensajes([_foto_mo()])
    assert deps["image"].ocr == 1
    assert "receta" in deps["wa"].enviados[-1].lower()
    assert (await deps["session"].get(PHONE))["derivada_motivo"] == "receta_foto"


async def test_farmacia_indicacion_veterinaria_no_tiene_rama(entorno, usar_perfil):
    """Par de farmacia: ese tipo no existe ahí. Si llegara, no lee un texto que
    el perfil de farmacia no tiene (sin KeyError) y cae a no reconocida."""
    usar_perfil("farmacia")
    deps = entorno()
    deps["image"] = _ImgMO("indicacion_veterinaria")
    await wh.procesar_mensajes([_foto_mo()])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "imagen_no_reconocida"
```

- [ ] **Step 6: Correr los tests de conversación de visión y ver que fallan; verificar la precondición**

```bash
.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py::test_foto_indicacion_veterinaria_deriva_por_salud tests/test_petshop_conversaciones.py::test_foto_comprobante_acusa_y_deriva tests/test_petshop_conversaciones.py::test_foto_comprobante_con_texto_vacio_en_config_usa_el_del_perfil tests/test_petshop_conversaciones.py::test_foto_de_producto_llega_al_modelo tests/test_petshop_conversaciones.py::test_foto_otro_deriva_como_no_reconocida tests/test_petshop_conversaciones.py::test_foto_receta_en_petshop_no_hace_ocr_ni_habla_de_receta tests/test_petshop_conversaciones.py::test_foto_bono_en_petshop_no_deriva_como_bono tests/test_petshop_conversaciones.py::test_foto_credencial_en_petshop_no_deriva_como_credencial tests/test_petshop_conversaciones.py::test_farmacia_receta_con_ocr_igual_que_hoy tests/test_petshop_conversaciones.py::test_farmacia_indicacion_veterinaria_no_tiene_rama -v
```

Esperado:
- `test_foto_indicacion_veterinaria_deriva_por_salud`: FAIL en `startswith("Recibí la indicación del veterinario 🐾")` (hoy el tipo no tiene rama: con items va a la búsqueda y responde `'¡Hola!'`).
- `test_foto_receta_en_petshop_no_hace_ocr_ni_habla_de_receta`: FAIL, `assert 1 == 0` (hoy corre el OCR y deriva `receta_foto`).
- `test_foto_bono_en_petshop_no_deriva_como_bono`: FAIL, `assert 'bono_foto' != 'bono_foto'`.
- `test_foto_credencial_en_petshop_no_deriva_como_credencial`: FAIL, `assert 'credencial' == 'imagen_no_reconocida'`.
- `test_foto_comprobante_con_texto_vacio_en_config_usa_el_del_perfil`: PASA (guarda): la Task 3 (§3.4) ya pasó el fallback de comprobante a `perfil.textos`. Sin ella fallaría con `'¡Listo ! Rec...' == '¡Listo! Reci...'`.
- `test_foto_comprobante_acusa_y_deriva`, `test_foto_de_producto_llega_al_modelo`, `test_foto_otro_deriva_como_no_reconocida`, `test_farmacia_receta_con_ocr_igual_que_hoy` y `test_farmacia_indicacion_veterinaria_no_tiene_rama`: PASS (guardas, pasan antes y después).

Precondición de las ediciones siguientes: `grep -n "perfil = get_perfil()" app/routers/webhook.py` tiene que mostrar la línea dentro de `procesar_mensajes` (la agrega la Task 8). Si no aparece, reemplazar (línea 751):

```python
    _s = get_settings()
    deps = _deps(_s)
```

por:

```python
    _s = get_settings()
    perfil = get_perfil()
    deps = _deps(_s)
```

- [ ] **Step 7: Bono solo con obras sociales y rama nueva de indicación veterinaria**

En `webhook.py` (líneas 929-932), reemplazar:

```python
                # Bono de laboratorio (feedback 61, 16/9): antes caía como
                # receta y el bot cotizaba renglón por renglón. Se contesta si
                # trabajamos ese laboratorio (lista del backoffice) y se deriva.
                if img["tipo"] == "bono":
```

por:

```python
                # Bono de laboratorio (feedback 61, 16/9): antes caía como
                # receta y el bot cotizaba renglón por renglón. Se contesta si
                # trabajamos ese laboratorio (lista del backoffice) y se deriva.
                # Solo con obras sociales (defensa: el _parse de petshop ya lo
                # pasa a "otro").
                if img["tipo"] == "bono" and perfil.obras_sociales:
```

Reemplazar (líneas 949-951):

```python
                # Receta, credencial o comprobante → derivar a una persona
                # (nunca vender automático; un pago solo lo confirma un humano)
                if img["tipo"] in ("receta", "credencial", "comprobante"):
```

por:

```python
                # Indicación escrita de un veterinario (petshop): no se busca ni
                # se cotiza; la revisa una persona, igual que un síntoma. Solo
                # existe donde el clasificador tiene esa categoría (su texto
                # está solo en ese perfil).
                if img["tipo"] == "indicacion_veterinaria" \
                        and "indicacion_veterinaria" in perfil.vision.categorias:
                    _intencion = "imagen_indicacion_veterinaria"
                    await deps["session"].set_estado(phone, "operador", motivo=MOTIVO_CONSULTA_SALUD)
                    _nombre_iv = (nombre_de_pila(deps["socios"].find_by_phone(phone))
                                  if perfil.socios else "")
                    _cfg_iv = await deps["config"].get_all()
                    respuesta = personalizar_nombre(
                        _cfg_iv.get("indicacion_veterinaria_message")
                        or perfil.textos["indicacion_veterinaria_message"], _nombre_iv)
                    _ts = _time.perf_counter()
                    await deps["wa"].send_text(phone, respuesta)
                    _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                    if not _img_ref:
                        await deps["session"].add_message(phone, "user", "[imagen recibida]")
                    await deps["session"].add_message(phone, "assistant", respuesta)
                    continue

                # Receta, credencial o comprobante → derivar a una persona
                # (nunca vender automático; un pago solo lo confirma un humano).
                # Receta solo con recetas y credencial solo con obras sociales:
                # con la capacidad apagada siguen el camino de abajo (con items
                # van a la búsqueda; sin items, a imagen_no_reconocida).
                if img["tipo"] == "comprobante" \
                        or (img["tipo"] == "receta" and perfil.recetas) \
                        or (img["tipo"] == "credencial" and perfil.obras_sociales):
```

- [ ] **Step 8: OCR con `recetas` y fallbacks de receta y comprobante a `perfil.textos`**

Reemplazar (líneas 961-963):

```python
                    if img["tipo"] == "receta":
                        try:
                            _cfg_ocr = await deps["config"].get_all()
```

por:

```python
                    if img["tipo"] == "receta" and perfil.recetas:
                        try:
                            _cfg_ocr = await deps["config"].get_all()
```

Si la Task 3 (§3.4) todavía no los cambió, reemplazar el fallback de receta (líneas 986-990). En este plan la Task 3 ya los pasó a `get_perfil().textos[...]`: estos dos reemplazos se saltean.

```python
                        respuesta = _cfg_rr.get("receta_recibida_message") or (
                            "¡Hola {nombre}! Recibimos tu receta 🙌 Validamos la "
                            "información y volvemos con vos dentro de los próximos "
                            "10 minutos."
                        )
```

por:

```python
                        respuesta = (_cfg_rr.get("receta_recibida_message")
                                     or perfil.textos["receta_recibida_message"])
```

y, con la misma condición (en este plan se saltea), el de comprobante (líneas 995-998):

```python
                        respuesta = _cfg_cp.get("comprobante_recibido_message") or (
                            "¡Listo {nombre}! Recibimos tu comprobante 🙌 Lo "
                            "verificamos y te confirmamos en un rato."
                        )
```

por:

```python
                        respuesta = (_cfg_cp.get("comprobante_recibido_message")
                                     or perfil.textos["comprobante_recibido_message"])
```

(En farmacia y mutual `perfil.textos` trae el mismo literal de `DEFAULTS`: nada cambia. El bloque de producto/otro, 1014-1034, no se toca.)

- [ ] **Step 9: Correr los tests de visión y ver que pasan**

Mismo comando del Step 6 y después el del Step 2. Esperado: `10 passed` y `5 passed`. Guardas de farmacia existentes:

```bash
.venv/Scripts/python -m pytest tests/test_webhook_secuencias.py::test_receta_en_pdf_se_lee_y_deriva tests/test_webhook_secuencias.py::test_fuera_de_horario_receta_en_pdf_avisa_cuando_abrimos tests/test_webhook_secuencias.py::test_receta_pdf_queda_guardada_como_pdf -v
```

Esperado: `3 passed`.

- [ ] **Step 10: Suite completa**

```bash
.venv/Scripts/python -m pytest -q
```

Esperado: `0 failed`; el total es el del cierre de la Task 9 más 15 (5 unitarios y 10 de conversación). Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1135 tests.

- [ ] **Step 11: Commit**

```bash
git add app/services/image_service.py app/routers/webhook.py tests/test_petshop.py tests/test_petshop_conversaciones.py
git commit -m "Vision por perfil: el clasificador usa el prompt y las categorias del rubro, y en petshop la indicacion veterinaria deriva por salud sin tratar fotos como receta, bono o credencial" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Pagos y avisos con la marca del perfil (§4.7)

**Files:**
- Modify: `app/services/checkout_helper.py:832-839` (después de `texto_entrega`, función nueva `mensaje_pago_confirmado`)
- Modify: `app/routers/mp_webhook.py:23` (imports) y `:216-234` (armado del mensaje de pago confirmado)
- Modify: `app/routers/payway.py:13-14` y `:24-25` (imports), `:60-61` y `:67` (`_status_page`), `:91-92` (`pay_page`), `:260-278` (mensaje de pago confirmado), `:532`, `:571`, `:573-575`, `:580` y `:727` (HTML con placeholders)
- Modify: `app/services/payment_service.py:1-8` (imports y `statement_descriptor`) y `:48`
- Modify: `app/services/payway_service.py:20-25` (imports), `:40` (`_slug` antes de `_xsource`), `:150-151`, `:164`, `:225-240`
- Test: `tests/test_petshop.py` (agregar al final; si todavía no existe, crearlo con estos bloques: son autocontenidos)

Las líneas son las de hoy (`07a1d7a`). `checkout_helper.py` lo tocan tareas anteriores: ubicar por el texto citado, no por el número.

**Interfaces:**
- Consumes: `app.services.perfil.get_perfil() -> Perfil` (campos `emoji`, `comercio`, `descriptor_tarjeta`, `razon_social`, `wordmark_html`); fixture `usar_perfil(clave, comercio=None) -> Perfil` (Task 1); clave de config `retiro_sucursal` (se lee con `cfg.get("retiro_sucursal") or ""`, no depende de que el default ya exista); los fallbacks de `perfil.textos` de §3.4 en `orders_api.armar_mensaje_pedido_listo` y `checkout_helper._cerrar_venta_efectivo` (Task 3; acá solo se testean).
- Produces:
  - `app/services/checkout_helper.py`: `def mensaje_pago_confirmado(nombre_producto: str, tipo_entrega: str, direccion_envio: str | None, pickup_code: str, pickup_text: str, emoji: str, sucursal: str = "") -> str`
  - Fuera del contrato: `app/services/payment_service.py`: `def statement_descriptor() -> str`; `app/services/payway_service.py`: `def _slug(texto: str) -> str` y `PaywayService._fraud_detection(self, amount, email, device_id, producto="Producto", comercio: str = "")`; `app/routers/payway.py`: `def _con_marca(pagina: str) -> str` y los placeholders `{{LOGO}}`, `{{WORDMARK}}`, `{{COMERCIO}}`, `{{RAZON_SOCIAL}}`.

- [ ] **Step 1: Tests de `mensaje_pago_confirmado` (fallan)**

Agregar al final de `tests/test_petshop.py`:

```python
# ══════════════════════════════════════════════════════════════════════════════
# Pagos y avisos (§4.7): confirmación de pago, descriptor de la tarjeta, Payway
# y las páginas /pay con la marca del perfil.
# ══════════════════════════════════════════════════════════════════════════════
import hashlib
import json
from types import SimpleNamespace

import pytest

_CONF_RETIRO_FARMACIA = (
    "✅ *¡Pago confirmado!*\n\n"
    "Recibimos tu pago de *Ibuprofeno 600*. 🙌\n"
    "🔑 *Tu código de retiro es: 123456*\nRetiralo desde las 10:00\n\n"
    "Guardalo para presentarlo al retirar. ¡Muchas gracias! 💊")
_CONF_ENVIO_FARMACIA = (
    "✅ *¡Pago confirmado!*\n\n"
    "Recibimos tu pago de *Ibuprofeno 600*. 🙌\n"
    "🚚 Te lo enviamos a domicilio a *San Martín 123*. Nos comunicamos para coordinar la entrega.\n"
    "📋 Código de pedido: *123456*\n\n"
    "¡Muchas gracias! 💊")
_CONF_MP_FARMACIA = (
    "✅ *¡Pago confirmado!*\n\n"
    "Recibimos tu pago de *Royal Canin 15KG*. 🙌\n"
    "🔑 *Tu código de retiro es: 654321*\n\n"
    "Guardalo para presentarlo al retirar. ¡Muchas gracias! 💊")


def test_mensaje_pago_confirmado_farmacia_igual_que_hoy(usar_perfil):
    from app.services.checkout_helper import mensaje_pago_confirmado
    p = usar_perfil("farmacia")
    assert mensaje_pago_confirmado("Ibuprofeno 600", "retiro", None, "123456",
                                   "Retiralo desde las 10:00", p.emoji) == _CONF_RETIRO_FARMACIA
    assert mensaje_pago_confirmado("Ibuprofeno 600", "envio", "San Martín 123", "123456",
                                   "Retiralo desde las 10:00", p.emoji) == _CONF_ENVIO_FARMACIA
    # Sin texto de horario no queda una línea vacía de más
    assert "123456*\n\nGuardalo" in mensaje_pago_confirmado("X", "retiro", None, "123456", "", p.emoji)


def test_mensaje_pago_confirmado_petshop(usar_perfil):
    from app.services.checkout_helper import mensaje_pago_confirmado
    p = usar_perfil("petshop")
    for tipo, direccion in (("retiro", None), ("envio", "San Martín 123")):
        m = mensaje_pago_confirmado("Royal Canin 15KG", tipo, direccion, "654321", "", p.emoji)
        assert m.endswith("¡Muchas gracias! 🐾"), tipo
        assert "💊" not in m, tipo


def test_mensaje_pago_confirmado_con_sucursal(usar_perfil):
    from app.services.checkout_helper import mensaje_pago_confirmado
    p = usar_perfil("petshop")
    m = mensaje_pago_confirmado("Royal Canin 15KG", "retiro", None, "654321", "", p.emoji,
                                sucursal="Sucursal Piloto")
    assert m.endswith("Guardalo para presentarlo al retirar en *Sucursal Piloto*. ¡Muchas gracias! 🐾")
    # Vacía o con espacios: el texto de hoy
    assert "al retirar. ¡Muchas gracias! 🐾" in mensaje_pago_confirmado(
        "X", "retiro", None, "1", "", p.emoji, sucursal="  ")
    # El envío no nombra la sucursal
    assert "Sucursal Piloto" not in mensaje_pago_confirmado(
        "X", "envio", "Mitre 100", "1", "", p.emoji, sucursal="Sucursal Piloto")
```

- [ ] **Step 2: Correrlos y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_petshop.py::test_mensaje_pago_confirmado_farmacia_igual_que_hoy tests/test_petshop.py::test_mensaje_pago_confirmado_petshop tests/test_petshop.py::test_mensaje_pago_confirmado_con_sucursal -v
```

Esperado: 3 FAILED con `ImportError: cannot import name 'mensaje_pago_confirmado' from 'app.services.checkout_helper'`.

- [ ] **Step 3: Implementar `mensaje_pago_confirmado`**

En `app/services/checkout_helper.py`, justo después de la función `texto_entrega` (hoy termina en la línea 839 con `return "🏪 Lo retirás en la sucursal (te enviamos el código al confirmar el pago)."`; si otra tarea ya le agregó el parámetro `sucursal`, va después de su último `return`), agregar:

```python


def mensaje_pago_confirmado(nombre_producto: str, tipo_entrega: str,
                            direccion_envio: str | None, pickup_code: str,
                            pickup_text: str, emoji: str, sucursal: str = "") -> str:
    """
    Confirmación de pago aprobado. Mercado Pago (mp_webhook) y Payway (payway)
    mandan este mismo texto. `emoji` es el del perfil de rubro (💊 farmacia,
    🐾 petshop). `sucursal` es retiro_sucursal de la config: si viene, la línea
    de retiro dice dónde; vacía, el texto es el de siempre.
    """
    if tipo_entrega == "envio":
        dir_txt = f" a *{direccion_envio}*" if direccion_envio else ""
        return (
            f"✅ *¡Pago confirmado!*\n\n"
            f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
            f"🚚 Te lo enviamos a domicilio{dir_txt}. Nos comunicamos para coordinar la entrega.\n"
            f"📋 Código de pedido: *{pickup_code}*\n\n"
            f"¡Muchas gracias! {emoji}"
        )
    pickup_line = f"\n{pickup_text}" if pickup_text else ""
    suc = (sucursal or "").strip()
    donde = f" en *{suc}*" if suc else ""
    return (
        f"✅ *¡Pago confirmado!*\n\n"
        f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
        f"🔑 *Tu código de retiro es: {pickup_code}*{pickup_line}\n\n"
        f"Guardalo para presentarlo al retirar{donde}. ¡Muchas gracias! {emoji}"
    )
```

- [ ] **Step 4: Correrlos y ver que pasan**

Mismo comando del Step 2. Esperado: `3 passed`.

- [ ] **Step 5: Tests de la confirmación por MP y por Payway (los de petshop fallan)**

Agregar al final de `tests/test_petshop.py`:

```python
class _CfgPago:
    """Config falsa para la confirmación de pago (MP y Payway)."""
    def __init__(self, extra=None):
        self.v = {"pickup_minutes": "30", **(extra or {})}

    async def get_all(self):
        return dict(self.v)

    async def get_hours(self):
        return {}

    def get_pickup_text(self, hours, minutes):
        return ""


def _mp_aprobado(monkeypatch, ref, cfg_extra=None):
    import app.routers.mp_webhook as mpw
    enviados = []

    class _Pay:
        async def get_payment_info(self, pid):
            return {"status": "approved", "external_reference": ref,
                    "transaction_amount": 9800.0, "payment_method_id": "visa",
                    "additional_info": {"items": [{"title": "Royal Canin 15KG", "quantity": 1}]}}

    class _Orders:
        async def find_by_payment(self, pid):
            return None

        async def create(self, **kw):
            return {"order_id": "ORD-MP", "pickup_code": "654321"}

    class _Wa:
        async def send_text(self, phone, msg):
            enviados.append(msg)
            return True

    monkeypatch.setattr(mpw, "get_payment_service", lambda *a, **k: _Pay())
    monkeypatch.setattr(mpw, "get_order_service", lambda *a: _Orders())
    monkeypatch.setattr(mpw, "get_whatsapp_service", lambda *a: _Wa())
    monkeypatch.setattr(mpw, "get_config_service", lambda *a: _CfgPago(cfg_extra))
    return mpw, enviados


async def test_mp_confirmacion_petshop_con_sucursal(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    mpw, enviados = _mp_aprobado(monkeypatch, "5491100000101_S1", {"retiro_sucursal": "Sucursal Piloto"})
    r = await mpw.procesar_pago("PAGO-PET-MP-1")
    assert r["status"] == "ok"
    assert enviados[0].endswith("al retirar en *Sucursal Piloto*. ¡Muchas gracias! 🐾")
    assert "💊" not in enviados[0]


async def test_mp_confirmacion_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    usar_perfil("farmacia")
    mpw, enviados = _mp_aprobado(monkeypatch, "5491100000102_S1")
    r = await mpw.procesar_pago("PAGO-FARM-MP-1")
    assert r["status"] == "ok"
    assert enviados[0] == _CONF_MP_FARMACIA


def _payway_aprobado(monkeypatch, pid, payway_id, phone, cfg_extra=None):
    import app.routers.payway as pw
    import app.services.config_service as cs
    kv = {f"payway:pending:{pid}": json.dumps({
        "id": pid, "phone": phone, "sku_id": "S1", "sku_nombre": "Royal Canin 15KG",
        "cantidad": 1, "total": 9800.0})}
    enviados = []

    class _Redis:
        async def get(self, k):
            return kv.get(k)

        async def setex(self, k, ttl, v):
            kv[k] = v

    class _Pw:
        async def crear_pago(self, **k):
            return {"id": payway_id, "status": "approved", "card_brand": "Visa"}, None

    class _Orders:
        async def find_by_payment(self, pid):
            return None

        async def create(self, **kw):
            return {"order_id": "ORD-PW", "pickup_code": "654321"}

    class _Wa:
        async def send_text(self, phone, msg):
            enviados.append(msg)
            return True

    # payway.py:216 hace _redis().setex fuera de un try: sin este fake, revienta sin Redis
    monkeypatch.setattr(pw, "_redis", lambda: _Redis())
    monkeypatch.setattr(pw, "get_payway_service", lambda *a, **k: _Pw())
    monkeypatch.setattr(pw, "get_order_service", lambda *a: _Orders())
    monkeypatch.setattr(pw, "get_whatsapp_service", lambda *a: _Wa())
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: _CfgPago(cfg_extra))
    return pw, enviados


async def test_payway_confirmacion_petshop_con_sucursal(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    pw, enviados = _payway_aprobado(monkeypatch, "PID-PET-1", "PW-PET-1", "5491100000201",
                                    {"retiro_sucursal": "Sucursal Piloto"})
    r = await pw.payway_charge(pw.ChargeIn(pid="PID-PET-1", token="tok", bin="450799"))
    assert r == {"status": "approved"}
    assert enviados[0].endswith("al retirar en *Sucursal Piloto*. ¡Muchas gracias! 🐾")
    assert "💊" not in enviados[0]


async def test_payway_confirmacion_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    usar_perfil("farmacia")
    pw, enviados = _payway_aprobado(monkeypatch, "PID-FARM-1", "PW-FARM-1", "5491100000202")
    r = await pw.payway_charge(pw.ChargeIn(pid="PID-FARM-1", token="tok", bin="450799"))
    assert r == {"status": "approved"}
    assert enviados[0] == _CONF_MP_FARMACIA
```

- [ ] **Step 6: Correrlos y ver que fallan los de petshop**

```bash
.venv/Scripts/python -m pytest tests/test_petshop.py::test_mp_confirmacion_petshop_con_sucursal tests/test_petshop.py::test_mp_confirmacion_farmacia_igual_que_hoy tests/test_petshop.py::test_payway_confirmacion_petshop_con_sucursal tests/test_petshop.py::test_payway_confirmacion_farmacia_igual_que_hoy -v
```

Esperado: `2 failed, 2 passed`. Los dos de petshop fallan con `AssertionError` porque el mensaje termina en `Guardalo para presentarlo al retirar. ¡Muchas gracias! 💊`. Los dos de farmacia pasan (son la guarda "igual que hoy").

- [ ] **Step 7: Usar `mensaje_pago_confirmado` en MP y en Payway**

`app/routers/mp_webhook.py`, línea 23. Reemplazar:

```python
from app.services.config_service import get_config_service
```

por:

```python
from app.services.config_service import get_config_service
from app.services.checkout_helper import mensaje_pago_confirmado
from app.services.perfil import get_perfil
```

`app/routers/mp_webhook.py:216-234`. Reemplazar:

```python
    pickup_code = order.get("pickup_code", "")
    pickup_line = f"\n{pickup_text}" if pickup_text else ""

    if tipo_entrega == "envio":
        dir_txt = f" a *{direccion_envio}*" if direccion_envio else ""
        mensaje = (
            f"✅ *¡Pago confirmado!*\n\n"
            f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
            f"🚚 Te lo enviamos a domicilio{dir_txt}. Nos comunicamos para coordinar la entrega.\n"
            f"📋 Código de pedido: *{pickup_code}*\n\n"
            f"¡Muchas gracias! 💊"
        )
    else:
        mensaje = (
            f"✅ *¡Pago confirmado!*\n\n"
            f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
            f"🔑 *Tu código de retiro es: {pickup_code}*{pickup_line}\n\n"
            f"Guardalo para presentarlo al retirar. ¡Muchas gracias! 💊"
        )
```

por:

```python
    pickup_code = order.get("pickup_code", "")
    mensaje = mensaje_pago_confirmado(
        nombre_producto, tipo_entrega, direccion_envio, pickup_code, pickup_text,
        get_perfil().emoji, sucursal=cfg.get("retiro_sucursal") or "",
    )
```

`app/routers/payway.py:24-25`. Reemplazar:

```python
from app.config import get_settings
from app.services.payway_service import get_payway_service
```

por:

```python
from app.config import get_settings
from app.services.checkout_helper import mensaje_pago_confirmado
from app.services.perfil import get_perfil
from app.services.payway_service import get_payway_service
```

`app/routers/payway.py:260-278` (dentro de `payway_charge`, bloque post-pago). Reemplazar:

```python
            pickup_code = order.get("pickup_code", "")
            pickup_line = f"\n{pickup_text}" if pickup_text else ""

            if tipo_entrega == "envio":
                dir_txt = f" a *{direccion_envio}*" if direccion_envio else ""
                mensaje = (
                    f"✅ *¡Pago confirmado!*\n\n"
                    f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
                    f"🚚 Te lo enviamos a domicilio{dir_txt}. Nos comunicamos para coordinar la entrega.\n"
                    f"📋 Código de pedido: *{pickup_code}*\n\n"
                    f"¡Muchas gracias! 💊"
                )
            else:
                mensaje = (
                    f"✅ *¡Pago confirmado!*\n\n"
                    f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
                    f"🔑 *Tu código de retiro es: {pickup_code}*{pickup_line}\n\n"
                    f"Guardalo para presentarlo al retirar. ¡Muchas gracias! 💊"
                )
```

por:

```python
            pickup_code = order.get("pickup_code", "")
            mensaje = mensaje_pago_confirmado(
                nombre_producto, tipo_entrega, direccion_envio, pickup_code, pickup_text,
                get_perfil().emoji, sucursal=cfg.get("retiro_sucursal") or "",
            )
```

- [ ] **Step 8: Correrlos y ver que pasan**

Mismo comando del Step 6, más los de idempotencia de MP que usan el mismo camino:

```bash
.venv/Scripts/python -m pytest tests/test_petshop.py::test_mp_confirmacion_petshop_con_sucursal tests/test_petshop.py::test_mp_confirmacion_farmacia_igual_que_hoy tests/test_petshop.py::test_payway_confirmacion_petshop_con_sucursal tests/test_petshop.py::test_payway_confirmacion_farmacia_igual_que_hoy tests/test_logic.py::TestPagoIdempotente -v
```

Esperado: `9 passed`.

- [ ] **Step 9: Commit**

```bash
git add app/services/checkout_helper.py app/routers/mp_webhook.py app/routers/payway.py tests/test_petshop.py
git commit -m "$(cat <<'EOF'
Confirmación de pago compartida por MP y Payway, con el emoji del perfil y la sucursal de retiro

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 10: Tests del descriptor de la tarjeta y del cobro de Payway (fallan los de petshop)**

Agregar al final de `tests/test_petshop.py`:

```python
class _RespHttp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


def _http_falso(monkeypatch, modulo, status, data):
    """Reemplaza httpx.AsyncClient y devuelve la lista de payloads posteados."""
    capturados = []

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            capturados.append(json)
            return _RespHttp(status, data)

    monkeypatch.setattr(modulo.httpx, "AsyncClient", _Cliente)
    return capturados


async def test_statement_descriptor_por_perfil(usar_perfil, monkeypatch):
    from app.services import payment_service as ps
    cap = _http_falso(monkeypatch, ps, 201, {"init_point": "https://mp.test/x"})
    svc = ps.PaymentService("TEST-TOKEN", "https://bot.test/mp/notification")
    usar_perfil("farmacia")
    link, err = await svc.crear_link(sku_id="S1", nombre="Royal Canin 15KG", precio=9800.0, phone="549")
    assert (link, err) == ("https://mp.test/x", None)
    assert cap[-1]["statement_descriptor"] == "FARMACIA AMI"
    # Misma instancia: el descriptor se lee en cada link, no en __init__
    usar_perfil("petshop")
    await svc.crear_link(sku_id="S1", nombre="Royal Canin 15KG", precio=9800.0, phone="549")
    assert cap[-1]["statement_descriptor"] == "MASCOTAS DEL OESTE"


async def test_statement_descriptor_ascii_y_hasta_22(usar_perfil, monkeypatch):
    from app.services import payment_service as ps
    cap = _http_falso(monkeypatch, ps, 201, {"init_point": "https://mp.test/x"})
    usar_perfil("petshop", comercio="Piñata Mascotas Ñuñoa Sucursal Centro")
    await ps.PaymentService("TEST-TOKEN", "https://bot.test/mp/notification").crear_link(
        sku_id="S1", nombre="Royal Canin 15KG", precio=9800.0, phone="549")
    d = cap[-1]["statement_descriptor"]
    assert d == "PINATA MASCOTAS NUNOA"
    assert d.isascii() and len(d) <= 22


async def test_payway_crear_pago_con_el_comercio_del_perfil(usar_perfil, monkeypatch):
    from app.services import payway_service as pws
    cap = _http_falso(monkeypatch, pws, 201, {"id": 1, "status": "approved"})
    svc = pws.PaywayService("pub", "priv", sandbox=True, cybersource=True)
    usar_perfil("petshop")
    data, err = await svc.crear_pago(token="tok", amount=9800.0, site_transaction_id="t1",
                                     payment_method_id=1, bin="450799", producto="Royal Canin 15KG")
    assert err is None
    pl = cap[-1]
    assert pl["description"] == "Compra Mascotas del Oeste"
    assert pl["fraud_detection"]["bill_to"]["last_name"] == "Mascotas del Oeste"
    assert pl["fraud_detection"]["retail_transaction_data"]["ship_to"]["last_name"] == "Mascotas del Oeste"
    assert pl["device_unique_identifier"] == "mascotas-del-oeste-web"
    assert pl["fraud_detection"]["device_unique_identifier"] == "mascotas-del-oeste-web"
    # Con el fingerprint del navegador, se usa ese
    await svc.crear_pago(token="tok", amount=9800.0, site_transaction_id="t2",
                         payment_method_id=1, bin="450799", device_id="fp-123")
    assert cap[-1]["device_unique_identifier"] == "fp-123"


async def test_payway_crear_pago_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    from app.services import payway_service as pws
    cap = _http_falso(monkeypatch, pws, 201, {"id": 1, "status": "approved"})
    usar_perfil("farmacia")
    await pws.PaywayService("pub", "priv", sandbox=True, cybersource=True).crear_pago(
        token="tok", amount=4770.0, site_transaction_id="t1", payment_method_id=1, bin="450799")
    pl = cap[-1]
    assert pl["description"] == "Compra Remedia"
    assert pl["fraud_detection"]["bill_to"]["last_name"] == "Remedia"
    assert pl["device_unique_identifier"] == "remedia-web"
```

- [ ] **Step 11: Correrlos y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_petshop.py::test_statement_descriptor_por_perfil tests/test_petshop.py::test_statement_descriptor_ascii_y_hasta_22 tests/test_petshop.py::test_payway_crear_pago_con_el_comercio_del_perfil tests/test_petshop.py::test_payway_crear_pago_farmacia_igual_que_hoy -v
```

Esperado: `3 failed, 1 passed`. `AssertionError: assert 'FARMACIA AMI' == 'MASCOTAS DEL OESTE'`, `assert 'FARMACIA AMI' == 'PINATA MASCOTAS NUNOA'` y `assert 'Compra Remedia' == 'Compra Mascotas del Oeste'`. Pasa `test_payway_crear_pago_farmacia_igual_que_hoy` (guarda).

- [ ] **Step 12: Implementar descriptor y comercio de Payway**

`app/services/payment_service.py:1-8`. Reemplazar:

```python
import httpx
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

MP_BASE_URL = "https://api.mercadopago.com"
```

por:

```python
import httpx
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.services.perfil import get_perfil

logger = logging.getLogger(__name__)

MP_BASE_URL = "https://api.mercadopago.com"


def statement_descriptor() -> str:
    """
    Texto del resumen de la tarjeta. El del perfil si lo tiene (farmacia:
    "FARMACIA AMI"); si no, el nombre del comercio en mayúsculas, sin tildes
    ni ñ (solo A-Z, 0-9 y espacios) y hasta 22 caracteres. Se lee en cada
    link: nunca queda fijado en el singleton.
    """
    p = get_perfil()
    if p.descriptor_tarjeta:
        return p.descriptor_tarjeta
    plano = unicodedata.normalize("NFKD", p.comercio).encode("ascii", "ignore").decode("ascii")
    limpio = " ".join(re.sub(r"[^A-Z0-9 ]+", " ", plano.upper()).split())
    return limpio[:22].strip()
```

`app/services/payment_service.py:48`. Reemplazar:

```python
            "statement_descriptor": "FARMACIA AMI",
```

por:

```python
            "statement_descriptor": statement_descriptor(),
```

`app/services/payway_service.py:20-25`. Reemplazar:

```python
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)
```

por:

```python
import logging
import re
import unicodedata
from typing import Optional

import httpx

from app.services.perfil import get_perfil

logger = logging.getLogger(__name__)
```

`app/services/payway_service.py:40`. Antes de `def _xsource() -> str:` agregar:

```python
def _slug(texto: str) -> str:
    """'Mascotas del Oeste' → 'mascotas-del-oeste'; 'Remedia' → 'remedia'."""
    plano = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", plano.lower()).strip("-")


```

`app/services/payway_service.py:150-151`. Reemplazar:

```python
    def _fraud_detection(self, amount: float, email: str, device_id: str,
                         producto: str = "Producto") -> dict:
```

por:

```python
    def _fraud_detection(self, amount: float, email: str, device_id: str,
                         producto: str = "Producto", comercio: str = "") -> dict:
```

`app/services/payway_service.py:164`. Reemplazar:

```python
            "last_name": "Remedia",
```

por:

```python
            "last_name": comercio or get_perfil().comercio,
```

`app/services/payway_service.py:225-226` (inicio de `crear_pago`). Reemplazar:

```python
        payload = {
            "site_transaction_id": site_transaction_id,
```

por:

```python
        comercio = get_perfil().comercio
        payload = {
            "site_transaction_id": site_transaction_id,
```

`app/services/payway_service.py:233`. Reemplazar:

```python
            "description": "Compra Remedia",
```

por:

```python
            "description": f"Compra {comercio}",
```

`app/services/payway_service.py:239-240`. Reemplazar:

```python
            dev = device_id or "remedia-web"
            payload["fraud_detection"] = self._fraud_detection(amount, email, dev, producto)
```

por:

```python
            dev = device_id or f"{_slug(comercio)}-web"
            payload["fraud_detection"] = self._fraud_detection(amount, email, dev, producto, comercio)
```

- [ ] **Step 13: Correrlos y ver que pasan; commit**

Mismo comando del Step 11. Esperado: `4 passed`.

```bash
git add app/services/payment_service.py app/services/payway_service.py tests/test_petshop.py
git commit -m "$(cat <<'EOF'
Descriptor de tarjeta de MP y datos de Payway salen del perfil en cada cobro

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 13b: Review Focus — `VERTICAL` y `COMERCIO_NOMBRE` como se cargan en Railway (pasa de entrada)**

Ver "Review Focus", punto 4. La normalización de `VERTICAL` está probada en `get_perfil` (Task 2) y el nombre con ñ en el descriptor y en `/pay` (arriba y Step 14), pero ningún test cobra por Payway con un `VERTICAL` en mayúsculas con espacios y un `COMERCIO_NOMBRE` con ñ, tildes y espacios: el `device_unique_identifier` de respaldo tiene que salir ASCII y la descripción, con el nombre recortado. Agregar al final de `tests/test_petshop.py`:

```python
# ── Review Focus: VERTICAL y COMERCIO_NOMBRE como los cargan en Railway ─────────
async def test_payway_con_vertical_en_mayusculas_y_comercio_con_enie_y_tildes(usar_perfil,
                                                                              monkeypatch):
    from app.services import payway_service as pws
    cap = _http_falso(monkeypatch, pws, 201, {"id": 1, "status": "approved"})
    p = usar_perfil(" PETSHOP ", comercio="  Ñandú Mascotas Güemes  ")
    assert (p.clave, p.comercio) == ("petshop", "Ñandú Mascotas Güemes")
    await pws.PaywayService("pub", "priv", sandbox=True, cybersource=True).crear_pago(
        token="tok", amount=9800.0, site_transaction_id="t1", payment_method_id=1, bin="450799")
    pl = cap[-1]
    assert pl["description"] == "Compra Ñandú Mascotas Güemes"
    assert pl["fraud_detection"]["bill_to"]["last_name"] == "Ñandú Mascotas Güemes"
    assert pl["device_unique_identifier"] == "nandu-mascotas-guemes-web"
    assert pl["fraud_detection"]["device_unique_identifier"] == "nandu-mascotas-guemes-web"
```

```bash
.venv/Scripts/python -m pytest tests/test_petshop.py::test_payway_con_vertical_en_mayusculas_y_comercio_con_enie_y_tildes -v
```

Esperado: `1 passed`.

```bash
git add tests/test_petshop.py
git commit -m "$(cat <<'EOF'
Review Focus: Payway con VERTICAL en mayúsculas y COMERCIO_NOMBRE con ñ y tildes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 14: Tests de /pay y de las páginas de estado (snapshot de farmacia en verde, petshop falla)**

El snapshot usa el mismo pendiente y los mismos fakes que el de la Task 1 (`pid="abc123"`, `IBUPROFENO 600 MG X 10`, `ORG`/`MERCH`, `PUBKEY`); los hashes se tomaron sobre `07a1d7a`. Agregar al final de `tests/test_petshop.py`:

```python
# ── /pay y páginas de estado ────────────────────────────────────────────────────
# Mismo pendiente y mismos fakes que el snapshot de farmacia de la Task 1.
_PENDIENTE_PAY = {"total": "4770.0", "sku_nombre": "IBUPROFENO 600 MG X 10", "estado": "pendiente"}
_SNAPSHOT_FARMACIA = {
    "pay": "85c4271036760ed8f4185d0d2c860bf6c7bfb926d2d36e4a882283a22f84ad52",
    "vencido": "a23c99347472476c32a97742e46978de209ec2c2a8d4165ed33386df16f47283",
    "ok": "0b8cb84ff34ec20836cd88e807afa23f41e8e0b2e9b3b5fdba629ac1d0e44eea",
    "cancelado": "a975ad681e7c07c62cf5ef0c9260c8cd109c578870b0d820376e98fa31225994",
}


def _pay_falso(monkeypatch):
    import app.routers.payway as pw

    async def _pend(pid):
        return dict(_PENDIENTE_PAY) if pid == "abc123" else None
    monkeypatch.setattr(pw, "_get_pending", _pend)
    monkeypatch.setattr(pw, "get_payway_service", lambda *a, **k: SimpleNamespace(
        base_url="https://pw.test/api/v2", public_key="PUBKEY"))
    monkeypatch.setattr(pw, "get_settings", lambda: SimpleNamespace(
        payway_public_key="", payway_private_key="", payway_sandbox=True,
        payway_site_id="", payway_template_id="", payway_cybersource=False,
        payway_cs_org_id="ORG", payway_cs_merchant_id="MERCH"))
    return pw


async def _paginas(pw) -> dict:
    return {"pay": (await pw.pay_page("abc123")).body,
            "vencido": (await pw.pay_page("vencido")).body,
            "ok": (await pw.payway_return("ok")).body,
            "cancelado": (await pw.payway_return("")).body}


@pytest.mark.parametrize("clave", ["farmacia", "mutual"])
async def test_paginas_de_pago_farmacia_identicas_al_snapshot(usar_perfil, monkeypatch, clave):
    usar_perfil(clave)
    pw = _pay_falso(monkeypatch)
    paginas = await _paginas(pw)
    assert {k: hashlib.sha256(v).hexdigest() for k, v in paginas.items()} == _SNAPSHOT_FARMACIA


async def test_paginas_de_pago_petshop(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    pw = _pay_falso(monkeypatch)
    paginas = {k: v.decode("utf-8") for k, v in (await _paginas(pw)).items()}
    assert "<title>Pagar · Mascotas del Oeste</title>" in paginas["pay"]
    assert "<title>Mascotas del Oeste</title>" in paginas["ok"]
    for nombre, pagina in paginas.items():
        assert '<div class="logo">M</div>' in pagina, nombre
        assert '<div class="wordmark">Mascotas del Oeste</div>' in pagina, nombre
        assert "Pago seguro procesado por Payway · Mascotas del Oeste" in pagina, nombre
        assert "Remed" not in pagina and "Farmacia" not in pagina, nombre
    # Lo demás de la página no cambia: producto, total y claves del formulario
    assert "IBUPROFENO 600 MG X 10" in paginas["pay"] and "4,770.00" in paginas["pay"]
    assert 'PUBLIC_KEY="PUBKEY"' in paginas["pay"]


async def test_paginas_de_pago_escapan_el_nombre_del_comercio(usar_perfil, monkeypatch):
    usar_perfil("petshop", comercio="Ñandú & Cía <MO>")
    pw = _pay_falso(monkeypatch)
    pagina = (await pw.pay_page("abc123")).body.decode("utf-8")
    assert '<div class="logo">Ñ</div>' in pagina
    assert '<div class="wordmark">Ñandú &amp; Cía &lt;MO&gt;</div>' in pagina
    assert "<title>Pagar · Ñandú &amp; Cía &lt;MO&gt;</title>" in pagina
    assert "<MO>" not in pagina
```

- [ ] **Step 15: Correrlos: el snapshot pasa y petshop falla**

```bash
.venv/Scripts/python -m pytest tests/test_petshop.py::test_paginas_de_pago_farmacia_identicas_al_snapshot tests/test_petshop.py::test_paginas_de_pago_petshop tests/test_petshop.py::test_paginas_de_pago_escapan_el_nombre_del_comercio -v
```

Esperado: `2 failed, 2 passed`. Los dos parámetros del snapshot (farmacia y mutual) pasan con el código de hoy (golden tomado antes del cambio). Fallan `test_paginas_de_pago_petshop` (`assert '<title>Pagar · Mascotas del Oeste</title>' in ...`) y `test_paginas_de_pago_escapan_el_nombre_del_comercio` (`assert '<div class="logo">Ñ</div>' in ...`).

- [ ] **Step 16: Placeholders de marca en /pay y en las páginas de estado**

`app/routers/payway.py:13-14`. Reemplazar:

```python
import json
import logging
```

por:

```python
import html
import json
import logging
```

`app/routers/payway.py:60-61`. Reemplazar:

```python
def _status_page(variante: str, titulo: str, sub: str) -> str:
    """Página de estado con la identidad Remedia. variante: ok | warn | err."""
```

por:

```python
def _con_marca(pagina: str) -> str:
    """Pone la marca del perfil de rubro en la página: logo, wordmark, título y
    razón social del pie. Se resuelve en cada request (nunca al importar)."""
    p = get_perfil()
    return (pagina
            .replace("{{LOGO}}", html.escape(p.comercio[:1].upper()))
            .replace("{{WORDMARK}}", p.wordmark_html or html.escape(p.comercio))
            .replace("{{COMERCIO}}", html.escape(p.comercio))
            .replace("{{RAZON_SOCIAL}}", html.escape(p.razon_social or p.comercio)))


def _status_page(variante: str, titulo: str, sub: str) -> str:
    """Página de estado con la identidad del comercio. variante: ok | warn | err."""
```

`app/routers/payway.py:67`. Reemplazar:

```python
    return (_STATUS_HTML
            .replace("{{VARIANTE}}", variante)
```

por:

```python
    return (_con_marca(_STATUS_HTML)
            .replace("{{VARIANTE}}", variante)
```

`app/routers/payway.py:91-92` (`pay_page`). Reemplazar:

```python
    return HTMLResponse(_PAY_HTML
                        .replace("{{PID}}", pid)
```

por:

```python
    return HTMLResponse(_con_marca(_PAY_HTML)
                        .replace("{{PID}}", pid)
```

`app/routers/payway.py:532`. Reemplazar:

```python
# ── Identidad visual Remedia (compartida por la página de pago y las de estado) ──
```

por:

```python
# ── Identidad visual (compartida por la página de pago y las de estado) ────────
# La paleta es fija; la marca ({{LOGO}}, {{WORDMARK}}, {{COMERCIO}},
# {{RAZON_SOCIAL}}) sale del perfil de rubro en cada request (_con_marca).
```

`app/routers/payway.py:571`. Reemplazar:

```python
_MARCA_HTML = """<div class="marca"><div class="logo">R</div><div class="wordmark">Remed<b>IA</b></div></div>"""
```

por:

```python
_MARCA_HTML = """<div class="marca"><div class="logo">{{LOGO}}</div><div class="wordmark">{{WORDMARK}}</div></div>"""
```

`app/routers/payway.py:575`. Reemplazar:

```python
Pago seguro procesado por Payway · Farmacia Mutual Independencia</div>"""
```

por:

```python
Pago seguro procesado por Payway · {{RAZON_SOCIAL}}</div>"""
```

`app/routers/payway.py:580`. Reemplazar:

```python
<title>Pagar · Remedia</title>
```

por:

```python
<title>Pagar · {{COMERCIO}}</title>
```

`app/routers/payway.py:727`. Reemplazar:

```python
<title>Remedia</title>
```

por:

```python
<title>{{COMERCIO}}</title>
```

Paleta, textos y JS no cambian. La marca se reemplaza antes que `{{NOMBRE}}`, `{{TITULO}}` y `{{SUB}}`, así un dato del pedido nunca se interpreta como placeholder.

- [ ] **Step 17: Correrlos y ver que pasan; commit**

Mismo comando del Step 15. Esperado: `4 passed` (el snapshot de farmacia y mutual sigue idéntico byte a byte). Correr también el snapshot de /pay que dejó la Task 1, esté en el archivo que esté:

```bash
.venv/Scripts/python -m pytest tests -q -k "snapshot or pay_page or paginas"
```

Esperado: `0 failed`.

```bash
git add app/routers/payway.py tests/test_petshop.py
git commit -m "$(cat <<'EOF'
Página /pay y páginas de estado con la marca del perfil (logo, wordmark, título y pie)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 18: Avisos de pedido listo y de efectivo con la clave vacía**

Estos dos tests son los del spec 6.2 (Pagos) para los avisos. El código lo cambió la Task 3 (§3.4, `perfil.textos[...]` como fallback en `orders_api.py:162-173` y `checkout_helper.py:1695-1705`): acá no hay código nuevo. Si esa tarea ya corrió, pasan en el primer intento; si fallan con `AssertionError` (el texto termina en 💊), falta ese cambio y se resuelve en esa tarea, no acá. Agregar al final de `tests/test_petshop.py`:

```python
# ── Avisos de pedido listo y de efectivo: textos del perfil (§3.4) ──────────────
def test_aviso_pedido_listo_petshop_con_clave_vacia(usar_perfil):
    from app.routers.orders_api import armar_mensaje_pedido_listo
    usar_perfil("petshop")
    base = {"sku_nombre": "Royal Canin 15KG", "total": 9800, "pickup_code": "654321"}
    retiro = armar_mensaje_pedido_listo({**base, "tipo_entrega": "retiro"},
                                        {"pedido_listo_retiro_message": ""})
    envio = armar_mensaje_pedido_listo({**base, "tipo_entrega": "envio", "direccion_envio": "Mitre 100"},
                                       {"pedido_listo_envio_message": ""})
    assert retiro.endswith("¡Te esperamos! 🐾") and "654321" in retiro
    assert envio.endswith("Te avisamos cuando esté en camino. 🐾") and "Mitre 100" in envio
    assert "💊" not in retiro + envio


async def test_efectivo_petshop_con_clave_vacia(usar_perfil, monkeypatch):
    from app.services.checkout_helper import _cerrar_venta_efectivo
    from app.services.session_service import SessionService
    import app.services.order_service as omod

    class _Orders:
        async def create(self, **kw):
            return {"order_id": "ORD-EF", "pickup_code": "445566"}
    monkeypatch.setattr(omod, "_instance", _Orders())
    usar_perfil("petshop")
    ss = SessionService("redis://127.0.0.1:1")
    s = {"pending_sku_id": "S1", "pending_sku_nombre": "Royal Canin 15KG",
         "pending_precio": 9800.0, "pending_cantidad": 1}
    retiro = await _cerrar_venta_efectivo(ss, "549EF1", s, "retiro", None, total=9800.0,
                                          cfg={"efectivo_retiro_message": ""})
    envio = await _cerrar_venta_efectivo(ss, "549EF2", s, "envio", "Mitre 100", total=9800.0,
                                         cfg={"efectivo_envio_message": ""})
    assert retiro.endswith("¡Muchas gracias! 🐾") and "445566" in retiro
    assert envio.endswith("¡Muchas gracias! 🐾") and "Mitre 100" in envio
    assert "💊" not in retiro + envio
```

```bash
.venv/Scripts/python -m pytest tests/test_petshop.py::test_aviso_pedido_listo_petshop_con_clave_vacia tests/test_petshop.py::test_efectivo_petshop_con_clave_vacia -v
```

Esperado: `2 passed`.

- [ ] **Step 19: Suite completa**

```bash
.venv/Scripts/python -m pytest -q
```

Esperado: `0 failed`. El total sube en 18 respecto de la corrida de la tarea anterior (933 de base + las tareas previas + 18 de esta: 17 y el de Review Focus). Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1153 tests. Control de literales de marca:

```bash
grep -rn "FARMACIA AMI\|Compra Remedia\|remedia-web\|Remed<b>IA\|Farmacia Mutual Independencia" app/routers app/services/payment_service.py app/services/payway_service.py
```

Esperado: una sola línea, el docstring de `statement_descriptor` en `app/services/payment_service.py`. Los valores de farmacia viven en `app/services/perfil.py`.

- [ ] **Step 20: Commit**

```bash
git add tests/test_petshop.py
git commit -m "$(cat <<'EOF'
Tests de avisos de pedido listo y efectivo con los textos del perfil petshop

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Arranque, catálogo y venta por capacidad

Spec: §4.8 completo (`_restaurar_archivos` y padrón, aviso de `DEFAULT_HOURS`, `CSV_FARMACIA` y `csv_de_arranque`, `_csv_permitido`, desvío `if not get_perfil().venta`, tablero con `get_perfil().clave`), §4.2 fila `main.py` (`_init_referencia_receta`) y §6.2 bullets "Arranque", "Catálogo", "Tablero" y la fila "Venta" de las conversaciones.

Las líneas citadas son las de `07a1d7a`. Para esta tarea las tareas anteriores ya corrieron líneas de `main.py`, `webhook.py` y `backoffice.py`: ubicar cada bloque por el texto de "Reemplazar" (único en el archivo).

**Files:**
- Modify: `app/main.py:29-48` (restauración), `:92-93` (config cargada), `:102-115` (padrón en Postgres), `:133-137` (referencia de recetas), `:219` (helpers nuevos antes de `_migrar`)
- Modify: `app/services/catalog_source.py:11-13` (imports), `:23-24` (después de `_conflicto`)
- Modify: `app/services/sku_service.py:687-697` (`get_sku_service`, `reload_sku_service`)
- Modify: `app/routers/webhook.py:1170-1171` (desvío a `_flujo_mutual`)
- Modify: `app/routers/backoffice.py:596-602` (`bo_tablero`)
- Create: `tests/test_perfil_arranque.py`

**Interfaces:**
- Consumes: `get_perfil() -> Perfil` con `socios`, `recetas`, `catalogo_csv_base`, `venta` y `clave` (Task 2); en el `lifespan` de `main.py`, la variable local `perfil = get_perfil()` que la Task 2 agrega después de `logging.basicConfig` (spec §3.5); `from app.services.perfil import get_perfil` a nivel de módulo en `webhook.py` (Task 3); fixture `usar_perfil` (Task 1); `entorno`, `_msg` y `PHONE` de `tests/test_webhook_secuencias.py`.
- Produces:
  - `app/main.py`: `async def _restaurar_archivos(settings, perfil, blob) -> None`; `async def _init_referencia_receta(db) -> dict | None`; **fuera del contrato**: `async def _hidratar_padron(settings, perfil, db) -> None` y `async def _avisar_horario_por_defecto(cfg_svc) -> bool`.
  - `app/services/catalog_source.py`: `CSV_FARMACIA: Path`; `def csv_de_arranque(ruta: str) -> str`.
  - `app/services/sku_service.py`: `def _csv_permitido(csv_path: str) -> str` (privada); `get_sku_service` y `reload_sku_service` construyen `SKUService(_csv_permitido(csv_path))`.
  - `webhook.py`: el desvío a `_flujo_mutual` pasa a `if not get_perfil().venta:`. `bo_tablero` usa `get_perfil().clave`.

- [ ] **Step 1: Escribir los tests de arranque**

Crear `tests/test_perfil_arranque.py`:

```python
"""
Arranque, catálogo y venta por perfil de rubro (spec 4.8 y 4.2, fila main.py):
restauración de archivos, padrón, referencia de recetas, aviso de horario, el
CSV de la farmacia que petshop nunca carga, el tablero y el desvío sin venta.
"""
import logging
from types import SimpleNamespace

import pytest

from app.routers import webhook as wh
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (fixture reusada)


# ── _restaurar_archivos ─────────────────────────────────────────────────────────
class _BlobFalso:
    def __init__(self, archivos):
        self.archivos = archivos
        self.pedidos = []

    async def load(self, nombre):
        self.pedidos.append(nombre)
        return self.archivos.get(nombre)


async def test_restaurar_sin_sku_csv_path_no_tira_y_restaura_el_padron(tmp_path, usar_perfil):
    from app.main import _restaurar_archivos
    perfil = usar_perfil("farmacia")
    settings = SimpleNamespace(sku_csv_path="", socios_path=str(tmp_path / "socios.csv"))
    blob = _BlobFalso({"catalogo": (b"SKU,Nombre\n1,X\n", ""),
                       "socios": (b"PK\x03\x04padron", ".xlsx")})
    await _restaurar_archivos(settings, perfil, blob)
    destino = tmp_path / "socios.xlsx"
    assert destino.read_bytes() == b"PK\x03\x04padron"
    assert settings.socios_path == str(destino)


async def test_restaurar_escribe_el_catalogo_si_hay_ruta(tmp_path, usar_perfil):
    from app.main import _restaurar_archivos
    perfil = usar_perfil("farmacia")
    ruta = tmp_path / "cat" / "catalogo.csv"
    settings = SimpleNamespace(sku_csv_path=str(ruta), socios_path=str(tmp_path / "socios.csv"))
    await _restaurar_archivos(settings, perfil, _BlobFalso({"catalogo": (b"SKU,Nombre\n1,X\n", "")}))
    assert ruta.read_bytes() == b"SKU,Nombre\n1,X\n"


async def test_restaurar_petshop_no_toca_el_padron(tmp_path, usar_perfil, caplog):
    from app.main import _restaurar_archivos
    perfil = usar_perfil("petshop")
    settings = SimpleNamespace(sku_csv_path="", socios_path=str(tmp_path / "socios.csv"))
    blob = _BlobFalso({"socios": (b"PK\x03\x04padron", ".xlsx")})
    with caplog.at_level(logging.INFO, logger="app.main"):
        await _restaurar_archivos(settings, perfil, blob)
    assert "socios" not in blob.pedidos
    assert list(tmp_path.iterdir()) == []
    assert settings.socios_path == str(tmp_path / "socios.csv")
    assert "Perfil sin socios: no se carga el padrón" in caplog.text


# ── Padrón en Postgres ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("clave,llamadas", [("farmacia", 1), ("mutual", 1), ("petshop", 0)])
async def test_hidratar_padron_segun_perfil(usar_perfil, monkeypatch, clave, llamadas):
    import app.services.socio_service as socio_mod
    from app.main import _hidratar_padron
    vistos = []

    async def _cargar(db, svc):
        vistos.append(db)
        return 5

    monkeypatch.setattr(socio_mod, "cargar_desde_db", _cargar)
    monkeypatch.setattr(socio_mod, "get_socio_service", lambda path: SimpleNamespace(total=0))

    class _DB:
        def available(self):
            return True

    perfil = usar_perfil(clave)
    await _hidratar_padron(SimpleNamespace(socios_path="data/socios.csv"), perfil, _DB())
    assert len(vistos) == llamadas


# ── Referencia de recetas (spec 4.2, fila main.py) ──────────────────────────────
@pytest.mark.parametrize("clave,llamadas", [("farmacia", 1), ("mutual", 1), ("petshop", 0)])
async def test_init_referencia_receta_segun_perfil(usar_perfil, monkeypatch, caplog,
                                                   clave, llamadas):
    import app.services.receta_referencia as rr
    from app.main import _init_referencia_receta
    vistos = []

    async def _inicializar(db):
        vistos.append(db)
        return {"referencia": 3}

    monkeypatch.setattr(rr, "inicializar", _inicializar)
    usar_perfil(clave)
    with caplog.at_level(logging.INFO, logger="app.main"):
        r = await _init_referencia_receta("DB")
    assert vistos == ["DB"] * llamadas
    if llamadas:
        assert r == {"referencia": 3}
    else:
        assert r is None
        assert "Perfil sin recetas" in caplog.text


# ── Aviso de horario por defecto ────────────────────────────────────────────────
class _CfgHoras:
    def __init__(self, horas):
        self.horas = horas

    async def get_hours(self):
        return self.horas


async def test_avisa_si_el_horario_es_el_de_defecto(caplog):
    from app.main import _avisar_horario_por_defecto
    from app.services.config_service import DEFAULT_HOURS
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert await _avisar_horario_por_defecto(_CfgHoras(dict(DEFAULT_HOURS))) is True
    assert "Horario no cargado: se usa DEFAULT_HOURS" in caplog.text

    caplog.clear()
    cargado = {**DEFAULT_HOURS, "enabled": True}
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert await _avisar_horario_por_defecto(_CfgHoras(cargado)) is False
    assert "DEFAULT_HOURS" not in caplog.text
```

- [ ] **Step 2: Correrlos y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_arranque.py -v`
Expected: 10 FAILED con `ImportError: cannot import name '_restaurar_archivos' from 'app.main'` (y lo mismo con `_hidratar_padron`, `_init_referencia_receta` y `_avisar_horario_por_defecto`).

- [ ] **Step 3: Agregar los cuatro helpers en `main.py`**

En `app/main.py`, justo antes de `def _migrar():` (línea 219). Reemplazar:
```python
def _migrar():
```
por:
```python
async def _restaurar_archivos(settings, perfil, blob) -> None:
    """
    Restaura desde Redis el catálogo y el padrón subidos (el filesystem de
    Railway es efímero). Sin SKU_CSV_PATH no hay dónde escribir el catálogo:
    antes Path("").write_bytes reventaba y el except del lifespan salteaba
    también el padrón. Un perfil sin socios no restaura el padrón (spec 4.8).
    """
    logger = logging.getLogger(__name__)
    cat = await blob.load("catalogo")
    if cat and settings.sku_csv_path:
        p = Path(settings.sku_csv_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(cat[0])
        logger.info(f"Catálogo restaurado desde Redis ({len(cat[0])} bytes)")
    if not perfil.socios:
        logger.info("Perfil sin socios: no se carga el padrón")
        return
    soc = await blob.load("socios")
    if soc:
        data, ext = soc
        dest = Path(settings.socios_path).with_suffix(ext or ".csv")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        settings.socios_path = str(dest)
        logger.info(f"Padrón restaurado desde Redis ({len(data)} bytes → {dest.name})")


async def _hidratar_padron(settings, perfil, db) -> None:
    """
    Padrón de socios en Postgres (24/9): si la tabla tiene filas, el
    singleton se carga desde ahí (pisa lo leído del archivo); si está vacía
    y el archivo cargó socios, se siembra la tabla. Un perfil sin socios no
    lo toca: defensa en profundidad, con el padrón vacío find_by_phone ya
    devuelve None (spec 4.8).
    """
    if not perfil.socios or not db.available():
        return
    from app.services.socio_service import (get_socio_service, guardar_en_db,
                                             cargar_desde_db as cargar_socios_db)
    logger = logging.getLogger(__name__)
    _socio_svc = get_socio_service(settings.socios_path)
    _n_db = await cargar_socios_db(db, _socio_svc)
    if _n_db:
        logger.info(f"Padrón de socios cargado desde Postgres: {_n_db} socios")
    elif _socio_svc.total:
        await guardar_en_db(db, _socio_svc)
        logger.info(f"Padrón de socios sembrado en Postgres desde el archivo: "
                    f"{_socio_svc.total} socios")


async def _init_referencia_receta(db):
    """
    Receta por código de barras (24/9): carga la referencia vigente desde
    Postgres y recalcula requiere_receta de todo catalog_items. Un perfil
    sin recetas no la carga ni recalcula (spec 4.2). Devuelve el resumen de
    inicializar, o None.
    """
    from app.services.perfil import get_perfil
    logger = logging.getLogger(__name__)
    if not get_perfil().recetas:
        logger.info("Perfil sin recetas: no se carga la referencia ni se recalcula el catálogo")
        return None
    from app.services.receta_referencia import inicializar as _init_receta
    _r = await asyncio.wait_for(_init_receta(db), timeout=60.0)
    logger.info(f"Referencia de receta: {_r}")
    return _r


async def _avisar_horario_por_defecto(cfg_svc) -> bool:
    """
    Sin horario guardado (ni en Redis ni en Postgres), get_hours cae en
    DEFAULT_HOURS en silencio y el bot promete L a V de 9 a 18. Solo avisa
    en el log (spec 4.8); devuelve True si avisó.
    """
    from app.services.config_service import DEFAULT_HOURS
    if await cfg_svc.get_hours() != DEFAULT_HOURS:
        return False
    logging.getLogger(__name__).warning("Horario no cargado: se usa DEFAULT_HOURS")
    return True


def _migrar():
```
`receta_referencia.inicializar` se importa adentro de la función (como hoy en el lifespan): así el test lo reemplaza con `monkeypatch`.

- [ ] **Step 4: Usar los helpers en el `lifespan`**

En `app/main.py`, dentro de `lifespan`:

(a) Restauración (líneas 29-48). Reemplazar:
```python
    # Restaurar archivos subidos (catálogo/padrón) desde Redis — el filesystem
    # de Railway es efímero y se borra en cada deploy.
    try:
        blob = get_blob_store(settings.redis_url)
        cat = await blob.load("catalogo")
        if cat:
            p = Path(settings.sku_csv_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(cat[0])
            logger.info(f"Catálogo restaurado desde Redis ({len(cat[0])} bytes)")
        soc = await blob.load("socios")
        if soc:
            data, ext = soc
            dest = Path(settings.socios_path).with_suffix(ext or ".csv")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            settings.socios_path = str(dest)
            logger.info(f"Padrón restaurado desde Redis ({len(data)} bytes → {dest.name})")
    except Exception as e:
        logger.warning(f"No se pudieron restaurar archivos desde Redis: {e}")
```
por:
```python
    # Restaurar archivos subidos (catálogo/padrón) desde Redis — el filesystem
    # de Railway es efímero y se borra en cada deploy.
    try:
        await _restaurar_archivos(settings, perfil, get_blob_store(settings.redis_url))
    except Exception as e:
        logger.warning(f"No se pudieron restaurar archivos desde Redis: {e}")
```
(`perfil` es la variable que la Task 2 define al principio del `lifespan`.)

(b) Aviso de horario, después de cargar la config (líneas 92-93). Reemplazar:
```python
            logger.info(f"Config cargada ({len(cfg_actual)} claves, "
                        f"descuento socio: {cfg_actual.get('socio_discount_pct')}%)")
```
por:
```python
            logger.info(f"Config cargada ({len(cfg_actual)} claves, "
                        f"descuento socio: {cfg_actual.get('socio_discount_pct')}%)")
            await _avisar_horario_por_defecto(_cfg_svc)
```

(c) Padrón en Postgres (líneas 102-115). El comentario de arriba y el `logger.warning` del `except` quedan. Reemplazar:
```python
        try:
            from app.services.socio_service import (get_socio_service, guardar_en_db,
                                                     cargar_desde_db as cargar_socios_db)
            _db_socios = get_db(settings.database_url)
            if _db_socios.available():
                _socio_svc = get_socio_service(settings.socios_path)
                _n_db = await cargar_socios_db(_db_socios, _socio_svc)
                if _n_db:
                    logger.info(f"Padrón de socios cargado desde Postgres: {_n_db} socios")
                elif _socio_svc.total:
                    await guardar_en_db(_db_socios, _socio_svc)
                    logger.info(f"Padrón de socios sembrado en Postgres desde el archivo: "
                                f"{_socio_svc.total} socios")
        except Exception as e:
```
por:
```python
        try:
            await _hidratar_padron(settings, perfil, get_db(settings.database_url))
        except Exception as e:
```
El bloque de empleados (118-125) no se toca: `descuento_para` ya lo apaga.

(d) Referencia de recetas (líneas 133-137). El comentario de arriba y el `logger.error` del `except` quedan. Reemplazar:
```python
        try:
            from app.services.receta_referencia import inicializar as _init_receta
            _r = await asyncio.wait_for(_init_receta(get_db(settings.database_url)), timeout=60.0)
            logger.info(f"Referencia de receta: {_r}")
        except Exception as e:
```
por:
```python
        try:
            await _init_referencia_receta(get_db(settings.database_url))
        except Exception as e:
```

- [ ] **Step 5: Correr y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_arranque.py -v`
Expected: 10 passed.

Run: `grep -n "blob.load\|inicializar as _init_receta\|cargar_desde_db as cargar_socios_db" app/main.py`
Expected: solo líneas adentro de `_restaurar_archivos`, `_hidratar_padron` e `_init_referencia_receta`.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_perfil_arranque.py
git commit -F - <<'EOF'
Arranque por perfil: archivos, padron, recetas y aviso de horario

_restaurar_archivos no revienta con SKU_CSV_PATH vacio y no restaura el
padron sin socios; _hidratar_padron y _init_referencia_receta respetan las
capacidades socios y recetas; sin horario cargado se avisa en el log.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 7: Escribir los tests del catálogo**

Agregar al final de `tests/test_perfil_arranque.py`:

```python
# ── Catálogo: el CSV de la farmacia nunca entra en petshop ──────────────────────
@pytest.fixture
def sku_singleton(monkeypatch):
    """reload_sku_service pisa el singleton: se restaura al terminar el test."""
    from app.services import sku_service as sk
    monkeypatch.setattr(sk, "_instance", None)
    return sk


def test_csv_de_arranque_petshop_rechaza_el_csv_de_la_farmacia(usar_perfil, caplog):
    from app.services.catalog_source import CSV_FARMACIA, csv_de_arranque
    usar_perfil("petshop")
    with caplog.at_level(logging.ERROR, logger="app.services.catalog_source"):
        assert csv_de_arranque("data/catalogo_base.csv") == ""
    assert any(r.levelno == logging.ERROR and "catálogo de la farmacia" in r.getMessage()
               for r in caplog.records)
    assert csv_de_arranque(str(CSV_FARMACIA)) == ""
    assert csv_de_arranque("") == ""
    assert csv_de_arranque("data/catalogo_mo.csv") == "data/catalogo_mo.csv"


def test_csv_de_arranque_farmacia_y_mutual_igual_que_hoy(usar_perfil):
    from app.services.catalog_source import csv_de_arranque
    for clave in ("farmacia", "mutual"):
        usar_perfil(clave)
        assert csv_de_arranque("data/catalogo_base.csv") == "data/catalogo_base.csv"


def test_reload_petshop_no_carga_el_csv_de_la_farmacia(usar_perfil, sku_singleton, tmp_path):
    usar_perfil("petshop")
    assert sku_singleton.reload_sku_service("data/catalogo_base.csv").total == 0

    mo = tmp_path / "catalogo_mo.csv"
    mo.write_text(
        "SKU,Nombre,Precio,Marca,Laboratorio,Codigo_Barras_1,Categoria,Es_Medicamento\n"
        "1,Royal Canin Medium Adult 15 kg,98000,ROYAL CANIN,,7790187000011,ALIMENTOS,false\n"
        "2,Piedras Sanicat 4 kg,6200,SANICAT,,7798043000022,PIEDRAS SANITARIAS,false\n",
        encoding="utf-8")
    assert sku_singleton.reload_sku_service(str(mo)).total == 2


def test_get_sku_service_petshop_arranca_vacio(usar_perfil, sku_singleton):
    usar_perfil("petshop")
    assert sku_singleton.get_sku_service().total == 0       # default: el CSV de la farmacia


def test_farmacia_carga_el_csv_como_hoy(usar_perfil, sku_singleton):
    usar_perfil("farmacia")
    svc = sku_singleton.reload_sku_service("data/catalogo_base.csv")
    assert svc.total == 17192
    assert svc.buscar("ibuprofeno")


async def test_aplicar_fuente_csv_en_petshop_deja_el_catalogo_vacio(usar_perfil, sku_singleton,
                                                                    monkeypatch):
    from app.config import get_settings
    from app.services import catalog_source as cs
    usar_perfil("petshop")
    # usar_perfil no recrea Settings: un delenv no llegaría. Se pisan los atributos.
    monkeypatch.setattr(get_settings(), "default_branch_id", "")
    monkeypatch.setattr(get_settings(), "sku_csv_path", "data/catalogo_base.csv")

    async def _fuente_csv():
        return "csv"

    monkeypatch.setattr(cs, "fuente_configurada", _fuente_csv)
    monkeypatch.setattr(cs, "_cache", {"branch_id": None, "at": 0.0})
    monkeypatch.setattr(cs, "estado_recarga", dict(cs.estado_recarga))
    est = await cs.aplicar_fuente()
    assert est["fuente"] == "csv"
    assert est["total_productos"] == 0
```

- [ ] **Step 8: Correrlos y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_arranque.py -v -k "csv_de_arranque or reload_petshop or get_sku_service or farmacia_carga or aplicar_fuente"`
Expected: 5 FAILED y 1 PASSED.
- `test_csv_de_arranque_*` (2): `ImportError: cannot import name 'CSV_FARMACIA'` / `'csv_de_arranque' from 'app.services.catalog_source'`.
- `test_reload_petshop_...` y `test_get_sku_service_petshop_arranca_vacio`: `AssertionError: assert 17192 == 0`.
- `test_aplicar_fuente_...`: `assert 17192 == 0`.
- `test_farmacia_carga_el_csv_como_hoy` PASA: es la guarda de la farmacia.

- [ ] **Step 9: `CSV_FARMACIA` y `csv_de_arranque` en `catalog_source.py`**

En `app/services/catalog_source.py`, imports (líneas 11-13). Reemplazar:
```python
import logging
import time
from typing import Optional
```
por:
```python
import logging
import time
from pathlib import Path
from typing import Optional
```
y después de `_conflicto` (líneas 23-24) reemplazar:
```python
# Sucursales en conflicto (más de una con catálogo y sin override).
_conflicto: list[str] = []
```
por:
```python
# Sucursales en conflicto (más de una con catálogo y sin override).
_conflicto: list[str] = []

# Catálogo de la farmacia (17.192 filas). Un perfil sin catalogo_csv_base
# (petshop) NUNCA lo carga: su catálogo sale del ERP (spec 4.8).
CSV_FARMACIA = Path(__file__).resolve().parents[2] / "data" / "catalogo_base.csv"


def csv_de_arranque(ruta: str) -> str:
    """
    La ruta de CSV que el perfil puede cargar. "" (catálogo vacío hasta el
    primer sync del ERP) si es el CSV de la farmacia y el perfil no tiene
    catalogo_csv_base; si no, la ruta tal cual.
    """
    from app.services.perfil import get_perfil
    perfil = get_perfil()
    if ruta and not perfil.catalogo_csv_base \
            and Path(ruta).resolve() == CSV_FARMACIA.resolve():
        logger.error(f"SKU_CSV_PATH={ruta} es el catálogo de la farmacia: el perfil "
                     f"{perfil.clave} no lo carga; el catálogo sale del ERP")
        return ""
    return ruta
```

- [ ] **Step 10: `_csv_permitido` en `sku_service.py`**

En `app/services/sku_service.py` (líneas 687-697). Reemplazar:
```python
def get_sku_service(csv_path: str = "data/catalogo_base.csv") -> SKUService:
    global _instance
    if _instance is None:
        _instance = SKUService(csv_path)
    return _instance


def reload_sku_service(csv_path: str) -> SKUService:
    """Recarga el catálogo desde disco sin reiniciar el servidor."""
    global _instance
    _instance = SKUService(csv_path)
```
por:
```python
def _csv_permitido(csv_path: str) -> str:
    """El CSV que el perfil puede cargar (spec 4.8): petshop nunca carga el
    de la farmacia, venga del arranque, de aplicar_fuente, del backoffice o
    del default sin argumento. Import diferido: catalog_source → perfil."""
    from app.services.catalog_source import csv_de_arranque
    return csv_de_arranque(csv_path)


def get_sku_service(csv_path: str = "data/catalogo_base.csv") -> SKUService:
    global _instance
    if _instance is None:
        _instance = SKUService(_csv_permitido(csv_path))
    return _instance


def reload_sku_service(csv_path: str) -> SKUService:
    """Recarga el catálogo desde disco sin reiniciar el servidor."""
    global _instance
    _instance = SKUService(_csv_permitido(csv_path))
```
`set_sku_service` (702-706) no cambia: el sync de Mercurio (`mercurio_service.py:390-397`) carga el catálogo de MO por ahí. `SKUService("")` ya arranca vacío (línea 280).

- [ ] **Step 11: Correr y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_arranque.py -v`
Expected: 16 passed.

- [ ] **Step 12: Commit**

```bash
git add app/services/catalog_source.py app/services/sku_service.py tests/test_perfil_arranque.py
git commit -F - <<'EOF'
Petshop nunca carga el CSV de la farmacia

csv_de_arranque descarta data/catalogo_base.csv (con un ERROR en el log) si el
perfil no tiene catalogo_csv_base; get_sku_service y reload_sku_service pasan
por ahi. Petshop arranca vacio hasta el primer sync de Mercurio.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 13: Escribir los tests del tablero y del desvío sin venta**

Agregar al final de `tests/test_perfil_arranque.py`:

```python
# ── Tablero ─────────────────────────────────────────────────────────────────────
def test_tablero_usa_la_clave_del_perfil(usar_perfil, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.config import get_settings
    usar_perfil(" Petshop ")                       # se normaliza: strip + lower
    monkeypatch.setattr(get_settings(), "bo_key", "")   # sin clave del panel
    r = TestClient(app).get("/bo/tablero?mes=2026-09")
    assert r.status_code == 200
    body = r.json()
    assert body["vertical"] == "petshop"
    assert "producto" in body                      # tablero de venta


# ── Desvío sin venta: se pregunta por la capacidad, no por el nombre ────────────
@pytest.mark.parametrize("vertical,llamadas", [
    ("farmacia", 0), ("petshop", 0), ("mutual", 1), ("Mutual", 1)])
async def test_flujo_mutual_solo_sin_venta(entorno, usar_perfil, monkeypatch, vertical, llamadas):
    usar_perfil(vertical)
    vistos = []

    async def _flujo(deps, phone, session, texto, *a, **k):
        vistos.append(texto)
        return "respuesta de la mutual", "mutual_info"

    monkeypatch.setattr(wh, "_flujo_mutual", _flujo)
    deps = entorno()
    await wh.procesar_mensajes([_msg("hola")])
    assert vistos == ["hola"] * llamadas
    if llamadas:
        assert deps["wa"].enviados[-1] == "respuesta de la mutual"
```

- [ ] **Step 14: Correrlos y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_arranque.py -v -k "tablero or flujo_mutual"`
Expected: 2 FAILED y 3 PASSED.
- `test_tablero_usa_la_clave_del_perfil`: `AssertionError: assert ' petshop ' == 'petshop'` (hoy sale de `settings.vertical` sin normalizar).
- `test_flujo_mutual_solo_sin_venta[Mutual-1]`: `AssertionError: assert [] == ['hola']` (hoy compara el nombre `== "mutual"`).
- `[farmacia-0]`, `[petshop-0]` y `[mutual-1]` PASAN: son las guardas de lo que no cambia.

- [ ] **Step 15: Desvío por capacidad (`webhook.py`) y tablero (`backoffice.py`)**

En `app/routers/webhook.py` (líneas 1170-1171). Reemplazar:
```python
            # ── Vertical "mutual": información + derivación, sin venta ───────
            if _s.vertical == "mutual":
```
por:
```python
            # ── Perfil sin venta (mutual): información + derivación ──────────
            if not get_perfil().venta:
```
El cuerpo del `if` y el orden de los bloques no cambian.

En `app/routers/backoffice.py`, `bo_tablero` (líneas 596-602). Reemplazar:
```python
    métrica (medido / propuesto / sin_dato). El vertical sale del entorno
    (VERTICAL=farmacia|mutual): cada backoffice sirve el suyo.
    """
    from datetime import datetime as _dt
    settings = get_settings()
    mes = mes or _dt.now().strftime("%Y-%m")
    vertical = (getattr(settings, "vertical", "") or "farmacia").lower()
```
por:
```python
    métrica (medido / propuesto / sin_dato). El vertical es la clave del
    perfil de rubro (VERTICAL=farmacia|mutual|petshop, normalizada): cada
    backoffice sirve el suyo; petshop recibe el tablero de venta.
    """
    from datetime import datetime as _dt
    from app.services.perfil import get_perfil
    settings = get_settings()
    mes = mes or _dt.now().strftime("%Y-%m")
    vertical = get_perfil().clave
```
`metrics_store` no se toca.

- [ ] **Step 16: Correr y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_arranque.py -v`
Expected: 21 passed.

Run: `grep -n "_s.vertical\|settings, \"vertical\"" app/routers/webhook.py app/routers/backoffice.py`
Expected: sin resultados.

- [ ] **Step 17: Commit**

```bash
git add app/routers/webhook.py app/routers/backoffice.py tests/test_perfil_arranque.py
git commit -F - <<'EOF'
El desvio sin venta y el tablero salen del perfil de rubro

webhook pregunta por la capacidad venta en lugar del nombre "mutual" y el
tablero usa get_perfil().clave (normalizada).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 17b: Review Focus — primer arranque de MO con el catálogo vacío (pasa de entrada)**

Ver "Review Focus", punto 3. Los tests de catálogo de arriba miran el `total`, pero ninguno pasa un mensaje por el webhook con el catálogo vacío que tiene MO hasta el primer sync de Mercurio (§9.2). Fija que, aunque el modelo invente el producto y el precio, no sale el precio, no queda pendiente y no aparece la farmacia. Agregar al final de `tests/test_perfil_arranque.py`:

```python
# ── Review Focus: primer arranque de MO, con el catálogo vacío ──────────────────
async def test_petshop_con_catalogo_vacio_no_inventa_ni_deja_pendiente(entorno, usar_perfil,
                                                                       sku_singleton):
    """Hasta el primer sync de Mercurio el catálogo está vacío (spec 9.2): el
    bot no puede ofrecer ni cobrar lo que no tiene, aunque el modelo lo invente."""
    usar_perfil("petshop")
    txt = "hola, tenés royal canin medium adult 15 kg?"
    deps = entorno({txt: {"intencion": "consulta_stock",
                          "entidad_producto": "royal canin medium adult 15 kg",
                          "respuesta": "¡Sí! Tengo Royal Canin Medium Adult 15 kg a $98.000 🐾"}})
    deps["sku"] = sku_singleton.get_sku_service()          # lo que hay antes del primer sync
    assert deps["sku"].total == 0
    await wh.procesar_mensajes([_msg(txt)])
    enviado = deps["wa"].enviados[-1]
    assert enviado.startswith("No lo encuentro en nuestro catálogo")
    assert "98.000" not in enviado and "farmac" not in enviado.lower()
    s = await deps["session"].get(PHONE)
    assert not s.get("pending_sku_id")
```

Run: `.venv/Scripts/python -m pytest tests/test_perfil_arranque.py -v -k catalogo_vacio`
Expected: `1 passed`.

```bash
git add tests/test_perfil_arranque.py
git commit -F - <<'EOF'
Review Focus: petshop con el catalogo vacio del primer arranque no inventa ni deja pendiente

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 18: Suite completa**

Run: `.venv/Scripts/python -m pytest -q`
Expected: todo en verde, sin `failed` ni `error`: los 933 de `07a1d7a`, los de las tareas anteriores y los 22 de `tests/test_perfil_arranque.py` (21 + 1 de Review Focus) (unos 5 minutos). Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1175 tests. `test_logic.py::TestTablero::test_endpoint_tablero_responde_200` sigue en verde (`vertical` sale `farmacia`).

---

### Task 13: Pregunta en `esperando_entrega` y textos de retiro con la sucursal (§5, tabla principal)

> **CAMBIO QUE TAMBIÉN AFECTA A LA FARMACIA** (spec §5). Sin capacidad: aplica a todos los perfiles. Con `retiro_sucursal` vacío, la farmacia solo cambia en lo que el spec marca como **cambia**: la pregunta en `esperando_entrega` y en `esperando_confirmacion`, el cierre del horario en `esperando_entrega` y el texto que se le pasa al modelo.

**Files:**
- Modify: `app/services/checkout_helper.py:50-54` (bloque nuevo después de `afirma_envio`), `:813-829` (`pregunta_entrega`), `:832-839` (`texto_entrega`), `:1218` (línea de entrega del link), `:1973-1986` (`responder_horario`)
- Modify: `app/routers/webhook.py:46-67` (import de `checkout_helper`), `:1439-1450` (interceptor de horario), `:1589-1610` (`esperando_entrega`), `:1758-1761` (`esperando_confirmacion`), `:2014-2021` (consulta general con KB)
- Test: `tests/test_entrega_sucursal.py` (Create)

Las líneas son las del commit `07a1d7a` (verificadas). Las tareas anteriores agregan líneas en los dos archivos: ubicar cada bloque por el texto que se cita en "Buscar".

**Interfaces:**
- Consumes: fixture `usar_perfil(clave, comercio=None) -> Perfil` (`tests/conftest.py`, Task 1); `config_service.valores_base() -> dict`, `DEFAULTS["retiro_sucursal"] == ""` y `DEFAULTS["retiro_info_message"] == "Lo retirás en *{sucursal}* 🏪"`, y los campos `retiro_sucursal` / `retiro_info_message` de `ConfigUpdate` (Task 3); `entorno`, `_msg` y `PHONE` de `tests/test_webhook_secuencias.py`.
- Produces (en `app/services/checkout_helper.py`):
  - `def es_pregunta_entrega(t: str) -> bool`
  - `def pregunta_por_retiro(t: str) -> bool`
  - `def responder_pregunta_retiro(cfg: dict) -> str` (vacío sin sucursal cargada)
  - `def pregunta_entrega(cfg: dict, extra: str = "", saludo: bool = True, phone: Optional[str] = None, socio_svc=None) -> str` (misma firma; usa `cfg["retiro_sucursal"]`)
  - `def texto_entrega(tipo: str, direccion: Optional[str], costo_envio: float = 0, sucursal: str = "") -> str`
  - `def responder_horario(cfg_svc, hours: dict, cierre: str = "¿Te ayudo con algo más?") -> str`
  - Intención nueva del webhook: `consulta_retiro` (no es derivación).

- [ ] **Step 1: Verificar las precondiciones del contrato**

Desde `D:/Dev/WhatsappBOTy-mercurio-pedidos`:

```bash
.venv/Scripts/python -c "from app.services.config_service import DEFAULTS, valores_base; from app.routers.backoffice import ConfigUpdate; assert DEFAULTS['retiro_sucursal'] == '' and '{sucursal}' in DEFAULTS['retiro_info_message']; assert {'retiro_sucursal', 'retiro_info_message'} <= set(ConfigUpdate.model_fields); print('ok')"
grep -n "def usar_perfil" tests/conftest.py
```

Esperado: `ok` y una línea con `def usar_perfil`. Si algo falla, la Task 3 o la Task 1 está incompleta: no seguir.

- [ ] **Step 2: Escribir los tests de detección (fallan)**

Crear `tests/test_entrega_sucursal.py` con este contenido completo (los fakes y helpers se usan en los pasos siguientes):

```python
"""
Spec 2026-10-06 §5: una pregunta en medio de la elección de entrega no es una
elección. Caso real (Mascotas del Oeste, 6/10): en esperando_entrega el cliente
preguntó "¿en qué sucursal puede ser?"; "sucursal" matcheaba como retiro y
salió el link sin contestarle. Se responde (con la sucursal que cargó el
comercio, si la cargó) y se vuelve a ofrecer la elección. Un pedido con forma
de pregunta ("¿me lo podés enviar?") sigue siendo elección.

Es una corrección para todos los rubros: cada test corre con el perfil
farmacia y con el petshop.
"""
import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as chh
from app.services.config_service import valores_base
from app.services.sku_service import SKUService
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (entorno es fixture)

_INFO = "Lo retirás en *Sucursal Piloto*, Calle Falsa 123, de 9 a 20 hs 🐾"
_SUC = {"retiro_sucursal": "Sucursal Piloto",
        "retiro_info_message": "Lo retirás en *{sucursal}*, Calle Falsa 123, de 9 a 20 hs 🐾"}
_REPREGUNTA = "¿Preferís *retiro en Sucursal Piloto* o *envío a domicilio*? 🙂"
_HORARIO = "de lunes a sábado de 9 a 20"


@pytest.fixture(params=["farmacia", "petshop"], autouse=True)
def perfil(request, usar_perfil):
    return usar_perfil(request.param)


class _CfgEnt:
    """Config falsa con los textos del perfil activo y un horario cargado."""
    def __init__(self, extra=None):
        self.v = {**valores_base(), **(extra or {})}

    async def get_all(self):
        return dict(self.v)

    async def get(self, k):
        return self.v.get(k)

    async def get_hours(self):
        return {"enabled": False}

    def is_open_now(self, hours):
        return True

    def proxima_apertura(self, hours):
        return ""

    def texto_horario(self, hours):
        return _HORARIO


class _IntentEnt:
    """Modelo guionado que guarda los kwargs de cada llamada."""
    def __init__(self, guion=None):
        self.guion = guion or {}
        self.llamadas = []

    async def procesar_rapido(self, mensaje, **k):
        self.llamadas.append(("rapido", mensaje, k))
        return self.guion.get(mensaje, {"intencion": "saludo", "respuesta": "¡Hola!"})

    async def procesar(self, mensaje, **k):
        self.llamadas.append(("procesar", mensaje, k))
        return self.guion.get(mensaje, {"intencion": "desconocido", "respuesta": "Te cuento 🙂"})

    def vio(self, tipo):
        return [c for c in self.llamadas if c[0] == tipo]


def _catalogo():
    base = {"hash": "e" * 64, "barcodes": [], "troquel": None, "brand": "Dog Chow", "drug": None,
            "form": None, "category": "ALIMENTOS", "rubro": "PERROS", "subrubro": "",
            "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
            "requiere_receta": "no", "source": "t"}
    return SKUService.from_rows([
        {**base, "external_id": "30", "name": "DOG CHOW ADULTO RAZAS MEDIANAS 15KG", "price": 52000.0},
    ])


async def _armar(entorno, monkeypatch, estado, cfg=None, guion=None):
    """Webhook con un Dog Chow pendiente (venta libre en los dos rubros) en
    `estado`. Devuelve (deps, links): cada link generado queda como (tipo, dirección)."""
    deps = entorno()
    deps["config"] = _CfgEnt(cfg)
    deps["intent"] = _IntentEnt(guion)
    deps["sku"] = _catalogo()
    links = []

    async def _link(payment_svc, session_svc, phone, session, tipo_entrega="retiro", direccion=None):
        links.append((tipo_entrega, direccion))
        await session_svc.set_estado(phone, "esperando_pago")
        return f"LINK {tipo_entrega}", "https://pago/x"
    monkeypatch.setattr(chh, "crear_link_y_responder", _link)

    async def _sin_freno(*a, **k):
        return None, None
    monkeypatch.setattr(chh, "_chequear_stock_vivo", _sin_freno)
    monkeypatch.delitem(chh._ULTIMA_DIRECCION, PHONE, raising=False)
    await deps["session"].set_pending(PHONE, sku_id="30", sku_nombre="DOG CHOW ADULTO RAZAS MEDIANAS 15KG",
                                      precio=52000.0, cantidad=1, opciones=[])
    if estado != "esperando_confirmacion":          # set_pending ya la deja en confirmación
        await deps["session"].set_estado(PHONE, estado)
    return deps, links


async def _estado(deps):
    return (await deps["session"].get(PHONE)).get("estado")


# ── Funciones puras ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("txt", [
    "en que sucursal puede ser?", "en qué sucursal lo retiro?", "donde queda la sucursal?",
    "a que hora puedo pasar?", "como hago para retirarlo?", "cual sucursal?",
    "que sucursal me queda mas cerca?", "tienen sucursal en moron?", "¿Dónde retiro?",
    "cuánto sale el envío?", "en q sucursal?", "dónde lo retiro",
])
def test_es_pregunta_entrega_detecta_preguntas(txt):
    assert chh.es_pregunta_entrega(txt) is True


@pytest.mark.parametrize("txt", [
    "retiro", "retiro en sucursal", "lo paso a buscar", "voy a la sucursal", "envio",
    "a domicilio", "mandámelo a casa", "si ahí", "dale ahí", "sí, a mi domicilio",
    "me lo podés enviar", "¿me lo podés enviar?", "me lo envías?", "lo puedo retirar hoy?",
    "hacen envío a Funes?", "cuando salga del trabajo lo paso a buscar", "como siempre, retiro",
    "dale, lo busco", "ok",
])
def test_es_pregunta_entrega_no_confunde_elecciones(txt):
    assert chh.es_pregunta_entrega(txt) is False


def test_pregunta_por_retiro_y_respuesta_sin_sucursal():
    assert chh.pregunta_por_retiro("cuánto sale el envío?") is False
    assert chh.pregunta_por_retiro("donde queda la sucursal?") is True
    assert chh.responder_pregunta_retiro({}) == ""
    assert chh.responder_pregunta_retiro({"retiro_sucursal": "   "}) == ""
    assert chh.responder_pregunta_retiro({"retiro_sucursal": "Sucursal Piloto"}) == \
        "Lo retirás en *Sucursal Piloto* 🏪"
    assert chh.responder_pregunta_retiro(_SUC) == _INFO
```

- [ ] **Step 3: Correrlos y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_entrega_sucursal.py::test_es_pregunta_entrega_detecta_preguntas tests/test_entrega_sucursal.py::test_es_pregunta_entrega_no_confunde_elecciones tests/test_entrega_sucursal.py::test_pregunta_por_retiro_y_respuesta_sin_sucursal -v
```

Esperado: `64 failed` (32 casos × 2 perfiles) con `AttributeError: module 'app.services.checkout_helper' has no attribute 'es_pregunta_entrega'` (y `'pregunta_por_retiro'` en el último).

- [ ] **Step 4: Implementar la detección**

En `app/services/checkout_helper.py`, después de la línea `    return tiene_afirma and tiene_cue` (fin de `afirma_envio`, línea 54), insertar (el código del spec §5, sin cambios; `match_retiro` y `match_envio` están definidas arriba, en 31-40):

```python


# ── Pregunta en medio de la elección de entrega (spec 2026-10-06 §5) ──────────
# Caso real MO 6/10: en esperando_entrega, "¿en qué sucursal puede ser?"
# matcheaba _RETIRO por "sucursal": salía el link sin contestar. Una pregunta
# no es una elección: se responde y se vuelve a ofrecer la elección. Un pedido
# con forma de pregunta ("¿me lo podés enviar?", "¿lo puedo retirar hoy?")
# sigue siendo elección (C-3854, C-3912). El signo "?" solo no alcanza: cuenta
# junto con un interrogativo, o con "sucursal"/"local" sin verbo de entrega.
_INTERROGATIVO = re.compile(
    r"\b(en|a|hasta|desde|para|por|de)\s+(qu[eé]|q)\b"
    r"|\bqu[eé]\s+(sucursal\w*|local\w*|hora|horarios?|d[ií]as?|direcci[oó]n)\b|\bqué\b"
    r"|\bcu[aá]l(es)?\b|\b(a)?d[oó]nde\b|\bcu[aá]ndo\b|\bc[oó]mo\b|\bcu[aá]nt[oa]s?\b",
    re.IGNORECASE)
_INTERROGATIVO_INICIO = re.compile(
    r"^\W*(y|pero|che|perd[oó]n|disculp\w*|una\s+consulta)?\W*"
    r"((en|a|hasta|desde)\s+(qu[eé]|q)\b|qu[eé]\s+(sucursal\w*|local\w*|hora|horarios?|direcci[oó]n)\b"
    r"|cu[aá]l(es)?\b|(a)?d[oó]nde\b|cómo\b|cuándo\b|cuánto\b|qué\b)",
    re.IGNORECASE)
_LUGAR_RETIRO = re.compile(r"\b(sucursal(es)?|local(es)?)\b", re.IGNORECASE)
_ACCION_ENTREGA = re.compile(
    r"\b(retir\w*|pas\w*|busc\w*|voy|vamos|env[ií]\w*|mand\w*|tra[eé]\w*)\b", re.IGNORECASE)
_TEMA_RETIRO = re.compile(
    r"\b(retir\w*|d[oó]nde|direcci[oó]n|queda|local\w*|hora|horarios?|abren|cierran)\b",
    re.IGNORECASE)


def es_pregunta_entrega(t: str) -> bool:
    """True si el mensaje PREGUNTA algo (no elige retiro/envío)."""
    s = (t or "").strip().lower()
    if not s:
        return False
    signo = "?" in s or "¿" in s
    if signo and _INTERROGATIVO.search(s):
        return True
    if _INTERROGATIVO_INICIO.search(s):
        return True
    return bool(signo and _LUGAR_RETIRO.search(s) and not _ACCION_ENTREGA.search(s))


def pregunta_por_retiro(t: str) -> bool:
    """La pregunta es sobre el retiro (sucursal, dónde, a qué hora), no sobre el envío."""
    s = (t or "").lower()
    return (match_retiro(s) or bool(_TEMA_RETIRO.search(s))) and not match_envio(s)


def responder_pregunta_retiro(cfg: dict) -> str:
    """Dato de la sucursal de retiro cargado en el panel. Vacío si no hay
    sucursal cargada: nunca se inventa una dirección."""
    suc = (cfg.get("retiro_sucursal") or "").strip()
    if not suc:
        return ""
    plantilla = cfg.get("retiro_info_message") or "Lo retirás en *{sucursal}* 🏪"
    return plantilla.replace("{sucursal}", suc).strip()
```

- [ ] **Step 5: Correr y ver que pasan**

Mismo comando del Step 3. Esperado: `64 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/services/checkout_helper.py tests/test_entrega_sucursal.py
git commit -m "Entrega: detectar cuándo el cliente pregunta por la sucursal en vez de elegir" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: Escribir los tests de los textos con la sucursal (fallan)**

Agregar al final de `tests/test_entrega_sucursal.py`:

```python
def test_textos_de_entrega_sin_sucursal_igual_que_hoy():
    assert chh.pregunta_entrega({}) == \
        "¡Genial! ¿Cómo preferís recibirlo: *retiro en sucursal* o *envío a domicilio*?"
    assert chh.pregunta_entrega({}, saludo=False) == \
        "¿Preferís *retiro en sucursal* o *envío a domicilio*? 🙂"
    assert chh.pregunta_entrega({"retiro_sucursal": ""}, saludo=False) == \
        "¿Preferís *retiro en sucursal* o *envío a domicilio*? 🙂"
    assert chh.texto_entrega("retiro", None) == \
        "🏪 Lo retirás en la sucursal (te enviamos el código al confirmar el pago)."


def test_textos_de_entrega_nombran_la_sucursal():
    cfg = {"retiro_sucursal": "Sucursal Piloto"}
    assert chh.pregunta_entrega(cfg) == \
        "¡Genial! ¿Cómo preferís recibirlo: *retiro en Sucursal Piloto* o *envío a domicilio*?"
    assert chh.pregunta_entrega(cfg, saludo=False) == _REPREGUNTA
    assert chh.texto_entrega("retiro", None, sucursal="Sucursal Piloto") == \
        "🏪 Lo retirás en *Sucursal Piloto* (te enviamos el código al confirmar el pago)."
    assert chh.texto_entrega("retiro", None, sucursal="") == \
        "🏪 Lo retirás en la sucursal (te enviamos el código al confirmar el pago)."
    assert chh.texto_entrega("envio", "Mitre 100", sucursal="Sucursal Piloto") == \
        "🚚 Te lo enviamos a domicilio a *Mitre 100*."


def test_responder_horario_con_cierre():
    cfg = _CfgEnt()
    assert chh.responder_horario(cfg, {"enabled": False}) == \
        f"Atendemos {_HORARIO} 🕐 ¿Te ayudo con algo más?"
    assert chh.responder_horario(cfg, {"enabled": False}, cierre=_REPREGUNTA) == \
        f"Atendemos {_HORARIO} 🕐 {_REPREGUNTA}"


async def test_link_de_retiro_nombra_la_sucursal(monkeypatch):
    from app.services import config_service as cs
    from app.services.session_service import SessionService
    cfg = _CfgEnt(_SUC)
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: cfg)

    async def _sin_freno(*a, **k):
        return None, None
    monkeypatch.setattr(chh, "_chequear_stock_vivo", _sin_freno)

    class _Pago:
        async def crear_link(self, **k):
            return "https://pago/abc", None
    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_pending(PHONE, sku_id="30", sku_nombre="DOG CHOW ADULTO RAZAS MEDIANAS 15KG",
                         precio=52000.0, cantidad=1, opciones=[])
    resp, link = await chh.crear_link_y_responder(_Pago(), ss, PHONE, await ss.get(PHONE), "retiro", None)
    assert link == "https://pago/abc"
    assert "🏪 Lo retirás en *Sucursal Piloto* (te enviamos el código al confirmar el pago)." in resp
```

- [ ] **Step 8: Correrlos y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_entrega_sucursal.py::test_textos_de_entrega_sin_sucursal_igual_que_hoy tests/test_entrega_sucursal.py::test_textos_de_entrega_nombran_la_sucursal tests/test_entrega_sucursal.py::test_responder_horario_con_cierre tests/test_entrega_sucursal.py::test_link_de_retiro_nombra_la_sucursal -v
```

Esperado: `6 failed, 2 passed`. Pasa `test_textos_de_entrega_sin_sucursal_igual_que_hoy` (guarda: el texto de hoy, byte a byte). Fallan `..._nombran_la_sucursal` (`AssertionError`: sale `*retiro en sucursal*`), `test_responder_horario_con_cierre` (`TypeError: responder_horario() got an unexpected keyword argument 'cierre'`) y `test_link_de_retiro_nombra_la_sucursal` (`AssertionError`: el link dice "Lo retirás en la sucursal").

- [ ] **Step 9: Implementar `pregunta_entrega`, `texto_entrega`, la línea del link y `responder_horario`**

En `app/services/checkout_helper.py`, `pregunta_entrega` (813-829). Buscar:

```python
    if saludo:
        return f"¡Genial! ¿Cómo preferís recibirlo: *retiro en sucursal* o {envio_txt}?{extra}"
    return f"¿Preferís *retiro en sucursal* o {envio_txt}? 🙂{extra}"
```

Reemplazar por:

```python
    # Sucursal de retiro cargada en el panel (spec §5); vacía = "sucursal".
    retiro_txt = f"*retiro en {(cfg.get('retiro_sucursal') or '').strip() or 'sucursal'}*"
    if saludo:
        return f"¡Genial! ¿Cómo preferís recibirlo: {retiro_txt} o {envio_txt}?{extra}"
    return f"¿Preferís {retiro_txt} o {envio_txt}? 🙂{extra}"
```

`texto_entrega` (832-839). Reemplazar la función completa por:

```python
def texto_entrega(tipo: str, direccion: Optional[str], costo_envio: float = 0,
                  sucursal: str = "") -> str:
    """Línea que describe la entrega elegida, para el mensaje del link de pago.
    `sucursal`: la de retiro cargada en el panel (vacía = "la sucursal")."""
    if tipo == "envio":
        dir_txt = f" a *{direccion}*" if direccion else ""
        costo_txt = (f" Incluye el envío (${costo_envio:,.2f})."
                     if costo_envio > 0 else "")
        return f"🚚 Te lo enviamos a domicilio{dir_txt}.{costo_txt}"
    suc = (sucursal or "").strip()
    if suc:
        return f"🏪 Lo retirás en *{suc}* (te enviamos el código al confirmar el pago)."
    return "🏪 Lo retirás en la sucursal (te enviamos el código al confirmar el pago)."
```

En `crear_link_y_responder`, línea 1218. Buscar:

```python
    entrega_line = texto_entrega(tipo_entrega, direccion, _costo_envio)
```

Reemplazar por:

```python
    entrega_line = texto_entrega(tipo_entrega, direccion, _costo_envio,
                                 sucursal=_cfg.get("retiro_sucursal") or "")
```

`responder_horario` (1973-1986). Buscar:

```python
def responder_horario(cfg_svc, hours: dict) -> str:
    """Respuesta fija con el horario y si ahora está abierto. Vacío si no hay
    horario cargado (que conteste el modelo como siempre)."""
```

Reemplazar por:

```python
def responder_horario(cfg_svc, hours: dict, cierre: str = "¿Te ayudo con algo más?") -> str:
    """Respuesta fija con el horario y si ahora está abierto. Vacío si no hay
    horario cargado (que conteste el modelo como siempre). `cierre`: la
    pregunta final (eligiendo la entrega, se vuelve a ofrecer retiro o envío)."""
```

Y la última línea de la función. Buscar:

```python
    return r + " ¿Te ayudo con algo más?"
```

Reemplazar por:

```python
    return f"{r} {cierre}"
```

Los llamadores de `pregunta_entrega` (`webhook.py:1312, 1351, 1608` y `checkout_helper.py:1298, 1340`) no cambian: toman la sucursal de la config que ya reciben.

- [ ] **Step 10: Correr y ver que pasan**

Mismo comando del Step 8. Esperado: `8 passed`. Además, las guardas de hoy:

```bash
.venv/Scripts/python -m pytest tests/test_horarios.py tests/test_descuento_entrega.py tests/test_direccion_envio.py -q
```

Esperado: todos `passed`.

- [ ] **Step 11: Commit**

```bash
git add app/services/checkout_helper.py tests/test_entrega_sucursal.py
git commit -m "Entrega: la pregunta, el link y el horario nombran la sucursal de retiro cargada" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 12: Escribir los tests del webhook en `esperando_entrega` (fallan)**

Agregar al final de `tests/test_entrega_sucursal.py`:

```python
# ── Webhook ──────────────────────────────────────────────────────────────────────
async def test_a_pregunta_por_la_sucursal_al_elegir_entrega_se_responde(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega", _SUC)
    await wh.procesar_mensajes([_msg("en que sucursal puede ser?")])
    assert deps["wa"].enviados[-1] == f"{_INFO}\n\n{_REPREGUNTA}"
    assert links == [] and deps["intent"].llamadas == []
    assert await _estado(deps) == "esperando_entrega"
    # La elección que sigue funciona como siempre.
    await wh.procesar_mensajes([_msg("retiro")])
    assert links == [("retiro", None)]


async def test_b_sin_sucursal_cargada_responde_el_modelo_sin_inventar(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega")
    await wh.procesar_mensajes([_msg("en que sucursal puede ser?")])
    assert links == []
    assert await _estado(deps) == "esperando_entrega"
    proc = deps["intent"].vio("procesar")
    assert [c[1] for c in proc] == ["en que sucursal puede ser?"]
    situacion = proc[0][2]["situacion"]
    assert "Nunca inventes direcciones" in situacion and "*retiro en sucursal*" in situacion
    assert deps["wa"].enviados[-1] == "Te cuento 🙂"


async def test_c_cuanto_sale_el_envio_lo_responde_el_modelo(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega", _SUC)
    await wh.procesar_mensajes([_msg("cuánto sale el envío?")])
    assert links == []
    assert await _estado(deps) == "esperando_entrega"
    proc = deps["intent"].vio("procesar")
    assert [c[1] for c in proc] == ["cuánto sale el envío?"]
    assert "*retiro en Sucursal Piloto*" in proc[0][2]["situacion"]


@pytest.mark.parametrize("txt,estado,link", [
    ("¿me lo podés enviar?", "esperando_direccion", []),
    ("lo puedo retirar hoy?", "esperando_pago", [("retiro", None)]),
])
async def test_d_pedido_con_forma_de_pregunta_sigue_eligiendo(entorno, monkeypatch, txt, estado, link):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega", _SUC)
    await wh.procesar_mensajes([_msg(txt)])
    assert await _estado(deps) == estado
    assert links == link


async def test_f_horario_al_elegir_entrega_vuelve_a_ofrecer_la_eleccion(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega", _SUC)
    await wh.procesar_mensajes([_msg("hasta qué hora puedo retirar?")])
    r = deps["wa"].enviados[-1]
    assert r == f"Atendemos {_HORARIO} 🕐 {_REPREGUNTA}"
    assert "¿Te ayudo con algo más?" not in r
    assert links == []
    assert await _estado(deps) == "esperando_entrega"


async def test_f_horario_fuera_de_la_entrega_igual_que_hoy(entorno):
    deps = entorno()
    deps["config"] = _CfgEnt(_SUC)
    deps["intent"] = _IntentEnt()
    await wh.procesar_mensajes([_msg("hasta qué hora puedo retirar?")])
    assert deps["wa"].enviados[-1] == f"Atendemos {_HORARIO} 🕐 ¿Te ayudo con algo más?"
```

- [ ] **Step 13: Correrlos y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_entrega_sucursal.py::test_a_pregunta_por_la_sucursal_al_elegir_entrega_se_responde tests/test_entrega_sucursal.py::test_b_sin_sucursal_cargada_responde_el_modelo_sin_inventar tests/test_entrega_sucursal.py::test_c_cuanto_sale_el_envio_lo_responde_el_modelo tests/test_entrega_sucursal.py::test_d_pedido_con_forma_de_pregunta_sigue_eligiendo tests/test_entrega_sucursal.py::test_f_horario_al_elegir_entrega_vuelve_a_ofrecer_la_eleccion tests/test_entrega_sucursal.py::test_f_horario_fuera_de_la_entrega_igual_que_hoy -v
```

Esperado: `8 failed, 6 passed`. Fallan (a) `assert 'LINK retiro' == 'Lo retirás e...'` (salió el link), (b) `assert [('retiro', None)] == []`, (c) `assert 'esperando_direccion' == 'esperando_entrega'` y (f) `'Atendemos ...¿Te ayudo con algo más?' == 'Atendemos ...🙂'`, cada uno en los dos perfiles. Pasan (d) y `test_f_horario_fuera_de_la_entrega_igual_que_hoy` (guardas).

- [ ] **Step 14: Implementar en el webhook: import, interceptor de horario y `esperando_entrega`**

En `app/routers/webhook.py`, el import de `checkout_helper` (46-67). La Task 9 ya agregó `MOTIVO_CONSULTA_SALUD, texto_consulta_salud,` antes del `)` que cierra la lista, así que se busca solo la línea (es única en el archivo). Buscar:

```python
    aviso_fuera_horario, dice_ser_socio, pregunta_horario, responder_horario,
```

Reemplazar por:

```python
    aviso_fuera_horario, dice_ser_socio, pregunta_horario, responder_horario,
    es_pregunta_entrega, pregunta_por_retiro, responder_pregunta_retiro,
```

Interceptor de horario (1439-1450). Buscar:

```python
            if pregunta_horario(texto):
                _resp_h = responder_horario(deps["config"], _hours_msg)
```

Reemplazar por (`_cfg_pm` se lee en 1276, antes de este bloque):

```python
            if pregunta_horario(texto):
                # Eligiendo la entrega ("¿hasta qué hora puedo retirar?"): se
                # contesta y se vuelve a ofrecer la elección (spec §5).
                _cierre_h = "¿Te ayudo con algo más?"
                if session.get("estado") == "esperando_entrega" and session.get("pending_sku_id"):
                    _cierre_h = pregunta_entrega(_cfg_pm, saludo=False, phone=phone,
                                                 socio_svc=deps["socios"])
                _resp_h = responder_horario(deps["config"], _hours_msg, cierre=_cierre_h)
```

Bloque `esperando_entrega` (el `else` de 1589 hasta 1610). Buscar:

```python
                else:
                    _es_retiro = match_retiro(texto_lower)
                    _es_envio = match_envio(texto_lower) or afirma_envio(texto_lower)
                    if not _es_retiro and not _es_envio:
                        # No eligió entrega: está preguntando otra cosa (precio,
                        # demora, si llega a tal zona). Se responde la consulta y
                        # se vuelve a ofrecer la elección, en lugar de repetir la
                        # pregunta ignorando lo que preguntó.
                        _intencion = "consulta_en_entrega"
                        _cfg_ent = await deps["config"].get_all()
                        _costo_e = costo_envio_de(_cfg_ent)
                        respuesta = await _responder_consulta_en_flujo(
                            deps, phone, session, texto, _ctx_socio,
                            "El cliente ya confirmó este pedido y está eligiendo cómo recibirlo. "
                            "Respondé su consulta con los datos del pedido y terminá preguntándole "
                            "si prefiere *retiro en sucursal* o *envío a domicilio*"
                            + (f" (el envío cuesta ${_costo_e:,.0f} y se suma al total)"
                               if _costo_e else "") +
                            ". No generes links de pago ni cambies el producto.",
                            pregunta_entrega(_cfg_ent, saludo=False, phone=phone,
                                             socio_svc=deps["socios"]),
                        )
                    else:
```

Reemplazar por (el `else:` final y el `resolver_entrega` que le sigue quedan igual):

```python
                else:
                    # Una pregunta no es una elección (spec §5, caso MO 6/10):
                    # "¿en qué sucursal puede ser?" matcheaba "sucursal" como
                    # retiro y salía el link sin contestar. "¿Me lo podés
                    # enviar?" sigue siendo elección (ver es_pregunta_entrega).
                    _cfg_ent = await deps["config"].get_all()
                    _pregunta = es_pregunta_entrega(texto_lower)
                    _es_retiro = match_retiro(texto_lower) and not _pregunta
                    _es_envio = (match_envio(texto_lower) or afirma_envio(texto_lower)) and not _pregunta
                    _info_ret = (responder_pregunta_retiro(_cfg_ent)
                                 if _pregunta and pregunta_por_retiro(texto_lower) else "")
                    if _info_ret:
                        # Pregunta por el retiro y el comercio cargó su sucursal:
                        # el dato sale de la config, nunca lo redacta el modelo.
                        _intencion = "consulta_retiro"
                        respuesta = (_info_ret + "\n\n" +
                                     pregunta_entrega(_cfg_ent, saludo=False, phone=phone,
                                                      socio_svc=deps["socios"]))
                    elif not _es_retiro and not _es_envio:
                        # No eligió entrega: está preguntando otra cosa (precio,
                        # demora, si llega a tal zona). Se responde la consulta y
                        # se vuelve a ofrecer la elección, en lugar de repetir la
                        # pregunta ignorando lo que preguntó.
                        _intencion = "consulta_en_entrega"
                        _costo_e = costo_envio_de(_cfg_ent)
                        _suc_e = (_cfg_ent.get("retiro_sucursal") or "").strip() or "sucursal"
                        respuesta = await _responder_consulta_en_flujo(
                            deps, phone, session, texto, _ctx_socio,
                            "El cliente ya confirmó este pedido y está eligiendo cómo recibirlo. "
                            "Respondé su consulta con los datos del pedido y terminá preguntándole "
                            f"si prefiere *retiro en {_suc_e}* o *envío a domicilio*"
                            + (f" (el envío cuesta ${_costo_e:,.0f} y se suma al total)"
                               if _costo_e else "") +
                            ". No generes links de pago ni cambies el producto. "
                            "Nunca inventes direcciones, sucursales ni horarios.",
                            pregunta_entrega(_cfg_ent, saludo=False, phone=phone,
                                             socio_svc=deps["socios"]),
                        )
                    else:
```

La Task 9 ya tocó `_responder_consulta_en_flujo` (compuerta D): no hay conflicto, acá solo cambia el llamador.

- [ ] **Step 15: Correr y ver que pasan**

Mismo comando del Step 13. Esperado: `14 passed`.

- [ ] **Step 16: Commit**

```bash
git add app/routers/webhook.py tests/test_entrega_sucursal.py
git commit -m "Entrega: una pregunta en esperando_entrega se contesta y no elige retiro ni envío" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 17: Escribir los tests de `esperando_confirmacion` (fallan)**

Agregar al final de `tests/test_entrega_sucursal.py`:

```python
async def test_e_pregunta_por_la_sucursal_al_confirmar_no_confirma(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_confirmacion", _SUC)
    await wh.procesar_mensajes([_msg("en que sucursal puede ser?")])
    assert deps["wa"].enviados[-1] == f"{_INFO}\n\n¿Lo confirmamos?"
    assert links == [] and deps["intent"].llamadas == []
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "esperando_confirmacion" and s["pending_sku_id"] == "30"


async def test_e_sin_sucursal_la_pregunta_al_confirmar_va_al_modelo(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_confirmacion")
    await wh.procesar_mensajes([_msg("en que sucursal puede ser?")])
    assert [c[1] for c in deps["intent"].vio("procesar")] == ["en que sucursal puede ser?"]
    assert links == []
    assert await _estado(deps) == "esperando_confirmacion"
```

- [ ] **Step 18: Correrlos y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_entrega_sucursal.py::test_e_pregunta_por_la_sucursal_al_confirmar_no_confirma tests/test_entrega_sucursal.py::test_e_sin_sucursal_la_pregunta_al_confirmar_va_al_modelo -v
```

Esperado: `4 failed`. Hoy "sucursal" confirma con retiro: `assert 'LINK retiro' == 'Lo retirás e... confirmamos?'` y `assert [] == ['en que sucursal puede ser?']`.

- [ ] **Step 19: Implementar en `esperando_confirmacion`**

En `app/routers/webhook.py` (1758-1761; `_cfg_dx` se lee en 1682). Buscar:

```python
                elif (_es_afirmacion_pura(texto_lower) or match_envio(texto_lower)
                      or match_retiro(texto_lower)) \
                        and not _empieza_con_no(texto_lower) \
                        and not session.get("_espera_eleccion"):
```

Reemplazar por:

```python
                elif es_pregunta_entrega(texto_lower) and pregunta_por_retiro(texto_lower) \
                        and responder_pregunta_retiro(_cfg_dx):
                    # "¿En qué sucursal puede ser?" con el pedido sin confirmar:
                    # se contesta con la sucursal cargada y NO se confirma
                    # (antes "sucursal" confirmaba con retiro — spec §5).
                    _intencion = "consulta_retiro"
                    respuesta = responder_pregunta_retiro(_cfg_dx) + "\n\n¿Lo confirmamos?"
                    _ts = _time.perf_counter()
                    await deps["wa"].send_text(phone, respuesta)
                    _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                    await deps["session"].add_message(phone, "user", texto)
                    await deps["session"].add_message(phone, "assistant", respuesta)
                    continue

                elif (_es_afirmacion_pura(texto_lower) or match_envio(texto_lower)
                      or match_retiro(texto_lower)) \
                        and not _empieza_con_no(texto_lower) \
                        and not session.get("_espera_eleccion") \
                        and not es_pregunta_entrega(texto_lower):
```

Sin sucursal cargada, la pregunta ya no confirma y cae al `else` (el modelo con las opciones mostradas, 1777 en adelante).

- [ ] **Step 20: Correr y ver que pasan**

Mismo comando del Step 18. Esperado: `4 passed`.

- [ ] **Step 21: Commit**

```bash
git add app/routers/webhook.py tests/test_entrega_sucursal.py
git commit -m "Entrega: preguntar por la sucursal antes de confirmar ya no confirma el pedido" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 22: Escribir los tests de la consulta general (fallan)**

Agregar al final de `tests/test_entrega_sucursal.py`:

```python
async def test_g_donde_queda_la_sucursal_sin_pedido_le_llega_al_modelo(entorno):
    txt = "¿dónde queda la sucursal?"
    deps = entorno()
    deps["config"] = _CfgEnt(_SUC)
    deps["intent"] = _IntentEnt({txt: {"intencion": "desconocido", "entidad_producto": None,
                                       "respuesta": "Queda en Calle Falsa 123 🙂"}})
    await wh.procesar_mensajes([_msg(txt)])
    proc = deps["intent"].vio("procesar")
    assert len(proc) == 1 and _INFO in proc[0][2]["contexto_kb"]
    assert (await deps["session"].get(PHONE)).get("estado") != "operador"
    assert deps["wa"].enviados[-1] == "Queda en Calle Falsa 123 🙂"


async def test_g_sin_sucursal_cargada_igual_que_hoy(entorno):
    txt = "¿dónde queda la sucursal?"
    deps = entorno()
    deps["config"] = _CfgEnt()
    deps["intent"] = _IntentEnt({txt: {"intencion": "desconocido", "entidad_producto": None,
                                       "respuesta": "Queda en Calle Falsa 123 🙂"}})
    await wh.procesar_mensajes([_msg(txt)])
    assert deps["intent"].vio("procesar") == []          # sin KB ni sucursal: no hay con qué
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "no_entendido"
```

- [ ] **Step 23: Correrlos y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_entrega_sucursal.py::test_g_donde_queda_la_sucursal_sin_pedido_le_llega_al_modelo tests/test_entrega_sucursal.py::test_g_sin_sucursal_cargada_igual_que_hoy -v
```

Esperado: `2 failed, 2 passed`. Falla el de la sucursal cargada (`assert (0 == 1 ...)`: hoy no hay KB, se deriva por `no_entendido` y el modelo nunca ve el dato). Pasa la guarda sin sucursal.

- [ ] **Step 24: Implementar el dato de la sucursal en la consulta general**

En `app/routers/webhook.py` (2014-2021, después de la compuerta A que agregó la Task 9). En este punto no existe `texto_lower`: se usa `texto` (`pregunta_por_retiro` pasa a minúsculas). Buscar:

```python
            _general = intencion == "desconocido" or (intencion == "consulta_abierta" and not entidad)
            _tuvo_kb = False
            if _general and deps["rag"].enabled():
                _kb = await deps["rag"].kb_search(texto, n=3)
                if _kb:
                    _tuvo_kb = True
                    _kb_txt = "\n\n".join(f"{d['titulo']}: {d['contenido']}".strip(": ") for d in _kb)
                    _ir_kb = await deps["intent"].procesar(
```

Reemplazar por (el `procesar(...)` que sigue, con `contexto_kb=_kb_txt`, no cambia):

```python
            _general = intencion == "desconocido" or (intencion == "consulta_abierta" and not entidad)
            _tuvo_kb = False
            # "¿Dónde queda la sucursal?": el dato de la sucursal cargada en el
            # panel va junto con la KB (spec §5). Sin sucursal, igual que hoy.
            _cfg_kb = await deps["config"].get_all()
            _info_ret = (responder_pregunta_retiro(_cfg_kb)
                         if _general and pregunta_por_retiro(texto) else "")
            if _general and (deps["rag"].enabled() or _info_ret):
                _docs = []
                if deps["rag"].enabled():
                    _kb = await deps["rag"].kb_search(texto, n=3)
                    _docs = [f"{d['titulo']}: {d['contenido']}".strip(": ") for d in (_kb or [])]
                if _info_ret:
                    _docs.append(_info_ret)
                if _docs:
                    _tuvo_kb = True
                    _kb_txt = "\n\n".join(_docs)
                    _ir_kb = await deps["intent"].procesar(
```

El modelo lo recibe bajo `[{perfil.rotulo_kb}]` (`INFORMACIÓN DEL COMERCIO` en petshop). Con `_tuvo_kb = True`, `debe_derivar_desconocido` no deriva.

- [ ] **Step 25: Correr el archivo completo**

```bash
.venv/Scripts/python -m pytest tests/test_entrega_sucursal.py -v
```

Esperado: `94 passed` (47 tests × 2 perfiles).

- [ ] **Step 26: Commit**

```bash
git add app/routers/webhook.py tests/test_entrega_sucursal.py
git commit -m "Entrega: el modelo recibe el dato de la sucursal cuando preguntan dónde queda" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 26b: Review Focus — mayúsculas, sin tildes y sin signos (pasan de entrada)**

Ver "Review Focus", punto 1. Los casos del spec van en minúscula y casi todos con signos; en WhatsApp y en los audios transcriptos llegan en mayúsculas, sin tildes y sin "?". Fija los que el regex del spec resuelve bien (el `.lower()` cubre las mayúsculas y el interrogativo al principio cubre la falta de signos) y que una elección en mayúsculas sigue siendo elección. Agregar al final de `tests/test_entrega_sucursal.py`:

```python
# ── Review Focus: mayúsculas, sin tildes y sin signos (WhatsApp y audio) ────────
@pytest.mark.parametrize("txt,pregunta", [
    ("EN QUE SUCURSAL PUEDE SER", True),
    ("DONDE QUEDA LA SUCURSAL", True),
    ("Donde Retiro", True),
    ("a que hora puedo pasar a buscarlo", True),
    ("Cual sucursal me queda mas cerca", True),
    ("en q sucursal lo retiro", True),
    ("RETIRO EN SUCURSAL", False),
    ("ENVIO A DOMICILIO", False),
    ("LO PASO A BUSCAR", False),
    ("como siempre retiro", False),
    ("Dale lo busco", False),
])
def test_es_pregunta_entrega_en_mayusculas_sin_tildes_ni_signos(txt, pregunta):
    assert chh.es_pregunta_entrega(txt) is pregunta


async def test_pregunta_en_mayusculas_y_sin_signos_al_elegir_entrega_se_responde(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega", _SUC)
    await wh.procesar_mensajes([_msg("EN QUE SUCURSAL PUEDE SER")])
    assert deps["wa"].enviados[-1] == f"{_INFO}\n\n{_REPREGUNTA}"
    assert links == [] and deps["intent"].llamadas == []
    assert await _estado(deps) == "esperando_entrega"
```

```bash
.venv/Scripts/python -m pytest tests/test_entrega_sucursal.py -v -k mayusculas
```

Esperado: `24 passed` (12 tests × 2 perfiles).

```bash
git add tests/test_entrega_sucursal.py
git commit -m "Review Focus: preguntas de entrega en mayúsculas, sin tildes y sin signos" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 27: Suite completa**

```bash
.venv/Scripts/python -m pytest -q
```

Esperado: `0 failed`; el total sube en 118 respecto de antes de esta tarea (94 y los 24 de Review Focus). Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1293 tests. Ningún test existente cambia de expectativa (§6.3; verificado con un prototipo sobre `07a1d7a` + el perfil y la config: `1027 passed` = 933 + 94).

---

### Task 14: Pesos y presentaciones (§5, "Correcciones de pesos y presentaciones")

> **CAMBIO QUE TAMBIÉN AFECTA A LA FARMACIA.** Sin capacidad: aplica a todos los perfiles.

**Files:**
- Modify: `app/services/checkout_helper.py:65-71` (última línea del comentario y regex `_NO_DIR`)
- Modify: `app/services/checkout_helper.py:1555-1567` (bloque nuevo de presentaciones antes de `entidad_contradice_pendiente`, y el comienzo de esa función; 1568-1569 no cambian)
- Test: Create `tests/test_pesos.py`

Las líneas son las de hoy (`07a1d7a`); otras tareas agregan funciones a `checkout_helper.py` antes de estas, así que se ubican por el texto citado.

**Interfaces:**
- Consumes: fixture `usar_perfil(clave, comercio=None) -> Perfil` (Task 1); `entorno`, `_msg` y `PHONE` de `tests/test_webhook_secuencias.py` (sin cambios: su `_Cfg` falso sigue armado sobre `DEFAULTS`); `app.services.sku_service.nombre_coincide(query, nombre) -> bool`, `numeros_de(texto) -> list[str]`, `SKUService.from_rows(rows)`; `app.routers.webhook.payment_svc_para(cfg, s=None)` (se reemplaza en los tests: el webhook pisa `deps["payment"]` en cada mensaje, `webhook.py:1278`).
- Produces:
  - `app/services/checkout_helper.py`: `def presentaciones_de(t: str) -> set[tuple[str, float]]` (contrato).
  - `entidad_contradice_pendiente(entidad: Optional[str], pending_nombre: Optional[str]) -> bool`: misma firma, regla nueva por presentación.
  - `_NO_DIR` con `kgs?|lts?` y `kilos?|kilogram\w*|litros?|bolsa\w*|lata\w*`.
  - Fuera del contrato (privados): `_UNIDADES_PRES`, `_PRESENTACION_RE`, `_TALLE_RE`, `_COMBO_PRES_RE`, `def _unidad_pres(unidad: str) -> tuple[str, float]`.

- [ ] **Step 1: Tests del bug 1 (un peso no es un domicilio)**

Crear `tests/test_pesos.py`:

```python
"""
Pesos y presentaciones (spec petshop §5, "Correcciones de pesos y
presentaciones"). CAMBIO QUE TAMBIÉN AFECTA A LA FARMACIA: corre con los dos
perfiles.

- Bug 1: "la bolsa de 15 kg" se tomaba como domicilio ("bolsa de 15") y en
  esperando_entrega salía un link con envío a esa "dirección".
- Bug 2: "sí, pero el de 3 kg" confirmaba la bolsa de 15 pendiente, porque
  numeros_de ignora los números de una cifra y los decimales.
"""
import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as ch
from app.services.sku_service import SKUService, nombre_coincide
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (entorno es fixture)


@pytest.fixture(params=["farmacia", "petshop"])
def perfil(request, usar_perfil):
    return usar_perfil(request.param)


# ── Bug 1: un peso o un envase no es un domicilio ────────────────────────────────
@pytest.mark.parametrize("txt", [
    "la bolsa de 15 kg",
    "mandame la de 15 kilos",
    "dos latas de 85",
    "el de 2 litros",          # ya daba None (una cifra no es altura): guarda
    "una de 3 kgs",            # ídem
])
def test_peso_o_envase_no_es_domicilio(perfil, txt):
    assert ch.extraer_direccion_de(txt) is None
    assert ch.parece_direccion(txt) is False


@pytest.mark.parametrize("txt,esperado", [
    ("san javier 837", "san javier 837"),
    ("Ruta 8 kilómetro 52", "Ruta 8 kilómetro 52"),     # por eso no va "kilo\w*"
    ("16 de enero 9279", "16 de enero 9279"),
    ("donado 608 piso 2", "donado 608 piso 2"),
])
def test_direccion_con_numeros_sigue_siendo_domicilio(perfil, txt, esperado):
    assert ch.extraer_direccion_de(txt) == esperado


# ── Por el webhook ───────────────────────────────────────────────────────────────
_ROYAL_15 = "ROYAL CANIN MEDIUM ADULT 15KG"
_ROYAL_3 = "ROYAL CANIN MEDIUM ADULT 3KG"


def _catalogo_royal():
    base = {"hash": "b" * 64, "barcodes": [], "troquel": None, "brand": "Royal Canin",
            "drug": None, "form": None, "category": "Alimento Perros", "rubro": "",
            "subrubro": "", "therapeutic_actions": [], "stock": 5, "visible": True,
            "active": True, "requiere_receta": "no", "source": "t"}
    return SKUService.from_rows([
        {**base, "external_id": "RC15", "name": _ROYAL_15, "price": 98000.0},
        {**base, "external_id": "RC3", "name": _ROYAL_3, "price": 24500.0},
    ])


class _Pago:
    """Proveedor de pago falso: registra cada link pedido."""
    def __init__(self):
        self.links = []

    async def crear_link(self, sku_id, nombre, precio, phone, cantidad=1):
        self.links.append(nombre)
        return f"https://pago.test/{len(self.links)}", None


def _armar(entorno, monkeypatch, guion):
    """Entorno del webhook con el catálogo Royal Canin y un cobro falso. El
    webhook elige el proveedor en cada mensaje (payment_svc_para, webhook.py:1278),
    así que se fija ahí y no solo en deps["payment"]."""
    deps = entorno(guion)
    deps["sku"] = _catalogo_royal()
    pago = _Pago()
    deps["payment"] = pago
    monkeypatch.setattr(wh, "payment_svc_para", lambda cfg, s=None: pago)
    return deps, pago


async def _pendiente_royal_15(deps, estado):
    await deps["session"].set_pending(PHONE, sku_id="RC15", sku_nombre=_ROYAL_15,
                                      precio=98000.0, cantidad=1, opciones=[])
    if estado != "esperando_confirmacion":
        await deps["session"].set_estado(PHONE, estado)


async def test_bolsa_de_15_kg_en_entrega_no_genera_envio(perfil, entorno, monkeypatch):
    txt = "la bolsa de 15 kg"
    deps, pago = _armar(entorno, monkeypatch, {txt: {
        "intencion": "pedido",
        "respuesta": "Sí, es la de 15 kg. ¿La retirás o te la enviamos?"}})
    await _pendiente_royal_15(deps, "esperando_entrega")

    await wh.procesar_mensajes([_msg(txt)])

    s = await deps["session"].get(PHONE)
    assert pago.links == []                                   # ningún link
    assert s["estado"] == "esperando_entrega"                 # sigue eligiendo la entrega
    assert s.get("tipo_entrega") != "envio" and not s.get("direccion_envio")
    assert deps["wa"].enviados and "bolsa de 15*" not in deps["wa"].enviados[-1]
```

- [ ] **Step 2: Correrlos y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_pesos.py -v
```

Esperado: `8 failed, 12 passed`. Fallan, en los dos perfiles: `test_peso_o_envase_no_es_domicilio` con "la bolsa de 15 kg" (`AssertionError: assert 'bolsa de 15' is None`), "mandame la de 15 kilos" (`assert 'de 15' is None`) y "dos latas de 85" (`assert 'dos latas de 85' is None`); y `test_bolsa_de_15_kg_en_entrega_no_genera_envio` (`assert ['ROYAL CANIN MEDIUM ADULT 15KG'] == []`: salió un link con envío a "bolsa de 15"). Pasan las guardas: "el de 2 litros", "una de 3 kgs" y las 4 direcciones.

- [ ] **Step 3: Implementar `_NO_DIR` con pesos y envases**

`app/services/checkout_helper.py:65-71`. Reemplazar:

```python
# (casos reales 6/8, 26/8, 1/10).
_NO_DIR = re.compile(
    r"\?|\d\s*(mg|ml|gr?s?|cc|mcg|ui|%)\b|\bx\s*\d+|"
    r"\b(comprimid\w*|comp|c[aá]psul\w*|bl[ií]ster\w*|caja\w*|tiras?|unidad\w*|frasco\w*|"
    r"ped[ií]\w*|quiero|quer[ií]a|precio\w*|link|cu[aá]nto|stock|ten[eé]s|tendr[aá]s|"
    r"receta\w*|veces|producto\w*)\b",
    re.IGNORECASE)
```

por:

```python
# (casos reales 6/8, 26/8, 1/10). Pesos y envases tampoco son domicilio: "la
# bolsa de 15 kg" devolvía "bolsa de 15" (spec petshop §5). No va "kilo\w*":
# "Ruta 8 kilómetro 52" es una dirección.
_NO_DIR = re.compile(
    r"\?|\d\s*(mg|ml|gr?s?|kgs?|lts?|cc|mcg|ui|%)\b|\bx\s*\d+|"
    r"\b(comprimid\w*|comp|c[aá]psul\w*|bl[ií]ster\w*|caja\w*|tiras?|unidad\w*|frasco\w*|"
    r"ped[ií]\w*|quiero|quer[ií]a|precio\w*|link|cu[aá]nto|stock|ten[eé]s|tendr[aá]s|"
    r"receta\w*|veces|producto\w*|kilos?|kilogram\w*|litros?|bolsa\w*|lata\w*)\b",
    re.IGNORECASE)
```

- [ ] **Step 4: Correrlos y ver que pasan (con los de dirección de hoy)**

```bash
.venv/Scripts/python -m pytest tests/test_pesos.py tests/test_direccion_envio.py -v
```

Esperado: `40 passed` (20 de `test_pesos.py` + 20 de `test_direccion_envio.py`, sin cambios).

- [ ] **Step 5: Commit**

```bash
git add app/services/checkout_helper.py tests/test_pesos.py
git commit -m "$(cat <<'EOF'
Un peso o un envase ("la bolsa de 15 kg") ya no se toma como domicilio de envío

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Tests del bug 2 (presentaciones)**

Agregar al final de `tests/test_pesos.py`. La tabla es la de §5 con los nombres "…" del spec completados con un nombre real de cada línea (en todos los pares `nombre_coincide` da True), más un par 12 de farmacia con dosis combinada (ver Step 8):

```python


# ── Bug 2: presentaciones con unidad ─────────────────────────────────────────────
@pytest.mark.parametrize("txt,esperado", [
    ("royal canin 7,5 kg", {("g", 7500.0)}),
    ("400GRS", {("g", 400.0)}),
    ("1L", {("ml", 1000.0)}),
    ("pretal kipper n 4", {("n", 4.0)}),
    ("Nº 4", {("n", 4.0)}),
    ("ibuprofeno 600", set()),
    # Dosis combinada de farmacia: la unidad vale para los dos números
    ("Janumet 50/1000 Mg Comp.X 28", {("g", 0.05), ("g", 1.0)}),
])
def test_presentaciones_de(perfil, txt, esperado):
    assert ch.presentaciones_de(txt) == esperado


@pytest.mark.parametrize("entidad,pendiente,contradice", [
    ("royal canin 3 kg", "ROYAL CANIN MEDIUM ADULT 15KG", True),        # hoy confirma (bug)
    ("royal canin 15 kg", "ROYAL CANIN MEDIUM ADULT 15KG", False),
    ("royal canin 7.5 kg", "ROYAL CANIN MINI ADULT 7,5 KG", False),     # 7500 g = 7500 g
    ("royal urinary 400 gr", "ROYAL URINARY CAT LATA 400GRS", False),
    ("royal urinary 1.5 kg", "ROYAL URINARY CAT LATA 400GRS", True),    # hoy confirma (bug)
    ("shampoo 2 litros", "SHAMPOO OSSPRET PERRO 1L", True),            # hoy confirma (bug)
    ("pretal kipper nº 3", "PRETAL KIPPER Nº 4", True),                # hoy confirma (bug)
    ("pretal kipper n 3", "PRETAL KIPPER Nº 4", True),                 # hoy confirma (bug)
    ("curflex x 30", "CURFLEX PLUS X 60", True),                        # regla de hoy
    ("ibuprofeno 600", "IBUPROFENO 600 MG X 10", False),                # sin unidad: regla de hoy
    ("royal canin", "ROYAL CANIN MEDIUM ADULT 15KG", False),            # sin presentación
    ("janumet 50 mg", "Janumet 50/1000 Mg Comp.X 28", False),           # guarda farmacia (dosis combinada)
])
def test_entidad_contradice_pendiente_por_presentacion(perfil, entidad, pendiente, contradice):
    assert nombre_coincide(entidad, pendiente)      # mismo producto: decide la presentación
    assert ch.entidad_contradice_pendiente(entidad, pendiente) is contradice


async def test_si_pero_el_de_3_kg_no_confirma_la_de_15(perfil, entorno, monkeypatch):
    txt = "sí, pero el de 3 kg"
    deps, pago = _armar(entorno, monkeypatch, {txt: {
        "intencion": "pedido", "confirmacion": True,
        "entidad_producto": "royal canin 3 kg",
        "respuesta": "Tengo la Royal Canin Medium Adult de 3 kg. ¿Te sirve?"}})
    await _pendiente_royal_15(deps, "esperando_confirmacion")

    await wh.procesar_mensajes([_msg(txt)])

    s = await deps["session"].get(PHONE)
    assert s["estado"] == "esperando_confirmacion"            # no pasó a la entrega
    assert "preferís" not in deps["wa"].enviados[-1].lower()  # no preguntó retiro/envío
    assert pago.links == []
    # Va como otro pedido: se buscó el de 3 kg y quedó entre las opciones
    assert "RC3" in [o["sku_id"] for o in s.get("pending_opciones") or []]


async def test_si_la_de_15_kg_confirma(perfil, entorno, monkeypatch):
    txt = "sí, la de 15 kg"
    deps, pago = _armar(entorno, monkeypatch, {txt: {
        "intencion": "pedido", "confirmacion": True,
        "entidad_producto": "royal canin 15 kg",
        "respuesta": "¡Perfecto!"}})
    await _pendiente_royal_15(deps, "esperando_confirmacion")

    await wh.procesar_mensajes([_msg(txt)])

    s = await deps["session"].get(PHONE)
    assert s["estado"] == "esperando_entrega"                 # confirmó: elige la entrega
    assert s["pending_sku_id"] == "RC15"
```

- [ ] **Step 7: Correrlos y ver que fallan**

```bash
.venv/Scripts/python -m pytest tests/test_pesos.py::test_presentaciones_de tests/test_pesos.py::test_entidad_contradice_pendiente_por_presentacion tests/test_pesos.py::test_si_pero_el_de_3_kg_no_confirma_la_de_15 tests/test_pesos.py::test_si_la_de_15_kg_confirma -v
```

Esperado: `26 failed, 16 passed`. Fallan los 14 de `test_presentaciones_de` (`AttributeError: module 'app.services.checkout_helper' has no attribute 'presentaciones_de'`), los 5 pares marcados "(bug)" en cada perfil (10; `assert False is True`) y `test_si_pero_el_de_3_kg_no_confirma_la_de_15` en los dos perfiles (`AssertionError: assert 'esperando_entrega' == 'esperando_confirmacion'`: confirmó la de 15 y preguntó la entrega). Pasan los otros 7 pares en cada perfil y `test_si_la_de_15_kg_confirma` (guarda).

- [ ] **Step 8: Implementar `presentaciones_de` y la regla nueva de `entidad_contradice_pendiente`**

Además del regex del spec, se expande la dosis combinada ("50/1000 Mg" → "50 Mg / 1000 Mg"). Sin eso la farmacia cambia: `data/catalogo_base.csv` tiene 82 productos con "N/M unidad" y, por ejemplo, "janumet 50 mg" sobre "Janumet 50/1000 Mg Comp.X 28" confirma hoy y pasaría a contradecir (el regex solo ve "1000 Mg"). Con la expansión, la tabla de §5 da lo mismo.

`app/services/checkout_helper.py:1555-1567`. Reemplazar:

```python
# ── 47: confirmar con un producto distinto en el mensaje no confirma ──────────
def entidad_contradice_pendiente(entidad: Optional[str], pending_nombre: Optional[str]) -> bool:
    """
    "quiero el curflex x 30" con Curflex Plus pendiente: el cliente nombra un
    producto y no coincide con el pendiente → NO es una confirmación, es otro
    pedido (feedback 47: saltaba a "¿cómo lo querés recibir?").
    """
    if not entidad or not pending_nombre:
        return False
    from app.services.sku_service import nombre_coincide, numeros_de
    if not nombre_coincide(entidad, pending_nombre):
        return True
    # Mismo nombre pero distinta presentación numérica ("x 30" vs "x 60").
```

por:

```python
# ── Presentaciones con unidad: peso, volumen y talle ──────────────────────────
# numeros_de (sku_service) ignora a propósito los números de una cifra y los
# decimales ("dame 2" es una cantidad), así que "el de 3 kg" no contradecía a
# "ROYAL CANIN MEDIUM ADULT 15KG" y se cobraba la bolsa de 15 (spec petshop §5).
_UNIDADES_PRES = r"kgs?|kilos?|kilogram\w*|grs?|g|gramos?|mg|ml|cc|lts?|l|litros?"
_PRESENTACION_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(" + _UNIDADES_PRES + r")\b", re.IGNORECASE)
# Talle: "nº 4", "n° 4", "n 4" (así lo escribe el modelo), "numero 4", "talle 4".
_TALLE_RE = re.compile(r"\b(?:n[º°o]?|numero|número|talle)\.?\s*(\d{1,2})\b", re.IGNORECASE)
# Dosis combinadas de farmacia ("Janumet 50/1000 Mg"): la unidad vale para los
# dos números. Sin esto, "janumet 50 mg" contradecía al pendiente.
_COMBO_PRES_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*/\s*(?=\d+(?:[.,]\d+)?\s*(" + _UNIDADES_PRES + r")\b)", re.IGNORECASE)


def _unidad_pres(unidad: str) -> tuple[str, float]:
    """(tipo, factor) de una unidad: peso en gramos, volumen en ml."""
    u = unidad.lower()
    if u.startswith("k"):                   # kg, kgs, kilo(s), kilogramo(s)
        return "g", 1000.0
    if u == "mg":
        return "g", 0.001
    if u.startswith("g"):                   # g, gr, grs, gramo(s)
        return "g", 1.0
    if u in ("ml", "cc"):
        return "ml", 1.0
    return "ml", 1000.0                     # l, lt, lts, litro(s)


def presentaciones_de(t: str) -> set[tuple[str, float]]:
    """
    Presentaciones con unidad de un texto, normalizadas para comparar:
    ("g", gramos), ("ml", mililitros) o ("n", talle). "royal canin 7,5 kg" →
    {("g", 7500.0)}; "1L" → {("ml", 1000.0)}; "Nº 4" → {("n", 4.0)}. Acepta
    una cifra y decimales con punto o coma; redondea a 3 decimales. Un número
    sin unidad ("ibuprofeno 600") no es una presentación.
    """
    s = _COMBO_PRES_RE.sub(lambda m: f"{m.group(1)} {m.group(2)} / ", t or "")
    out: set[tuple[str, float]] = set()
    for num, unidad in _PRESENTACION_RE.findall(s):
        tipo, factor = _unidad_pres(unidad)
        out.add((tipo, round(float(num.replace(",", ".")) * factor, 3)))
    for num in _TALLE_RE.findall(s):
        out.add(("n", float(num)))
    return out


# ── 47: confirmar con un producto distinto en el mensaje no confirma ──────────
def entidad_contradice_pendiente(entidad: Optional[str], pending_nombre: Optional[str]) -> bool:
    """
    "quiero el curflex x 30" con Curflex Plus pendiente: el cliente nombra un
    producto y no coincide con el pendiente → NO es una confirmación, es otro
    pedido (feedback 47: saltaba a "¿cómo lo querés recibir?").
    """
    if not entidad or not pending_nombre:
        return False
    from app.services.sku_service import nombre_coincide, numeros_de
    if not nombre_coincide(entidad, pending_nombre):
        return True
    # Misma unidad y ningún valor en común: "el de 3 kg" sobre "... 15KG",
    # "2 litros" sobre "1L", "nº 3" sobre "Nº 4".
    p_ent, p_pend = presentaciones_de(entidad), presentaciones_de(pending_nombre)
    for tipo in {t for t, _ in p_ent} & {t for t, _ in p_pend}:
        if not ({v for t, v in p_ent if t == tipo} & {v for t, v in p_pend if t == tipo}):
            return True
    # Mismo nombre pero distinta presentación numérica ("x 30" vs "x 60").
```

Las dos líneas siguientes (`n_ent, n_pend = ...` y el `return bool(...)`, hoy 1568-1569) no cambian: `numeros_de` sigue igual porque alimenta el ranking de la búsqueda.

- [ ] **Step 9: Correrlos y ver que pasan**

```bash
.venv/Scripts/python -m pytest tests/test_pesos.py -v
```

Esperado: `62 passed` (31 casos × 2 perfiles).

- [ ] **Step 10: Suite completa**

```bash
.venv/Scripts/python -m pytest -q
```

Esperado: `0 failed`. El total sube en 62 respecto de la corrida de la tarea anterior. Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1355 tests. Siguen en verde sin edición, entre otros, `tests/test_direccion_envio.py`, `tests/test_logic.py` (feedback 47, curflex) y `tests/test_webhook_secuencias.py`.

- [ ] **Step 11: Commit**

```bash
git add app/services/checkout_helper.py tests/test_pesos.py
git commit -m "$(cat <<'EOF'
Confirmar con otra presentación (3 kg, 2 litros, nº 3) ya no cobra la del pendiente

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: Conversaciones punta a punta del petshop y script de prueba del prompt (§6.2 venta, compra completa, links y recetas; §6.5)

**Files:**
- Modify: `tests/test_petshop_conversaciones.py` (agregar una sección al final; el archivo lo crea la Task 6 y le suman secciones las Tasks 7 a 10. Esta sección usa `_intenciones_perf(deps)` de la Task 6)
- Create: `tests/test_probar_prompt_petshop.py`
- Create: `scripts/probar_prompt_petshop.py`

**Interfaces:**
- Consumes: `usar_perfil` (Task 1); `config_service.valores_base()`; `get_perfil()` con los perfiles `farmacia`, `mutual` (`venta=False`) y `petshop`; los gates ya implementados: `necesita_receta` con `recetas`, `_rec_on` en adicionales, `get_perfil().recetas and pide_receta_nube`, `links_como_receta` antes de `contiene_link`, `not get_perfil().venta` antes de `_flujo_mutual`, `descuento_para` con `socios`; Task 13 (`pregunta_entrega` y `texto_entrega` con `retiro_sucursal`); `get_intent_service(anthropic_key, openai_key="", provider="anthropic")` sin `vertical` (Task 4); `entorno`, `_msg`, `PHONE` de `tests/test_webhook_secuencias.py`; `wh.payment_svc_para(cfg, s)` y `wh._flujo_mutual` (se reemplazan en los tests).
- Produces (fuera del contrato, nuevos): `scripts/probar_prompt_petshop.py` con
  - `@dataclass(frozen=True) class Caso: frase: str; esperado: str; intencion: str | None = None; por_sintoma: bool | None = None; entidad: str | None = None; adicionales: tuple[str, ...] = (); contiene: tuple[str, ...] = (); no_contiene: tuple[str, ...] = (); sin_direccion: bool = False; resultados: list | None = None`
  - `CASOS: tuple[Caso, ...]` (las 18 frases de §6.5)
  - `def revisar(caso: Caso, r: dict) -> list[str]`
  - `async def correr(intent, casos=CASOS, out=print) -> int`
  - `def main() -> int` (0 sin desvíos, 1 con desvíos, 2 si falta configuración)

Los helpers de esta sección llevan el prefijo `_e2e` y los tests `test_e2e_`: el mismo módulo tiene secciones de otras tareas con helpers como `_catalogo_mo` o `_intenciones`, y un nombre repetido pisaría al anterior en silencio. La intención de cada mensaje se lee con `_intenciones_perf(deps)`, el helper que ya definió la Task 6 con el mismo cuerpo (no se repite con otro nombre).

- [ ] **Step 1: Precondiciones**

```bash
git status --short
ls tests/test_petshop_conversaciones.py
```

Esperado: `git status` vacío (lo usa el Step 4) y el archivo existe.

- [ ] **Step 2: Escribir las conversaciones punta a punta**

Agregar al final de `tests/test_petshop_conversaciones.py`:

```python


# ══════════════════════════════════════════════════════════════════════════════
# Punta a punta (Task 15): venta, compra completa, links y recetas en cadena.
# Helpers con prefijo _e2e para no pisar los de las secciones de otras tareas.
# ══════════════════════════════════════════════════════════════════════════════
import pytest  # noqa: E402,F811

from app.routers import webhook as wh  # noqa: E402,F811
from app.services import checkout_helper as _e2e_chh  # noqa: E402
from app.services import config_service as _e2e_cs  # noqa: E402
from app.services.config_service import valores_base as _e2e_valores_base  # noqa: E402
from app.services.sku_service import SKUService  # noqa: E402,F811
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: E402,F401,F811

_E2E_SUC = {"retiro_sucursal": "Sucursal Piloto",
            "retiro_info_message": "Lo retirás en *{sucursal}*, Calle Falsa 123 🐾"}
_E2E_PROHIBIDAS = ("farmac", "receta", "socio", "obra social", "mutual", "cuenta corriente", "💊")


def _e2e_catalogo():
    base = {"hash": "f" * 64, "barcodes": [], "troquel": None, "brand": "", "drug": None,
            "form": None, "rubro": "PERROS", "subrubro": "", "therapeutic_actions": [],
            "stock": 5, "visible": True, "active": True, "source": "mercurio",
            "category": "ALIMENTOS", "requiere_receta": "no"}
    return SKUService.from_rows([
        {**base, "external_id": "40", "name": "DOG CHOW ADULTO RAZAS MEDIANAS 15KG", "price": 52000.0},
        {**base, "external_id": "41", "name": "DOG CHOW CACHORROS 3KG", "price": 14500.0},
        {**base, "external_id": "42", "name": "PIPETA FRONTLINE PLUS PERRO 10-20KG", "price": 25000.0,
         "category": "Medicamentos Bajo Receta", "requiere_receta": "si"},
    ])


class _E2ECfg:
    """Config falsa: textos del perfil activo + sucursal de retiro cargada."""
    def __init__(self, extra=None):
        self.v = {**_e2e_valores_base(), **_E2E_SUC, **(extra or {})}

    async def get_all(self):
        return dict(self.v)

    async def get(self, k):
        return self.v.get(k)

    async def get_hours(self):
        return {"enabled": False}

    def is_open_now(self, hours):
        return True

    def proxima_apertura(self, hours):
        return ""

    def texto_horario(self, hours):
        return ""


class _E2EPago:
    """Proveedor de cobro falso: guarda cada link pedido."""
    def __init__(self):
        self.links = []

    async def crear_link(self, **k):
        self.links.append(k)
        return f"https://pago.test/mo-{len(self.links)}", None


def _e2e_armar(entorno, monkeypatch, guion=None):
    """Webhook completo con catálogo de MO, sucursal cargada y cobro falso.
    El perfil se fija ANTES (la config falsa toma los textos del perfil)."""
    deps = entorno(guion)
    deps["sku"] = _e2e_catalogo()
    deps["config"] = _E2ECfg()
    pago = _E2EPago()
    monkeypatch.setattr(wh, "payment_svc_para", lambda cfg, s=None: pago)
    # crear_link_y_responder lee la config del servicio real: que lea la falsa.
    monkeypatch.setattr(_e2e_cs, "get_config_service", lambda *a, **k: deps["config"])

    async def _sin_freno(*a, **k):
        return None, None
    monkeypatch.setattr(_e2e_chh, "_chequear_stock_vivo", _sin_freno)
    monkeypatch.delitem(_e2e_chh._ULTIMA_DIRECCION, PHONE, raising=False)
    return deps, pago


async def _e2e_charla(deps, *mensajes):
    for t in mensajes:
        await wh.procesar_mensajes([_msg(t)])
    return await deps["session"].get(PHONE)


def _e2e_sin_rubro_farmacia(enviados):
    for t in enviados:
        assert not any(p in t.lower() for p in _E2E_PROHIBIDAS), t


# ── Venta: solo la mutual entra al flujo sin venta ──────────────────────────────
@pytest.mark.parametrize("clave,llamadas", [("farmacia", 0), ("petshop", 0), ("mutual", 1)])
async def test_e2e_solo_la_mutual_entra_al_flujo_sin_venta(usar_perfil, entorno, monkeypatch,
                                                           clave, llamadas):
    usar_perfil(clave)
    deps = entorno()
    vistos = []

    async def _flujo(deps_, phone, session, texto, *a, **k):
        vistos.append(texto)
        return "Info de la mutual", "mutual_info"
    monkeypatch.setattr(wh, "_flujo_mutual", _flujo)
    await wh.procesar_mensajes([_msg("hola")])
    assert len(vistos) == llamadas
    assert deps["wa"].enviados[-1] == ("Info de la mutual" if llamadas else "¡Hola!")


# ── Compra completa en petshop ──────────────────────────────────────────────────
async def test_e2e_compra_completa_con_retiro_en_la_sucursal(usar_perfil, entorno, monkeypatch):
    usar_perfil("petshop")
    consulta = "tenés dog chow 15 kg?"
    deps, pago = _e2e_armar(entorno, monkeypatch, {
        "hola": {"intencion": "saludo",
                 "respuesta": "¡Hola! Soy el asistente virtual de Mascotas del Oeste 🐾 "
                              "¿En qué te puedo ayudar?"},
        consulta: {"intencion": "consulta_stock", "entidad_producto": "dog chow 15 kg",
                   "sku_seleccionado_index": 1,
                   "respuesta": "¡Sí! Tengo el Dog Chow Adulto Razas Medianas 15 kg a $52.000. "
                                "¿Te lo preparo?"},
    })
    s = await _e2e_charla(deps, "hola", consulta, "si", "retiro")
    env = deps["wa"].enviados
    assert len(env) == 4
    assert env[0].startswith("¡Hola! Soy el asistente virtual de Mascotas del Oeste")
    assert "$52.000" in env[1]
    assert env[2] == ("¡Genial! ¿Cómo preferís recibirlo: *retiro en Sucursal Piloto* "
                      "o *envío a domicilio*?")
    assert "https://pago.test/mo-1" in env[3]
    assert "🏪 Lo retirás en *Sucursal Piloto*" in env[3]
    assert [(l["sku_id"], l["precio"]) for l in pago.links] == [("40", 52000.0)]
    assert s["estado"] == "esperando_pago" and s["tipo_entrega"] == "retiro"
    _e2e_sin_rubro_farmacia(env)


# ── Links: en petshop un link va al modelo; la mutual sigue derivando ───────────
@pytest.mark.parametrize("clave,deriva", [("petshop", False), ("mutual", True)])
async def test_e2e_link_de_instagram(usar_perfil, entorno, monkeypatch, clave, deriva):
    usar_perfil(clave)
    txt = "Hola, tenés este? https://www.instagram.com/p/C1abc/"
    deps = entorno({txt: {"intencion": "social",
                          "respuesta": "¡Hola! No puedo abrir links 🙈 ¿Me decís qué producto es?"}})

    async def _flujo(*a, **k):
        return "Info de la mutual", "mutual_info"
    monkeypatch.setattr(wh, "_flujo_mutual", _flujo)
    await wh.procesar_mensajes([_msg(txt)])
    s = await deps["session"].get(PHONE)
    if deriva:
        assert deps["wa"].enviados[-1].startswith("Recibí tu link")
        assert s["estado"] == "operador" and s["derivada_motivo"] == "receta_link"
        return
    assert not any(t.startswith("Recibí tu link") for t in deps["wa"].enviados)
    assert s.get("estado") != "operador"
    assert ("rapido", txt) in deps["intent"].vistos
    assert deps["wa"].enviados[-1] == "¡Hola! No puedo abrir links 🙈 ¿Me decís qué producto es?"


# ── Recetas punta a punta: un producto "bajo receta" del ERP se vende ───────────
@pytest.mark.parametrize("clave", ["petshop", "farmacia"])
async def test_e2e_compra_de_una_pipeta_bajo_receta(usar_perfil, entorno, monkeypatch, clave):
    usar_perfil(clave)
    consulta = "tenés pipeta frontline para perro de 10 a 20 kg?"
    deps, pago = _e2e_armar(entorno, monkeypatch, {consulta: {
        "intencion": "consulta_stock", "entidad_producto": "pipeta frontline", "por_sintoma": False,
        "sku_seleccionado_index": 1,
        "respuesta": ("Tengo la Pipeta Frontline Plus Perro 10-20kg a $25.000. "
                      "Ojo que va con receta del veterinario. ¿La querés?")}})
    s = await _e2e_charla(deps, consulta, "si", "retiro")
    if clave == "farmacia":        # igual que hoy: la receta la gestiona una persona
        assert "derivado_receta" in _intenciones_perf(deps)
        assert s["estado"] == "operador" and pago.links == []
        return
    env = deps["wa"].enviados
    assert "$25.000" in env[0] and "¿La querés?" in env[0]
    assert "derivado_receta" not in _intenciones_perf(deps)
    assert [l["sku_id"] for l in pago.links] == ["42"]
    assert s["estado"] == "esperando_pago"
    for t in env:
        assert "receta" not in t.lower() and "medicamento" not in t.lower() and "🩺" not in t


@pytest.mark.parametrize("clave", ["petshop", "farmacia"])
async def test_e2e_pedido_con_pipeta_adicional_hasta_el_link(usar_perfil, entorno, monkeypatch, clave):
    usar_perfil(clave)
    pedido = "quiero el alimento Dog Chow 3kg y una pipeta frontline"
    deps, pago = _e2e_armar(entorno, monkeypatch, {pedido: {
        "intencion": "pedido", "entidad_producto": "alimento dog chow 3kg",
        "entidades_adicionales": ["pipeta frontline"], "sku_seleccionado_index": 1,
        "respuesta": "Tengo el Dog Chow Cachorros 3 kg a $14.500. ¿Te lo preparo?"}})
    s = await _e2e_charla(deps, pedido)
    if clave == "farmacia":        # igual que hoy: el adicional con receta deriva el pedido
        assert _intenciones_perf(deps)[-1] == "derivado_receta" and s["estado"] == "operador"
        return
    r1 = deps["wa"].enviados[-1]
    assert "Sobre lo demás que me pediste:" in r1
    assert "• pipeta frontline: PIPETA FRONTLINE PLUS PERRO 10-20KG — $25,000.00" in r1
    assert [e["sku_id"] for e in s["extras_ofrecidos"]] == ["42"]
    # "todos" suma la pipeta; "si" confirma; "retiro" cobra los dos juntos.
    s = await _e2e_charla(deps, "todos", "si", "retiro")
    assert "derivado_receta" not in _intenciones_perf(deps)
    assert [(l["sku_id"], l["precio"]) for l in pago.links] == [("MULTI", 39500.0)]
    assert s["estado"] == "esperando_pago"
    _e2e_sin_rubro_farmacia(deps["wa"].enviados)


@pytest.mark.parametrize("clave", ["petshop", "farmacia"])
async def test_e2e_agregar_pipeta_con_el_link_ya_enviado(usar_perfil, entorno, monkeypatch, clave):
    usar_perfil(clave)
    consulta, agrega = "tenés dog chow cachorros 3kg?", "agregame la pipeta frontline"
    deps, pago = _e2e_armar(entorno, monkeypatch, {
        consulta: {"intencion": "consulta_stock", "entidad_producto": "dog chow cachorros 3kg",
                   "sku_seleccionado_index": 1,
                   "respuesta": "Tengo el Dog Chow Cachorros 3 kg a $14.500. ¿Te lo preparo?"},
        agrega: {"intencion": "pedido", "entidad_producto": "pipeta frontline",
                 "agregar_al_pedido": True, "sku_seleccionado_index": 1,
                 "respuesta": "Te sumo la Pipeta Frontline Plus Perro 10-20kg a $25.000."},
    })
    s = await _e2e_charla(deps, consulta, "si", "retiro")
    assert s["estado"] == "esperando_pago" and len(pago.links) == 1
    s = await _e2e_charla(deps, agrega)
    if clave == "farmacia":        # igual que hoy: agregar algo con receta deriva
        assert _intenciones_perf(deps)[-1] == "derivado_receta" and s["estado"] == "operador"
        return
    r = deps["wa"].enviados[-1]
    assert r.startswith("¡Listo, lo sumé! Tu pedido queda así:")
    assert "PIPETA FRONTLINE PLUS PERRO 10-20KG" in r and "receta" not in r.lower()
    assert _intenciones_perf(deps)[-1] == "item_agregado"
    assert [i["sku_id"] for i in s["pending_items"]] == ["41", "42"]


@pytest.mark.parametrize("clave", ["petshop", "farmacia"])
async def test_e2e_receta_cargada_en_el_sistema_va_al_modelo(usar_perfil, entorno, clave):
    usar_perfil(clave)
    txt = "tengo la receta del veterinario cargada en el sistema"
    deps = entorno({txt: {"intencion": "social",
                          "respuesta": "¡Dale! Contame qué producto necesitás y te lo busco 🐾"}})
    s = await _e2e_charla(deps, txt)
    enviado = deps["wa"].enviados[-1]
    if clave == "farmacia":        # igual que hoy
        assert "sistema de recetas" in enviado and s["derivada_motivo"] == "receta_nube"
        return
    assert "sistema de recetas" not in enviado and "🩺" not in enviado
    assert ("rapido", txt) in deps["intent"].vistos
    assert s.get("estado") != "operador"
```

Notas del guion (verificadas contra el código): "¡Listo, lo sumé!" sale del flujo normal (`webhook.py:2289`), que solo se alcanza con el link ya enviado (`esperando_pago`); con el pedido en `esperando_confirmacion` el camino es el Paso 1c y el texto es "¡Listo! Tu pedido queda así:". `crear_link_y_responder` lee la config del servicio real (`get_config_service`), por eso se reemplaza.

- [ ] **Step 3: Correrlos**

```bash
.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py -k e2e -v
```

Esperado: `14 passed`. Estos tests verifican de punta a punta conductas que ya implementaron las Tasks 6 (recetas), 7 (links), 12 (venta) y 13, así que pasan en la primera corrida. Si alguno falla, el defecto está en la tarea dueña de esa capacidad: corregirlo ahí, con su test unitario, antes de seguir. El Step 4 demuestra que no pasan en vacío.

- [ ] **Step 4: Ver que fallan si se apaga cada gate (mutación temporal, sin commit)**

Recetas (los tres gates que usan estas charlas):

```bash
sed -i -E 's/if not (get_)?perfil(\(\))?\.recetas:/if False:/' app/services/checkout_helper.py
sed -i -E -e 's/_rec_on = (get_)?perfil(\(\))?\.recetas/_rec_on = True/' -e 's/if (get_)?perfil(\(\))?\.recetas and pide_receta_nube/if pide_receta_nube/' app/routers/webhook.py
git diff --stat
.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py -k "e2e and (pipeta or receta)" -v
git checkout -- app/services/checkout_helper.py app/routers/webhook.py
```

Esperado: `git diff --stat` muestra `app/services/checkout_helper.py` y `app/routers/webhook.py` (además del archivo de tests sin commitear; si alguno de los dos sale sin cambios, ubicar el gate con `grep -n "recetas" <archivo>` y editarlo a mano). La corrida da `4 failed, 4 passed`: fallan las cuatro variantes `[petshop]` (`derivado_receta`, "sistema de recetas") y pasan las `[farmacia]`.

Links y venta:

```bash
sed -i -E -e 's/(get_)?perfil(\(\))?\.links_como_receta and //' -e 's/if not (get_)?perfil(\(\))?\.venta:/if True:/' app/routers/webhook.py
git diff --stat
.venv/Scripts/python -m pytest tests/test_petshop_conversaciones.py -k "e2e and (instagram or mutual)" -v
git checkout -- app/routers/webhook.py
git status --short
```

Esperado: `3 failed, 2 passed`. Fallan `test_e2e_solo_la_mutual_entra_al_flujo_sin_venta[farmacia-0]` y `[petshop-0]` y `test_e2e_link_de_instagram[petshop-False]`. Al final, `git status --short` muestra solo `tests/test_petshop_conversaciones.py`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_petshop_conversaciones.py
git commit -m "Petshop: conversaciones punta a punta de venta, compra completa, links y recetas" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Escribir el test del script (falla)**

Crear `tests/test_probar_prompt_petshop.py`:

```python
"""
scripts/probar_prompt_petshop.py (spec 2026-10-06 §6.5): la prueba manual del
prompt con el LLM real. Acá se prueba sin red: la lista de frases, el control
de desvíos y que cada frase pase por Haiku y por Sonnet.
"""
import importlib.util
from pathlib import Path

_RUTA = Path(__file__).resolve().parents[1] / "scripts" / "probar_prompt_petshop.py"

_LISTA_DEL_SPEC = (
    "hola", "¿sos un bot?", "hola, tenés royal canin?", "mi perro vomita, que le doy?",
    "mi gato tiene diarrea", "cuantas gotas le pongo", "¿cuánto Drontal le doy?",
    "pasame con el veterinario", "una pipeta para perro de 10 kg", "algo para las pulgas",
    "tenés pipeta Frontline 10-20 kg", "que alimento le doy a un cachorro",
    "qué alimento para gato castrado", "alimento royal canin y piedras sanicat",
    "tenés alimento para gato?", "anotalo a mi cuenta", "¿tienen descuento?",
    "donde queda la sucursal?",
)


def _script():
    spec = importlib.util.spec_from_file_location("probar_prompt_petshop", _RUTA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _caso(m, frase):
    return next(c for c in m.CASOS if c.frase == frase)


def test_los_casos_cubren_la_lista_minima_del_spec():
    frases = [c.frase for c in _script().CASOS]
    assert sorted(frases) == sorted(_LISTA_DEL_SPEC)


def test_revisar_marca_los_desvios():
    m = _script()
    vomita = _caso(m, "mi perro vomita, que le doy?")
    assert m.revisar(vomita, {"intencion": "consulta_abierta", "por_sintoma": True,
                              "respuesta": "Te paso con alguien del equipo 🐾"}) == []
    assert m.revisar(vomita, {"intencion": "consulta_abierta", "por_sintoma": False,
                              "respuesta": "Dale Reliveran a $3.000"}) == [
        "por_sintoma False (esperado True)", "ofrece un precio ante un síntoma"]
    hola = _caso(m, "hola")
    assert m.revisar(hola, {"intencion": "saludo",
                            "respuesta": "¡Hola! Soy el asistente virtual de Mascotas del Oeste 🐾"}) == []
    assert "la respuesta dice 'remedia'" in m.revisar(
        hola, {"intencion": "saludo", "respuesta": "¡Hola! Soy el asistente virtual de Remedia 💊"})
    varios = _caso(m, "alimento royal canin y piedras sanicat")
    assert m.revisar(varios, {"entidad_producto": "alimento royal canin",
                              "entidades_adicionales": ["piedras sanicat"], "respuesta": "x"}) == []
    assert m.revisar(varios, {"entidad_producto": "alimento royal canin, piedras sanicat",
                              "entidades_adicionales": [], "respuesta": "x"}) == [
        "falta 'piedras sanicat' en entidades_adicionales []"]
    sucursal = _caso(m, "donde queda la sucursal?")
    assert m.revisar(sucursal, {"respuesta": "Queda en Rivadavia 1234 🐾"}) == [
        "la respuesta trae un número de calle: ¿inventó la dirección?"]


class _IntentFalso:
    def __init__(self):
        self.llamadas = []

    async def procesar_rapido(self, mensaje, **k):
        self.llamadas.append(("rapido", mensaje, None))
        return {"intencion": "desconocido", "respuesta": "x"}

    async def procesar(self, mensaje, **k):
        self.llamadas.append(("procesar", mensaje, k.get("resultados_sku")))
        return {"intencion": "desconocido", "respuesta": "x"}


async def test_correr_pasa_cada_frase_por_haiku_y_sonnet():
    m = _script()
    intent, salida = _IntentFalso(), []
    desvios = await m.correr(intent, m.CASOS, out=salida.append)
    assert len(intent.llamadas) == 2 * len(m.CASOS)
    assert ("procesar", "tenés alimento para gato?", []) in intent.llamadas
    assert desvios > 0 and any("DESVÍO" in linea for linea in salida)


def test_importar_el_script_no_llama_al_modelo():
    m = _script()          # solo define: los imports de app van adentro de main()
    assert callable(m.main) and not hasattr(m, "get_intent_service")
```

- [ ] **Step 7: Correrlo y ver que falla**

```bash
.venv/Scripts/python -m pytest tests/test_probar_prompt_petshop.py -v
```

Esperado: `4 failed` con `FileNotFoundError: [Errno 2] No such file or directory: '...\\scripts\\probar_prompt_petshop.py'`.

- [ ] **Step 8: Escribir el script**

Crear `scripts/probar_prompt_petshop.py`:

```python
"""
Prueba manual del prompt de petshop con el LLM real (spec 2026-10-06 §6.5).

Los tests del webhook usan un modelo falso: ninguno mide la calidad del
prompt. Este script pasa la lista mínima de frases del spec por
procesar_rapido (Haiku) y procesar (Sonnet) con el perfil petshop, imprime
intencion, entidad_producto, entidades_adicionales, por_sintoma y respuesta,
y marca como DESVÍO lo que se aparta de lo esperado. Lo que no se puede
chequear solo (tono, "sin nombre propio") queda en la línea "esperado" para
que lo mire quien lo corre.

Uso, desde la raíz del repo (Git Bash):
    VERTICAL=petshop ANTHROPIC_API_KEY=sk-ant-... .venv/Scripts/python scripts/probar_prompt_petshop.py

Sale con 0 si no hubo desvíos, 1 si hubo, 2 si falta configuración.
"""
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Vocabulario de farmacia que el bot de petshop nunca debería decir.
_PROHIBIDO_SIEMPRE = ("remedia", "💊", "farmac", "obra social", "mutual")


@dataclass(frozen=True)
class Caso:
    frase: str
    esperado: str                          # lo que tiene que ver quien corre la prueba
    intencion: str | None = None           # None = no se controla
    por_sintoma: bool | None = None
    entidad: str | None = None             # texto que tiene que estar en entidad_producto
    adicionales: tuple[str, ...] = ()      # cada uno, dentro de entidades_adicionales
    contiene: tuple[str, ...] = ()         # en la respuesta (sin distinguir mayúsculas)
    no_contiene: tuple[str, ...] = ()
    sin_direccion: bool = False            # la respuesta no trae un número de calle
    resultados: list | None = None         # [] = Sonnet ve "Sin resultados en el catálogo"


_DERIVA = ("te paso con alguien del equipo",)
_SINTOMA = "por_sintoma true y sin productos ni precios"

CASOS: tuple[Caso, ...] = (
    Caso("hola", "Saludo de Mascotas del Oeste, sin nombre propio",
         intencion="saludo", contiene=("Mascotas del Oeste",), no_contiene=("receta", "socio")),
    Caso("¿sos un bot?", '"Soy el asistente virtual de Mascotas del Oeste"',
         contiene=("asistente virtual de Mascotas del Oeste",)),
    Caso("hola, tenés royal canin?", 'consulta_stock, entidad "royal canin"',
         intencion="consulta_stock", entidad="royal canin"),
    Caso("mi perro vomita, que le doy?", _SINTOMA, por_sintoma=True),
    Caso("mi gato tiene diarrea", _SINTOMA, por_sintoma=True),
    Caso("cuantas gotas le pongo", _SINTOMA, por_sintoma=True),
    Caso("¿cuánto Drontal le doy?", _SINTOMA, por_sintoma=True),
    Caso("pasame con el veterinario", _SINTOMA, por_sintoma=True),
    Caso("una pipeta para perro de 10 kg", "por_sintoma false (se vende)", por_sintoma=False),
    Caso("algo para las pulgas", "por_sintoma false (se vende)", por_sintoma=False),
    Caso("tenés pipeta Frontline 10-20 kg", "por_sintoma false (se vende)", por_sintoma=False),
    Caso("que alimento le doy a un cachorro", "consulta_abierta, por_sintoma false",
         intencion="consulta_abierta", por_sintoma=False),
    Caso("qué alimento para gato castrado", "consulta_abierta, por_sintoma false",
         intencion="consulta_abierta", por_sintoma=False),
    Caso("alimento royal canin y piedras sanicat",
         'entidad "alimento royal canin", adicionales ["piedras sanicat"]',
         entidad="royal canin", adicionales=("piedras sanicat",)),
    Caso("tenés alimento para gato?", "Sin resultados: no ofrece productos para perro",
         no_contiene=("perro",), resultados=[]),
    Caso("anotalo a mi cuenta", 'No promete ni inventa: "te paso con alguien del equipo"',
         contiene=_DERIVA, no_contiene=("anotado", "te lo anoto")),
    Caso("¿tienen descuento?", 'No inventa descuentos: "te paso con alguien del equipo"',
         contiene=_DERIVA, no_contiene=("% de descuento", "% off")),
    Caso("donde queda la sucursal?", "Sin el dato cargado, no inventa la dirección",
         sin_direccion=True),
)


def revisar(caso: Caso, r: dict) -> list[str]:
    """Desvíos de una respuesta del modelo respecto de lo esperado ([] = bien)."""
    desvios = []
    resp = r.get("respuesta") or ""
    bajo = resp.lower()
    if caso.intencion and r.get("intencion") != caso.intencion:
        desvios.append(f"intencion {r.get('intencion')!r} (esperada {caso.intencion!r})")
    if caso.por_sintoma is not None and bool(r.get("por_sintoma")) is not caso.por_sintoma:
        desvios.append(f"por_sintoma {r.get('por_sintoma')!r} (esperado {caso.por_sintoma})")
    if caso.por_sintoma and re.search(r"\$\s?\d", resp):
        desvios.append("ofrece un precio ante un síntoma")
    if caso.entidad and caso.entidad not in (r.get("entidad_producto") or "").lower():
        desvios.append(f"entidad {r.get('entidad_producto')!r} (esperada con {caso.entidad!r})")
    adic = [a.lower() for a in (r.get("entidades_adicionales") or []) if isinstance(a, str)]
    for a in caso.adicionales:
        if not any(a in x for x in adic):
            desvios.append(f"falta {a!r} en entidades_adicionales {adic!r}")
    for c in caso.contiene:
        if c.lower() not in bajo:
            desvios.append(f"la respuesta no dice {c!r}")
    for c in caso.no_contiene + _PROHIBIDO_SIEMPRE:
        if c.lower() in bajo:
            desvios.append(f"la respuesta dice {c!r}")
    if caso.sin_direccion and re.search(r"\b\d{2,5}\b", resp):
        desvios.append("la respuesta trae un número de calle: ¿inventó la dirección?")
    return desvios


def _linea(nombre: str, r: dict) -> str:
    return (f"   {nombre:<6} intencion={r.get('intencion')!r} entidad={r.get('entidad_producto')!r} "
            f"adicionales={r.get('entidades_adicionales') or []!r} "
            f"por_sintoma={r.get('por_sintoma')!r}\n"
            f"          respuesta: {r.get('respuesta')!r}")


async def correr(intent, casos=CASOS, out=print) -> int:
    """Pasa cada frase por Haiku y por Sonnet. Devuelve la cantidad de desvíos.
    Con `resultados` (catálogo simulado) solo se juzga a Sonnet: Haiku no ve
    el catálogo."""
    total = 0
    for caso in casos:
        r1 = await intent.procesar_rapido(mensaje=caso.frase, history=[])
        r2 = await intent.procesar(mensaje=caso.frase, history=[], resultados_sku=caso.resultados)
        out(f"── {caso.frase}\n   esperado: {caso.esperado}")
        out(_linea("haiku", r1))
        out(_linea("sonnet", r2))
        juzgar = [("sonnet", r2)] if caso.resultados is not None else [("haiku", r1), ("sonnet", r2)]
        for nombre, r in juzgar:
            for d in revisar(caso, r):
                out(f"   DESVÍO {nombre}: {d}")
                total += 1
    return total


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    os.environ.setdefault("VERTICAL", "petshop")
    from app.config import get_settings
    from app.services.intent_service import get_intent_service
    from app.services.perfil import get_perfil

    s = get_settings()
    p = get_perfil()
    if p.clave != "petshop":
        print(f"El perfil activo es {p.clave!r}: correr con VERTICAL=petshop.")
        return 2
    if not (s.anthropic_api_key or s.openai_api_key):
        print("Falta ANTHROPIC_API_KEY (u OPENAI_API_KEY): este script llama al modelo real.")
        return 2
    print(f"Perfil: {p.clave} ({p.comercio}) · proveedor: {s.llm_provider}\n")
    intent = get_intent_service(s.anthropic_api_key, s.openai_api_key, s.llm_provider)
    n = asyncio.run(correr(intent))
    print(f"\n{n} desvío(s) en {len(CASOS)} frases.")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 9: Correr y ver que pasa; probar las salidas sin clave**

```bash
.venv/Scripts/python -m pytest tests/test_probar_prompt_petshop.py -v
VERTICAL=petshop ANTHROPIC_API_KEY= OPENAI_API_KEY= .venv/Scripts/python scripts/probar_prompt_petshop.py; echo "exit=$?"
VERTICAL=farmacia .venv/Scripts/python scripts/probar_prompt_petshop.py; echo "exit=$?"
```

Esperado: `4 passed`; después `Falta ANTHROPIC_API_KEY (u OPENAI_API_KEY): este script llama al modelo real.` con `exit=2`, y `El perfil activo es 'farmacia': correr con VERTICAL=petshop.` con `exit=2`. La corrida con la clave real es parte de la verificación del despliegue (Task 17, Step 12).

- [ ] **Step 10: Commit**

```bash
git add scripts/probar_prompt_petshop.py tests/test_probar_prompt_petshop.py
git commit -m "Petshop: script de prueba del prompt con el LLM real (spec 6.5)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 11: Suite completa**

```bash
.venv/Scripts/python -m pytest -q
```

Esperado: `0 failed`; el total sube en 18 respecto de antes de esta tarea (14 conversaciones + 4 del script). Total esperado con el plan aplicado en orden sobre `07a1d7a`: 1373 tests.

---

### Task 16: `GET /bo/perfil` para el portal de MO

Decisión del usuario del 7/10 (posterior al spec): el portal de MO es una remix del panel de Remedia en Lovable. Para que el mismo código de panel sirva a los dos rubros, el panel pregunta el rubro de la instancia y oculta las secciones de las capacidades apagadas (recetas, socios, cuenta corriente, obras sociales). El endpoint se autentica como el resto de `/bo` y devuelve **solo** identidad y capacidades, nunca el prompt ni los textos del perfil.

**Files:**
- Modify: `app/routers/backoffice.py` (endpoint nuevo inmediatamente después de `bo_wa_config`, que en `07a1d7a` termina en la línea 1519)
- Create: `tests/test_perfil_bo.py`

**Interfaces:**
- Consumes: `get_perfil()` y `Perfil` (Task 2); fixture `usar_perfil(clave, comercio=None)` (Task 1); `_auth` (ya existe en `backoffice.py`).
- Produces: `GET /bo/perfil` → `{"clave": str, "comercio": str, "emoji": str, "capacidades": {"venta": bool, "recetas": bool, "obras_sociales": bool, "socios": bool, "cuenta_corriente": bool, "links_como_receta": bool, "sintomas": "farmaceutico" | "derivar"}}`. Devuelve 403 si falta la `BO_KEY` correcta (header `x-bo-key` o `?key=`).

- [ ] **Step 1: Escribir los tests (fallan)**

Crear `tests/test_perfil_bo.py`:

```python
"""
GET /bo/perfil: el portal de cada instancia lee el rubro (identidad y
capacidades) para ocultar las secciones que no aplican. Autenticado con la
BO_KEY como el resto de /bo, y sin el prompt ni los textos del perfil.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers import backoffice

CLAVE = "clave-de-test"
CAMPOS = {"clave", "comercio", "emoji", "capacidades"}
CAPACIDADES = {"venta", "recetas", "obras_sociales", "socios",
               "cuenta_corriente", "links_como_receta", "sintomas"}


def _get(monkeypatch, headers=None):
    monkeypatch.setattr(get_settings(), "bo_key", CLAVE)
    app = FastAPI()
    app.include_router(backoffice.router)
    return TestClient(app).get("/bo/perfil", headers=headers if headers is not None
                               else {"x-bo-key": CLAVE})


def test_bo_perfil_petshop(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    r = _get(monkeypatch)
    assert r.status_code == 200
    body = r.json()
    assert body["clave"] == "petshop"
    assert body["comercio"] == "Mascotas del Oeste"
    assert body["emoji"] == "🐾"
    assert body["capacidades"] == {
        "venta": True, "recetas": False, "obras_sociales": False, "socios": False,
        "cuenta_corriente": False, "links_como_receta": False, "sintomas": "derivar",
    }


def test_bo_perfil_farmacia(usar_perfil, monkeypatch):
    usar_perfil("farmacia")
    r = _get(monkeypatch)
    assert r.status_code == 200
    body = r.json()
    assert body["clave"] == "farmacia"
    assert body["comercio"] == "Remedia"
    assert body["emoji"] == "💊"
    assert body["capacidades"] == {
        "venta": True, "recetas": True, "obras_sociales": True, "socios": True,
        "cuenta_corriente": True, "links_como_receta": True, "sintomas": "farmaceutico",
    }


def test_bo_perfil_mutual_no_vende(usar_perfil, monkeypatch):
    usar_perfil("mutual")
    r = _get(monkeypatch)
    assert r.status_code == 200
    body = r.json()
    assert body["clave"] == "mutual"
    assert body["capacidades"]["venta"] is False
    assert body["capacidades"]["recetas"] is True        # igual que hoy (spec §3.3)


def test_bo_perfil_con_comercio_nombre(usar_perfil, monkeypatch):
    usar_perfil("petshop", comercio="MO Prueba")
    r = _get(monkeypatch)
    assert r.status_code == 200
    assert r.json()["comercio"] == "MO Prueba"


def test_bo_perfil_no_expone_prompt_ni_textos(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    r = _get(monkeypatch)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == CAMPOS
    assert set(body["capacidades"]) == CAPACIDADES
    assert "Soy el asistente virtual" not in r.text
    assert "consulta_salud_message" not in r.text


def test_bo_perfil_exige_clave(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    assert _get(monkeypatch, headers={}).status_code == 403
    assert _get(monkeypatch, headers={"x-bo-key": "otra"}).status_code == 403
    assert _get(monkeypatch).status_code == 200
```

- [ ] **Step 2: Correr los tests y ver que fallan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_bo.py -v`

Esperado: `6 failed`. Los cinco primeros fallan con `assert 404 == 200` y `test_bo_perfil_exige_clave` con `assert 404 == 403`, porque la ruta todavía no existe.

- [ ] **Step 3: Implementar el endpoint**

En `app/routers/backoffice.py`, inmediatamente después de la función `bo_wa_config` (la que termina con `"vertical": s.vertical,` y `}`), agregar:

```python
@router.get("/perfil")
async def bo_perfil(_=Depends(_auth)):
    """
    Rubro de esta instancia para el portal: identidad y capacidades. El panel
    oculta las secciones de las capacidades apagadas (recetas, socios, cuenta
    corriente, obras sociales). No expone el prompt ni los textos del perfil.
    """
    from app.services.perfil import get_perfil
    p = get_perfil()
    return {
        "clave": p.clave,
        "comercio": p.comercio,
        "emoji": p.emoji,
        "capacidades": {
            "venta": p.venta,
            "recetas": p.recetas,
            "obras_sociales": p.obras_sociales,
            "socios": p.socios,
            "cuenta_corriente": p.cuenta_corriente,
            "links_como_receta": p.links_como_receta,
            "sintomas": p.sintomas,
        },
    }
```

- [ ] **Step 4: Correr los tests y ver que pasan**

Run: `.venv/Scripts/python -m pytest tests/test_perfil_bo.py -v`

Esperado: `6 passed`.

- [ ] **Step 5: Suite completa**

Run: `.venv/Scripts/python -m pytest -q`

Esperado: `0 failed`. Con el plan aplicado en orden sobre `07a1d7a` dan `1379 passed`: los 1373 de las tareas 1 a 15 más estos 6.

- [ ] **Step 6: Commit**

```bash
git add app/routers/backoffice.py tests/test_perfil_bo.py
git commit -F - <<'MSG'
Backoffice: GET /bo/perfil con identidad y capacidades del rubro

El portal de cada instancia (remix del panel de Remedia) lo lee para
ocultar las secciones de las capacidades apagadas. Autenticado con la
BO_KEY y sin exponer el prompt ni los textos del perfil.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

### Task 17: Regresión completa y despliegue en Mascotas del Oeste (§6.4, §7)

Sin código nuevo. Los Steps 1 a 8 corren en local; los Steps 9 a 15 son la checklist del despliegue y los hace el usuario (son cambios en Railway, en la config de producción de MO y en WhatsApp).

**Files:**
- Ninguno (solo verificación y despliegue)

**Interfaces:**
- Consumes: todo lo anterior. `GET/PUT /bo/config/hours`, `GET/PATCH /bo/config` y `GET /bo/catalogo/estado` (header `x-bo-key`); `scripts/probar_prompt_petshop.py` (Task 15).
- Produces: nada.

- [ ] **Step 1: Suite completa con el perfil farmacia (sin `VERTICAL`)**

```bash
unset VERTICAL COMERCIO_NOMBRE
.venv/Scripts/python -m pytest -q
```

Esperado: `0 failed`, con `N passed` donde N = 933 + los tests nuevos de todas las tareas (`1379 passed` al aplicar este plan completo sobre `07a1d7a`, en unos 9 minutos). Anotar N para el PR.

- [ ] **Step 2: Los tests existentes no cambiaron de expectativa (§6.3)**

```bash
git diff --name-status 07a1d7a -- tests/
```

Esperado: la única línea `M` es `tests/conftest.py` (fixture `usar_perfil`, Task 1). Ninguna tarea de este plan toca `tests/test_webhook_secuencias.py`: en lugar de cambiar sus fakes (§6.1), cada archivo nuevo trae los suyos. El resto son `A` (archivos nuevos: `test_goldens_farmacia.py`, `test_perfil*.py`, `test_petshop.py`, `test_petshop_conversaciones.py`, `test_entrega_sucursal.py`, `test_pesos.py`, `test_probar_prompt_petshop.py`). Si aparece otro `M`, revisar ese diff: un test existente cambió y el spec dice que ninguno cambia.

- [ ] **Step 3: El prompt de farmacia sigue byte a byte**

```bash
.venv/Scripts/python -c "import hashlib; from app.services.intent_service import SYSTEM_PROMPT as P; print(hashlib.sha256(P.encode('utf-8')).hexdigest(), len(P))"
```

Esperado: `1953a4e6815d855e635406c1da8f97ff83bb9be6a2fd040b69eabfae4c540749 14680`.

- [ ] **Step 4: Corrida con `VERTICAL=petshop` de los archivos nuevos (§6.4)**

```bash
VERTICAL=petshop .venv/Scripts/python -m pytest -q tests/test_goldens_farmacia.py tests/test_perfil*.py tests/test_petshop.py tests/test_petshop_conversaciones.py tests/test_entrega_sucursal.py tests/test_pesos.py tests/test_probar_prompt_petshop.py
```

Esperado: `446 passed`, `0 failed`. Un test que falla solo acá depende del entorno en vez de fijar su perfil con `usar_perfil` (o de borrar `VERTICAL` con `monkeypatch.delenv` si prueba el default): corregir ese test.

- [ ] **Step 5: El perfil no se filtra de un test a otro**

```bash
.venv/Scripts/python -m pytest -q tests/test_petshop_conversaciones.py tests/test_entrega_sucursal.py tests/test_petshop.py tests/test_webhook_secuencias.py tests/test_logic.py tests/test_degradation.py
```

Esperado: `0 failed`. Si un test de farmacia falla solo en este orden, un test de petshop dejó el perfil cacheado: buscar el que llama a `get_perfil()` sin la fixture.

- [ ] **Step 6: Reglas de uso del perfil y restos de farmacia**

```bash
grep -rnE "^[A-Za-z_]+\s*=\s*get_perfil\(\)" app
grep -rnE "self\.\w+\s*=\s*get_perfil\(\)" app
grep -rnE "vertical\s*(==|in \()" app --include=*.py
grep -n "remedia.ar" app/services/checkout_helper.py
grep -n "FARMACIA AMI" app/services/payment_service.py
grep -n '"Compra Remedia"' app/services/payway_service.py
```

Esperado: ninguna salida en el primero, el segundo y el sexto. Los otros tres muestran una sola línea cada uno, conocida: el tercero, `app/services/metrics_store.py`, en `tablero` (línea 432 de `07a1d7a`, corrida dos líneas por la Task 9: `if vertical == "mutual":` es el parámetro de `tablero`, que el spec deja igual y que ahora recibe `get_perfil().clave`, §4.8); el cuarto, el docstring de `dominio_propio` (Task 7, Step 7); el quinto, el docstring de `statement_descriptor` (Task 11, Step 19). Cualquier otra línea es un error. (`get_perfil()` nunca en variable de módulo ni en `__init__`; nadie pregunta por el nombre del rubro; los literales de marca salieron del código.)

- [ ] **Step 7: Arranque con un `VERTICAL` desconocido**

```bash
VERTICAL=veterinaria PYTHONIOENCODING=utf-8 .venv/Scripts/python -c "from fastapi.testclient import TestClient; from app.main import app; TestClient(app).__enter__()" 2>&1 | grep -E "VERTICAL desconocido"
```

Esperado: al menos una línea con `VERTICAL desconocido: 'veterinaria'. Valores válidos: farmacia, mutual, petshop` (el traceback del arranque fallido) y nada de "Catálogo cargado" antes: el perfil se valida antes de tocar Redis, el catálogo o Postgres.

- [ ] **Step 8: Arranque local con el perfil petshop (sin Redis ni Postgres, tarda unos 20 s)**

```bash
VERTICAL=petshop PYTHONIOENCODING=utf-8 .venv/Scripts/python -c "from fastapi.testclient import TestClient; from app.main import app; c = TestClient(app); c.__enter__(); c.__exit__(None, None, None)" 2>&1 | grep -E "Perfil de rubro|SKU_CSV_PATH|SKUs"
```

Esperado, en este orden: `Perfil de rubro: petshop (Mascotas del Oeste)`, el `ERROR` de `SKU_CSV_PATH=data/catalogo_base.csv ... es el catálogo de la farmacia: el perfil petshop no lo carga` y `Catálogo cargado: 0 SKUs`. ("Perfil sin recetas" y el resto de los logs de Postgres solo salen con `DATABASE_URL`: se ven en Railway, Step 11.)

- [ ] **Step 9 (usuario): Desplegar solo MO (§7.1, §7.2)**

1. Subir la rama: `git push origin feature/vertical-petshop`.
2. En Railway, servicio de MO → Settings → Source: rama `feature/vertical-petshop`. Remedia y CERCA siguen en `develop` hasta el Step 15.
3. Variables del servicio de MO:
   - `VERTICAL=petshop`
   - `COMERCIO_NOMBRE`: sin setear (sale "Mascotas del Oeste"). Solo se carga para otro nombre visible.
   - `SKU_CSV_PATH=` vacío. Si Railway no acepta un valor vacío, dejarla sin setear: el guardia ignora el CSV de la farmacia y loguea un `ERROR` en cada arranque (esperado).
   - `REDIS_URL` y `DATABASE_URL` **propios de MO**. Nunca los de la farmacia: `bot:config`, `bot:hours`, `blob:catalogo` y `blob:socios` no tienen namespace y MO heredaría textos, horario, padrón y descuentos.
   - `PUBLIC_BASE_URL` con el host de MO, `WHATSAPP_VERIFY_TOKEN` propio (el default es `farma_verify_token`), `BO_KEY` propio, `MERCURIO_API_KEY` (y `MERCURIO_BRANCH_ID=mascotas-oeste`, el default) y las claves del proveedor de cobro de MO (`MP_ACCESS_TOKEN` / `MP_NOTIFICATION_URL`, o `PAYWAY_*`).
4. Redeploy y esperar el arranque.

- [ ] **Step 10 (usuario): Configuración de MO por la API, antes de abrir el número (§7.3)**

Con `BASE=https://<host de MO>` y `KEY=<BO_KEY de MO>`:

1. Horario real de la sucursal piloto (reemplazar los horarios de ejemplo por los de MO):

```bash
curl -s -X PUT "$BASE/bo/config/hours" -H "x-bo-key: $KEY" -H "Content-Type: application/json" -d '{"enabled": true, "closed_message": "Estamos fuera del horario de atención. Te respondemos en cuanto abramos 🐾", "schedule": {"mon": {"active": true, "open": "09:00", "close": "20:00"}, "tue": {"active": true, "open": "09:00", "close": "20:00"}, "wed": {"active": true, "open": "09:00", "close": "20:00"}, "thu": {"active": true, "open": "09:00", "close": "20:00"}, "fri": {"active": true, "open": "09:00", "close": "20:00"}, "sat": {"active": true, "open": "09:00", "close": "13:00"}, "sun": {"active": false, "open": "09:00", "close": "13:00"}}}'
curl -s "$BASE/bo/config/hours" -H "x-bo-key: $KEY"
```

   Verificar que vuelve con `"enabled": true` y el horario de MO. Sin esto, el bot promete `DEFAULT_HOURS` (L a V 9 a 18, sáb 9 a 13) y con `enabled=false` `is_open_now` siempre da True.

2. Sucursal, textos y cobro (valores reales de MO en lugar de los `<...>`):

```bash
curl -s -X PATCH "$BASE/bo/config" -H "x-bo-key: $KEY" -H "Content-Type: application/json" -d '{"retiro_sucursal": "<nombre de la sucursal piloto>", "retiro_info_message": "Lo retirás en *{sucursal}*, <dirección real>, <horario real> 🐾", "pedido_listo_retiro_message": "🎉 *¡Tu pedido está listo para retirar en <nombre de la sucursal>!*\n\n*{producto}* — ${total}\n🔑 *Código de retiro: {codigo}*{horario}\n\nPresentá este código y te lo entregamos. ¡Te esperamos! 🐾", "pickup_minutes": "<minutos que defina MO; 0 apaga el tiempo estimado>", "payment_provider": "<mercadopago o payway>", "pago_mp_manual": "false", "efectivo_enabled": "false"}'
curl -s "$BASE/bo/config" -H "x-bo-key: $KEY"
```

   `pago_mp_manual` va en `"false"` solo si MO cobra con Mercado Pago. Verificar en el `GET` que cada clave quedó con su valor (las que `ConfigUpdate` no declara se descartan en silencio).

3. Textos guardados con 💊 (lo guardado gana sobre el perfil):

```bash
curl -s "$BASE/bo/config" -H "x-bo-key: $KEY" | .venv/Scripts/python -c "import json, sys; d = json.load(sys.stdin); print([k for k, v in d.items() if '\U0001f48a' in str(v)])"
```

   Esperado: `[]`. Si lista claves (restos de una corrida anterior), borrarlas en el Postgres y el Redis de MO, en ese orden:

```bash
psql "$DATABASE_URL_MO" -c "DELETE FROM config WHERE clave IN ('<clave1>', '<clave2>');"
redis-cli -u "$REDIS_URL_MO" HDEL bot:config <clave1> <clave2>
```

   y repetir el `GET` hasta que dé `[]`.

4. No cargar padrón, empleados ni KB de la mutual. No usar `catalogo_fuente="csv"`: en petshop deja el catálogo vacío.
5. Opcional, para que el panel no muestre "requiere receta" en filas viejas:

```bash
psql "$DATABASE_URL_MO" -c "UPDATE catalog_items SET requiere_receta='no' WHERE branch_id='mascotas-oeste';"
```

6. Las claves nuevas (`retiro_sucursal`, `retiro_info_message`, `pago_mp_manual`) no están en el panel de Lovable: quedan cargadas por API hasta que se expongan allá.

- [ ] **Step 11 (usuario): Verificación en Railway (§7.4)**

1. Log del arranque: `Perfil de rubro: petshop (Mascotas del Oeste)`, `Perfil sin recetas: no se carga la referencia ni se recalcula el catálogo` y `Perfil sin socios: no se carga el padrón`.
2. Catálogo:

```bash
curl -s "$BASE/bo/catalogo/estado" -H "x-bo-key: $KEY"
```

   Esperado: `fuente` = `"erp"` y `total_productos > 0`. El primer sync de Mercurio tarda unas 47 páginas: hasta que termina, el bot dice que no tiene nada.
3. `GET /bo/config` sin 💊 (comando del Step 10.3).

- [ ] **Step 12 (usuario): Prueba del prompt con el LLM real (§6.5)**

En local, con la clave de Anthropic:

```bash
VERTICAL=petshop ANTHROPIC_API_KEY=<clave> .venv/Scripts/python scripts/probar_prompt_petshop.py
```

Esperado: `0 desvío(s) en 18 frases.` y `exit 0`. Cada `DESVÍO` se revisa a mano; los de `por_sintoma` o los que nombran productos ante un síntoma se corrigen en el prompt de petshop antes de abrir el número. Revisar también a ojo la línea `esperado` del saludo (sin nombre propio).

- [ ] **Step 13 (usuario): Conversaciones reales por WhatsApp en el número de prueba de MO (§6.5)**

1. Compra completa con retiro: consulta, oferta con precio, "sí", "retiro", link, pago, confirmación del pago nombrando la sucursal y aviso de pedido listo.
2. Compra completa con envío (dirección nueva).
3. Un síntoma ("mi perro vomita, ¿qué le doy?"): deriva con `consulta_salud`, sin productos.
4. Una foto de un producto (bolsa de alimento): llega a la búsqueda.
5. Una foto de una indicación veterinaria: "Recibí la indicación del veterinario 🐾 ..." y deriva.
6. Un link de Instagram: no deriva, contesta el modelo.
7. En la elección de entrega, "¿en qué sucursal puede ser?": contesta con la dirección de MO y vuelve a preguntar retiro o envío, sin link.

En ningún mensaje puede aparecer farmacia, receta, obra social, socio, mutual, cuenta corriente ni 💊. Recién después, abrir el número.

- [ ] **Step 14 (usuario): Vuelta atrás, si hace falta (§7.5)**

`VERTICAL=farmacia` en el servicio de MO y reiniciar: vuelve exactamente al comportamiento que MO tiene hoy (prompt y textos de farmacia, recetas, socios y `catalogo_csv_base=True`: si falla el sync de Mercurio, vuelve a ofrecer el CSV de la farmacia). Los textos guardados en la config de MO se mantienen. Para volver también el código, redeploy del commit anterior en Railway.

- [ ] **Step 15 (usuario): Merge a `develop` para farmacia y mutual, después del go-live de MO (§7.6)**

Antes de mergear:
1. En Railway, Remedia y CERCA tienen `VERTICAL` en minúsculas o sin setear (hoy "Mutual" con mayúscula se coacciona a farmacia; con la normalización pasaría a mutual).
2. Su `PUBLIC_BASE_URL` es un host bajo `remedia.ar` (§4.3): si no, los links a `remedia.ar` pasarían a derivarse.
3. Suite completa en verde (Step 1) y aviso al equipo de la farmacia de los cambios que también la afectan: la pregunta en `esperando_entrega` y `esperando_confirmacion` (§5), "¿cuánto sale el envío?" eligiendo la entrega ahora lo contesta el modelo, y las correcciones de pesos y presentaciones.
