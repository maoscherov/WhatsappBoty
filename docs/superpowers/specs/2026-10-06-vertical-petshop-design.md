# Vertical petshop — Mascotas del Oeste sobre un perfil de rubro

Diseño aprobado por el usuario el 6/10/2026 (secciones 1-3 más el agregado de
la pregunta en `esperando_entrega`). Rama `feature/vertical-petshop`. Todas las
líneas citadas son del commit `07a1d7a`. Inventario relevado por grupo y
revisado por un crítico de cobertura. Donde hubo contradicciones, se resolvieron
a favor del diseño aprobado (ver §3.7 y §9.1).

---

## 1. Contexto

El bot de ventas por WhatsApp (FastAPI + Redis + Postgres + Claude) hoy atiende
a una farmacia (Remedia) y a una mutual (CERCA / Mutual AMI). El rubro se elige
con la env `VERTICAL`.

Se suma **Mascotas del Oeste (MO)**, una cadena de petshops. Corre como
instancia separada del mismo código: servicio propio en Railway, con su Postgres
y su Redis (no es un tenant). El catálogo sale del ERP Mercurio (alimento,
accesorios, piedras sanitarias, snacks, higiene y salud animal sin receta; ver
`2026-09-14-mercurio-api-v1.md`). Se cobra con MP o Payway en la cuenta de MO.

Hoy `VERTICAL=petshop` no existe. `intent_service.py:181` lo coacciona a
farmacia sin avisar, y MO vende como Remedia: se presenta como Remedia, deriva
por receta, contesta por obras sociales, busca socios en el padrón, ofrece
cuenta corriente y firma con 💊.

Por qué un vertical y no un `if` más: los ganchos de farmacia están
desparramados en el webhook, el checkout, la visión, los pagos y el arranque.
Preguntar en cada uno por un nombre de rubro no escala. Preguntar por una
**capacidad** sí.

**Decisiones del usuario**
- Identidad: "Soy el asistente virtual de Mascotas del Oeste", sin nombre propio.
- Salud animal: vende los productos de salud que el cliente pide por nombre.
  Ante un síntoma ("mi perro vomita, ¿qué le doy?") no recomienda tratamiento
  ni dosis y **deriva a una persona** (motivo `consulta_salud`).
- Entrega MVP: envío a domicilio y retiro en **una** sucursal piloto. La
  dirección la carga MO desde el panel. El bot no inventa direcciones.
- Enfoque B: **perfil de rubro con capacidades**. Cada gancho pregunta por una
  capacidad (`perfil.cuenta_corriente`), nunca por el nombre del vertical.

## 2. Objetivo y criterio de éxito

Que MO venda por WhatsApp con su identidad y sus reglas, desde el mismo código
y sin tocar a la farmacia.

Criterio de éxito:
1. Un cliente de MO compra de punta a punta: consulta, oferta con precio,
   confirmación, retiro o envío, link de pago, confirmación del pago y aviso de
   pedido listo. En ningún paso el bot menciona farmacia, recetas, obras
   sociales, socios, mutual ni cuenta corriente. Tampoco dispara lógica de
   farmacia: derivación por receta, OCR, bonos, padrón, descuento de socio o
   empleado, oferta del farmacéutico ni el CSV de la farmacia.
2. Ante un síntoma, el bot no ofrece productos y deriva con `consulta_salud`.
3. La farmacia no cambia: prompt idéntico byte a byte, mismos textos y la suite
   completa (933 tests) en verde. Únicas excepciones: las correcciones de §5
   (la pregunta en `esperando_entrega` y los pesos y presentaciones), marcadas
   como **cambio que también afecta a la farmacia**.
4. La mutual queda registrada como perfil sin cambios de comportamiento.
5. Con un `VERTICAL` desconocido, el arranque se corta con un error claro.

## 3. Arquitectura

### 3.1 Módulos nuevos

- `app/services/prompts.py`: solo textos, sin imports de `app`. Contiene el
  prompt de farmacia armado por bloques (re-exportado por `intent_service`),
  la plantilla de petshop y los prompts de visión de los dos rubros.
- `app/services/perfil.py`: las dataclasses, el registro de perfiles,
  `get_perfil()` y `perfil_por_clave()`.

`perfil.py` importa solo stdlib, `app.config`, `app.services.prompts` y
`app.services.mutual_helper` (que solo usa stdlib). `config_service.DEFAULTS`
se importa **adentro** de la función que arma el registro, porque
`config_service` importa `perfil`. `perfil.py` nunca importa `intent_service`,
`sku_service`, `image_service` ni `checkout_helper`, porque todos ellos importan
`perfil` y se armaría un ciclo. Por eso la lista de marcas de audio pasa a
`perfil.py`, y los prompts a `prompts.py`.

Regla de uso: `get_perfil()` se llama en el momento de usarlo, adentro de cada
función. Nunca se guarda en una variable de módulo, en un `__init__` ni en un
singleton (`IntentService`, `PaymentService`, `PaywayService`, `ConfigService`).

### 3.2 Dataclasses

```python
@dataclass(frozen=True)
class VocabularioAudio:
    prefijo: str                    # "Consulta a una farmacia"
    marcas_base: tuple[str, ...]    # marcas fijas, antes de las del catálogo

@dataclass(frozen=True)
class VisionPerfil:
    categorias: tuple[str, ...]     # tipos válidos; cualquier otro pasa a "otro"
    prompt: str                     # prompt del clasificador de imágenes

@dataclass(frozen=True)
class Perfil:
    clave: str                      # "farmacia" | "mutual" | "petshop"
    comercio: str                   # nombre visible; COMERCIO_NOMBRE lo pisa
    system_prompt: str
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
    textos: Mapping[str, str]       # MappingProxyType: un test no lo puede mutar
    # Agregados al diseño (justificación en §3.7)
    venta: bool                     # False = solo informa (mutual)
    catalogo_csv_base: bool         # puede cargar data/catalogo_base.csv
    descriptor_tarjeta: str = ""    # "" = derivado de comercio
    razon_social: str = ""          # "" = comercio
    wordmark_html: str = ""         # "" = html.escape(comercio)
```

### 3.3 Valores de los tres perfiles

| Campo | farmacia | mutual | petshop |
|---|---|---|---|
| `clave` | `farmacia` | `mutual` | `petshop` |
| `comercio` | `Remedia` | `Remedia` (es lo que hoy muestran /pay y Payway) | `Mascotas del Oeste` |
| `system_prompt` | `prompts.SYSTEM_PROMPT` (sha256 `1953a4e6…0749`, 14.680 caracteres) | `mutual_helper.SYSTEM_PROMPT_MUTUAL`, el mismo objeto (sha256 `4377db47…c469`) | plantilla petshop resuelta (§4.1) |
| `emoji` | 💊 | 💊 | 🐾 |
| `rotulo_kb` | `INFORMACIÓN DE LA FARMACIA` | `INFORMACIÓN DE LA FARMACIA` (así se comporta hoy; ver §9) | `INFORMACIÓN DEL COMERCIO` |
| `vocabulario_audio` | `Consulta a una farmacia` + las 38 marcas de hoy, en el mismo orden | igual que farmacia | `Consulta a un petshop` + `()` |
| `recetas` / `obras_sociales` / `socios` / `cuenta_corriente` / `links_como_receta` | True | True | False |
| `sintomas` | `farmaceutico` | `farmaceutico` | `derivar` |
| `vision` | `VISION_FARMACIA` | `VISION_FARMACIA` (el mismo objeto) | `VISION_PETSHOP` |
| `textos` | las 13 claves de §3.4, tomadas de `DEFAULTS` | igual que farmacia | las 13 claves de petshop + `consulta_salud_message` + `indicacion_veterinaria_message` |
| `venta` | True | False | True |
| `catalogo_csv_base` | True | True | False |
| `descriptor_tarjeta` | `FARMACIA AMI` | `FARMACIA AMI` | `""` (da `MASCOTAS DEL OESTE`) |
| `razon_social` | `Farmacia Mutual Independencia` | igual | `""` |
| `wordmark_html` | `Remed<b>IA</b>` | igual | `""` |

La mutual lleva todas las capacidades de farmacia en True. Los bloques de
imagen, horario, link y padrón corren hoy **antes** del desvío a
`_flujo_mutual` (`webhook.py:1171`), y `receta_referencia.inicializar` corre
en todos los verticales. Si alguna capacidad quedara en False, la mutual
cambiaría.

### 3.4 Textos del perfil

`CLAVES_TEXTO_RUBRO` (constante en `perfil.py`) son las 13 claves de `DEFAULTS`
que tienen vocabulario o emoji de farmacia:

`pedido_listo_retiro_message`, `pedido_listo_envio_message`,
`efectivo_retiro_message`, `efectivo_envio_message`,
`sintoma_farmaceutico_message`, `receta_recibida_message`,
`socio_discount_message`, `socio_discount_info_message`,
`socio_discount_off_message`, `bono_recibido_message`,
`bono_no_reconocido_message`, `bono_consulta_si_message`,
`comprobante_recibido_message`.

Reglas:
- Farmacia y mutual: `textos = {k: DEFAULTS[k] for k in CLAVES_TEXTO_RUBRO}`.
  Se arman **desde** `DEFAULTS`, así que el merge no cambia nada.
- Todo perfil define las 13. Así un fallback `perfil.textos[x]` nunca da
  `KeyError` (era una contradicción entre grupos).
- Las claves exclusivas de una capacidad (`consulta_salud_message`,
  `indicacion_veterinaria_message`) solo existen en los perfiles que tienen esa
  capacidad, y solo se leen detrás de su gate. Un test lo garantiza:
  `sintomas == "derivar"` ⇒ existe `consulta_salud_message`;
  `"indicacion_veterinaria" in vision.categorias` ⇒ existe
  `indicacion_veterinaria_message`.
- `DEFAULTS` no se modifica, salvo las tres claves nuevas de §4.4 y §5
  (`pago_mp_manual`, `retiro_sucursal`, `retiro_info_message`), cuyos
  defaults dejan todo como hoy.

**Merge.** En `config_service` se agrega `valores_base() -> dict` que devuelve
`{**DEFAULTS, **get_perfil().textos}`. Los tres `return` de `get_all`
(413, 428 y 430) pasan a `{**valores_base(), **guardado}`, y `get` (459-461)
usa `valores_base().get(key, "")` como default. Lo guardado sigue ganando.
`valores_base()` es pública, para que los tests de petshop armen su config
falsa con los textos del perfil.

**Fallbacks del código que pasan a `perfil.textos[x]`** (un solo dueño:
arranque-config):

| Archivo:línea | Clave |
|---|---|
| `orders_api.py:162-166` | `pedido_listo_envio_message` |
| `orders_api.py:168-173` | `pedido_listo_retiro_message` |
| `checkout_helper.py:1695-1699` | `efectivo_envio_message` |
| `checkout_helper.py:1701-1705` | `efectivo_retiro_message` |
| `checkout_helper.py:1544-1545` (`agregar_oferta_farmaceutico`) | `sintoma_farmaceutico_message` |
| `webhook.py:986-990` | `receta_recibida_message` |
| `webhook.py:995-998` | `comprobante_recibido_message` |
| `webhook.py:1482-1485` | `socio_discount_info_message` |
| `webhook.py:1487-1490` | `socio_discount_off_message` |
| `checkout_helper.py:1524-1526` | `bono_recibido_message` |
| `checkout_helper.py:1528-1530` | `bono_consulta_si_message` |
| `checkout_helper.py:1532-1535` | `bono_no_reconocido_message` |

Cada literal de hoy es igual a `DEFAULTS[k]`, así que la farmacia queda
idéntica. No cambian:
- `socio_discount_message` (`checkout_helper.py:1121`) sigue con `or ''`: si
  pasara a `perfil.textos`, un texto vaciado en el panel volvería a mostrar la
  línea en la farmacia.
- Los fallbacks con texto neutro o que solo se alcanzan con una capacidad
  prendida: `consulta_saldo_message` (`webhook.py:1229-1231`),
  `cc_no_habilitada_message` (1369-1371), `imagen_no_reconocida_message`
  (1026-1029 y 1067-1069), `sin_stock_derivar`, `encargo`, `pago_*`,
  `obras_sociales_*`, `bono_consulta_no_message`, `closed_message` y los de
  la mutual. Así `get_all()` de la farmacia sigue siendo igual a `DEFAULTS`.
  Un grupo proponía meter `consulta_saldo_message` y
  `cc_no_habilitada_message` en los textos de farmacia; se descartó porque eso
  cambiaba el `get_all()` de la farmacia.

**Textos de petshop** (el 🐾 sale de `perfil.emoji` al armar el registro):

```
pedido_listo_retiro_message:
🎉 *¡Tu pedido está listo para retirar!*\n\n*{producto}* — ${total}\n🔑 *Código de retiro: {codigo}*{horario}\n\nPresentá este código y te lo entregamos. ¡Te esperamos! 🐾

pedido_listo_envio_message:
🎉 *¡Tu pedido está listo!*\n\n*{producto}* — ${total}\n🚚 Sale para *{direccion}*. Te avisamos cuando esté en camino. 🐾

efectivo_retiro_message:
✅ *¡Listo! Tomamos tu pedido* 🙌\n\n*{producto}* — ${total}\n💵 Lo pagás en efectivo al retirar.{plazo}\n🔑 *Tu código de retiro es: {codigo}*\n\n¡Muchas gracias! 🐾

efectivo_envio_message:
✅ *¡Listo! Tomamos tu pedido* 🙌\n\n*{producto}* — ${total}{envio}\n🚚 Te lo enviamos a *{direccion}* y lo pagás en efectivo al recibirlo.\n📋 Código de pedido: *{codigo}*\n\n¡Muchas gracias! 🐾

sintoma_farmaceutico_message:   (vacío: apaga el agregado)

receta_recibida_message / bono_recibido_message / bono_no_reconocido_message:
¡Hola {nombre}! Recibí tu imagen 🙌 Te paso con alguien del equipo que la mira y te ayuda.

socio_discount_message:
🎉 Te aplicamos un {pct}% de descuento (precio de lista: ${antes}).

socio_discount_info_message / socio_discount_off_message:
Por ahora te puedo ofrecer el precio de lista 🙂

bono_consulta_si_message:
Eso lo confirma el equipo: te paso con alguien para que lo vea con vos 🙂

comprobante_recibido_message:   (sin {nombre}: MO no tiene padrón y hoy saldría "¡Listo !")
¡Listo! Recibimos tu comprobante 🙌 Lo verificamos y te confirmamos en un rato.

consulta_salud_message:         (solo petshop)
Para temas de salud prefiero que te atienda una persona del equipo, así no te recomiendo nada a ciegas 🐾 Ya te paso. Si lo notás muy decaído o empeora, no esperes y consultá con un veterinario.

indicacion_veterinaria_message: (solo petshop)
¡Hola {nombre}! Recibí la indicación del veterinario 🐾 Te paso con alguien del equipo que la revisa y te ayuda con lo que necesita tu mascota.
```

Con el perfil petshop, varias de estas claves nunca se leen, porque sus ganchos
quedan apagados por capacidad. Igual tienen un texto neutro: si un gancho
quedara sin gate, el cliente no lee "receta", "bono" ni "socio".

### 3.5 `get_perfil()` y falla al arrancar

```python
@lru_cache
def get_perfil() -> Perfil:
    s = get_settings()
    clave = (s.vertical or "").strip().lower() or "farmacia"
    registro = _registro()                      # lru_cache; importa DEFAULTS adentro
    if clave not in registro:
        raise ValueError(f"VERTICAL desconocido: {s.vertical!r}. "
                         "Valores válidos: farmacia, mutual, petshop")
    p = registro[clave]
    nombre = (s.comercio_nombre or "").strip()
    if nombre:
        p = replace(p, comercio=nombre, descriptor_tarjeta="",
                    razon_social="", wordmark_html="")
        if p.clave == "petshop":
            p = replace(p, system_prompt=resolver_plantilla(
                prompts.SYSTEM_PROMPT_PETSHOP_PLANTILLA, nombre, p.emoji))
    return p

def perfil_por_clave(clave: str) -> Perfil:    # sin cache ni env; para tests
    return _registro()[clave]
```

- `app/config.py:25-27`: se agrega `comercio_nombre: str = ""` (env
  `COMERCIO_NOMBRE`, opcional) y el comentario de `vertical` pasa a
  `"farmacia" | "mutual" | "petshop"`; un valor desconocido corta el arranque.
- `COMERCIO_NOMBRE` pisa `perfil.comercio` y vacía los tres campos de marca, así
  todo sale del nombre. En petshop además se vuelve a resolver la plantilla del
  prompt. En farmacia y mutual el prompt es constante y no se toca, para
  conservar los bytes.
