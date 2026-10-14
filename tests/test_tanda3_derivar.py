"""
Tanda 3 de bugs (14/10): entender y derivar. Conversaciones reales del 6 al
13/10 (C-4694, C-4397, C-4793, C-4893, C-4207, C-4225, C-4954, C-4917,
C-4949) y la cuota social (C-2371, C-4766, C-4455, C-4207).
"""
import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as ch
from app.services.sku_service import SKUService
from tests.test_webhook_secuencias import (PHONE, _Cfg, _Img, _Intent, _Rag, _Rec, _Socios,
                                           _Wa, _msg)


@pytest.fixture
def entorno(monkeypatch):
    from app.services.session_service import SessionService
    filas = [{"external_id": "1", "name": "TENSIOMETRO CORONET ENV x 1", "price": 27017.87,
              "brand": "Coronet", "stock": 3, "category": "Ortopedia", "requiere_receta": "no"},
             {"external_id": "2", "name": "RAQUIFEROL D3 100000 UI bl. CAP x 4", "price": 38953.0,
              "brand": "Raquiferol", "stock": 3, "category": "Medicamentos", "requiere_receta": "no"}]

    def armar(guion=None):
        deps = {
            "wa": _Wa(), "sku": SKUService.from_rows(filas),
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


# ── Cuota social ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("txt,esperado", [
    ("hola me pasas la cuota de socio", "sola"),
    ("la puedo abonar por acá la cuota", "sola"),
    ("me podes decir si te debo alguna cuota de socio", "sola"),
    ("Si te debo alguna cuota de socia también te lo pago", "sola"),
    ("necesito un desodorante rexona de mujer y quiero pagar la cuota de socio", "con_pedido"),
    ("puedo pagar en 3 cuotas?", None),
    ("tienen cuotas sin interés?", None),
])
def test_cuota_social(txt, esperado):
    assert ch.consulta_cuota_social(txt) == esperado


async def test_cuota_social_deriva_con_mensaje_claro(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg("hola me pasas la cuota de socio")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s.get("derivada_motivo") == "cuota_social"
    assert "cuota social" in _ultimo(deps) and "entendido" not in _ultimo(deps)


async def test_cuota_social_con_un_producto_no_lo_pierde(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg(
        "hola buenas tardes necesito un desodorante rexona de mujer y quiero pagar la cuota de socio")])
    assert "lo que pediste" in _ultimo(deps)


# ── Seguimiento: receta / pedido ─────────────────────────────────────────────────
def test_seguimiento():
    assert ch.consulta_seguimiento("Hola chicas quwnpasp con la receta.") == "receta"
    assert ch.consulta_seguimiento("Chicas ya llego el pedido con las mascaras?") == "pedido"
    assert ch.consulta_seguimiento("ya llegó el link?") is None
    assert ch.consulta_seguimiento("paso con la receta mañana") is None


async def test_ya_llego_el_pedido_deriva_sin_hablar_de_devoluciones(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg("Chicas ya llego el pedido con las mascaras?")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador"
    assert "devoluciones" not in _ultimo(deps)


async def test_cambio_postventa_deriva_de_verdad(entorno):
    deps = entorno({"me llegó roto el tensiómetro": {"intencion": "cambio_postventa",
                                                     "respuesta": "Uh, perdón."}})
    await wh.procesar_mensajes([_msg("me llegó roto el tensiómetro")])
    assert (await deps["session"].get(PHONE))["estado"] == "operador"


# ── Otra consulta / receta / pedir una persona ───────────────────────────────────
async def test_otra_consulta_espera_la_consulta(entorno):
    """C-4694: "Otra consulta" derivaba con "no te entendí"."""
    deps = entorno()
    await wh.procesar_mensajes([_msg("Otra consulta")])
    assert _ultimo(deps) == "¡Dale! Decime 🙂"
    assert (await deps["session"].get(PHONE)).get("estado") != "operador"


async def test_te_envio_la_receta_pide_la_foto(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg(
        "no puedo considerar esa opcion por el momento hasta no saber si sirve mi receta, te la envio?")])
    assert "foto de la receta" in _ultimo(deps)


def test_pedir_una_persona_con_otras_palabras():
    assert ch.pide_humano("como hago para consultar con el equipo de la farmacia?")
    assert ch.pide_humano("consultar con el equipo por favor")
    assert not ch.pide_humano("quiero consultar el precio del ibuprofeno")


def test_la_misma_oferta_tres_veces_es_un_bucle():
    hist = [{"role": "assistant", "content": "Tengo el Raquiferol 100000 a $38.953, requiere receta."},
            {"role": "user", "content": "tengo receta para otra"},
            {"role": "assistant", "content": "Sin embargo, tengo el Raquiferol a $38.953. ¿Te sirve?"}]
    assert ch.repite_oferta("Lamentablemente no. Tengo el Raquiferol a $38.953.", hist)
    assert not ch.repite_oferta("Lamentablemente no. Tengo el Raquiferol a $38.953.", hist[:1])


