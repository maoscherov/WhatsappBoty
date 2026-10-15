"""
Arreglos de 48 h de la minuta del 14/10, con conversaciones reales del 14/10:
C-5000 (pedido confirmado a cuenta corriente que no se armaba), derivación
que vencía a las 24 h, C-4996 / C-5135 / C-5188 ("lo consulto" sin derivar),
C-4993 (producto fantasma) y los arreglos que no alcanzaron (C-5228, C-5084,
C-5051, C-5135 "La Puissange", C-5034).
"""
import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as ch
from app.services.sku_service import SKUService
from tests.test_webhook_secuencias import (PHONE, _Cfg, _Img, _Intent, _Rag, _Rec, _Socios,
                                           _Wa, _msg)

_FILAS = [
    ("1", "ADERMICINA CUIDADO FACIAL antiar. OFERTA CRE x 100", 9989.87, "Adermicina", 5),
    ("2", "ADERMICINA CRE x 30", 23590.07, "Adermicina", 3),
    ("3", "FRUCTIS CONT Y DEFI LISO POD. SHA x 650", 12000.0, "Fructis", 0),
    ("4", "LA PUISSANCE GEL ULTIMATE FIX ENV x 200", 18527.52, "La Puissance", 4),
    ("5", "LA PUISSANCE GEL WET LOOK ENV x 200", 17036.8, "La Puissance", 3),
    ("6", "MEDIAS SOQUETE HOMBRE ENV x 1", 2922.98, "Medias", 2),
    ("7", "MEDIAS MUJER CORTAS LISAS ENV x 1", 1916.64, "Medias", 2),
]


def _sku():
    return SKUService.from_rows([
        {"external_id": i, "name": n, "price": p, "brand": m, "stock": st,
         "category": "Perfumeria", "requiere_receta": "no"} for i, n, p, m, st in _FILAS])


@pytest.fixture
def entorno(monkeypatch):
    from app.services.session_service import SessionService

    def armar(guion=None):
        deps = {
            "wa": _Wa(), "sku": _sku(),
            "session": SessionService("redis://127.0.0.1:1"),
            "intent": _Intent(guion or {}), "payment": _Rec(), "audio": _Rec(),
            "image": _Img(), "perf": _Rec(), "config": _Cfg(),
            "socios": _Socios(), "msgs": _Rec(), "metrics": _Rec(), "rag": _Rag(),
        }
        monkeypatch.setattr(wh, "_deps", lambda s=None: deps)
        return deps

    return armar


def _ultimo(deps):
    return deps["wa"].enviados[-1]


# ── 1. C-5000: pedido confirmado que no se armaba ───────────────────────────────
def test_si_la_quiero_llevar_confirma():
    assert wh._es_afirmacion_pura("si, la quiero llevar")
    assert wh._es_afirmacion_pura("sí, me lo llevo")
    assert not wh._es_afirmacion_pura("Ok ok gracias")          # C-5084: acuse, no un sí
    assert not wh._es_afirmacion_pura("bueno gracias")
    assert wh._es_afirmacion_pura("dale, gracias")


async def test_no_gracias_a_las_alternativas_no_borra_el_pedido(entorno):
    deps = entorno()
    ss = deps["session"]
    await ss.set_pending(PHONE, "1", "ADERMICINA CUIDADO FACIAL antiar. OFERTA CRE x 100", 7991.9,
                         1, opciones=[])
    await ss.set_estado(PHONE, "esperando_entrega")
    s = await ss.get(PHONE)
    s["history"] = [{"role": "user", "content": "y adermicina A tenes"},
                    {"role": "assistant", "content": "Justo no tengo stock de la Adermicina A. Tengo la "
                     "Adermicina Crema de 30 g a $23.590. ¿Te interesa alguna de estas opciones?"}]
    await ss.save(PHONE, s)
    await wh.procesar_mensajes([_msg("no, gracias")])
    s = await ss.get(PHONE)
    assert s.get("pending_sku_id") == "1"
    assert "Seguimos con" in _ultimo(deps)


