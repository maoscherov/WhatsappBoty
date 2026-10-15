"""
Revisión de las conversaciones del 15/10: C-5360 (loop del Dove), C-5419
(link sin confirmar), C-5290 / C-5596 (productos fantasma), C-5550 / C-5596
("no te puedo ayudar" sin derivar), C-5425, C-5473, C-5485, C-5455, C-5576,
C-5409.
"""
import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as ch
from app.services.sku_service import SKUService
from tests.test_webhook_secuencias import PHONE, _Cfg, _Img, _Intent, _Rag, _Rec, _Socios, _Wa, _msg


@pytest.fixture
def entorno(monkeypatch):
    from app.services.session_service import SessionService

    def armar(guion=None, cfg=None):
        deps = {"wa": _Wa(), "sku": SKUService.from_rows([
                    {"external_id": "20", "name": "DOVE JAB LQ 220 RTO ORIGINAL JLI x 220",
                     "price": 3145.21, "brand": "Dove", "stock": 10},
                    {"external_id": "30", "name": "AMOR AMOR CACHAREL(M) EST x 1", "price": 155000,
                     "brand": "Cacharel", "stock": 2}]),
                "session": SessionService("redis://127.0.0.1:1"), "intent": _Intent(guion or {}),
                "payment": _Rec(), "audio": _Rec(), "image": _Img(), "perf": _Rec(),
                "config": _Cfg(cfg), "socios": _Socios(), "msgs": _Rec(), "metrics": _Rec(),
                "rag": _Rag()}
        monkeypatch.setattr(wh, "_deps", lambda s=None: deps)
        return deps

    return armar


def _ultimo(deps):
    return deps["wa"].enviados[-1]


# ── Productos fantasma ──────────────────────────────────────────────────────────
def test_entidades_que_no_son_productos():
    assert not ch.entidad_util("el que es como la foto")        # C-5596 → "Como Quieres"
    assert not ch.entidad_util("necesito")                      # C-5290 → Neceser
    assert ch.entidad_util("kevin femme")


def test_la_correccion_de_tipeo_no_toca_palabras_comunes():
    sku = SKUService.from_rows([{"external_id": "1", "name": "Neceser travel 17013 ENV x 1",
                                 "price": 1, "brand": "x", "stock": 1}])
    assert sku._corregir_tipeo("necesito") == "necesito"


# ── "No, gracias" / "no lo quiero" sobre el pedido (C-5360) ─────────────────────
def test_no_al_pedido_que_nombra_el_bot_lo_cancela():
    hist = [{"role": "assistant", "content": "Recordá que tenés el Dove Jabón Líquido Original de "
             "220 ml en tu pedido por $3.145,21. ¿Retiro o envío?"}]
    assert not ch.no_es_sobre_el_pendiente(hist, "DOVE JAB LQ 220 RTO ORIGINAL JLI x 220", 2516.17)
    assert wh._match_no("No lo quiero")
    assert wh._match_no("por ahora no lo voy a llevar")


async def test_no_lo_quiero_cancela_y_no_resucita(entorno):
    deps = entorno()
    ss = deps["session"]
    await ss.set_pending(PHONE, "20", "DOVE JAB LQ 220 RTO ORIGINAL JLI x 220", 2516.17, 1, opciones=[])
    await ss.set_estado(PHONE, "esperando_entrega")
    s = await ss.get(PHONE)
    s["history"] = [{"role": "assistant", "content": "Recordá que tenés el Dove Jabón Líquido Original "
                     "en tu pedido. ¿Retiro o envío?"}]
    await ss.save(PHONE, s)
    await wh.procesar_mensajes([_msg("No lo quiero")])
    assert not (await ss.get(PHONE)).get("pending_sku_id")


# ── Link sin confirmación (C-5419) ──────────────────────────────────────────────
async def test_sin_un_si_no_se_manda_link(entorno):
    deps = entorno({"Busco masculino": {"intencion": "pedido", "confirmacion": True,
                                        "respuesta": "Perfecto, te mando el link."}})
    ss = deps["session"]
    await ss.set_pending(PHONE, "30", "AMOR AMOR CACHAREL(M) EST x 1", 155000, 1, opciones=[])
    s = await ss.get(PHONE)
    s["history"] = [{"role": "assistant", "content": "Tengo el Amor Amor Cacharel. ¿Te gustaría?"}]
    await ss.save(PHONE, s)
    await wh.procesar_mensajes([_msg("Busco masculino")])
    assert (await ss.get(PHONE)).get("estado") not in ("esperando_entrega", "esperando_pago")
    assert "retiro" not in _ultimo(deps).lower()


