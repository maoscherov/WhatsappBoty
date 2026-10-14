"""
Tanda 1 de bugs (14/10): pedidos y precios. Conversaciones reales del 6 al
9/10 (C-4340, C-4342, C-4736, C-4841, C-4586, C-4660, C-4694, C-4232,
C-4793, C-4747, C-4207, C-4385).
"""
import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as ch
from app.services.sku_service import SKUService
from tests.test_webhook_secuencias import (PHONE, _Cfg, _Img, _Intent, _Rag, _Rec, _Socios,
                                           _Wa, _msg)

_FILAS = [
    ("1", "OTOWIL NATIVA PETAL ACO x 300", 10726.23, "Otowil"),
    ("2", "KOREIA AGUA MICELAR ENV x 200", 11341.88, "Koreia"),
    ("3", "SENSIDENT PTA x 90", 12058.83, "Sensident"),
    ("4", "EVAGINA JLI x 200", 12365.59, "Evagina"),
    ("5", "DERMAGLOS F ACNE SECANTE GEL x 15", 15995.19, "Dermaglos"),
    ("6", "DOVE ANTIBACTERIAL JLI x 250", 7667.0, "Dove"),
    ("7", "DOVE ORIGINAL JAB x 90", 2627.57, "Dove"),
    ("8", "Ruby Rose mascara de pestañas ENV x 1", 12538.35, "Ruby Rose"),
    ("9", "BLUE SEDUCTION FOR MEN EDT x 100", 82861.25, "Antonio Banderas"),
    ("10", "TAFIROL FORTE 650 COM x 30", 15592.81, "Tafirol"),
]


@pytest.fixture
def entorno(monkeypatch):
    from app.services.session_service import SessionService
    filas = [{"external_id": i, "name": n, "price": p, "brand": m, "stock": 5,
              "category": "Perfumeria", "requiere_receta": "no"} for i, n, p, m in _FILAS]

    def armar(guion=None, cfg=None):
        deps = {
            "wa": _Wa(), "sku": SKUService.from_rows(filas),
            "session": SessionService("redis://127.0.0.1:1"),
            "intent": _Intent(guion or {}), "payment": _Rec(), "audio": _Rec(),
            "image": _Img(), "perf": _Rec(), "config": _Cfg(cfg),
            "socios": _Socios(), "msgs": _Rec(), "metrics": _Rec(), "rag": _Rag(),
        }
        monkeypatch.setattr(wh, "_deps", lambda s=None: deps)
        return deps

    return armar


async def _pendiente(deps, sku_id, nombre, precio, ultimo_bot):
    ss = deps["session"]
    await ss.set_pending(PHONE, sku_id=sku_id, sku_nombre=nombre, precio=precio,
                         cantidad=1, opciones=[])
    s = await ss.get(PHONE)
    s["history"] = [{"role": "user", "content": "hola"},
                    {"role": "assistant", "content": ultimo_bot}]
    await ss.save(PHONE, s)


def _ultimo(deps):
    return deps["wa"].enviados[-1]


# ── 1. Confirmaciones de más ─────────────────────────────────────────────────────
async def test_dale_gracias_sin_pregunta_no_confirma(entorno):
    """C-4340: rechazó el perfume y el "dale gracias" preguntó retiro o envío."""
    deps = entorno()
    await _pendiente(deps, "9", "BLUE SEDUCTION FOR MEN EDT x 100", 66289.0,
                     "¡Entendido, Mariano! Si en algún momento querés explorar más opciones, "
                     "estoy aquí para ayudarte. 😊")
    await wh.procesar_mensajes([_msg("dale gracias")])
    assert "retiro" not in _ultimo(deps).lower()


async def test_por_el_momento_no_cancela_el_pendiente(entorno):
    deps = entorno()
    await _pendiente(deps, "9", "BLUE SEDUCTION FOR MEN EDT x 100", 66289.0,
                     "Tengo el Blue Seduction a $66.289. ¿Te gustaría llevar este?")
    await wh.procesar_mensajes([_msg("por el momento no.")])
    assert not (await deps["session"].get(PHONE)).get("pending_sku_id")


