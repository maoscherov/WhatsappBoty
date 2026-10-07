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
    datos con la farmacia). `armar` fija el perfil con `usar_perfil` antes de armar
    el entorno; la config falsa sale de DEFAULTS (el fixture `entorno` usa
    `_Cfg(dict(DEFAULTS))`), no del perfil."""
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