- `resolver_plantilla` usa `.replace("{comercio}", …).replace("{emoji}", …)`,
  nunca `str.format`, porque el JSON del prompt tiene llaves.
- `app/main.py:24-27` (lifespan, justo después de `logging.basicConfig`):
  `perfil = get_perfil()` sin try/except, **antes** de tocar Redis, el
  catálogo o Postgres, y después
  `logger.info(f"Perfil de rubro: {perfil.clave} ({perfil.comercio})")`.
  El `ValueError` corta el startup de uvicorn ("Application startup failed").
- Reemplaza la coerción silenciosa de `intent_service.py:181`.

### 3.6 Cómo lo consumen

| Archivo:línea | Hoy | Cambio |
|---|---|---|
| `intent_service.py:36-171` | `SYSTEM_PROMPT` literal | `from app.services.prompts import SYSTEM_PROMPT` (re-export). `_SYSTEM_CACHED` (174) queda. `tests/test_logic.py:1372` sigue importándolo de acá. |
| `intent_service.py:178-185` | parámetro `vertical` y coerción en 181 | se quitan los dos; el log pasa a `IntentService: perfil '{get_perfil().clave}', proveedor primario ...` |
| `intent_service.py:187-192` (`_system_prompt`) | elige por `self._vertical` | `return get_perfil().system_prompt`, en cada llamada. El singleton nunca queda con un perfil viejo. |
| `intent_service.py:385-393` (`get_intent_service`) | el primer llamador fija el vertical | se quita el parámetro `vertical` |
| `webhook.py:257` (`_deps`) | pasa `s.vertical` | `get_intent_service(s.anthropic_api_key, s.openai_api_key, s.llm_provider)` |
| `simulate.py:90` | arma el bot sin vertical (queda farmacia y puede fijar el singleton) | sin cambio de código: queda corregido porque el prompt sale de `get_perfil()` |
| `config_service.py:407-430, 459-461` | `{**DEFAULTS, **guardado}` | `valores_base()` (§3.4) |
| `webhook.py:1170-1180` | `if _s.vertical == "mutual"` | `if not perfil.venta` (§4.8) |
| `backoffice.py:602` (`bo_tablero`) | `settings.vertical` | `get_perfil().clave` |
| `backoffice.py` (nuevo, decisión del usuario del 7/10) | No hay forma de que el panel sepa el rubro | `GET /bo/perfil` con `{clave, comercio, emoji, capacidades}`: `venta`, `recetas`, `obras_sociales`, `socios`, `cuenta_corriente`, `links_como_receta`, `sintomas` y `catalogo_csv_base` (ruling de la revisión final, hallazgo 17: con `false` el portal esconde la importación de catálogo CSV/PDF y el botón de fuente "csv", que el servidor igual rechaza; ver §4.8). Sin prompt y sin textos. Lo usa el portal de MO (remix del panel de Remedia) para ocultar secciones. |

### 3.7 Campos agregados al diseño, y por qué