async def test_no_gracias_al_pedido_si_lo_cancela(entorno):
    deps = entorno()
    ss = deps["session"]
    await ss.set_pending(PHONE, "1", "ADERMICINA CUIDADO FACIAL antiar. OFERTA CRE x 100", 7991.9,
                         1, opciones=[])
    await ss.set_estado(PHONE, "esperando_entrega")
    s = await ss.get(PHONE)
    s["history"] = [{"role": "assistant", "content": "La Adermicina antiarrugas sale $7.991,90. "
                     "¿Retiro en sucursal o envío?"}]
    await ss.save(PHONE, s)
    await wh.procesar_mensajes([_msg("no, gracias")])
    assert not (await ss.get(PHONE)).get("pending_sku_id")


async def test_me_llevo_solo_x_a_la_cuenta_recupera_el_pedido(entorno, monkeypatch):
    deps = entorno()
    ss = deps["session"]
    await ss.set_pending(PHONE, "1", "ADERMICINA CUIDADO FACIAL antiar. OFERTA CRE x 100", 7991.9,
                         1, opciones=[])
    await ss.clear_pending(PHONE)

    async def _cc(*a, **k):
        return {"nombre": "Gasperi"}
    monkeypatch.setattr(wh, "habilitado_cc", _cc)
    entregas = []

    async def _entrega(deps_, phone, session, texto, tipo):
        entregas.append((session.get("pending_sku_id"), tipo))
        return "✅ ¡Listo! Quedó cargado a tu cuenta corriente", "pedido_confirmado"
    monkeypatch.setattr(wh, "_entrega_dicha", _entrega)
    await wh.procesar_mensajes([_msg(
        "me llevo solo la adermicina antiarrugas, me la pones en la cuenta, la paso a buscar")])
    assert entregas == [("1", "retiro")]


# ── 2. La derivación sin atender no vence en el día ─────────────────────────────
async def test_derivada_no_vence_a_las_24_horas():
    from app.services import session_service as ssm
    ss = ssm.SessionService("redis://127.0.0.1:1")
    usados = []

    class _R:
        async def setex(self, key, ttl, val):
            usados.append(ttl)

    async def _si():
        return True
    ss._redis, ss._use_redis = _R(), _si
    await ss.save(PHONE, {"history": [], "estado": "operador"})
    await ss.save(PHONE, {"history": [], "estado": "idle"})
    assert usados == [ssm.SESSION_TTL_OPERADOR, ssm.SESSION_TTL]
    assert ssm.SESSION_TTL_OPERADOR >= 7 * 24 * 3600


# ── 3. "Lo consulto" sin derivar ────────────────────────────────────────────────
@pytest.mark.parametrize("txt", [
    "Entiendo. Lo consultaré con el equipo y te aviso.",
    "Coordinaré para que el equipo lo cargue a tu cuenta.",
    "Voy a consultar con el equipo cuándo llegan. Te mantendré informado.",
])
def test_promesas_del_modelo_derivan(txt):
    assert ch.derivacion_prometida(txt)


def test_ofrecer_consultar_no_es_prometer():
    assert not ch.derivacion_prometida("¿Querés que lo consulte con el equipo?")


def test_aceptar_la_consulta_con_tilde():
    assert wh.acepta_consulta_ofrecida("Ok consúltalo gracias")
    assert ch.pide_humano("Consultarle al equipo cuando llegarian")


# ── 4. Producto fantasma (C-4993) ───────────────────────────────────────────────
async def test_no_busca_un_producto_que_no_se_nombro(entorno):
    guion = {"Okey, y realizan entregas a domicilio?": {
        "intencion": "consulta_stock", "entidad_producto": "FRUCTIS CONT Y DEFI LISO",
        "respuesta": "Sí, hacemos envíos a domicilio."}}
    deps = entorno(guion)
    s = await deps["session"].get(PHONE)
    s["history"] = [{"role": "user", "content": "tenes shampoo fructis liso?"},
                    {"role": "assistant", "content": "No me figura el FRUCTIS CONT Y DEFI LISO."}]
    await deps["session"].save(PHONE, s)
    buscados = []
    real = deps["sku"].buscar
    deps["sku"].buscar = lambda q, **k: buscados.append(q) or real(q, **k)
    await wh.procesar_mensajes([_msg("Okey, y realizan entregas a domicilio?")])
    assert buscados == []


