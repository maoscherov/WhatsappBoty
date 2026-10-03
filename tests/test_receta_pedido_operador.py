"""
Decisión 3/10: si en el pedido hay un producto con receta, TODO pasa al
operador, con los productos de venta libre ya elegidos en el pedido.

Caso real 5/9: "Diclofenac, Ibuprofeno 600 y Segurite" → un único "requiere
receta"; "envíame lo otro entonces" quedó sin respuesta.
"""
from app.services import checkout_helper as ch
from app.services.session_service import SessionService

PHONE = "5493410000099"


class _Sku:
    def __init__(self, receta):
        self.receta = receta          # sku_id -> "si" | "no"

    def get_by_id(self, sku_id):
        r = self.receta.get(str(sku_id))
        if r is None:
            return None
        return type("S", (), {"requiere_receta": r, "sku_nombre_original": f"PROD {sku_id}",
                              "sku_nombre": f"prod {sku_id}"})()


async def test_receta_con_venta_libre_pasa_todo_al_operador():
    ss = SessionService("redis://127.0.0.1:1")
    sku = _Sku({"DICLO": "si", "SEG": "no", "ALG": "no"})
    await ss.set_pending(PHONE, "DICLO", "Diclofenac", 5000.0, 1, opciones=[])
    msg = await ch.derivar_si_receta(
        sku, ss, {"receta_mode": "conservador"}, PHONE, "DICLO",
        extras=[{"sku_id": "SEG", "nombre": "Segurite", "precio": 3000.0, "cantidad": 1},
                {"sku_id": "ALG", "nombre": "Algodón", "precio": 1200.0, "cantidad": 2}])
    s = await ss.get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "receta"
    assert [i["sku_id"] for i in s["pending_items"]] == ["SEG", "ALG"]
    assert "PROD DICLO requiere receta" in msg
    assert "Segurite — $3,000.00" in msg and "Algodón — $2,400.00" in msg
    assert [p["sku_id"] for p in s["_productos_receta"]] == ["DICLO"]


async def test_agregar_receta_a_un_carrito_conserva_el_carrito():
    ss = SessionService("redis://127.0.0.1:1")
    sku = _Sku({"HIPO": "no", "AMOX": "si"})
    await ss.set_pending(PHONE, "HIPO", "Hipoglós", 5000.0, 1, opciones=[])
    msg = await ch.derivar_si_receta(sku, ss, {"receta_mode": "conservador"}, PHONE, "AMOX")
    s = await ss.get(PHONE)
    assert s["estado"] == "operador"
    assert [i["sku_id"] for i in s["pending_items"]] == ["HIPO"]
    assert "Hipoglós" in msg


async def test_solo_receta_mensaje_de_siempre():
    ss = SessionService("redis://127.0.0.1:1")
    sku = _Sku({"AMOX": "si"})
    await ss.set_pending(PHONE, "AMOX", "Amoxidal", 5000.0, 1, opciones=[])
    msg = await ch.derivar_si_receta(sku, ss, {"receta_mode": "conservador"}, PHONE, "AMOX")
    s = await ss.get(PHONE)
    assert s["estado"] == "operador" and not s.get("pending_items")
    assert "requiere receta" in msg


def test_quitar_receta_inventada():
    r = ch.quitar_receta_inventada("Tengo Hipoglós a $5.000. Ese producto requiere receta 🩺. ¿Te lo preparo?")
    assert "receta" not in r and "Hipoglós" in r and "¿Te lo preparo?" in r
    assert ch.quitar_receta_inventada("Tengo curitas, ¿las querés?") == "Tengo curitas, ¿las querés?"