El diseño fija la lista de campos. Para cumplir sus propias reglas ("cada
gancho pregunta por una capacidad" y "farmacia reproduce EXACTAMENTE") hicieron
falta estos agregados. Todos se marcan para validar (§9.1):

- `venta`: `webhook.py:1171` pregunta hoy por el nombre `mutual`. Ningún campo
  del diseño expresa "vende o solo informa".
- `catalogo_csv_base`: hace cumplir "el perfil petshop NUNCA carga el CSV de la
  farmacia" sin preguntar por el nombre del rubro.
- `descriptor_tarjeta`, `razon_social` y `wordmark_html`: el diseño dice que el
  `statement_descriptor`, la descripción de Payway y la página `/pay` usan
  `perfil.comercio`. Aplicado al pie de la letra, eso cambia la farmacia: el
  descriptor pasaría de `FARMACIA AMI` a `REMEDIA` y el pie de `/pay` dejaría de
  decir "Farmacia Mutual Independencia". Vacíos significan "derivado de
  `comercio`". Petshop los deja vacíos, así que cumple el diseño literalmente;
  farmacia y mutual cargan los valores de hoy.
- `VocabularioAudio` y `VisionPerfil` no son campos nuevos: son los tipos de
  `vocabulario_audio` y `vision`, que ya estaban en el diseño.
- Se descartó el campo `sitio` (subtítulo del simulador en `/`), porque
  `index.html` no está en el diseño (§8).

## 4. Comportamiento petshop por capacidad

Convención de las tablas: **Hoy** describe el comportamiento en todos los
verticales; **Cambio** dice qué se toca. Salvo indicación, farmacia y mutual
quedan iguales.

### 4.1 Prompt, identidad, contexto y audio

| Archivo:línea | Hoy | Cambio | Capacidad |
|---|---|---|---|
| `intent_service.py:36-171` → `prompts.py` | Un literal único de farmacia (identidad Remedia, recetas, obras sociales, cuenta corriente, socios, farmacéutico, "link real de Mercado Pago", "actualizado semanalmente") | Se mueve a `prompts.py` y se arma por bloques (abajo). El prompt de farmacia queda idéntico byte a byte. | `system_prompt` |
| `prompts.py` (nuevo) | MO se presenta como "el asistente virtual de Remedia" | `SYSTEM_PROMPT_PETSHOP_PLANTILLA` con `{comercio}` y `{emoji}`. No incluye cuenta corriente, obras sociales, receta ni personalización de socios. | `system_prompt`, `comercio`, `emoji` |
| `intent_service.py:332-344` (`_con_contexto`) | Siempre agrega `[DATOS DEL SOCIO]` si hay contexto de cliente; la KB va rotulada `[INFORMACIÓN DE LA FARMACIA]` en todo vertical | Sigue siendo staticmethod (`test_degradation.py:42` la llama sobre la clase). Adentro: `p = get_perfil()`; el bloque de socio pasa a `if contexto_cliente and p.socios`; el rótulo pasa a `f"\n\n[{p.rotulo_kb}]\n{contexto_kb}\n"`. La instrucción que sigue no cambia. | `socios`, `rotulo_kb` |
| `sku_service.py:714-720, 742-755` (`vocabulario_audio`) | Cada audio manda a Whisper "Consulta a una farmacia. Productos y marcas: " + 38 marcas de farmacia + marcas del catálogo | Las 38 marcas pasan a `perfil.py`. `MARCAS_AUDIO_BASE = list(...)` queda por compatibilidad. Firma: `vocabulario_audio(sku_svc, max_chars=650, perfil=None)`; `p = perfil or get_perfil()`; `fuentes = list(p.vocabulario_audio.marcas_base) + marcas_frecuentes(sku_svc)`; `out = f"{p.vocabulario_audio.prefijo}. Productos y marcas: "`. El dedup y el tope no cambian. El llamado de `webhook.py:846-850` no cambia. | `vocabulario_audio` |

**Composición del prompt de farmacia** (líneas de `intent_service.py`; cada
línea lleva su `\n` y la 171 va sin `\n` final). Verificada con un prototipo
que reproduce el sha256 exacto:

```
Bloques compartidos (verbatim de farmacia, sin rubro):
  SEGUIMIENTO 46-49 · DERIVACION 74-76 · RESERVAS 100-102
  CONFIRMACIONES 124-126 · RESPUESTA_DIRECTA 127-130

Bloque mecánico con huecos por perfil:
  matriz_intenciones(fila_saludo, fila_consulta_abierta)
    = 110-113 + fila_saludo + 115-118 + fila_consulta_abierta + 120-123
  formato_respuesta(linea_entidad, parrafo_agregar, linea_por_sintoma,
                    linea_rechazo, lineas_cambio)
    = 141-144 + linea_entidad + 146-155 + parrafo_agregar + 157-159
      + linea_por_sintoma + 161-163 + linea_rechazo + 165-167
      + lineas_cambio + 170-171

SYSTEM_PROMPT (farmacia)
  = 36-45 + SEGUIMIENTO + 50-73 + DERIVACION + 77-99 + RESERVAS + 103-109
    + matriz(114, 119) + CONFIRMACIONES + RESPUESTA_DIRECTA + 131-140
    + formato(145, 156, 160, 164, 168-169)

SYSTEM_PROMPT_PETSHOP_PLANTILLA
  = PET_IDENTIDAD + SEGUIMIENTO + PET_VENTA + DERIVACION + PET_REGLAS
    + RESERVAS + matriz(PET_SALUDO, PET_ABIERTA) + CONFIRMACIONES
    + RESPUESTA_DIRECTA + PET_VARIOS
    + formato(PET_ENTIDAD, PET_AGREGAR, PET_SINTOMA, PET_RECHAZO, PET_CAMBIO)
```

El enum de `intencion`, las claves del JSON, "REGLA ESTRICTA: solo podés
ofrecer productos..." y "NUNCA incluyas URLs, links..." son idénticos en los dos
prompts (test de contrato).

**Bloques propios de petshop.** Al prototipo verificado se le sumaron las
reglas del crítico (descuentos; "te paso con alguien del equipo" para que
`cumplir_derivacion_prometida` derive de verdad) y la frontera de salud del
grupo síntomas. Las líneas marcadas "= farmacia NN" se copian verbatim.

```
PET_IDENTIDAD
Sos el asistente virtual de {comercio}, una cadena de petshops.

IDENTIDAD Y TONO:
- Sos cálido, cercano y amable. Como el equipo de un petshop de confianza que conoce y quiere a las mascotas de sus clientes.
- Hablás en rioplatense correcto y cuidado: cordial y simpático, sin exagerar la informalidad ni sonar vendedor insistente. Evitá "bárbaro/genial/buenísimo" en exceso.
- Si te preguntan quién sos o si sos un bot: "Soy el asistente virtual de {comercio}". No tenés nombre propio: no te inventes uno ni digas que sos una persona.
- No sos un bot genérico. Sos parte del equipo de {comercio}.
- Saludás al inicio de la conversación; después NO repitas el saludo en cada mensaje.
- No conocés el nombre del cliente: saludá de forma genérica, sin inventar nombres. Si te cuenta cómo se llama su mascota, podés usarlo con naturalidad.
- El canal es relacional antes de transaccional: primero conectás, después vendés.

PET_VENTA
ALTERNATIVAS SIEMPRE CON PRECIO:
- Si mencionás un producto de la lista como alternativa, SIEMPRE con su precio ("tengo el Pedigree Adulto 3 kg a $9.800"). Nombrar un producto sin precio no sirve: el cliente no puede decidir y el sistema no lo toma como ofrecido.
- NUNCA cierres con "¿te gustaría más información?", "¿te interesa alguna de estas opciones?" ni similares. Cerrá con una pregunta concreta de compra ("¿te sirve?", "¿cuál preferís?") o no preguntes nada.

PRECIOS:
- Si el cliente pregunta un precio y el producto está en el contexto, SIEMPRE respondé con el precio concreto (ej.: "Las piedras Sanicat de 4 kg están $6.200"). Nunca esquives la pregunta de precio.

BÚSQUEDA EN CATÁLOGO SKU:
- El catálogo tiene productos con stock disponible.
(= farmacia 59-64)
- Si la lista dice "Sin resultados en el catálogo" o no hay opciones que coincidan con lo que pidió el cliente, NO ofrezcas productos de otro tipo. Decí con honestidad que no lo tenés y ofrecé encargarlo o pasarlo con una persona del equipo. Nunca sugieras un producto de otro rubro ni para otra especie (ej.: si pide alimento para gato y no está, no ofrezcas alimento para perro ni un juguete).

LÓGICA DE PAGO:
(= farmacia 68-70)
- El sistema envía el link de pago después de que confirme.
(= farmacia 72-73)

PET_REGLAS
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

PET_SALUDO (fila de la matriz)
| saludo | "Hola", "Buen día", "Buenas", "Cómo están", "Buenas tardes" | Saludar con calidez. Ejemplo: "¡Hola! Bienvenido a {comercio} {emoji} ¿En qué te puedo ayudar?". OJO: si además de saludar el cliente menciona o pide un PRODUCTO ("hola, tenés Royal Canin?"), NO es un simple saludo — usá la intención de producto (consulta_stock/consulta_precio/pedido) y poné el producto en entidad_producto. |

PET_ABIERTA (fila de la matriz)
| consulta_abierta | "Qué alimento me recomendás para un cachorro", "Algo para un gato castrado", "Qué piedras me conviene", "Un juguete para un perro grande" | Indagar lo que falte (especie, edad, tamaño o raza) → sugerir productos del catálogo. Si cuenta un síntoma o un problema de salud, no es consulta_abierta: poné "por_sintoma": true |

PET_VARIOS
PEDIDOS DE VARIOS PRODUCTOS:
Si el cliente menciona MÁS de un producto en el mismo mensaje ("un alimento para gato, piedras sanitarias y unos snacks"):
(= farmacia 133-134)
UN PRODUCTO = TIPO + MARCA: "alimento Royal Canin", "pretal Kipper", "piedras Sanicat", "correa Petnation" son UN solo producto aunque la transcripción de un audio haya puesto una coma en el medio ("pretal, kipper"). No los separes.
DOS TIPOS CON LA MISMA MARCA SON DOS PRODUCTOS: "alimento y snacks Pedigree" = "alimento pedigree" + "snacks pedigree"; "correa y pretal Kipper" = "correa kipper" + "pretal kipper". Repetí la marca en cada uno.
ESCRIBÍ LA MARCA COMO LA DIJO EL CLIENTE: no la "corrijas" a una palabra común ("excellent" NO es "excelente", "kipper" no es "kiper"). El sistema busca con esas palabras.
(= farmacia 138)
(= farmacia 139, con la última oración cambiada por: Decir "no tengo las piedras" cuando el sistema encuentra las piedras dos líneas más abajo deja al bot contradiciéndose solo.)

PET_ENTIDAD
  "entidad_producto": "nombre del producto mencionado o null — CONSERVÁ los números y unidades tal como los dijo el cliente: peso, tamaño, cantidad, talle (ej: 'royal canin mini adult 3 kg', 'piedras sanicat 4 kg', 'pretal kipper n 4', 'dentastix x 7'); son lo que distingue una presentación de otra",

PET_AGREGAR
(= farmacia 156, con "sumale unos snacks" en lugar de "sumale unas gomitas")

PET_SINTOMA
El campo "por_sintoma": true si el cliente cuenta un síntoma o un problema de salud de su mascota, pregunta qué darle o qué dosis, o pide un veterinario ("mi perro vomita, ¿qué le doy?", "tiene diarrea", "cuántas gotas le pongo", "¿cuánto Drontal le doy?", "pasame con el veterinario"). false si pide un producto por nombre, marca o tipo ("una pipeta para perro de 10 kg", "algo para las pulgas", "alimento para gato castrado").

PET_RECHAZO
- false → el usuario cancela O pide un producto DIFERENTE al pendiente (ej: "mejor Pro Plan", "no, quiero Excellent", "prefiero otra marca"). En estos casos siempre false, nunca null.

PET_CAMBIO
Si el cliente rechaza el pendiente mencionando OTRO producto (ej: "no, un Excellent", "mejor dame Pro Plan", "prefiero Vitalcan"), NO es una simple cancelación. Además de confirmacion=false, DEBÉS:
  - poner ese nuevo producto en "entidad_producto" (ej: "excellent", "pro plan", "vitalcan"),
```

Resuelto con los valores de MO, el saludo de ejemplo queda: "¡Hola! Bienvenido a
Mascotas del Oeste 🐾 ¿En qué te puedo ayudar?" (cambio pedido por el usuario el
7/10; la regla de identidad sigue siendo "Soy el asistente virtual de Mascotas del
Oeste").

### 4.2 Recetas (`recetas = False`)

| Archivo:línea | Hoy | Cambio | Capacidad |
|---|---|---|---|
| `catalog_rules.py:48-65` (`explicar_receta`; `ORIGENES` 38-45) | Aplica la regla de farmacia en todo vertical. Un rubro de Mercurio con "medicament" queda `ambiguo` y en modo conservador deriva. La usan `catalog_store._fila` (cada upsert), `recalcular_catalogo` y `receta_marcas.py:51`. | Primera línea: `if not get_perfil().recetas: return "no", "sin_recetas"`, antes de `es_venta_libre` y del import perezoso de `receta_referencia.buscar`. Se agrega `ORIGENES["sin_recetas"] = "Este comercio no vende con receta"` (solo lo ve el panel). | `recetas` |
| `checkout_helper.py:581-588` (`necesita_receta`) | Es la llave única de la derivación por receta | Al principio: `if not get_perfil().recetas: return False`. Con eso quedan apagados, sin tocar su código: `derivar_si_receta` (591-664), `confirmar_pedido` paso 1 (1249-1266), `_sumar_productos_nuevos` (`webhook.py:555`), `referencia_ambigua_bloquea` (1685), agregar al pedido (2273) y los usos de `simulate.py` (287, 509). `quitar_receta_inventada` (2490-2497) pasa a correr siempre que haya productos ofrecidos con precio: es lo buscado, el bot de petshop no dice "necesita receta". | `recetas` |
| `webhook.py:2344-2362` (adicionales) | `_top2` y `_top_rec` leen `requiere_receta` directo, sin pasar por `necesita_receta` | `_rec_on = get_perfil().recetas`, calculado una vez. `_top2`: `vendible and not (_rec_on and requiere_receta in ("si","ambiguo"))`. `_top_rec`: `vendible and _rec_on and ...`. Sin este gate, un adicional marcado desaparecería en silencio. | `recetas` |
| `checkout_helper.py:1795-1801` (`texto_alternativas`) | Sufijo " (requiere receta)" | Solo con `get_perfil().recetas` | `recetas` |
| `intent_service.py:360-361` (`_formatear_productos`) | Marca "REQUIERE RECETA" al modelo | Solo con `get_perfil().recetas` | `recetas` |
| `webhook.py:1422-1437` (`pide_receta_nube`) | Deriva con motivo `receta_nube` y habla del "sistema de recetas 🩺" | `if get_perfil().recetas and pide_receta_nube(texto)`. Si no, el mensaje va al modelo. | `recetas` |
| `main.py:127-138` | En cada arranque carga la referencia de recetas y recalcula `requiere_receta` de todo `catalog_items` | Se extrae a `async def _init_referencia_receta(db)`. Sin `recetas` loguea "Perfil sin recetas: no se carga la referencia ni se recalcula el catálogo" y devuelve `None`. Con `recetas` hace lo mismo que hoy. | `recetas` |
| `webhook.py:949-1012` (foto `receta`) | Deriva `receta_foto`, OCR si `receta_ocr_enabled` | Dueño: §4.6 | `recetas` |

Sin cambios: `config_service` (`receta_mode`, `receta_recibida_message`,
`receta_ocr_enabled`, porque ningún camino las alcanza con `recetas=False`;
ocultarlas en el panel queda fuera de alcance), `sku_service.VENTA_LIBRE`,
`requiere_derivacion` y el prompt de farmacia (líneas 85 y 90-93: el prompt de
petshop no las incluye).

### 4.3 Links (`links_como_receta = False`)

| Archivo:línea | Hoy | Cambio | Capacidad |
|---|---|---|---|
| `webhook.py:1155-1168` | Toda URL o nombre `.pdf/.jpg/.png` se toma como receta o bono: deriva `receta_link` con "Recibí tu link 🙌..." | `if get_perfil().links_como_receta and contiene_link(texto, dominio_propio(get_settings().public_base_url))`. En petshop, el mensaje con link va al modelo. | `links_como_receta` |
| `checkout_helper.py:565-578` (`contiene_link`) | Excluye links con `remedia.ar` (fijo) o `/pay/` | Se agrega `dominio_propio(base_url) -> str`: hostname en minúsculas; si tiene 3 o más etiquetas y las dos últimas no son un sufijo genérico de segundo nivel (`com`, `net`, `org`, `gob`, `gov`, `edu` o `co` + código de país de 2 letras, como `com.ar`), devuelve el dominio padre (`cerca.remedia.ar` → `remedia.ar`); si no, el host tal cual (`bot.mascotasdeloeste.com.ar` queda igual, nunca `com.ar`); sin URL devuelve `""`. `contiene_link(t, dominio_propio="")`: `/pay/` se excluye siempre; el dominio propio, solo si no está vacío y aparece en el link. Se saca el literal `remedia.ar`. | `PUBLIC_BASE_URL` |

Las dos propuestas de firma se unificaron en esta. La farmacia queda idéntica
**solo si** su `PUBLIC_BASE_URL` en Railway es un host bajo `remedia.ar`
(precondición del merge a develop, §7.6). Con `PUBLIC_BASE_URL` vacío, los
links a `remedia.ar` pasarían a derivarse.

### 4.4 Socios, cuenta corriente y obras sociales

| Archivo:línea | Hoy | Cambio | Capacidad |
|---|---|---|---|
| `webhook.py:751` + `1095-1126` | En cada mensaje consulta el padrón, arma `[DATOS DEL SOCIO]`, agrega "Es EMPLEADO de la mutual" y la nota de precios con descuento. Se pasa a seis llamadas al modelo y a `_flujo_mutual`. | `perfil = get_perfil()` una vez al inicio de `procesar_mensajes`, junto a `_s = get_settings()`. Se envuelve 1097-1126 en `if perfil.socios:` con el código intacto. Si no: `_ctx_socio = None`, `_socio_data = None`, `_nombre_socio = ""`. | `socios` |
| `checkout_helper.py:675-708` (`descuento_para`) | Único lugar que decide el descuento: empleado (`empleado_discount_pct`, 20 por default) o socio del padrón | Primera línea: `if not get_perfil().socios: return 0.0, ""`. **Apaga también el descuento de empleado**: es una extensión explícita del diseño, porque el 20% de empleado es regla de la farmacia. Cubre catálogo, link, prompt y cotización. | `socios` |
| `webhook.py:1219-1237` (`consulta_saldo`) | "saldo", "mi deuda", "cuánto te debo" derivan con `consulta_cuenta_corriente` | `if perfil.cuenta_corriente and consulta_saldo(...)`. El fallback literal no cambia (§3.4). | `cuenta_corriente` |
| `webhook.py:1324-1377` | "cuenta corriente", "a la cuenta", "anotalo"/"anotame" interceptan antes del modelo | `if perfil.cuenta_corriente and (pide_cuenta_corriente(texto) or (_hay_pedido_cc and pide_anotar(texto)))`. En petshop, "anotame 2 bolsas más" va al modelo como pedido. En farmacia no cambia la semántica de `cc_enabled`. | `cuenta_corriente` |
| `checkout_helper.py:337-378` (`habilitado_cc`) | Sin gate de rubro | Gate defensivo al inicio: `if not get_perfil().cuenta_corriente: return None` | `cuenta_corriente` |
| `checkout_helper.py:224-241` (`pide_pago_manual`) + `webhook.py:1379, 1391` | Incluye `cuenta corriente` como red de seguridad y toma `mercado pago` como pago manual: si MO cobra con MP, "¿puedo pagar con mercado pago?" saca al cliente de la venta | `pide_pago_manual(t, incluir_cuenta_corriente=True, incluir_mercado_pago=True)`. El webhook calcula una vez `_pide_pm = pide_pago_manual(texto, incluir_cuenta_corriente=perfil.cuenta_corriente, incluir_mercado_pago=str(cfg.get("pago_mp_manual", "true")).lower() != "false")` y la usa en 1379 y 1391. Clave nueva `DEFAULTS["pago_mp_manual"] = "true"` (todo igual que hoy), editable por `ConfigUpdate`. MO la pone en `"false"` si cobra con MP. No se ata al proveedor activo porque la farmacia cambiaría. | `cuenta_corriente` + config |
| `webhook.py:1452-1468` (`dice_ser_socio`) | "soy socio" fuera del padrón deriva con `socio_no_reconocido` ("...padrón de socios...DNI") | `if perfil.socios and dice_ser_socio(texto) and ...` | `socios` |
| `webhook.py:1470-1496` (`pregunta_descuento`) | Cualquier "descuento" corta el flujo con textos de socios, o "Como empleado tenés 20%... sin receta" | `if perfil.socios and pregunta_descuento(texto)`. En petshop va al modelo, y el prompt prohíbe afirmar descuentos (§4.1). | `socios` |
| `webhook.py:1546-1574` (obra social y bono por texto) | Lista fija de más de 50 obras sociales con palabras comunes ("andar", "prensa"), "cobertura", cualquier "bono" | La detección va bajo `if perfil.obras_sociales:`; si no, `_os_preg = _bono_preg = None` y sigue al modelo. Las funciones no cambian. | `obras_sociales` |
| `simulate.py:98-100` | Arma el contexto del socio sin gate | El mismo criterio que el webhook: con `socios` queda lo de hoy, y si no `None`/`""` | `socios` |

La foto de bono y de credencial está en §4.6.

### 4.5 Salud de la mascota (`sintomas = "derivar"`)

Piezas nuevas compartidas con la visión:
- En `checkout_helper.py`, junto a 1542: `MOTIVO_CONSULTA_SALUD = "consulta_salud"`
  y `def texto_consulta_salud(cfg) -> str: return cfg.get("consulta_salud_message") or get_perfil().textos["consulta_salud_message"]`.
  Solo se llama con `sintomas == "derivar"`.
- En `webhook.py`: `async def _derivar_consulta_salud(deps, phone, texto) -> str`.
  Hace `set_estado(phone, "operador", motivo="consulta_salud")`, envía
  `texto_consulta_salud(await deps["config"].get_all())`, guarda el historial
  (usuario y asistente) y devuelve el texto. La intención es
  `derivado_consulta_salud`.

| Archivo:línea | Hoy | Cambio | Capacidad |
|---|---|---|---|
| `webhook.py:2010-2012` (compuerta A, tras Claude 1 y antes de la KB) | Con `por_sintoma` y sin entidad busca en el catálogo con la frase, llama a Claude 2 y suma la oferta del farmacéutico | `if get_perfil().sintomas == "derivar" and intent_result.get("por_sintoma")`: `_derivar_consulta_salud` y `continue`. No consulta la KB, no busca, no llama a Claude 2 y no deja pendiente. | `sintomas` |
| `webhook.py:2161-2162` (compuerta B, tras Claude 2) | Claude 2 puede marcar `por_sintoma` y el flujo sigue: pendiente, métrica, imagen y farmacéutico | Lo mismo que A, antes de `_sin_precios_inventados`, `set_pending`, la métrica o la imagen | `sintomas` |
| `webhook.py:1796-1798` (compuerta C, `esperando_confirmacion`) | Ignora `por_sintoma`; manda el texto del modelo | Lo mismo que A. No confirma, no cambia de producto y no limpia el pendiente (igual que `pidio_humano`). | `sintomas` |
| `webhook.py:563-585` (compuerta D, `_responder_consulta_en_flujo`; agregada por el crítico) | En `esperando_entrega` y `esperando_direccion` descarta `por_sintoma`. El prompt promete "te paso" y nadie deriva. | Si `sintomas == "derivar"` y `resultado.get("por_sintoma")`: `set_estado(..., "operador", motivo="consulta_salud")` y devuelve `texto_consulta_salud(cfg)`. Los llamadores (1600, 1661) lo envían como hoy. Revisión final, hallazgo 4 (**cambio que también afecta a la farmacia**, sin capacidad): después de la compuerta, si `derivacion_prometida(respuesta)` ("te paso con alguien del equipo", la frase que `PET_REGLAS` pide ante cuotas, descuentos, fiado o la sucursal sin cargar), `cumplir_derivacion_prometida(..., soltar_pendiente=False)`: pasa a `operador` con motivo `derivacion_prometida` y el pendiente queda intacto (el pedido ya está confirmado). | `sintomas` |
| `webhook.py:487-529` (`_sin_precios_inventados`, rama `sintoma` 517-524) | Con un precio inventado y síntoma: "Para eso lo mejor es que te asesore el farmacéutico" | Dentro de `if sintoma:`, con `derivar`: `set_estado(..., motivo="consulta_salud")` y `return texto_consulta_salud(cfg)`. Si no, la rama de farmacia tal cual. | `sintomas` |
| `webhook.py:2164-2166` y `2501-2506` | `sintoma = por_sintoma or intencion == "consulta_abierta"` | `sintoma = bool(por_sintoma) or (intencion == "consulta_abierta" and get_perfil().sintomas == "farmaceutico")`. En petshop, "¿qué alimento para un gato castrado?" con un dato inventado cae en la rama genérica ("No lo encuentro en nuestro catálogo 😕 ¿Querés que lo consulte con el equipo?"), no en salud. | `sintomas` |
| `webhook.py:2316-2322` | Agrega la oferta del farmacéutico y marca `farmaceutico_ofrecido` | `if get_perfil().sintomas == "farmaceutico" and por_sintoma` | `sintomas` |
| `webhook.py:1511-1526` | Con `farmaceutico_ofrecido`, "farmacéutico" deriva con motivo `farmaceutico` | `if get_perfil().sintomas == "farmaceutico" and session.get("farmaceutico_ofrecido")` | `sintomas` |
| `config_service.py:203-207` | El comentario dice "Vacío = apagado", pero es falso por el `or` | Se corrige el comentario: un espacio lo apaga; vacío vuelve al texto por defecto. El valor no cambia. | — |
| `metrics_store.py:16-19` + `dashboard.html:290` | No cuentan las intenciones nuevas como derivación | Se agregan `derivado_consulta_salud` e `imagen_indicacion_veterinaria`. La farmacia nunca las emite. | — |

Sin cambio: `_HUMANO` (`checkout_helper.py:140-166`). El crítico propuso sumar
"veterinario", pero eso tocaba la regex de la farmacia. "Pasame con el
veterinario" queda cubierto porque el prompt de petshop lo marca
`por_sintoma: true` y la compuerta A deriva. "¿Me pasás con alguien?" sigue
derivando con `pidio_humano`.

### 4.6 Visión (`vision`)

| Archivo:línea | Hoy | Cambio | Capacidad |
|---|---|---|---|
| `prompts.py` + `perfil.py` | Un único prompt "enviado a una farmacia" con las categorías receta, bono, credencial, comprobante, producto y otro | `VISION_FARMACIA = VisionPerfil(("receta","bono","credencial","comprobante","producto","otro"), prompt=<el _PROMPT de hoy, byte a byte, movido a prompts.py>)`. `VISION_PETSHOP = VisionPerfil(("producto","comprobante","indicacion_veterinaria","otro"), prompt=VISION_PROMPT_PETSHOP)`. | `vision` |
| `image_service.py:27-50, 138, 154` | `_anthropic_vision` y `_openai_vision` mandan siempre el prompt de farmacia | Los dos usan `get_perfil().vision.prompt`. `_PROMPT = prompts.VISION_PROMPT_FARMACIA` queda como alias (`test_logic.py:2079-2081`). `leer_receta` no se toca. | `vision` |
| `image_service.py:161-173` (`_parse`) | Tupla fija; lo desconocido pasa a `otro` | `_parse(raw, categorias=None)`; con `None` usa `get_perfil().vision.categorias`. En petshop, `receta`, `bono` y `credencial` llegan como `otro`. Sigue aceptando la llamada con un argumento. | `vision` |
| `webhook.py:929-947` (foto `bono`) | Deriva `bono_foto` | `if img["tipo"] == "bono" and perfil.obras_sociales` (defensa: con el `_parse` de petshop no llega) | `obras_sociales` |
| `webhook.py:949-1012` (receta, credencial, comprobante) | `receta` deriva `receta_foto` con OCR; `credencial` deriva con texto fijo; `comprobante` deriva `comprobante` | En 951: `receta` entra solo con `perfil.recetas` y `credencial` solo con `perfil.obras_sociales`. El OCR (964) exige `recetas` además de `receta_ocr_enabled`. El fallback de receta y el de comprobante pasan a `perfil.textos` (§3.4). Si en petshop llega un tipo apagado, sigue el camino normal de 1014 (con items va a la búsqueda; vacío va a `imagen_no_reconocida`). | `recetas`, `obras_sociales` |
| `webhook.py`, entre 947 y 949 (rama nueva) | Una indicación del veterinario se clasifica como `receta` y responde "Recibimos tu receta... 10 minutos" | `if img["tipo"] == "indicacion_veterinaria"`: `_intencion = "imagen_indicacion_veterinaria"`, `set_estado(phone, "operador", motivo="consulta_salud")`, respuesta `personalizar_nombre(cfg.get("indicacion_veterinaria_message") or perfil.textos["indicacion_veterinaria_message"], nombre)`, envío, historial y `continue`. No busca en el catálogo. | `vision` + `sintomas` |
| `webhook.py:1014-1034` (producto / otro) | Producto con items → búsqueda; si no, deriva `imagen_no_reconocida` | Sin cambio de lógica. Con el prompt de petshop, una bolsa o un accesorio vuelve como `producto` con MARCA + línea + especie + tamaño, y la foto de la mascota, una herida, la libreta sanitaria o un folleto caen en `otro`. | — |

Este grupo es el **único dueño** del bloque de imagen 929-1034. Los grupos de
recetas y de beneficios no lo editan aparte.

```
VISION_PROMPT_PETSHOP
Analizá esta imagen o documento (puede ser un PDF) enviado por WhatsApp a un petshop (alimento balanceado, accesorios, piedras sanitarias, snacks, higiene y productos de salud para mascotas) y clasificala.
Respondé SOLO con un JSON (sin texto extra) con este esquema:
{"tipo": "producto|comprobante|indicacion_veterinaria|otro", "items": "nombres separados por coma o vacío"}

- producto: es la foto de uno o más productos para mascotas (bolsa o lata de alimento, snack, piedras sanitarias, juguete, collar, correa, cama, comedero, shampoo, pipeta, antiparasitario...) o la captura de un producto (web, catálogo, redes).
- comprobante: es un comprobante de pago — transferencia bancaria, captura de una billetera virtual (Mercado Pago, etc.) o ticket/recibo de pago.
- indicacion_veterinaria: es una receta, orden o indicación escrita de un veterinario (manuscrita o impresa, con sello, firma o membrete de veterinaria), aunque nombre productos.
- otro: cualquier otra cosa: la foto de la mascota o de una herida/síntoma, la libreta sanitaria o carnet de vacunas, un folleto o cupón de promoción, o algo que no encaje.
En items va SOLO cuando el tipo es producto y hay productos identificables, UNO por envase, escrito como MARCA + línea + especie/etapa + tamaño o peso tal como figura en el envase (ej: 'Royal Canin Medium Adult 15kg, Pro Plan Gato Adulto 7.5kg', 'Pipeta Frontline Plus perro 10-20kg'). Un envase = un item. NUNCA listes ingredientes, composición ni tabla nutricional como items: no son productos pedidos. Si no hay productos identificables, dejalo vacío.
```

### 4.7 Pagos y avisos (`emoji`, `comercio` y campos de marca)

| Archivo:línea | Hoy | Cambio | Capacidad |
|---|---|---|---|
| `mp_webhook.py:219-234` + `payway.py:263-278` | Las dos confirmaciones de pago son idénticas byte a byte y terminan en "¡Muchas gracias! 💊". No nombran la sucursal de retiro. | Función pura compartida `mensaje_pago_confirmado(nombre_producto, tipo_entrega, direccion_envio, pickup_code, pickup_text, emoji, sucursal="")`. `emoji = get_perfil().emoji`, leído al armar el mensaje. `sucursal` es `retiro_sucursal` de la config (`get_all`). Con sucursal cargada, la línea de retiro dice "Guardalo para presentarlo al retirar en *{sucursal}*."; vacía, el texto es el de hoy. | `emoji` |
| `payment_service.py:48` | `statement_descriptor: "FARMACIA AMI"` en cualquier deploy | `perfil.descriptor_tarjeta`, o `perfil.comercio` en mayúsculas, sin tildes ni ñ (ASCII) y hasta 22 caracteres. Se lee en cada `crear_link`, no en `__init__`. | `descriptor_tarjeta`, `comercio` |
| `payway_service.py:233, 164, 239` | `description` "Compra Remedia"; antifraude `last_name` "Remedia"; device de respaldo "remedia-web" | `f"Compra {comercio}"`; `last_name = comercio`; `f"{slug(comercio)}-web"` (minúsculas, espacios a guiones; en farmacia sigue dando `remedia-web`). Se lee en cada `crear_pago`. | `comercio` |
| `payway.py:571, 580, 727` (+ reemplazos en 91-98 y 67-71) | `/pay` y las páginas de estado tienen fijos el logo "R", "Remed<b>IA</b>", "Pagar · Remedia" y "Remedia" | Placeholders `{{LOGO}}`, `{{WORDMARK}}` y `{{COMERCIO}}`, reemplazados por request en `pay_page` y `_status_page`: `LOGO = html.escape(comercio[:1].upper())`, `WORDMARK = wordmark_html or html.escape(comercio)`, `COMERCIO = html.escape(comercio)`. Paleta, textos y JS no cambian. | `comercio`, `wordmark_html` |
| `payway.py:573-575` (pie) | "Pago seguro procesado por Payway · Farmacia Mutual Independencia" | `{{RAZON_SOCIAL}} = html.escape(razon_social or comercio)` | `razon_social` |

Los avisos de pedido listo y de efectivo se resuelven por `perfil.textos`
(§3.4). El prompt de petshop dice "link de pago", y el de farmacia sigue
diciendo "link real de Mercado Pago" (§4.1).

Petshop resuelto: descriptor `MASCOTAS DEL OESTE`, "Compra Mascotas del Oeste",
device `mascotas-del-oeste-web`, `<title>Pagar · Mascotas del Oeste</title>`,
logo "M", wordmark "Mascotas del Oeste" y pie "Pago seguro procesado por Payway
· Mascotas del Oeste".

### 4.8 Arranque, config y catálogo

| Archivo:línea | Hoy | Cambio | Capacidad |
|---|---|---|---|
| `main.py:31-38` (blob `catalogo`) | Con `SKU_CSV_PATH=""`, `Path("").write_bytes` revienta, y el `except` común se saltea también el padrón | `if cat and settings.sku_csv_path:`. Se extrae a un helper `_restaurar_archivos(settings, perfil, blob)` para poder testearlo. | — |
| `main.py:39-46` + `97-116` (padrón) | Siempre restaura `blob:socios` y carga o siembra la tabla `socios` | `if perfil.socios:`; si no, log "Perfil sin socios: no se carga el padrón". Es defensa en profundidad: con el padrón vacío, `find_by_phone` devuelve `None`. El bloque de empleados (118-125) no se toca: `descuento_para` ya lo apaga. | `socios` |
| `main.py` (lifespan, después de cargar la config) | Sin horario guardado, `get_hours` usa `DEFAULT_HOURS` en silencio | `logger.warning("Horario no cargado: se usa DEFAULT_HOURS")` si no hay `hours` en Redis ni en Postgres. Es solo un log; la farmacia ya tiene horario. | — |
| `catalog_source.py` (después de 24, nuevo) | Si no hay sucursal ERP, `aplicar_fuente` (134) recarga `data/catalogo_base.csv`, que tiene 17.192 filas de farmacia | `CSV_FARMACIA = Path(__file__).resolve().parents[2] / "data" / "catalogo_base.csv"` y `csv_de_arranque(ruta) -> str`. Si la ruta no está vacía, el perfil no tiene `catalogo_csv_base` y `Path(ruta).resolve() == CSV_FARMACIA.resolve()`, loguea `ERROR` ("SKU_CSV_PATH=… es el catálogo de la farmacia: el perfil petshop no lo carga; el catálogo sale del ERP") y devuelve `""`. Si no, devuelve la ruta tal cual. | `catalogo_csv_base` |
| `sku_service.py:687-699` (`get_sku_service`, `reload_sku_service`) | Construyen `SKUService(csv_path)` con lo que reciban (arranque, fallback de `aplicar_fuente`, default sin argumento de `receta_marcas.py:93`, backoffice) | `SKUService(_csv_permitido(csv_path))`, con import diferido de `csv_de_arranque`. Petshop arranca con catálogo vacío (`SKUService("")`) hasta que el primer sync de Mercurio hace `set_sku_service` (`mercurio_service.py:390-397`). | `catalogo_csv_base` |
| `backoffice.py` (`bo_sku_import`, `bo_sku_import_pdf`, `bo_config_update`; revisión final, hallazgo 17) | En petshop, importar un CSV o un PDF desde el panel reemplaza el catálogo del ERP en memoria (con `SKU_CSV_PATH` vacío, por uno vacío) y `catalogo_fuente="csv"` lo vacía y bloquea la recarga del sync | Sin `catalogo_csv_base`: las dos importaciones responden 409 `{"detail": "Este comercio toma el catálogo del ERP"}` antes de escribir el archivo, recargar o guardar el blob, y `PATCH /bo/config` con `catalogo_fuente="csv"` responde 422 sin guardar nada. `aplicar_fuente` con la fuente `"csv"` ya guardada no recarga: loguea `ERROR` y deja el catálogo como está. | `catalogo_csv_base` |
| `webhook.py:1170-1180` | `if _s.vertical == "mutual":` es el único desvío por nombre | `if not get_perfil().venta:`. El orden no cambia. | `venta` |
| `backoffice.py:602` | `vertical` sale de settings | `get_perfil().clave`. `metrics_store` no se toca: petshop recibe el tablero de venta. | `clave` |

## 5. Corrección: pregunta en `esperando_entrega` y textos de retiro

> **CAMBIO QUE TAMBIÉN AFECTA A LA FARMACIA.** Es una corrección, no un
> comportamiento de rubro. Va sin capacidad y aplica a todos los perfiles.

Caso real (prueba del usuario en MO): en `esperando_entrega` el cliente
preguntó "¿en qué sucursal puede ser?". `_RETIRO` matchea `\bsucursal\b`, así
que se tomó como elección de retiro: salió el link sin contestar y el estado
pasó a `esperando_pago` (`webhook.py:1589-1616`). Pasa lo mismo con "¿dónde
queda la sucursal?" y "¿cuál sucursal?". "¿Cuánto sale el envío?" pide la
dirección. En `esperando_confirmacion` (1758-1775), la misma pregunta confirma
el pedido con retiro.

Regla: **una pregunta no es una elección**. Se responde (con la dirección y el
horario de la sucursal piloto, si MO los cargó) y se vuelve a ofrecer la
elección. Un pedido con forma de pregunta sigue siendo elección: "¿me lo podés
enviar?", "¿lo puedo retirar hoy?". Esto mantiene lo que ya resolvieron los
casos C-3854 y C-3912 del 5/10.

| Archivo:línea | Hoy | Cambio | Farmacia |
|---|---|---|---|
| `checkout_helper.py`, después de 54 | No hay detección de pregunta | Funciones puras `es_pregunta_entrega`, `pregunta_por_retiro` y `responder_pregunta_retiro` (código abajo). `_RETIRO`, `_ENVIO` y `afirma_envio` no se tocan. | sin cambio (funciones nuevas) |
| `config_service.py:274` (`DEFAULTS`, después de `envio_costo`) | No hay dato de sucursal | `"retiro_sucursal": ""` (nombre corto; vacío deja todo como hoy) y `"retiro_info_message": "Lo retirás en *{sucursal}* 🏪"` (respuesta a la pregunta; solo se usa con sucursal cargada). Ningún default trae una dirección. | sin cambio |
| `backoffice.py:900-966` (`ConfigUpdate`) | Lista blanca: `PATCH /bo/config` descarta lo que no está declarado | `retiro_sucursal`, `retiro_info_message` y `pago_mp_manual` (§4.4), todos `str \| None = None` | sin cambio |
| `checkout_helper.py:813-829` (`pregunta_entrega`) | "*retiro en sucursal*" fijo | `retiro_txt = f"*retiro en {(cfg.get('retiro_sucursal') or '').strip() or 'sucursal'}*"` en los dos textos. Cubre los llamadores `webhook.py:1312, 1351, 1608` y `checkout_helper.py:1298, 1340`. | idéntico con la sucursal vacía |
| `checkout_helper.py:832-839` + `1218` (`texto_entrega`) | "🏪 Lo retirás en la sucursal (te enviamos el código al confirmar el pago)." | Parámetro `sucursal=""`; si viene, "🏪 Lo retirás en *{sucursal}* (te enviamos el código al confirmar el pago).". En 1218 se pasa `_cfg.get("retiro_sucursal") or ""`. | idéntico con la sucursal vacía |
| `checkout_helper.py:1973-1986` (`responder_horario`) | Cierre fijo "¿Te ayudo con algo más?" | Parámetro `cierre="¿Te ayudo con algo más?"` (el default es el de hoy) | sin cambio |
| `webhook.py:1439-1450` (interceptor de horario) | "¿Hasta qué hora puedo retirar?" en `esperando_entrega` contesta el horario y "¿Te ayudo con algo más?", y no vuelve a ofrecer la elección | Si el estado es `esperando_entrega` con pendiente: `cierre=pregunta_entrega(_cfg_pm, saludo=False, phone=phone, socio_svc=deps["socios"])`. En otros estados, igual que hoy. | **cambia** |
| `webhook.py:1577-1622` (`esperando_entrega`) | Una pregunta con "sucursal" elige retiro y manda el link; "¿cuánto sale el envío?" pide la dirección | En el `else` de 1589: `_pregunta = es_pregunta_entrega(texto_lower)`; `_es_retiro = match_retiro(...) and not _pregunta`; `_es_envio = (match_envio(...) or afirma_envio(...)) and not _pregunta`. `_cfg_ent` se lee antes (se sube desde 1598). Si `_pregunta` y `pregunta_por_retiro`, y `responder_pregunta_retiro(_cfg_ent)` no está vacío: `_intencion = "consulta_retiro"`, respuesta = info + `"\n\n"` + `pregunta_entrega(_cfg_ent, saludo=False, ...)`. Si no eligió, va a `_responder_consulta_en_flujo` como hoy. Una pregunta nunca llega a `resolver_entrega`. | **cambia** |
| `webhook.py:1598-1610` (situación para el modelo) | Pide terminar con "*retiro en sucursal*" y no prohíbe inventar | Usa la sucursal cargada (o "sucursal") y agrega al final "Nunca inventes direcciones, sucursales ni horarios.". El fallback sigue siendo `pregunta_entrega(...)`. | **cambia** (texto al modelo) |
| `webhook.py:1758-1775` (`esperando_confirmacion`) | Una pregunta con "sucursal" confirma el pedido con retiro | Antes de ese `elif`: `elif es_pregunta_entrega(...) and pregunta_por_retiro(...) and responder_pregunta_retiro(_cfg_dx)`: `_intencion = "consulta_retiro"`, respuesta = info + `"\n\n¿Lo confirmamos?"`, se envía y `continue`, sin confirmar. Además, el `elif` de 1758 suma `and not es_pregunta_entrega(texto_lower)`. Sin sucursal cargada, la pregunta cae al modelo (1777 en adelante). | **cambia** |
| `webhook.py:2012-2027` (consulta general con KB; agregado por el crítico) | "¿Dónde queda la sucursal?" fuera de esos estados va al modelo sin el dato | `_info_ret = responder_pregunta_retiro(cfg) if menciona_sucursal(texto) else ""` (con `cfg` leído antes de 2016). El disparador es `menciona_sucursal(texto)` (sucursal/local/retir*): en la consulta general el dato de la sucursal se agrega solo si el mensaje la nombra (ruling del 7/10: el disparador amplio dejaba de derivar "¿dónde está mi pedido?"). La condición pasa a `if _general and (deps["rag"].enabled() or _info_ret)`; la búsqueda en la KB solo corre con RAG habilitado; `_kb_txt` = documentos de la KB + `_info_ret`. El modelo lo recibe como `[INFORMACIÓN DEL COMERCIO]`. | sin cambio (`retiro_sucursal` vacío) |
| Confirmación de pago, rama retiro | No nombra la sucursal | Dueño: §4.7 (`mensaje_pago_confirmado(..., sucursal)`) | sin cambio con la sucursal vacía |

El aviso de pedido listo no lleva un placeholder nuevo. Como hay una sola
sucursal, MO carga el nombre en su `pedido_listo_retiro_message` desde el
panel (§7.3).

Código de detección (verificado contra los 31 casos de `es_pregunta_entrega`
listados en §6.2: todos dan lo esperado; `pregunta_por_retiro` también):

```python
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
    s = (t or "").lower()
    return (match_retiro(s) or bool(_TEMA_RETIRO.search(s))) and not match_envio(s)

def responder_pregunta_retiro(cfg: dict) -> str:
    """Vacío si no hay sucursal cargada: nunca se inventa una dirección."""
    suc = (cfg.get("retiro_sucursal") or "").strip()
    if not suc:
        return ""
    plantilla = cfg.get("retiro_info_message") or "Lo retirás en *{sucursal}* 🏪"
    return plantilla.replace("{sucursal}", suc).strip()
```

Ajuste respecto de la regla "signo de pregunta + interrogativo": el signo solo
cuenta junto con un interrogativo, o con "sucursal" o "local" sin un verbo de
entrega ("¿tienen sucursal en Morón?"). Si cualquier "?" contara como pregunta,
"¿me lo podés enviar?" se volvería a preguntar en lugar de elegir. Se suman
"cuánto", "cuándo" y "qué" a los interrogativos. Consecuencia, también en la
farmacia: en `esperando_entrega`, "¿cuánto sale el envío?" deja de pedir la
dirección y lo contesta el modelo.

**Textos de retiro con datos de MO** (los carga MO desde el panel; acá van
como claves de config):

```
pregunta_entrega:   ¿Preferís *retiro en {retiro_sucursal}* o *envío a domicilio*? 🙂
texto_entrega:      🏪 Lo retirás en *{retiro_sucursal}* (te enviamos el código al confirmar el pago).
retiro_info_message (lo escribe MO, con la dirección y el horario reales de la sucursal piloto):
                    Lo retirás en *{sucursal}*, <dirección cargada por MO>, <horario cargado por MO> 🐾
respuesta a la pregunta en esperando_entrega:
                    {retiro_info_message}\n\n¿Preferís *retiro en {retiro_sucursal}* o *envío a domicilio*? 🙂
respuesta a la pregunta en esperando_confirmacion:
                    {retiro_info_message}\n\n¿Lo confirmamos?
horario en esperando_entrega:
                    Atendemos {horario cargado en el panel} 🕐 ¿Preferís *retiro en {retiro_sucursal}* o *envío a domicilio*? 🙂
```

### Correcciones de pesos y presentaciones (decisión del usuario del 6/10: entran al go-live)

> **CAMBIO QUE TAMBIÉN AFECTA A LA FARMACIA.** Son correcciones, sin
> capacidad, para todos los perfiles. Las dos cobran el producto equivocado y en
> un petshop los pesos aparecen en casi todos los mensajes.

**Bug 1: "15 kg" se toma como domicilio.** `_NO_DIR` (`checkout_helper.py:66-71`)
reconoce `mg`, `ml`, `gr` y `cc`, pero no `kg`. En "la bolsa de 15 kg", el `15`
pasa como número de calle y `extraer_direccion_de` devuelve "bolsa de 15": en
`esperando_entrega` sale un link con envío a esa "dirección".

| Archivo:línea | Hoy | Cambio | Farmacia |
|---|---|---|---|
| `checkout_helper.py:66-71` (`_NO_DIR`) | Grupo de unidades `(mg\|ml\|gr?s?\|cc\|mcg\|ui\|%)` | El grupo pasa a `(mg\|ml\|gr?s?\|kgs?\|lts?\|l\|cc\|mcg\|ui\|%)` y la lista de palabras suma `kilos?\|kilogram\w*\|gramos?\|litros?\|bolsa\w*\|lata\w*`. **No** se agrega `kilo\w*`: excluiría "Ruta 8 kilómetro 52", que es una dirección válida. Con `gramos?` y la `l` suelta, "la de 400 gramos", "el de 500 gramos" y "la de 15 l" tampoco son domicilio (ruling del 7/10: sin eso quedaban como "de 400" y el vocabulario de `_NO_DIR` no coincidía con el de `presentaciones_de`). | **cambia** (ningún peso era un domicilio válido) |

**Bug 2: "el de 3 kg" confirma la bolsa de 15.** `entidad_contradice_pendiente`
(`checkout_helper.py:1556-1569`) compara con `numeros_de`
(`sku_service.py:99, 138-140`), que por diseño ignora los números de una cifra
y los decimales ("dame 2" es una cantidad). Con un pendiente "ROYAL CANIN
MEDIUM ADULT 15KG", "sí, pero el de 3 kg" no aporta números, no hay
contradicción y se cobra la de 15. Lo mismo pasa con "el de 2 litros" frente a
"1L" y con "el nº 3" frente a "PRETAL KIPPER Nº 4".

| Archivo:línea | Hoy | Cambio | Farmacia |
|---|---|---|---|
| `checkout_helper.py`, junto a 1556 (nuevo) | No hay comparación de presentaciones con unidad | Función pura `presentaciones_de(t) -> set[tuple[str, float]]`: pares `(tipo, valor normalizado)` para peso (`mg`; `kg` ×1.000.000, `g` ×1.000), volumen (`ml`; `l`/`lt`/`litro` ×1000) y talle (`n`: "nº", "n°", "n", "numero", "talle"). Acepta una cifra y decimales con punto o coma ("7.5 kg", "1,5 l"); el peso se normaliza a miligramos (kg ×1.000.000, g ×1.000) y el volumen a mililitros, con valores redondeados a 6 decimales (ruling del 7/10: en gramos y con 3 decimales "0,5 mg" y "1 mg" daban los dos 0.001 y se cobraba la otra dosis). Regex: `(\d+(?:[.,]\d+)?)\s*(kgs?\|kilos?\|kilogram\w*\|grs?\|g\|gramos?\|mg\|ml\|cc\|lts?\|l\|litros?)\b` para peso y volumen, y `\b(?:n[º°o]?\|numero\|número\|talle)\.?\s*(\d{1,2})\b` para talle. La `º` es opcional porque el prompt de petshop le pide al modelo escribir `pretal kipper n 4`. | sin cambio (función nueva) |
| `checkout_helper.py:1567-1569` (`entidad_contradice_pendiente`) | Mismo nombre y `numeros_de` disjuntos → contradicción | Primero: `p_ent, p_pend = presentaciones_de(entidad), presentaciones_de(pending_nombre)`. Si para algún **tipo** los dos tienen valores y no comparten ninguno → `True`. Si no, sigue la regla de hoy con `numeros_de`. `numeros_de` no se toca: alimenta el ranking de la búsqueda. | **cambia**: "el de 2 litros" sobre "1L" deja de confirmar. Lo que hoy contradice sigue contradiciendo. |

Casos (todos con el mismo nombre de producto, así `nombre_coincide` da True):

| Entidad | Pendiente | Hoy | Después |
|---|---|---|---|
| "royal canin 3 kg" | "ROYAL CANIN MEDIUM ADULT 15KG" | confirma (bug) | contradice |
| "royal canin 15 kg" | "ROYAL CANIN MEDIUM ADULT 15KG" | confirma | confirma |
| "royal canin 7.5 kg" | "ROYAL CANIN … 7,5 KG" | confirma | confirma (7.500.000 mg = 7.500.000 mg) |
| "royal urinary 400 gr" | "ROYAL URINARY CAT … 400GRS" | confirma | confirma |
| "royal urinary 1.5 kg" | "ROYAL URINARY CAT … 400GRS" | confirma (bug) | contradice |
| "shampoo 2 litros" | "SHAMPOO … 1L" | confirma (bug) | contradice |
| "pretal kipper nº 3" | "PRETAL KIPPER Nº 4" | confirma (bug) | contradice |
| "pretal kipper n 3" | "PRETAL KIPPER Nº 4" | confirma (bug) | contradice |
| "curflex x 30" | "CURFLEX PLUS X 60" | contradice | contradice (regla de hoy) |
| "ibuprofeno 600" | "IBUPROFENO 600 MG X 10" | confirma | confirma (la entidad no tiene unidad: regla de hoy) |
| "royal canin" | "ROYAL CANIN MEDIUM ADULT 15KG" | confirma | confirma (sin presentación en la entidad) |

**Ajuste al planificar (7/10):** `presentaciones_de` expande además las dosis
combinadas ("50/1000 Mg" → dos pesos). Sin eso, la farmacia cambiaría en los 82
productos de `catalogo_base.csv` con ese formato: "janumet 50 mg" frente a
"Janumet 50/1000 Mg Comp.X 28" confirma hoy y pasaría a contradecir. Ese par se
suma como 12º caso de la tabla (guarda de farmacia); §6.2 dice "los 12 pares"
por eso: 11 de la tabla + 1 guarda.

**Ajuste de la ronda de arreglo 1 (ruling del 7/10):** (1) `_NO_DIR` suma
`gramos?` a la lista de palabras y `l` al grupo de unidades, para que "la de
400 gramos", "el de 500 gramos" y "la de 15 l" no sean domicilio; sigue sin
excluir "kilómetro". (2) `presentaciones_de` normaliza el peso a **miligramos**
(`kg`, `kilo(s)` y `kilogramo(s)` ×1.000.000; `g`, `gr`, `grs` y `gramo(s)`
×1.000; `mg` ×1) y el volumen a mililitros, con 6 decimales. En gramos y con 3
decimales las dosis bajo 1 mg se confundían ("clonazepam 0,5 mg" y
"CLONAZEPAM 1 MG" daban los dos `("g", 0.001)`) y la confirmación cobraba la
otra dosis. Las dosis combinadas usan la misma escala. El tipo de la tupla de
peso pasa a llamarse `"mg"`. Es un cambio del contrato interno, no de
comportamiento: los 12 pares de la tabla dan lo mismo y solo cambian las dosis
bajo 1 mg, que ahora se distinguen.

Verificado sobre `07a1d7a`: la columna "Hoy" con la función real (en los 11
pares `nombre_coincide` da True) y la columna "Después" con un prototipo de
`presentaciones_de`. Del bug 1, también con prototipo: "la bolsa de 15 kg",
"mandame la de 15 kilos" y "dos latas de 85" dejan de ser domicilio, y "san
javier 837", "Ruta 8 kilómetro 52", "16 de enero 9279" y "donado 608 piso 2"
lo siguen siendo.

**Ajuste de la revisión final (7/10, hallazgos 2, 3 y 16).** Los tres son
correcciones sin capacidad, para todos los perfiles:

- **Hallazgo 2.** `_es_afirmacion_pura` (`webhook.py`) devuelve `False` si el
  mensaje tiene un dígito. "sí, la de 3" con la bolsa de 15 pendiente
  confirmaba la de 15 por el atajo, sin el modelo ni
  `entidad_contradice_pendiente`; ahora va al modelo. "sí", "dale" y "si dale"
  siguen confirmando por el atajo.
- **Hallazgo 3.** `extraer_direccion_de` saltea la candidata si todos los
  tokens de la calle son conectores (`de`, `del`, `la`, `el`, `los`, `las`):
  "la de 20" devolvía el domicilio "de 20" y, con el link de retiro enviado,
  "uh, me confundí, era la de 20" lo regeneraba como envío. "16 de enero
  9279" sigue valiendo porque tiene "enero".
- **Hallazgo 16.** `l`, `lt` y `lts` son también Lote y departamento ("Mz 5
  L 12", "Corrientes 1234 4 L"). Salen del grupo de unidades de `_NO_DIR` y
  cuentan como litros solo si no les sigue un número y si el número que las
  precede no viene pegado a otro número (separado por un espacio o por coma y
  espacio): en "1234 4 L" el 4 es el piso. "la de 15 lts", "el bidon de 20 l
  por favor", "mandame 2 l" y "quiero el de 5 lt" siguen sin ser domicilio.
  Costo (ruling): un volumen escrito como "1234 4 l" se toma como domicilio,
  y "Calle 15 L" sigue sin reconocerse.

**Ajuste de la ronda de arreglo 2 (7/10).** También sin capacidad, para todos
los perfiles (**cambio que también afecta a la farmacia**):

- **Atajo de confirmación con palabra de entrega (residuo del hallazgo 2).**
  En `esperando_confirmacion`, "sí, la de 3, la paso a buscar", "si la de 3 la
  retiro" o "dale la de 3, mandamela" confirmaban la bolsa de 15 por el atajo
  (`match_retiro` / `match_envio`). `numero_contradice_pendiente(texto,
  pendiente)` (`checkout_helper.py`) da `True` si el mensaje trae una
  presentación con unidad que el pendiente no tiene, o un número que elige
  otra ("la de 3", "el de 400", "x 30") y no está en el nombre del pendiente
  (o del carrito): el atajo no corre y el mensaje va al modelo. Si el modelo
  igual confirma el pendiente, la entrega elegida en el mismo mensaje se pasa
  a `confirmar_pedido`. Siguen por el atajo "sí, lo retiro", "mandámelo",
  "si, envío", "sí, la de 15, la retiro" y los números que no eligen producto
  ("lo retiro en 2 horas", "a las 5", "mandámelo a Corrientes 1234").
- **Hallazgo 16 (resto).** La L de depto después de una altura de calle de 3 a
  5 cifras seguida solo de un separador ("," o "-"), "piso", "p", "dto",
  "dpto" o "depto" y el número no es litros: "Corrientes 1234 piso 3 L",
  "1234 dto 4 L", "1234 p 4 L", "1234 - 4 L" y "1234,4 L" vuelven a ser
  domicilio, como en develop (`_menciona_litros`, que sale de `_NO_DIR`). "el
  bidón de 1000 l" y "pagué 1500 por la de 15 l" siguen siendo volumen.
- **Derivación prometida.** `_DERIV_PROMETIDA` toma "ya te paso" / "ya te
  derivo" como promesa de una persona solo si sigue "con ...", "a(l)
  <persona>" (alguien, una persona, el equipo, las chicas) o el fin de la
  oración. "Elegí y ya te paso el link de pago." ya no deriva; "Ya te paso.",
  "ya te paso con alguien del equipo" y el texto de consulta de salud siguen
  derivando.

## 6. Pruebas

TDD: cada test nuevo se escribe antes que el código y se ve fallar. Los dos
goldens de farmacia (prompt y `DEFAULTS`) se escriben **antes** del refactor y
tienen que pasar en verde con el código de hoy.

### 6.1 Infraestructura

- Fixture común en `tests/conftest.py`:
  ```python
  @pytest.fixture
  def usar_perfil(monkeypatch):
      def _usar(clave, comercio=None):
          monkeypatch.setenv("VERTICAL", clave)
          if comercio is not None:
              monkeypatch.setenv("COMERCIO_NOMBRE", comercio)
          get_settings.cache_clear(); get_perfil.cache_clear()
          return get_perfil()
      yield _usar
      get_settings.cache_clear(); get_perfil.cache_clear()
  ```
  Sin el `cache_clear` del teardown, el perfil petshop se filtra al resto de
  la suite. Los tests que pasan por `get_intent_service` además hacen
  `intent_service._instance = None`.

  **Corrección al planificar (6/10):** la fixture final **no** usa `setenv` +
  `get_settings.cache_clear()`. La fixture `pg_dsn` deja `DATABASE_URL` en el
  entorno, así que un `Settings` nuevo apuntaría el resto de la suite al
  Postgres de prueba. `usar_perfil` pisa `vertical` y `comercio_nombre` en el
  `Settings` cacheado (`monkeypatch.setattr`) y limpia solo `get_perfil`. El
  código de arriba queda como ilustración de la interfaz; vale el del plan
  (`docs/superpowers/plans/2026-10-06-vertical-petshop.md`, Task 1).

  **Corrección al planificar (7/10):** los fakes de `tests/test_webhook_secuencias.py`
  (`_Intent`, `_Cfg`, `_Img`) **no** se modifican. Cada archivo de test nuevo trae
  sus propios fakes (con `valores_base()`, `texto_horario` e items donde hacen
  falta), así ningún test existente cambia (§6.3).
- `tests/test_webhook_secuencias.py`: el `_Intent` falso guarda los kwargs
  además del mensaje. El `_Cfg` falso se arma con `config_service.valores_base()`
  (y no con `dict(DEFAULTS)`) y suma `texto_horario`. Hace falta una variante de
  `_Img` que devuelva `items`. Los tests existentes no cambian.
- Los goldens van como hash adentro del test, no como `.txt`: el repo tiene
  `core.autocrlf=true` sin `.gitattributes`.

### 6.2 Tests nuevos

**`tests/test_perfil.py` (arquitectura, prompt, textos, config, arranque)**
- `test_prompt_farmacia_identico_byte_a_byte`: sha256 de
  `perfil_por_clave("farmacia").system_prompt` y de `intent_service.SYSTEM_PROMPT`
  == `1953a4e6815d855e635406c1da8f97ff83bb9be6a2fd040b69eabfae4c540749`, con
  largo 14.680.
- Mutual: `system_prompt is SYSTEM_PROMPT_MUTUAL` (sha256 `4377db47f567816d994d1824bd25adb7cb40e5bb9a96d00b74491f81258dc469`),
  `venta` False, el resto de las capacidades y los textos iguales a farmacia,
  `vision is` la de farmacia, `rotulo_kb == "INFORMACIÓN DE LA FARMACIA"`.
- `VERTICAL="veterinaria"` → `ValueError` cuyo mensaje contiene
  "veterinaria" y "farmacia, mutual, petshop". Entrar a `with TestClient(app)`
  con ese valor falla al arrancar. `""` o sin setear → farmacia;
  `" Petshop "` → petshop.
- `COMERCIO_NOMBRE="MO Prueba"` en petshop → `comercio == "MO Prueba"`; el
  prompt contiene "Soy el asistente virtual de MO Prueba" y no "Mascotas del
  Oeste". En farmacia, el prompt mantiene el hash.
- Prompt petshop: en `.lower()` no aparece `farmac`, `receta`, `socio`,
  `obra social`, `remedia`, `mercado pago`, `mutual`, `medicament`, `remedio`,
  `semanalmente`, `{comercio}`, `{emoji}`, `ibuprofeno`, `lotrial`, `bayer`,
  `aveno` ni `talco`. Contiene "Soy el asistente virtual de Mascotas del Oeste",
  "¡Hola! Bienvenido a Mascotas del Oeste 🐾 ¿En qué te puedo ayudar?",
  "El sistema envía el link de pago después de que confirme.",
  "SALUD DE LA MASCOTA", "DESCUENTOS, PROMOCIONES Y CUPONES",
  "Nunca inventes la dirección" y "especie".
- Contrato: cada bloque compartido es substring de los dos prompts; la línea del
  enum `intencion` y el conjunto de claves del JSON son idénticos; "REGLA
  ESTRICTA: solo podés ofrecer productos..." y "- NUNCA incluyas URLs,
  links..." están en los dos.
- Textos: para los tres perfiles `CLAVES_TEXTO_RUBRO ⊆ set(p.textos)`; para
  farmacia y mutual `set(p.textos) == CLAVES_TEXTO_RUBRO` y `{**DEFAULTS, **p.textos} == DEFAULTS`.
  En petshop, ningún valor matchea `farmac|receta|socio|mutual|bono|obra social`
  ni contiene 💊. Invariantes: `sintomas == "derivar"` ⇒ `consulta_salud_message`
  existe, contiene `perfil.emoji` y no contiene farmac/receta/socio/obra social;
  `"indicacion_veterinaria" in vision.categorias` ⇒ `indicacion_veterinaria_message` existe;
  `not recetas` ⇒ `"receta" not in vision.categorias`; `not obras_sociales` ⇒
  `bono` y `credencial` no están.
- Config (Redis caído, sin `DATABASE_URL`): en farmacia, `get_all() == DEFAULTS`,
  y el sha256 de `json.dumps(<DEFAULTS sin las tres claves nuevas>, sort_keys=True,
  ensure_ascii=False)` es igual al golden tomado antes del cambio (90 claves,
  `655cbe78…0e1d`, verificado sobre `07a1d7a`). En petshop,
  `pedido_listo_retiro_message` termina en 🐾, `sintoma_farmaceutico_message == ""`
  y `send_images` sale de `DEFAULTS`; tras `set_many({"pedido_listo_retiro_message": "X"})`
  devuelve `"X"`.
- `IntentService("")._system_prompt()`: en farmacia `is SYSTEM_PROMPT`, en
  petshop contiene "Mascotas del Oeste" y en mutual `is SYSTEM_PROMPT_MUTUAL`.
  Singleton: se crea con farmacia, se cambia a petshop con `cache_clear` y la
  misma instancia devuelve el prompt de petshop. `get_intent_service("", "", "anthropic")`
  (la llamada de `simulate.py:90`) con petshop contiene "Mascotas del Oeste".
- `_con_contexto("m", "Nombre de pila (para saludar): Ana", "Horario: 9 a 18")`
  en petshop == `"m\n\n[INFORMACIÓN DEL COMERCIO]\nHorario: 9 a 18\nUsá esta información para responder si aplica. Si no alcanza, ofrecé pasar con una persona del equipo. No inventes datos."`.
  En mutual siguen `[DATOS DEL SOCIO]` e `[INFORMACIÓN DE LA FARMACIA]`.
- Audio: en farmacia, `marcas_base` tiene 38 elementos y empieza con
  `("Aveno", "Atopix", "Actron")`. En petshop, con un `SKUService` de prueba
  con ROYAL CANIN y SANICAT: empieza con "Consulta a un petshop. Productos y
  marcas: ", contiene "Royal Canin" y "Sanicat", no contiene "Atopix" y
  `len <= 650`.
- Arranque: `_restaurar_archivos` con un blob falso. Farmacia con
  `SKU_CSV_PATH=""` no tira y sigue restaurando el padrón; petshop no escribe el
  padrón. `_init_referencia_receta`: petshop no llama a `inicializar`, y
  farmacia y mutual la llaman una vez.
- Catálogo: en petshop, `csv_de_arranque("data/catalogo_base.csv") == ""` con
  un `ERROR` en `caplog`; `reload_sku_service("data/catalogo_base.csv").total == 0`;
  un CSV temporal con 2 filas de MO da `total == 2`; `aplicar_fuente()` con
  `catalogo_fuente="csv"` no toca el catálogo del ERP en memoria y loguea
  `ERROR` (revisión final, hallazgo 17; antes daba `total_productos == 0`).
  Importar CSV o PDF desde el panel da 409 y `catalogo_fuente="csv"` da 422,
  sin escribir nada. En farmacia, el mismo total que hoy, `buscar("ibuprofeno")`
  devuelve resultados y la importación y el botón de pánico andan igual.
- Tablero: en petshop, `GET /bo/tablero?mes=2026-09` → 200, `vertical == "petshop"`
  y viene `producto`.

**`tests/test_petshop.py` (unitarios por capacidad; cada uno con su par de farmacia "igual que hoy")**
- Recetas: `explicar_receta("MEDICAMENTOS","PERROS","ANTIPARASITARIOS","Pipeta Frontline 10-20kg", referencia=lambda b: "si")`
  → `("no","sin_recetas")`; `catalog_store._fila` con `category="MEDICAMENTOS"` →
  `requiere_receta "no"`; `necesita_receta` → False en los dos modos;
  `texto_alternativas` sin "(requiere receta)"; `_formatear_productos` sin
  "REQUIERE RECETA". Farmacia: `("ambiguo","sin_referencia")` y
  `("si","categoria_bajo_receta")`, como hoy.
- Links: antes del cambio, regresión de farmacia con
  `PUBLIC_BASE_URL=https://farmacia.remedia.ar` (hoy no hay tests de
  `contiene_link`). `dominio_propio`: `cerca.remedia.ar` → `remedia.ar`,
  `bot.mascotasdeloeste.com.ar` igual, `""` → `""`. `contiene_link`: links
  propios y `/pay/` → False; drive o `receta.jpg` → True; con el dominio de MO,
  `https://otra.com.ar/x` → True.
- Beneficios: con un empleado cargado y descuentos de 20 y 15,
  `descuento_para` → `(0.0, "")`; `aplicar_descuento_socio` no toca los precios;
  `crear_link_y_responder` no agrega "🎉 Como empleado...";
  `habilitado_cc` → `None`; `pide_pago_manual("me lo anotás en cuenta corriente?", incluir_cuenta_corriente=False)`
  → False; `pide_pago_manual("lo pago con mercado pago", incluir_mercado_pago=False)` → False.
- Síntomas: `_sin_precios_inventados(..., "Dale Vomitol $5.000", [], cfg, None, sintoma=True)`
  → `consulta_salud_message`, operador con motivo `consulta_salud` y sin
  `farmaceutico_ofrecido`. `agregar_oferta_farmaceutico("Te ofrezco Pipeta X", {"sintoma_farmaceutico_message": ""})`
  → sin agregados.
- Visión: `_parse` de `receta`, `bono` o `credencial` → `otro`;
  `indicacion_veterinaria` → igual; `producto` con items los conserva. Con un
  cliente Anthropic u OpenAI falso, el bloque de texto es `VISION_PETSHOP.prompt`
  (en farmacia, `_PROMPT`).
- Pagos: `mensaje_pago_confirmado` en las dos entregas y los dos perfiles
  (petshop termina en 🐾 sin 💊; con sucursal, "al retirar en *Sucursal
  Piloto*"). `crear_link` captura `statement_descriptor == "MASCOTAS DEL OESTE"`
  (farmacia `FARMACIA AMI`; con `COMERCIO_NOMBRE` con ñ queda ASCII y ≤ 22).
  `crear_pago`: `"Compra Mascotas del Oeste"`, `last_name` y
  `mascotas-del-oeste-web`. `pay_page` y `payway_return`: título, logo "M",
  wordmark y pie de MO, sin "Remed" ni "Farmacia". En farmacia, el body es
  idéntico a un snapshot tomado antes del cambio. `armar_mensaje_pedido_listo`
  y `_cerrar_venta_efectivo` con la clave vacía en `cfg` caen al texto petshop.

**`tests/test_petshop_conversaciones.py` (punta a punta, reusa `entorno`, `_msg` y `PHONE` de `test_webhook_secuencias.py`)**

| Grupo | Entrada (guion del `_Intent`) | Esperado en petshop |
|---|---|---|
| Recetas | Pendiente Pipeta `20` ("Medicamentos Bajo Receta"), "si" | No sale `derivado_receta` ni "receta"/"medicamento"; pregunta la entrega o manda el link |
| Recetas | "quiero el alimento Dog Chow 3kg y una pipeta frontline" (adicional con "si") | "Sobre lo demás que me pediste:" con la pipeta y su precio; `extras_ofrecidos` incluye `20`; no deriva |
| Recetas | "agregame la pipeta frontline" con un alimento pendiente | "¡Listo, lo sumé! Tu pedido queda así:" con `item_agregado` |
| Recetas | "tengo la receta del veterinario cargada en el sistema" | Nada de "sistema de recetas" ni 🩺; el texto llega al modelo |
| Recetas | El modelo responde "...Ojo que va con receta del veterinario. ¿La querés?" | Lo enviado conserva el precio y la pregunta, sin "receta" |
| Links | "Hola, tenés este? https://www.instagram.com/p/C1abc/" | No sale "Recibí tu link"; estado ≠ operador; llega al modelo. Mutual: sigue derivando. |
| Beneficios | "hola, tienen alimento para gato?" con padrón y empleado falsos | `contexto_cliente=None`; nada con mutual, socio ni empleado |
| Beneficios | "cuánto te debo?", "tienen saldo de piedras?", "anotame 2 bolsas más", "sumale una bolsa a la cuenta", "lo anoto en la cuenta" (empleado, `cc_enabled`, link enviado) | Ninguno deriva por cuenta corriente ni cierra en cuenta; todos llegan al modelo |
| Beneficios | "¿puedo pagar con cuenta corriente?" (`pago_manual_mode=derivar`) | No deriva `transferencia_efectivo`. Control: "te pago por transferencia" sí deriva. |
| Beneficios | "Hola, soy socio del club, tienen piedras sanitarias?" | Nada de "padrón" ni "DNI"; llega al modelo |
| Beneficios | "¿tienen descuento por bolsa grande?" / empleado + "tengo descuento?" | Nada de "socios", "lo estamos habilitando", "Como empleado" ni "sin receta" |
| Beneficios | "¿Le va a andar bien a mi perro?", "¿tienen cobertura de envío a Castelar?", "aceptan el bono de Royal Canin?" | No dispara obra social ni bono; llega al modelo |
| Síntomas A | "mi perro vomita, ¿qué le doy?" (`consulta_abierta`, `por_sintoma` True, respuesta "Dale Reliveran $3.000") | 1 mensaje = `consulta_salud_message`; operador con `consulta_salud`; `vistos` solo `rapido`; sin pendiente; sin "farmac" ni "$" |
| Síntomas A | "pasame con el veterinario" (`desconocido`, `por_sintoma` True) | `consulta_salud`, no `no_entendido` |
| Síntomas, venta | "tenés pipeta Frontline para perro de 10 a 20 kg?" (`por_sintoma` False) | Queda pendiente; estado ≠ operador |
| Síntomas B | Claude 1 `por_sintoma` False; Claude 2 True con producto | Deriva `consulta_salud`; sin pendiente; sin evento `producto_ofrecido` |
| Síntomas C | Pendiente cargado + "che, y mi gata está vomitando, ¿qué le doy?" | Deriva; sin link; el "dale" siguiente no genera mensaje |
| Síntomas D | En `esperando_entrega`, "mi perro vomita, que le doy?" | Operador con `consulta_salud` |
| Síntomas | "qué alimento me recomendás para un gato castrado?" con nombre o precio inventado | "No lo encuentro en nuestro catálogo..."; `derivacion_ofrecida`; sin "farmac" ni "25.000" |
| Síntomas | Sesión con `farmaceutico_ofrecido` puesta a mano + "farmacéutico" | No deriva con motivo `farmaceutico` |
| Visión | Foto `indicacion_veterinaria` | "Recibí la indicación del veterinario 🐾 ..."; operador con `consulta_salud`; `vistos` vacío; después "Hola" no responde |
| Visión | Foto `comprobante` | "¡Listo! Recibimos tu comprobante 🙌 ..."; motivo `comprobante` |
| Visión | Foto `producto` con "Royal Canin Medium Adult 15kg" / foto `otro` | Llega al modelo / "Recibí tu imagen 🙌 ..." con `imagen_no_reconocida` |
| Visión | `entorno(img_tipo="receta")` con `receta_ocr_enabled="true"` | `leer_receta` no se llama; nada con "receta"; motivo ≠ `receta_foto` |
| Venta | "hola" con `wh._flujo_mutual` falso | No se llama en petshop ni en farmacia; en mutual se llama una vez |
| Compra completa | "hola" → "tenés dog chow 15 kg?" → "si" → "retiro" | Link de retiro con la sucursal; ningún enviado contiene farmacia, receta, socio, obra social, mutual, cuenta corriente ni 💊 |

**`tests/test_entrega_sucursal.py` (corrección §5; corre con farmacia y con petshop)**
- `es_pregunta_entrega` da True con: "en que sucursal puede ser?", "en qué
  sucursal lo retiro?", "donde queda la sucursal?", "a que hora puedo pasar?",
  "como hago para retirarlo?", "cual sucursal?", "que sucursal me queda mas
  cerca?", "tienen sucursal en moron?", "¿Dónde retiro?", "cuánto sale el
  envío?", "en q sucursal?" y "dónde lo retiro".
- Da False con: "retiro", "retiro en sucursal", "lo paso a buscar", "voy a la
  sucursal", "envio", "a domicilio", "mandámelo a casa", "si ahí", "dale ahí",
  "sí, a mi domicilio", "me lo podés enviar", "¿me lo podés enviar?", "me lo
  envías?", "lo puedo retirar hoy?", "hacen envío a Funes?", "cuando salga del
  trabajo lo paso a buscar", "como siempre, retiro", "dale, lo busco" y "ok".
- `pregunta_por_retiro("cuánto sale el envío?")` es False;
  `responder_pregunta_retiro({})` es `""`.
- `pregunta_entrega({})` y `texto_entrega("retiro", None)` dan el texto de hoy,
  byte a byte. Con `retiro_sucursal="Sucursal Piloto"`, nombran la sucursal.
- Webhook, con `cfg {"retiro_sucursal": "Sucursal Piloto", "retiro_info_message": "Lo retirás en *{sucursal}*, Calle Falsa 123, de 9 a 20 hs 🐾"}`:
  (a) `esperando_entrega` + "en que sucursal puede ser?" → contiene "Calle
  Falsa 123" y "*retiro en Sucursal Piloto*", no hay link y el estado no
  cambia; después "retiro" genera el link `[("retiro", None)]`. (b) Farmacia sin
  sucursal: responde el modelo, no hay link, el estado no cambia y la situación
  contiene "Nunca inventes direcciones". (c) "cuánto sale el envío?" → responde
  el modelo y el estado no cambia. (d) Guarda: "¿me lo podés enviar?" sigue
  llevando a `esperando_direccion`. (e) `esperando_confirmacion` + "en que
  sucursal puede ser?" → info + "¿Lo confirmamos?", sin link; en farmacia sin
  sucursal, va a `procesar`. (f) "hasta qué hora puedo retirar?" en
  `esperando_entrega` → "Atendemos ... 🕐" + re-pregunta, sin "¿Te ayudo con
  algo más?". (g) "¿dónde queda la sucursal?" sin pendiente
  (`desconocido`) → el modelo recibe la info en `contexto_kb`.
- (a), (b), (c), (e) y (f) fallan sobre el código actual; (d) pasa antes y
  después.

**`tests/test_pesos.py` (correcciones de pesos de §5; corre con farmacia y con petshop)**
- `extraer_direccion_de` da `None` con: "la bolsa de 15 kg", "mandame la de 15
  kilos", "dos latas de 85", "el de 2 litros", "una de 3 kgs", "la de 400
  gramos", "el de 500 gramos", "la de 15 l", "dame 2 de 1 l" (ruling del 7/10). Sigue
  devolviendo la dirección con: "san javier 837", "Ruta 8 kilómetro 52", "16 de
  enero 9279", "donado 608 piso 2" (y los casos de `test_direccion_envio.py`,
  sin cambios).
- `presentaciones_de` (escala nueva, ruling del 7/10): "royal canin 7,5 kg" →
  `{("mg", 7500000.0)}`; "400GRS" → `{("mg", 400000.0)}`; "1L" →
  `{("ml", 1000.0)}`; "pretal kipper n 4" y "Nº 4" → `{("n", 4.0)}`;
  "ibuprofeno 600" → `set()`; "Janumet 50/1000 Mg Comp.X 28" →
  `{("mg", 50.0), ("mg", 1000.0)}`. Dosis bajo 1 mg: "clonazepam 0,5 mg" →
  `{("mg", 0.5)}` y "CLONAZEPAM 1 MG" → `{("mg", 1.0)}` (distintas, y
  `entidad_contradice_pendiente` da `True`); "1,5 mg" y "2 mg", "0,25 mg" y
  "0,1 mg" también se distinguen, y "0,25 mg" frente a "0.25 MG" no contradice.
  También "kilos", "kilogramos", "gramos", "litros", "lts", "cc", "numero" y
  "talle".
- `entidad_contradice_pendiente`: los 12 pares de la tabla de §5, con el
  resultado de la columna "Después".
- Webhook, en `esperando_entrega` con un pendiente "ROYAL CANIN MEDIUM ADULT
  15KG": "la bolsa de 15 kg" no genera link con envío ni pasa a
  `esperando_pago`. En `esperando_confirmacion`: "sí, pero el de 3 kg" no
  confirma (va como otro pedido), y "sí, la de 15 kg" confirma.
- Fallan sobre el código actual: "la bolsa de 15 kg", "mandame la de 15 kilos"
  y "dos latas de 85" en `extraer_direccion_de`; todo `presentaciones_de` (no
  existe); los pares marcados "(bug)" en la tabla; y los dos casos de webhook
  con 15 kg y 3 kg. "el de 2 litros" y "una de 3 kgs" ya dan `None` hoy (una
  cifra no es altura de calle): quedan como guarda.

### 6.3 Tests existentes

Ninguno cambia de expectativa. Se tocan solo los fakes de §6.1. Tienen que
seguir en verde sin edición, entre otros: `test_logic.py:1372-1374` (import de
`SYSTEM_PROMPT`), `test_degradation.py:40-45, 62-79`,
`test_webhook_secuencias.py:288-296, 434-439` y los de receta, saldo y cuenta
corriente, `test_logic.py:129-143, 245-250, 1151-1155, 1565-1613, 2073-2092, 2321-2328, 3027-3036, 3090+, 3343`,
`test_casos_5_10.py:11-31`, `test_descuento_entrega.py:74-81`,
`test_receta_marcas.py:224, 261, 277`, `test_direccion_envio.py:13-27`,
`test_horarios.py:87-121`, `test_cotizacion_varios.py` y
`test_pedido_operador.py:60`.

### 6.4 Regresión

La suite completa con el perfil farmacia (sin `VERTICAL`) en verde: los 933 de
hoy más los nuevos. Después, una corrida con `VERTICAL=petshop` solo sobre
`test_perfil.py`, `test_petshop*.py` y `test_entrega_sucursal.py`.

### 6.5 Pruebas manuales con el LLM real

Los tests de webhook usan un `_Intent` falso: ninguno ejercita la calidad del
prompt. `/simulate` devuelve la intención pero no `por_sintoma`, y además no
aplica las compuertas ni la corrección de §5. Por eso se agrega
`scripts/probar_prompt_petshop.py`: con `VERTICAL=petshop`, pasa una lista de
frases por `procesar_rapido` y `procesar` (Haiku y Sonnet) e imprime
`intencion`, `entidad_producto`, `entidades_adicionales`, `por_sintoma` y
`respuesta`. Lista mínima y resultado esperado:

| Frase | Esperado |
|---|---|
| "hola" | Saludo de Mascotas del Oeste, sin nombre propio |
| "¿sos un bot?" | "Soy el asistente virtual de Mascotas del Oeste" |
| "hola, tenés royal canin?" | `consulta_stock`, entidad "royal canin" |
| "mi perro vomita, que le doy?" / "mi gato tiene diarrea" / "cuantas gotas le pongo" / "¿cuánto Drontal le doy?" / "pasame con el veterinario" | `por_sintoma` true y sin productos |
| "una pipeta para perro de 10 kg" / "algo para las pulgas" / "tenés pipeta Frontline 10-20 kg" | `por_sintoma` false |
| "que alimento le doy a un cachorro" / "qué alimento para gato castrado" | `consulta_abierta`, `por_sintoma` false |
| "alimento royal canin y piedras sanicat" | entidad "alimento royal canin", adicionales ["piedras sanicat"] |
| "tenés alimento para gato?" sin resultados | No ofrece productos para perro |
| "anotalo a mi cuenta" / "¿tienen descuento?" | No promete ni inventa; "te paso con alguien del equipo" |
| "donde queda la sucursal?" sin dato | No inventa la dirección |

Después, en el número de prueba de MO, por WhatsApp real: la compra completa
con retiro y con envío, un síntoma, una foto de un producto, una foto de una
indicación veterinaria, un link de Instagram y "¿en qué sucursal puede ser?" en
la elección de entrega.

## 7. Despliegue y vuelta atrás

### 7.1 Orden

Primero **solo MO**: su servicio de Railway despliega `feature/vertical-petshop`.
La farmacia y la mutual siguen en `develop` hasta §7.6.

### 7.2 Variables de entorno del servicio de MO

- `VERTICAL=petshop`.
- `COMERCIO_NOMBRE`: vacío (sale "Mascotas del Oeste"). Solo se carga para
  otro nombre visible.
- `SKU_CSV_PATH=""`. El default (`config.py:30`) es el CSV de la farmacia:
  el guardia lo ignora, pero loguea un error en cada arranque.
- `REDIS_URL` y `DATABASE_URL` propios. `bot:config`, `bot:hours`,
  `blob:catalogo` y `blob:socios` no tienen namespace: compartidos, MO
  heredaría textos, horario, padrón y descuentos de la farmacia.
- `PUBLIC_BASE_URL` con el host de MO, `WHATSAPP_VERIFY_TOKEN` propio (el
  default es `farma_verify_token`) y las claves de Mercurio y del proveedor de
  pago de MO.
- `MERCURIO_PEDIDOS_ENABLED=false` (el default) hasta completar la lista de
  §7.7. Con el flag apagado no se manda ningún pedido al ERP.

### 7.3 Configuración desde el panel o la API (antes de abrir el número)

1. Horario real de la sucursal piloto: `PUT /bo/config/hours` con
   `enabled=true`, y verificar con `GET /bo/config/hours`. Sin esto el bot
   promete el horario de `DEFAULT_HOURS` (L a V 9 a 18, sáb 9 a 13) en la
   respuesta de horario, en las confirmaciones de MP y Payway y en el aviso de
   pedido listo. Y con `enabled=false`, `is_open_now` siempre da True.
2. `retiro_sucursal` (nombre de la sucursal piloto) y `retiro_info_message`
   (con la dirección y el horario reales) por `PATCH /bo/config`.
3. `pedido_listo_retiro_message` con el nombre de la sucursal, partiendo del
   texto petshop de §3.4.
4. `pickup_minutes`: el valor que defina MO (`"0"` apaga "Tiempo estimado: 30
   min").
5. `payment_provider` de MO y, si cobra con MP, `pago_mp_manual="false"`.
6. `efectivo_enabled="false"`: los pedidos en efectivo no llegan a Mercurio.
7. Revisar la tabla `config` y el hash `bot:config` de MO, y **borrar** las
   claves de texto guardadas con 💊 (`pedido_listo_*`, `efectivo_*` u otras de
   §3.4). Lo guardado gana sobre el perfil.
8. No cargar padrón, empleados ni KB de la mutual. El panel rechaza
   `catalogo_fuente="csv"` y la importación de CSV/PDF en petshop (§4.8); si
   la config de MO ya tenía guardado `catalogo_fuente="csv"`, volverlo a
   `erp`: `aplicar_fuente` no lo aplica, pero bloquea la recarga del sync.
9. Opcional, para que el panel no muestre "requiere receta" en filas viejas:
   `UPDATE catalog_items SET requiere_receta='no' WHERE branch_id='mascotas-oeste'`.

### 7.4 Verificación

- El log del arranque dice `Perfil de rubro: petshop (Mascotas del Oeste)` y
  "Perfil sin recetas" / "Perfil sin socios".
- `/bo/catalogo/estado` muestra `fuente="erp"` y `total_productos > 0` (el
  primer sync tarda unas 47 páginas).
- `GET /bo/config` sin 💊 en ningún texto.
- Correr `scripts/probar_prompt_petshop.py` y las conversaciones reales de §6.5.
- Recién después, abrir el número.

### 7.5 Vuelta atrás

`VERTICAL=farmacia` en el servicio de MO y reiniciar. Vuelve exactamente al
comportamiento que MO tiene hoy: prompt y textos de farmacia, recetas,
socios, `catalogo_csv_base=True` (si falla el sync de Mercurio, vuelve a ofrecer
el CSV de la farmacia). Los textos guardados en la config de MO se mantienen.
Si se quiere volver también el código, redeploy del commit anterior en Railway.

### 7.6 Merge a `develop` (farmacia y mutual, después del go-live de MO)

Antes de mergear:
- Confirmar en Railway que Remedia y CERCA tienen `VERTICAL` en minúsculas o
  sin setear. Hoy "Mutual" con mayúscula se coacciona a farmacia, y con la
  normalización pasaría a mutual.
- Confirmar que su `PUBLIC_BASE_URL` es un host bajo `remedia.ar` (§4.3).
- Suite completa en verde y aviso al equipo de la farmacia con la lista de
  "Aviso del merge" (abajo): §5 y todo lo demás que también la afecta.
- La rama al día con `develop` (ya trae 07e3aa2, operadores), `alembic heads`
  con una sola cabeza (`0019`) y la suite completa corrida sobre el resultado
  del merge.

Migraciones (7/10, hallazgos 1 y 13 de la revisión final). `develop` agregó
su propia `0018` (operadores) y la migración de pedidos de la rama, que
también era `0018`, pasó a `0019_orders_y_mercurio_codigos.py` (revision
`0019`, down_revision `0018`). La `0019` se auto-repara: la base de MO quedó
estampada en `0018` con la vieja migración de pedidos (tiene `orders` y
`mercurio_codigos`, no tiene `operadores`), y la `0019` corre el upgrade() de
`0018_operadores.py` si falta esa tabla y después crea lo suyo con
`IF NOT EXISTS`. Farmacia y mutual, en `0017` o en `0018` (operadores), llegan
solas a `0019`. No hace falta `alembic stamp` a mano en ningún servicio.

Verificación después de cada deploy, en los tres servicios (MO al desplegar
esta versión de la rama; farmacia y mutual al desplegar `develop` con el
merge):
- `GET /health` devuelve `"db": "0019"`. Si dice `0017` o `0018`, la
  migración no se aplicó: buscar en el log del arranque "No se pudieron
  aplicar migraciones Alembic".
- Las tablas `orders` y `operadores` existen: en la consola de Postgres del
  servicio, `SELECT to_regclass('orders'), to_regclass('operadores');` no
  devuelve ningún NULL.
- El índice único de pagos existe: `SELECT to_regclass('ux_orders_payment');`
  no devuelve NULL. Si da NULL, la base ya tenía pagos repetidos y la `0019`
  no lo creó (su NOTICE no llega a ningún log; el arranque deja un WARNING "La
  tabla orders NO tiene el índice único ux_orders_payment"). Buscar los
  duplicados con `SELECT payment_id, count(*) FROM orders WHERE payment_id IS
  NOT NULL GROUP BY payment_id HAVING count(*) > 1`, dejar una sola orden por
  pago (a mano, mirando cuál se confirmó) y crear el índice: `CREATE UNIQUE
  INDEX ux_orders_payment ON orders (payment_id) WHERE payment_id IS NOT
  NULL; DROP INDEX IF EXISTS ix_orders_payment;`.

Pedidos copiados a Postgres en todos los perfiles (ruling del 7/10, hallazgo
15). El write-through de pedidos a la tabla `orders` (`OrderService.create` y
`_save` → `_persistir`) corre en TODOS los perfiles, también en la farmacia y
la mutual, y no depende de `MERCURIO_PEDIDOS_ENABLED`: protege los pedidos
cobrados ante una pérdida de Redis (TTL de 7 días, reinicio o evicción). Es un
cambio explícito para la farmacia. La tabla guarda el JSON completo del pedido
(teléfono y dirección incluidos) y **la política de retención queda
pendiente**. El aviso del merge al equipo de la farmacia tiene que decir: "los
pedidos se copian a Postgres (tabla orders)". No es solo una copia: desde el
hallazgo 11 también decide si la orden se crea. `create` escribe en Postgres
antes que en Redis, y un pago que ya tiene orden en la tabla corta la creación
(`PedidoDuplicado`): el cierre responde "duplicado" sin crear otra orden ni
confirmar de nuevo.

Consola de pedidos (revisión final, hallazgo 9). `/orders/api/list` y
`/orders/api/{id}` traen, en todos los perfiles, cinco campos más del alta en
el ERP (`erp_estado`, `erp_ultimo_error`, `erp_intentos`, `erp_numero`,
`erp_id_comprobante`), leídos de `orders` con una consulta por llamada (tope
de 3 s). En la farmacia van en null (y `erp_intentos` en 0). Es aditivo:
ningún campo existente cambia. También va en el aviso del merge.

**Aviso del merge al equipo de la farmacia.** Todo lo que la rama cambia
también en la farmacia (y la mutual), con su commit (`git log --grep
FARMACIA`):
1. Entrega (§5): una pregunta en `esperando_entrega` o `esperando_confirmacion`
   ("¿en qué sucursal puede ser?") se contesta y no elige ni confirma; "¿cuánto
   sale el envío?" eligiendo la entrega lo contesta el modelo; "¿hasta qué
   hora puedo retirar?" eligiendo la entrega contesta el horario y vuelve a
   ofrecer la elección; la sucursal de retiro cargada aparece en la pregunta,
   el link y el horario.
2. Pesos y presentaciones (§5): un peso o un envase no es un domicilio ("la
   bolsa de 15 kg"; "la de 20": 3f65551); `entidad_contradice_pendiente`
   compara presentaciones con unidad ("el de 3 kg" sobre "15KG", dosis bajo 1
   mg). La L de depto o lote sigue siendo domicilio ("Mz 5 L 12", "Corrientes
   1234 4 L": 19a2d03; "Corrientes 1234 piso 3 L", "1234 - 4 L", "1234,4 L":
   ronda 2).
3. Un número nunca es aceptación pura (52a832b): "sí, la de 3" va al modelo en
   lugar de confirmar el pendiente por el atajo. La regla es de
   `_es_afirmacion_pura`, así que también alcanza a la respuesta a "¿querés que
   lo consulte con el equipo?" (`acepta_consulta_ofrecida`): "si, 2", "sí, 1" o
   "si, 600" ya no derivan como `sin_stock` y van al modelo como charla nueva;
   a la oferta del oficial de préstamos de la mutual ("si, 12" ya no deriva);
   y al "sí" con adicionales u opciones mostradas. "sí", "dale" y "si dale"
   siguen igual.
4. Atajo de confirmación con palabra de entrega (ronda 2): un número que no es
   del pendiente ("si, el de 400, lo retiro" con Ibuprofeno 600, "si, el x 30,
   lo retiro" con Curflex x 60) manda el mensaje al modelo en lugar de
   confirmar el pendiente.
5. Derivación prometida: en `esperando_entrega` y `esperando_direccion`, "te
   paso con alguien del equipo" del modelo ahora deriva de verdad (pasa a
   operador sin soltar el pedido, 4014d90); y en todos los caminos, "ya te
   paso el link" o "ya te paso los datos" ya no cuenta como derivación (ronda
   2).
6. Pedidos en Postgres (b97104b, d8d4412): los pedidos se copian a la tabla
   `orders`, y un mismo pago crea una sola orden aunque Redis se pierda: índice
   único `ux_orders_payment` (0019), `find_by_payment` consulta `orders` cuando
   Redis no tiene el pago (o falla), y una renotificación de MP o un cobro
   repetido de Payway responde "duplicado" sin crear otra orden ni mandar otra
   confirmación. Payway guarda el id de pago vacío (no "None") si el cobro no
   trae id. El arranque avisa (WARNING) si falta el índice único (ronda 2).
7. Renglones del pedido (c28cb00): la preferencia de MP lleva `metadata` con
   los renglones, el pago pendiente de Payway guarda `items` y `costo_envio`, y
   las órdenes cobradas online guardan `items` y `costo_envio` en su JSON
   (visibles en la API de pedidos). El cobro, el link y los montos no cambian.
8. Cotización de un producto x N desde el panel (`/bo/paylink`, modo
   "cotizar", ronda 2): la cotización sigue diciendo el total, pero al
   confirmar el link dice "<producto> xN ($total)" y la preferencia de MP sale
   N x unitario (antes 1 x total).
9. Consola de pedidos (f7ffd85): `/orders/api` trae los cinco campos `erp_*`
   (null en la farmacia) y `/bo/mercurio/estado` el bloque `pedidos`. La tabla
   `orders` suma las columnas `erp_proximo_intento` y `erp_actualizado_at`
   (null en la farmacia).

### 7.7 Alta de pedidos en Mercurio (F5): antes de prender `MERCURIO_PEDIDOS_ENABLED`

Revisión final del 7/10 (hallazgos 5 a 12). El alta de un pedido cobrado en el
ERP (`POST /pedidos`) queda detrás de `MERCURIO_PEDIDOS_ENABLED` y sale apagada.

**Antes de prender MERCURIO_PEDIDOS_ENABLED**:
1. Confirmar con el proveedor (mail del 14/9,
   `docs/superpowers/specs/2026-09-14-mercurio-api-v1.md`):
   - `state`: qué valor corresponde a un pedido cobrado y sin preparar. El
     default de `MERCURIO_PEDIDO_STATE` es `"complete"` y podría darlo por
     cerrado.
   - `customer_id`: un cliente genérico para los compradores sin alta, o el
     DNI del comprador (hoy el bot no lo pide).
   - `payment_details` y el medio de pago (tarjeta online por Payway o MP):
     hoy el POST no los lleva.
   - Datos de entrega: dirección y teléfono del comprador en los envíos (hoy
     no van), y cómo se informa el envío (asumimos `shipping_total`; si el ERP
     lo ignora o lo rechaza, los pedidos con envío quedan `rechazado`).
   - Qué depósito descuenta el stock del pedido (1, 4 o 27).
   - Qué significa un 409 en `POST /pedidos` (el contrato no lo documenta). Hoy
     se trata como rechazo definitivo, pero puede ser la respuesta a una
     `Idempotency-Key` "en proceso" después de un timeout: ver "cargar a mano"
     abajo.
2. Cargar `MERCURIO_CUSTOMER_ID_DEFAULT` con el valor que indique el
   proveedor. Sin él no se manda ningún pedido: quedan `pendiente` con
   "customer_id sin configurar" hasta vencer. También `MERCURIO_API_KEY` (la
   productiva). `MERCURIO_PEDIDOS_MAX_DIAS` queda en 6 (nunca 7 o más).
3. Al arrancar, el log no muestra ninguna línea "Alta de pedidos en el ERP:"
   como ERROR (falta de clave, de customer_id o un tope de días que alcanza
   los 7 de la Idempotency-Key).
4. Probar en preproducción una venta con retiro y una con envío, y un carrito
   de dos productos. Verificar el pedido en el ERP (renglones, cantidades,
   envío, total) y que un `id` mayor a 2^31-1 no da error (el `id` sale de
   32 bits del sha256 de la order_id).
5. Después de la primera venta real, mirar `GET /bo/mercurio/estado`: el
   bloque `pedidos` tiene que mostrar `habilitado: true`,
   `customer_id_default: true`, 0 rechazados y 0 vencidos, y el pedido con
   `erp_estado: "enviado"` en la API de pedidos (`/orders/api/{id}`).
6. Decidir quién se entera de un `rechazado` o un `vencido`. Ninguna pantalla
   del repo los muestra ni avisa (ver "Dónde se ven" abajo). **Pendiente del
   portal** (panel de Lovable, fuera de este repo): mostrar `erp_estado` en la
   lista de pedidos (lo trae `/orders/api/list`) y alertar cuando suben
   `rechazados`, `vencidos` o `pendientes_mas_1h` en `/bo/mercurio/estado`.
   Mientras no esté, alguien revisa `GET /bo/mercurio/estado` todos los días.

**Cómo funciona** (detalle en los docstrings de `mercurio_pedidos.py` y
`order_store.py`):
- La orden cobrada nace `pendiente` en la tabla `orders`. El webhook de MP y
  `/payway/charge` confirman al cliente y programan el alta en segundo plano
  (`programar_alta_erp`): ninguno espera al ERP. Si el proceso se reinicia en
  el medio, el job de reintentos (cada `MERCURIO_PEDIDOS_RETRY_SECS`, 300 s)
  la retoma con la misma `Idempotency-Key` (la `order_id`). El job saltea los
  pedidos que el hook está mandando en ese proceso, y el hook no vuelve a
  mandar (ni reabre) un pedido que el job ya dejó `enviado`, `rechazado` o
  `vencido` (solo encola uno sin estado o `pendiente`).
- Respuestas del ERP: 422, 400, 404, 409 y 413 → `rechazado` (no se
  reintenta). 401 y 403 → error de credencial: el job corta la pasada sin
  gastar intentos y deja un ERROR. 429, 5xx, red, o un 2xx sin
  `id_comprobante` → sigue `pendiente`, con backoff por pedido: el próximo
  intento es `now() + min(300 s × 2^(intentos-1), 6 h)`.
- Un `pendiente` con más de `MERCURIO_PEDIDOS_MAX_DIAS` (6) pasa a `vencido` y
  no se reintenta más: la `Idempotency-Key` dura 7 días y un reintento
  posterior podría duplicar el pedido.
- Estados (`erp_estado`): NULL (no aplica), `pendiente`, `enviado`,
  `rechazado`, `vencido`. **Dónde se ven**: en el log (cada `rechazado` o
  `vencido` deja un ERROR), en un evento de métricas `erp_pedido_rechazado` /
  `erp_pedido_vencido` (tabla `eventos`, `ref` = order_id; hoy ningún tablero
  lo lee), en `/bo/mercurio/estado` (contadores y el último error, ordenado
  por `erp_actualizado_at`, la fecha del último cambio del alta) y en los
  campos `erp_estado`, `erp_ultimo_error`, `erp_intentos`, `erp_numero` y
  `erp_id_comprobante` de la API de pedidos (`/orders/api/list` y
  `/orders/api/{id}`). Ninguna pantalla del repo (ni `orders.html` ni el
  tablero) los muestra: es el pendiente del portal del paso 6.
- Un `rechazado` o un `vencido` lo carga a mano un humano en el ERP: no hay
  reintento manual. **Antes de cargarlo, buscarlo en el ERP por `number` =
  `order_id`**: pudo haber entrado igual (un timeout, un 2xx sin
  `id_comprobante` o un 409 de una `Idempotency-Key` en proceso), y cargarlo
  otra vez lo duplicaría (doble descuento de stock y doble comprobante).
- Un pedido con un ítem que no es un artículo del ERP (SKU sintéticos:
  `MANUAL` de un link o una cotización por monto libre del panel, ítems libres
  `LIBRE1`, `LIBRE2`... del panel y `TEST` de `/payway/test`) queda
  `rechazado` de entrada, sin POST, con "ítem sin artículo del ERP (<sku>):
  cargar a mano" (ronda de arreglo 2; antes quedaba `pendiente` 6 días y
  vencía). En un carrito mixto se carga a mano el pedido entero.

## 8. Fuera de alcance

Lista aprobada:
- Plantillas de WhatsApp para la ventana de 24 h.
- Guarda de línea (`phone_number_id`).
- Historial honesto de envíos fallidos.
- **Tramo 2, calidad de búsqueda petshop**: indexar `category`, kg/kilos,
  números de 1 dígito y "por NN" → `fps`. Se suman explícitamente: unidades de
  petshop (latas, bolsas, sobres), el borrado de "caja de" y "tiras de"
  (`sku_service.py:99-167`), y el caption de la foto que pisa `img["items"]`
  (`webhook.py:1014`; en el camino Meta ni se parsea).
- Ocultar las pantallas de receta del panel, en el código del panel (Lovable, fuera de este repo). El backend solo expone `GET /bo/perfil` (§3.6, decisión del 7/10) para que el panel decida (incluye `cotizar_receta` →
  "Sale por obra social $X", el OCR del panel y los textos `receta_cotizacion_*`).
- Bugs de regex de retiro y envío que no sean el caso de la pregunta ("busco",
  "paso", "voy", "casa"; `_RETIRO`, `_ENVIO`, `afirma_envio`).
- Efectivo y cuenta corriente sin alta en el ERP (incluye que `backoffice_pedidos`
  pueda cerrar en cuenta corriente con 💊 sin mirar la capacidad).

No están en la lista literal, pero quedan afuera porque el diseño no los pide
(validado por el usuario el 6/10). `_NO_DIR` con kg y
`entidad_contradice_pendiente` estaban acá y **pasaron al go-live** (§5):
- Antifraude de Payway: el email de respaldo `cliente@remedia.ar`,
  `dispatch_method` fijo en "Store Pick Up" y X-Source `remedia`.
- "Tiempo estimado: 30 min" en el aviso de pedido listo (se mitiga con
  `pickup_minutes="0"`).
- Falsos positivos de `_PREGUNTA_HORARIO` ("¿la bolsa está abierta?").
- La raíz `/` (`app/static/index.html`) sigue mostrando el simulador con la
  marca Remedia en el host de MO.
- `/simulate`: no aplica las compuertas de salud, `/simulate/image` responde
  "Recibí la receta" y su `esperando_entrega` (`simulate.py:139-153, 211-231`)
  conserva el bug de la pregunta.
- El rótulo de KB de la mutual: `SYSTEM_PROMPT_MUTUAL` espera
  `[INFORMACIÓN DE LA MUTUAL]` y recibe `[INFORMACIÓN DE LA FARMACIA]`.
- `sintoma_farmaceutico_message` vacío vuelve al texto por defecto en la
  farmacia (solo un espacio lo apaga).
- `personalizar_nombre` no limpia "¡Listo {nombre}!" en la farmacia.
- La respuesta de una foto mientras atiende un operador.
- El prompt de farmacia sigue diciendo "link real de Mercado Pago" aunque
  cobre con Payway.
- Las claves `mutual_*` en el `GET /bo/config` de MO, la pastilla y las
  secciones de farmacia en `tablero.html`, y `SOCIOS_AREA_DEFAULT=341`.
- `consulta_salud_message` e `indicacion_veterinaria_message` no se editan
  desde el panel (no entran en `ConfigUpdate`).

No aplica a petshop:
- Redis compartido con la farmacia: es un error de despliegue y lo cubre §7.2.
- `POST /bo/kb/cargar-mutual` (`backoffice.py:1814-1840`): ningún flujo lo
  llama.
- Mensajes `interactive`, `button` u `order` descartados: el bot de MO solo
  manda texto y el catálogo sale de Mercurio.

## 9. Riesgos

### 9.1 Decisiones tomadas al consolidar

Validadas por el usuario el 6/10: la frontera de salud del punto 7 ("algo para
las pulgas" se vende), el descuento de empleado apagado del punto 2 (MO no lo
necesita) y el punto 10 (los dos bugs de pesos **entran al go-live**, §5). El
resto son decisiones de diseño que se revisan en el spec.

1. **Campos agregados al `Perfil`**: `venta`, `catalogo_csv_base`,
   `descriptor_tarjeta`, `razon_social` y `wordmark_html` (§3.7). Sin ellos, o
   se pregunta por el nombre del rubro o la farmacia cambia. Con
   `COMERCIO_NOMBRE` seteado, los tres campos de marca se vacían y todo sale del
   nombre.
2. **El descuento de empleado se apaga con `socios=False`**. El diseño solo
   nombra el de socio; el de empleado (20% por default) es regla de la
   farmacia. Si MO quiere descuento para sus empleados, hace falta un campo
   nuevo.
3. **`pago_mp_manual`** (clave de config nueva): sin ella, si MO cobra con MP,
   "¿puedo pagar con Mercado Pago?" deriva o contesta "solo tarjeta".
4. **La corrección de §5 se extiende** al interceptor de horario y a
   `esperando_confirmacion` (el mismo bug, en el estado vecino), y en
   `esperando_entrega` "¿cuánto sale el envío?" pasa a contestarlo el modelo.
   Los tres cambian a la farmacia.
5. **El signo "?" solo no alcanza para que sea pregunta** (§5), para que
   "¿me lo podés enviar?" siga eligiendo envío.
6. **Reglas propias del prompt petshop** que el diseño no dicta literalmente:
   no ofrecer productos para otra especie; no prometer "anotarlo", fiado ni
   pago diferido; no afirmar descuentos, promos ni cupones; no inventar la
   dirección ni el horario; y responder "te paso con alguien del equipo" para
   que la derivación se cumpla.
7. **Frontera de salud**: pedir por nombre o tipo vende ("algo para las
   pulgas", "pipeta para perro de 10 kg"); contar un síntoma, pedir una dosis o
   pedir un veterinario deriva. Hay que validarlo con MO: si prefieren derivar
   también "algo para las pulgas", solo cambia el ejemplo del prompt.
8. **Sin cambio en `_HUMANO`**: "pasame con el veterinario" se cubre por prompt
   y compuerta A, para no tocar la regex de la farmacia.
9. `consulta_saldo_message` y `cc_no_habilitada_message` conservan su fallback
   literal (§3.4), para que el `get_all()` de la farmacia no cambie.
10. `_NO_DIR` con kg y `entidad_contradice_pendiente`: el usuario decidió que
    entran al go-live (§5, "Correcciones de pesos y presentaciones").

### 9.2 Riesgos

- **Ciclo de imports**: si `perfil.py` importa `intent_service`, `sku_service`,
  `image_service` o `checkout_helper`, el arranque falla. Por eso existe
  `prompts.py` y las marcas de audio viven en `perfil.py`.
- **Caché en tests**: `get_perfil` y `get_settings` usan `lru_cache`. Un test
  que no use la fixture de §6.1 deja el perfil petshop para el resto de la
  suite. Si un módulo guarda el perfil en una variable de módulo o en un
  `__init__`, los tests no pueden cambiarlo.
- **Calidad del prompt**: la derivación depende de que el modelo marque bien
  `por_sintoma`. Un falso positivo deriva (falla segura, con costo operativo).
  Un falso negativo cae en la venta, y ahí solo protegen el prompt y el control
  de precios y nombres inventados. Mitigación: §6.5 antes de abrir.
- **Con `socios` y `obras_sociales` apagados**, "descuento", "promo", "bono" y
  "cupón" van al modelo. El interceptor de descuentos se había puesto porque el
  modelo inventó un descuento (caso 29). El guard de precios ataja importes,
  pero no frases como "tenés 10% off": solo lo frena la regla nueva del prompt.
- **Config guardada en MO**: lo guardado pisa a `perfil.textos`. Si quedaron
  textos con 💊 de una corrida anterior, MO los sigue mandando (§7.3, paso 7).
- **Horario, demora y sucursal sin cargar**: el bot promete `DEFAULT_HOURS` y
  30 minutos, y ante "¿dónde queda la sucursal?" deriva sin dato. Es un paso
  obligatorio del despliegue.
- **Filas viejas en Postgres de MO** con `requiere_receta` en `si` o `ambiguo`:
  el bot las ignora por los gates, pero el panel las muestra (§7.3, paso 9).
- **Primer arranque de MO**: hasta que termina el primer sync de Mercurio, el
  catálogo está vacío y el bot dice que no tiene nada.
- **`contiene_link` de la farmacia** depende de su `PUBLIC_BASE_URL` (§4.3).
- **Normalización de `VERTICAL`** con `strip().lower()` (§7.6).
- **El rótulo de KB de la mutual** queda con el bug de hoy, para no cambiarla;
  corregirlo es una decisión aparte.
- **`statement_descriptor`**: no está verificado qué largo y qué caracteres
  acepta MP para "MASCOTAS DEL OESTE" (18 caracteres); hay que confirmarlo en
  la primera venta real.
- **Frontera de la visión**: una indicación del veterinario que nombra
  productos que MO vende se deriva igual; una caja de pipeta con sticker de
  veterinaria puede caer en cualquiera de los dos tipos. Hay que probarlo con
  fotos reales de MO.
- **Fricción en `esperando_confirmacion`**: "dale, retiro. ¿a qué hora paso?"
  ya no confirma; contesta y vuelve a preguntar "¿Lo confirmamos?".
- **Un pendiente queda en la sesión** al derivar por salud con un link ya
  enviado. Si el cliente paga, el pago sigue su curso normal (igual que con
  `pidio_humano`).
- **La compuerta A va antes de la KB**: una consulta de salud nunca se responde
  con la información del comercio, aunque MO cargue "tenemos veterinario".
- **El panel real (Lovable) está fuera del repo**: las claves nuevas
  (`retiro_sucursal`, `retiro_info_message`, `pago_mp_manual`) hay que
  exponerlas allá o cargarlas por API en el despliegue.