def test_entidad_en_mensaje():
    assert ch.entidad_en_mensaje("ibuprofeno 400", "tenés ibuprofeno?")
    assert ch.entidad_en_mensaje("dove original", "y ese en 90 gramos?")      # lo señala
    assert not ch.entidad_en_mensaje("fructis liso", "realizan entregas a domicilio?")


# ── 5. Lo que no alcanzó el 14/10 ───────────────────────────────────────────────
def test_frases_nuevas():
    assert ch.consulta_seguimiento("Hola chicas, ¿llegó mi producto?") == "pedido"     # C-5228
    assert ch.pide_otras_opciones("me podrias dar algunas opciones mas?")              # C-5051


def test_busqueda_tolera_errores_de_tipeo():
    sku = _sku()
    ids = [r["sku_id"] for r in sku.buscar("productos de la marca La Puissange")]
    assert {"4", "5"} & set(ids)                                                       # C-5135
    assert sku.buscar("medias para caballero")[0]["sku_id"] == "6"                     # C-5034


# ── C-5360 (15/10): otro producto mientras elige la entrega ────────────────────
async def _dove_eligiendo_entrega(deps):
    ss = deps["session"]
    await ss.set_pending(PHONE, "20", "DOVE JAB LQ 220 RTO ORIGINAL JLI x 220", 3145.21, 1, opciones=[])
    await ss.set_estado(PHONE, "esperando_entrega")
    s = await ss.get(PHONE)
    s["history"] = [{"role": "assistant", "content": "¿Cómo preferís recibirlo: retiro o envío?"}]
    await ss.save(PHONE, s)


def _sku_kevin(stock_kevin):
    return SKUService.from_rows([
        {"external_id": "20", "name": "DOVE JAB LQ 220 RTO ORIGINAL JLI x 220", "price": 3145.21,
         "brand": "Dove", "stock": 10},
        {"external_id": "21", "name": "KEVIN FEMME EDP SPR x 60", "price": 10548.37,
         "brand": "Kevin", "stock": stock_kevin}])


async def test_otro_producto_eligiendo_la_entrega_se_busca_y_se_suma(entorno):
    q = "Quiero saber el precio del Kevin fem el perfume"
    deps = entorno({q: {"intencion": "consulta_precio", "entidad_producto": "kevin femme perfume",
                        "respuesta": "El Kevin Femme EDP de 60 ml sale $10,548.37."}})
    deps["sku"] = _sku_kevin(3)
    await _dove_eligiendo_entrega(deps)
    await wh.procesar_mensajes([_msg(q)])
    assert "¿Lo sumo a tu pedido" in _ultimo(deps)
    s = await deps["session"].get(PHONE)
    assert s["pending_sku_id"] == "20" and s["_oferta_extra"]["sku_id"] == "21"
    await wh.procesar_mensajes([_msg("sí, sumalo")])
    items = (await deps["session"].get(PHONE)).get("pending_items") or []
    assert {i["sku_id"] for i in items} == {"20", "21"}
    assert "Tu pedido queda así" in _ultimo(deps)


async def test_otro_producto_sin_stock_no_toca_el_pedido(entorno):
    q = "Quiero saber el precio del Kevin fem el perfume"
    deps = entorno({q: {"intencion": "consulta_precio", "entidad_producto": "kevin femme perfume",
                        "respuesta": "No me figura el Kevin Femme en stock. ¿Querés que lo consulte?"}})
    deps["sku"] = _sku_kevin(0)
    await _dove_eligiendo_entrega(deps)
    await wh.procesar_mensajes([_msg(q)])
    assert "No me figura" in _ultimo(deps) and "Seguimos con" in _ultimo(deps)
    assert (await deps["session"].get(PHONE))["pending_sku_id"] == "20"


async def test_gracias_eligiendo_la_entrega_no_repite_el_pedido(entorno):
    deps = entorno()
    await _dove_eligiendo_entrega(deps)
    await wh.procesar_mensajes([_msg("Gracias")])
    assert _ultimo(deps) == "¡De nada! 🙂"