# ── Cambio de entrega en pedidos con envío ───────────────────────────────────────
async def test_traelo_al_negocio_con_pedido_enviado_deriva(entorno):
    """C-4207: el pedido salía a Chacabuco 782 y "Tráelo al negocio" recibió
    "¿qué producto necesitás que traiga?" y después se cerró la charla."""
    deps = entorno()
    ss = deps["session"]
    await ss.set_entrega(PHONE, "envio", "Chacabuco 782")
    await ss.set_estado(PHONE, "pedido_confirmado")
    await wh.procesar_mensajes([_msg("Tráelo al negocio")])
    s = await ss.get(PHONE)
    assert s["estado"] == "operador" and s.get("derivada_motivo") == "cambio_entrega"


# ── Adjuntos con un operador atendiendo ──────────────────────────────────────────
async def test_documento_con_operador_atendiendo_no_responde(entorno, monkeypatch):
    """C-4225: con Claudia atendiendo, el bot contestó "Recibí tu archivo"."""
    deps = entorno()
    await deps["session"].set_estado(PHONE, "operador", motivo="consulta_cuenta_corriente")

    async def _bajar(url):
        return b"PK\x03\x04 xlsx"
    monkeypatch.setattr(wh, "_descargar_url", _bajar)
    await wh.procesar_mensajes([_msg("", tipo="document", media_url="https://x/archivo.xlsx",
                                     mime="application/vnd.ms-excel", filename="resumen.xlsx")])
    assert deps["wa"].enviados == []


# ── Esperando la dirección ───────────────────────────────────────────────────────
async def test_envio_a_otra_sucursal_deriva(entorno):
    """C-4954: pidió la dirección tres veces a quien quería envío a otra sucursal."""
    deps = entorno()
    ss = deps["session"]
    await ss.set_pending(PHONE, sku_id="1", sku_nombre="TENSIOMETRO CORONET ENV x 1",
                         precio=27017.87, cantidad=1, opciones=[])
    await ss.set_estado(PHONE, "esperando_direccion")
    await wh.procesar_mensajes([_msg("necesito que lo envien a sucursal gral cabrera")])
    assert (await ss.get(PHONE))["estado"] == "operador"
    assert "calle" not in _ultimo(deps)


async def test_te_confirmo_luego_deja_de_pedir_la_direccion(entorno):
    deps = entorno()
    ss = deps["session"]
    await ss.set_pending(PHONE, sku_id="1", sku_nombre="TENSIOMETRO CORONET ENV x 1",
                         precio=27017.87, cantidad=1, opciones=[])
    await ss.set_estado(PHONE, "esperando_direccion")
    await wh.procesar_mensajes([_msg("le paso los precios a la asociada y te confirmo luego")])
    s = await ss.get(PHONE)
    assert s["estado"] == "esperando_confirmacion" and s.get("pending_sku_id") == "1"
    assert "dirección" not in _ultimo(deps)


# ── Leche Sancor no es la obra social ────────────────────────────────────────────
def test_leche_sancor_no_es_obra_social():
    assert ch.pregunta_obra_social(
        "Hola tenes leche Sancor líquida, para mi bebé de 9 meses ? Creo q es la 2") is None
    assert ch.pregunta_obra_social("trabajan con sancor?") == "sancor"


# ── Fuera de horario: no se come lo que se le dice al cliente ────────────────────
def test_aviso_fuera_de_horario_conserva_el_mensaje():
    t = ch.aviso_fuera_horario("¡Dale! Te paso con alguien del equipo que te manda la foto "
                               "del producto 📷 En un momento te contactamos.", "hoy a las 16:00")
    assert "foto del producto" in t and "En un momento" not in t


# ── Tanda 4: mensajes ────────────────────────────────────────────────────────────
def test_no_saluda_con_una_inicial():
    """C-4527: "¡Hola S!"."""
    from app.services.socio_service import nombre_de_pila
    assert nombre_de_pila({"nombre": "S"}) == ""
    assert nombre_de_pila({"nombre": "Gomez S."}, "apellido_nombre") == ""
    assert nombre_de_pila({"nombre": "Perez Luciana"}, "apellido_nombre") == "Luciana"


@pytest.mark.parametrize("hist,con_link,esperado", [
    # C-4736: "Nada mas" → despedida del bot → no "damos por cerrada"
    ([{"role": "user", "content": "Nada mas"},
      {"role": "assistant", "content": "Gracias, Estefano. ¡Que tengas una buena noche!"}], False, "silencio"),
    # C-4793: el último fue "tu pedido está listo para retirar" (no espera respuesta)
    ([{"role": "user", "content": "Retiro"},
      {"role": "assistant", "content": "🎉 ¡Tu pedido está listo para retirar! Código 965412"}], False, "silencio"),
    # el bot preguntó y el cliente no contestó → aviso
    ([{"role": "user", "content": "tenés paracetamol?"},
      {"role": "assistant", "content": "Sí, sale $7.162. ¿Te gustaría llevarlo?"}], False, "avisar"),
    # link de pago abierto: el recordatorio va aunque no haya pregunta
    ([{"role": "user", "content": "retiro"},
      {"role": "assistant", "content": "Acá te mando el link de pago. ¡Cualquier cosa me avisás!"}], True, "avisar"),
])
def test_cierre_por_inactividad(hist, con_link, esperado):
    assert ch.cierre_por_inactividad(hist, con_link=con_link) == esperado