# ── "No te puedo ayudar" por segunda vez → deriva (C-5550, C-5596) ─────────────
async def test_segunda_vez_sin_poder_ayudar_deriva(entorno):
    deps = entorno({"y con tarjeta de credito en cuotas sin interes?": {
        "intencion": "consulta_abierta",
        "respuesta": "Perdón, no te estoy pudiendo ayudar bien con esto 🙏 ¿Querés que lo vea alguien del equipo?"}})
    s = await deps["session"].get(PHONE)
    s["history"] = [{"role": "user", "content": "algo"},
                    {"role": "assistant", "content": "Perdón, no te estoy pudiendo ayudar bien con esto 🙏 "
                     "¿Querés que lo vea alguien del equipo?"}]
    await deps["session"].save(PHONE, s)
    await wh.procesar_mensajes([_msg("y con tarjeta de credito en cuotas sin interes?")])
    assert (await deps["session"].get(PHONE))["estado"] == "operador"


# ── Formas de pago (C-5550) ─────────────────────────────────────────────────────
def test_formas_de_pago():
    assert ch.pide_formas_de_pago("Que forma de pago tengo?")
    assert ch.pide_formas_de_pago("Puedo pagar con tarjeta?")
    assert not ch.pide_formas_de_pago("puedo pagar con transferencia?")     # va a su flujo
    t = ch.formas_de_pago({"efectivo_enabled": "true", "transferencia_alias": "x"}, "549", None)
    assert "link" in t and "efectivo" in t and "transferencia" in t


async def test_formas_de_pago_en_el_chat(entorno):
    deps = entorno(cfg={"efectivo_enabled": "true"})
    await wh.procesar_mensajes([_msg("Que forma de pago tengo?")])
    assert "Podés pagar" in _ultimo(deps) and "efectivo" in _ultimo(deps)


# ── Otros ───────────────────────────────────────────────────────────────────────
def test_varios():
    assert ch.catalogo_de("quiero ver el catálogo de perfumes") == "perfumes"            # C-5473
    assert ch.derivacion_prometida("No tengo acceso a nuestro catálogo. Te sugiero que consultes "
                                   "con nuestro equipo.")                                  # C-5425
    assert not ch.pide_foto("Ahora les paso una foto")                                    # C-5485
    assert ch.ofrece_mandar_receta("¿Puedo enviarte una receta digital?")                 # C-5455


def test_obra_social_no_es_el_descuento_de_empleado():
    assert ch.pregunta_obra_social("Con Jerárquicos PMI 2000 qué descuento tiene")       # C-5576


def test_no_saluda_con_la_inicial_guardada_en_la_base():
    from app.services.socio_service import SocioService
    svc = SocioService.__new__(SocioService)
    svc.find_by_phone = lambda p: {"nombre": "4 S Desarrollos Srl", "nombre_pila": "S",
                                   "apellido": "", "nro_socio": ""}
    assert "S" not in svc.contexto_para_prompt("549").split(":")[1][:3]


def test_busqueda_de_perfumes_por_genero_y_carteras():
    sku = SKUService.from_rows([
        {"external_id": "1", "name": "AB THE ICON FOR MEN EDT LOC x 100", "price": 86741, "brand": "x", "stock": 11},
        {"external_id": "2", "name": "GINO BOGANI P/FEMME EDP SPR x 60", "price": 40360, "brand": "y", "stock": 2},
        {"external_id": "3", "name": "Amayra Cartera 00351 ENV x 1", "price": 62794, "brand": "z", "stock": 3},
        {"external_id": "4", "name": "BOOS CARTERAS ENV x 1", "price": 1222, "brand": "w", "stock": 0}])
    assert sku.buscar("perfumes masculinos")[0]["sku_id"] == "1"
    assert sku.buscar("perfume para mujer")[0]["sku_id"] == "2"
    assert "3" in [r["sku_id"] for r in sku.buscar("carteras")]
