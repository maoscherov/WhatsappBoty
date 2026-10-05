"""
Padrón: nombre de pila, domicilio y calidad de columnas (caso real 24/9:
"Muff Claudia Beatriz" saludada como "Muff" y sin domicilio reconocido).
"""
import pytest

from app.services.socio_service import SocioService, nombre_de_pila


@pytest.mark.parametrize("nombre,apellido,orden,esperado", [
    ("Juan Carlos", "Pérez", "", "Juan"),
    ("Muff Claudia Beatriz", "Muff", "", "Claudia"),
    ("MUFF CLAUDIA BEATRIZ", "MUFF", "", "Claudia"),
    ("Pérez, Juan", "Pérez", "", "Juan"),
    ("Pérez, Juan", "", "", "Juan"),
    ("De La Fuente Ana María", "De La Fuente", "", "Ana"),
    ("Perez Juan", "", "apellido_nombre", "Juan"),
    ("Juan Perez", "", "nombre_apellido", "Juan"),
    ("Claudia", "", "apellido_nombre", "Claudia"),
    ("", "Muff", "", ""),
])
def test_nombre_de_pila(nombre, apellido, orden, esperado):
    assert nombre_de_pila({"nombre": nombre, "apellido": apellido}, orden=orden) == esperado


def test_nombre_de_pila_sin_socio():
    assert nombre_de_pila(None) == ""


def _padron(tmp_path, cabecera, filas):
    p = tmp_path / "padron.csv"
    p.write_text(cabecera + "\n" + "\n".join(filas) + "\n", encoding="utf-8")
    return SocioService(str(p))


def test_padron_con_tildes_y_calle(tmp_path):
    """'DIRECCIÓN' con tilde se ignoraba sin avisar; 'Calle' tampoco se reconocía."""
    s = _padron(tmp_path, "APELLIDO,NOMBRE,N° SOCIO,CELULAR,DIRECCIÓN,OBSERVACIONES",
                ["Muff,Muff Claudia Beatriz,4001,3415807742,San Javier 837,vip"])
    socio = s.find_by_phone("5493415807742")
    assert socio["domicilio"] == "San Javier 837"
    assert socio["nombre_pila"] == "Claudia"
    assert socio["nro_socio"] == "4001"
    r = s.reporte_carga
    assert r["columnas_reconocidas"]["domicilio"] == "DIRECCIÓN"
    assert r["columnas_ignoradas"] == ["OBSERVACIONES"]
    assert r["con_domicilio"] == 1
    assert r["nombre_con_apellido"] == 1
    assert r["nombre_con_apellido_ejemplos"][0]["saluda_como"] == "Claudia"

    s2 = _padron(tmp_path, "Apellido,Nombre,Celular,Calle", ["Perez,Juan,3415550001,Mitre 100"])
    assert s2.find_by_phone("3415550001")["domicilio"] == "Mitre 100"


def test_celdas_vacias_no_son_nan(tmp_path):
    s = _padron(tmp_path, "APELLIDO,NOMBRE,SOCIO,CELULAR,DOMICILIO",
                ["Perez,Juan,,3415550002,"])
    socio = s.find_by_phone("3415550002")
    assert socio["domicilio"] == "" and socio["nro_socio"] == ""
    assert s.reporte_carga["con_domicilio"] == 0


def test_contexto_para_prompt_trae_nombre_de_pila(tmp_path):
    s = _padron(tmp_path, "APELLIDO,NOMBRE,SOCIO,CELULAR",
                ["Muff,Muff Claudia Beatriz,4001,3415807742"])
    ctx = s.contexto_para_prompt("5493415807742")
    assert "Nombre de pila (para saludar): Claudia" in ctx
    assert "Apellido: Muff" in ctx
    assert "Nombre: Muff" not in ctx


# ── Stock en vivo al confirmar, antes de preguntar la entrega ───────────────────
class _SS:
    """Sesión mínima en memoria."""
    def __init__(self):
        from app.services.session_service import SessionService
        self.real = SessionService("redis://127.0.0.1:1")

    def __getattr__(self, n):
        return getattr(self.real, n)


async def _sesion_con_atenolol(ss, phone):
    await ss.set_pending(phone, sku_id="77", sku_nombre="ATENOLOL GADOR 50 mg COM x 60",
                         precio=9000.0, cantidad=1, opciones=[])
    s = await ss.get(phone)
    s["receta_validada"] = True          # lo cotizó el operador
    await ss.save(phone, s)
    return await ss.get(phone)


async def test_sin_stock_se_dice_antes_de_pedir_la_direccion(monkeypatch):
    """Caso 24/9 (Claudia): confirmó, eligió envío, dio la dirección y RECIÉN
    ahí supo que no había stock. Ahora se entera al confirmar."""
    from app.services import checkout_helper as ch
    ss = _SS()
    phone = "5493415807742"
    session = await _sesion_con_atenolol(ss, phone)

    async def _sin_stock(session, phone, session_svc, cfg):
        return "Justo me fijé y no nos queda stock de ATENOLOL GADOR 50 mg COM x 60.", None
    monkeypatch.setattr(ch, "_chequear_stock_vivo", _sin_stock)

    class _Sku:
        def get_by_id(self, i):
            return type("S", (), {"requiere_receta": "si"})()

    resp, intencion = await ch.confirmar_pedido(_Sku(), None, ss, None, {"envio_enabled": "true"},
                                                phone, session)
    assert intencion == "sin_stock_vivo"
    assert "no nos queda stock" in resp
    assert "dirección" not in resp.lower() and "retiro" not in resp.lower()


async def test_con_stock_pregunta_entrega_y_no_vuelve_a_consultar(monkeypatch):
    from app.services import checkout_helper as ch
    ss = _SS()
    phone = "5493415807743"
    session = await _sesion_con_atenolol(ss, phone)
    llamadas = []

    async def _hay(session, phone, session_svc, cfg):
        llamadas.append(1)
        return None, None
    monkeypatch.setattr(ch, "_chequear_stock_vivo", _hay)

    class _Sku:
        def get_by_id(self, i):
            return type("S", (), {"requiere_receta": "si"})()

    resp, intencion = await ch.confirmar_pedido(_Sku(), None, ss, None, {"envio_enabled": "true"},
                                                phone, session)
    assert intencion == "esperando_entrega" and len(llamadas) == 1
    s = await ss.get(phone)
    assert s.get("_stock_ok_para") == ch._clave_stock(s)