async def test_si_a_consultar_con_el_equipo_deriva_aunque_haya_pendiente(entorno):
    """C-4342: "Si" a "¿querés que lo consulte con el equipo?" → retiro o envío."""
    deps = entorno()
    await _pendiente(deps, "10", "TAFIROL FORTE 650 COM x 30", 12474.25,
                     "No tengo información sobre cremas faciales. Si querés, puedo consultar "
                     "con el equipo para ver qué opciones tenemos disponibles. ¿Te gustaría que lo haga?")
    await wh.procesar_mensajes([_msg("Si")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador"
    assert "retiro" not in _ultimo(deps).lower()


async def test_no_tenes_otro_producto_no_arma_el_carrito(entorno):
    """C-4736: "¿No tenés jabón Dove original x90grs?" armó el pedido."""
    guion = {"No tenés jabón dove original x90grs?": {
        "intencion": "consulta_stock", "entidad_producto": "jabón dove original x90",
        "entidades_adicionales": ["jabón dove original x90"], "confirmacion": None,
        "agregar_al_pedido": True, "respuesta": "Sí, tengo el Dove Original."}}
    deps = entorno(guion)
    await _pendiente(deps, "6", "DOVE ANTIBACTERIAL JLI x 250", 6516.93,
                     "Tengo el Dove Antibacterial a $6.516,93. ¿Te interesa esta opción?")
    await wh.procesar_mensajes([_msg("No tenés jabón dove original x90grs?")])
    assert "Tu pedido queda así" not in _ultimo(deps)
    # pasa a ofrecer el Dove original: el antibacterial no queda en el carrito
    items = (await deps["session"].get(PHONE)).get("pending_items") or []
    assert "6" not in {i["sku_id"] for i in items}


async def test_si_y_quiero_algo_mas_pregunta_que_suma(entorno):
    """C-4841: "Sí y quiero algo más" pasaba a retiro o envío."""
    guion = {"Koreia agua micelar": {
        "intencion": "consulta_stock", "entidad_producto": "koreia agua micelar",
        "confirmacion": None, "respuesta": "Tengo el Koreia."}}
    deps = entorno(guion)
    await _pendiente(deps, "1", "OTOWIL NATIVA PETAL ACO x 300", 8580.98,
                     "Tengo el Otowil Nativa Petal a $8.580,98. ¿Te gustaría llevar este?")
    await wh.procesar_mensajes([_msg("Sí y quiero algo más")])
    assert "qué más" in _ultimo(deps).lower()
    assert "retiro" not in _ultimo(deps).lower()
    # lo que nombra después se SUMA (no reemplaza al Otowil)
    await wh.procesar_mensajes([_msg("Koreia agua micelar")])
    items = (await deps["session"].get(PHONE)).get("pending_items") or []
    assert {i["sku_id"] for i in items} == {"1", "2"}


async def test_quiero_algo_mas_eligiendo_la_entrega_vuelve_al_carrito(entorno):
    deps = entorno()
    await _pendiente(deps, "1", "OTOWIL NATIVA PETAL ACO x 300", 8580.98, "¿Retiro o envío?")
    await deps["session"].set_estado(PHONE, "esperando_entrega")
    await wh.procesar_mensajes([_msg("Quiero algo más")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "esperando_confirmacion" and s.get("_quiere_sumar")


def test_reglas_de_confirmacion():
    assert wh._match_no("por el momento no.")
    assert wh._match_no("no por ahora, gracias")
    assert wh._match_no("Gracias! Por ahora solo consulta")
    assert not wh._match_no("si, lo retiro ahora")
    assert ch.pide_algo_mas("Sí y quiero algo más")
    assert not ch.pide_algo_mas("quiero algo más, un shampoo Dove")
    assert ch.pide_sumar("Genial y te pregunto también por la bruma de eximia")
    assert not ch.pide_sumar("mejor en vez del gel, la bruma")


# ── 2. Duplicados en el carrito ──────────────────────────────────────────────────
async def test_el_carrito_no_repite_un_producto(entorno):
    """C-4586: "Listo confirmado!" volvió a sumar el Sensident."""
    deps = entorno()
    ss = deps["session"]
    await ss.set_pending(PHONE, sku_id="4", sku_nombre="EVAGINA JLI x 200", precio=9892.47,
                         cantidad=1, opciones=[])
    await ss.agregar_item(PHONE, "3", "SENSIDENT PTA x 90", 9647.06)
    items = await ss.agregar_item(PHONE, "3", "SENSIDENT PTA x 90", 9647.06)
    assert [i["sku_id"] for i in items] == ["4", "3"]


async def test_todos_no_suma_el_mismo_producto_dos_veces(entorno):
    """C-4660: lápiz secante y gel secante eran el mismo producto: iba dos veces."""
    deps = entorno()
    ss = deps["session"]
    await ss.set_pending(PHONE, sku_id="5", sku_nombre="DERMAGLOS F ACNE SECANTE GEL x 15",
                         precio=12796.15, cantidad=1, opciones=[])
    await ss.guardar_extras(PHONE, [{"sku_id": "5", "nombre": "DERMAGLOS F ACNE SECANTE GEL x 15",
                                     "precio": 12796.15, "cantidad": 1}])
    items = await ss.sumar_extras(PHONE)
    assert len(items) == 1


# ── 3. Entrega y pago en el mismo mensaje ────────────────────────────────────────
def test_transferencia_no_es_efectivo():
    assert not ch.pide_efectivo("Retiro en sucursal y pago con transferencia")
    assert not ch.pide_efectivo("lo retiro y pago con tarjeta")
    assert ch.pide_efectivo("Lo retiro en sucursal, lo abono en efectivo")
    assert ch.match_retiro("Retito")


def test_entrega_en_el_texto():
    assert ch.entrega_en_texto("Retiro en sucursal y lo anoto en la cta cte") == "retiro"
    assert ch.entrega_en_texto("Envío a domicilio, lo quiero anotar en la cuenta corriente") == "envio"
    assert ch.entrega_en_texto("lo anoto en la cuenta") is None


async def test_retiro_y_efectivo_en_el_mismo_mensaje_no_repregunta(entorno):
    """C-4586: "Lo retiro en sucursal, lo abono en efectivo" volvía a preguntar."""
    deps = entorno(cfg={"efectivo_enabled": "true"})
    await _pendiente(deps, "4", "EVAGINA JLI x 200", 9892.47, "¿Cómo preferís recibirlo?")
    llamadas = []

    async def _link(payment, session_svc, phone, session, tipo, direccion):
        llamadas.append((tipo, direccion))
        return "✅ ¡Listo! Tomamos tu pedido", "pedido_confirmado"
    import app.routers.webhook as w
    w_orig = w.crear_link_y_responder
    w.crear_link_y_responder = _link
    import app.services.checkout_helper as c
    c_orig = c.crear_link_y_responder
    c.crear_link_y_responder = _link
    try:
        await wh.procesar_mensajes([_msg("Lo retiro en sucursal, lo abono en efectivo")])
    finally:
        w.crear_link_y_responder = w_orig
        c.crear_link_y_responder = c_orig
    assert llamadas == [("retiro", None)]
    assert "¿Preferís" not in _ultimo(deps)


# ── 4. Descuento en el cambio de producto ────────────────────────────────────────
async def test_cambio_de_producto_con_pendiente_aplica_el_descuento(entorno, monkeypatch):
    """Otowil / Ruby Rose: la búsqueda del cambio de producto iba sin descuento
    y el modelo decía "ya con tu 20%" sobre el precio de lista."""
    monkeypatch.setattr(ch, "descuento_para", lambda phone, cfg, socio_svc=None: (20.0, "empleado"))
    guion = {"mejor una mascara ruby rose": {
        "intencion": "consulta_stock", "entidad_producto": "ruby rose mascara",
        "confirmacion": False, "respuesta": "Tengo la Ruby Rose."}}
    deps = entorno(guion)
    await _pendiente(deps, "10", "TAFIROL FORTE 650 COM x 30", 12474.25, "¿Lo llevás?")
    await wh.procesar_mensajes([_msg("mejor una mascara ruby rose")])
    s = await deps["session"].get(PHONE)
    assert s["pending_sku_id"] == "8"
    assert abs(float(s["pending_precio"]) - 12538.35 * 0.8) < 0.01


# ── 5. Pedido para la cuenta de otra persona ─────────────────────────────────────
def test_cuenta_de_tercero():
    assert ch.cuenta_de_tercero("Buenos días, les paso un pedido para la cuenta de Silvia vuadens") \
        == "Silvia Vuadens"
    assert ch.cuenta_de_tercero("Anoto en cta de vuadens Silvia") == "Vuadens Silvia"
    assert ch.cuenta_de_tercero("lo anoto en la cuenta corriente") is None
    assert ch.cuenta_de_tercero("cargalo en mi cuenta") is None


async def test_pedido_para_la_cuenta_de_otra_persona_deriva(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg("Buenos días, les paso un pedido para la cuenta de Silvia vuadens")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador"
    assert "Silvia Vuadens" in _ultimo(deps)


# ── 6. Armado del pedido ─────────────────────────────────────────────────────────
def test_marca_de_la_linea():
    assert ch.marca_de_linea("Y de la línea cepage, la crema cepage sensitive y protector solar f50") \
        == "cepage"
    assert ch.marca_de_linea("un protector solar f50") is None


def test_resumen_para_el_operador_trae_lo_demas():
    t = ch._texto_otros(["CLONAGIN 2 mg (requiere receta)", "gota de la nariz (no lo encontré)",
                         "gota de la nariz (no lo encontré)"])
    assert "También pediste" in t and t.count("gota de la nariz") == 1


# ── 7. "Se vende sin receta" con la venta ya cerrada ─────────────────────────────
async def test_no_retoma_la_venta_si_el_cliente_ya_siguio_con_el_bot(entorno):
    """C-4747: el citrato ya estaba pedido y le volvió a preguntar retiro o envío."""
    from app.services import receta_marcas
    deps = entorno()
    await deps["session"].set_estado(PHONE, "pedido_confirmado")
    with pytest.raises(receta_marcas.ConflictoMarca):
        await receta_marcas.retomar_venta(PHONE, "3")
    assert deps["wa"].enviados == []


# ── 8. Confirmación inventada por el modelo ──────────────────────────────────────
def test_esta_listo_para_retirar_del_modelo_se_saca():
    t = ("Perfecto, Luisiana. El polvo compacto efecto mate de Lucy Anderson está listo para "
         "que lo retires en la sucursal. ¡Te esperamos!")
    limpio = ch.quitar_confirmaciones_fantasma(t)
    assert "listo para" not in limpio and "esperamos" not in limpio.lower()


async def test_tambien_por_otro_producto_suma_en_vez_de_reemplazar(entorno):
    """C-4385: "te pregunto también por la bruma" reemplazó al gel."""
    guion = {"Genial y te pregunto también por el agua micelar koreia": {
        "intencion": "consulta_stock", "entidad_producto": "agua micelar koreia",
        "confirmacion": None, "respuesta": "Tengo el Koreia."}}
    deps = entorno(guion)
    await _pendiente(deps, "1", "OTOWIL NATIVA PETAL ACO x 300", 8580.98,
                     "Tengo el Otowil a $8.580,98. ¿Te gustaría llevarlo?")
    await wh.procesar_mensajes([_msg("Genial y te pregunto también por el agua micelar koreia")])
    items = (await deps["session"].get(PHONE)).get("pending_items") or []
    assert {i["sku_id"] for i in items} == {"1", "2"}
