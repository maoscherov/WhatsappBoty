"""
"¿Qué otras opciones tienen?" (C-4939, 13/10): sin producto nombrado no se
buscaba nada y el modelo inventó tres polvos compactos con precio ($3.200,
$3.600, $3.800) que no existen en el catálogo.
"""
import pytest

from app.routers import webhook as wh
from app.services.checkout_helper import pide_otras_opciones, tipo_de_producto
from app.services.sku_service import SKUService
from tests.test_webhook_secuencias import (PHONE, _Cfg, _Img, _Intent, _Rag, _Rec, _Socios,
                                           _Wa, _msg)

_ROWS = [
    ("1", "VOGUE CPTO.MATE NAT.CANELA POL x 1", "13922.86", "Vogue", "Vogue", "771", "Maquillaje", "false"),
    ("2", "VOGUE CPTO.RES.MIEL POL x 1", "27000", "Vogue", "Vogue", "772", "Maquillaje", "false"),
    ("3", "LUCY ANDERSON POLVO COMPACTO EFECTO MATE ENV x 1", "18085.19", "Lucy Anderson",
     "Lucy Anderson", "773", "Maquillaje", "false"),
    ("4", "FIT ME RUBOR ENV x 1", "22968.10", "Maybelline", "Maybelline", "774", "Maquillaje", "false"),
    ("5", "COREGA TABS PRO ORTODONCIA EFERV TAB x 30", "11081", "Corega", "GSK", "775",
     "Higiene", "false"),
    ("6", "IDI POLVO COMPACTO HD BEIGE ENV x 1", "15342", "Idi", "Idi", "776", "Maquillaje", "false"),
]

_INVENTADA = ("Te puedo ofrecer estas opciones similares disponibles:\n\n"
              "1. Polvo compacto Maybelline Fit Me! Matte Poreless 12g a $3.200, ya con tu 20% de empleado.\n"
              "2. Polvo compacto L'Oréal Infallible 24H a $3.600, ya con tu 20% de empleado.\n"
              "3. Polvo compacto Revlon Colorstay 10g a $3.800, ya con tu 20% de empleado.\n\n"
              "¿Cuál te sirve?")


@pytest.fixture
def entorno(monkeypatch):
    from app.services.session_service import SessionService
    filas = [{"external_id": i, "name": n, "price": p, "brand": m, "stock": 5,
              "category": c, "requiere_receta": "no"} for i, n, p, m, _l, _cb, c, _med in _ROWS]

    def armar(guion):
        deps = {
            "wa": _Wa(), "sku": SKUService.from_rows(filas),
            "session": SessionService("redis://127.0.0.1:1"),
            "intent": _Intent(guion), "payment": _Rec(), "audio": _Rec(),
            "image": _Img(), "perf": _Rec(), "config": _Cfg(),
            "socios": _Socios(), "msgs": _Rec(), "metrics": _Rec(), "rag": _Rag(),
        }
        monkeypatch.setattr(wh, "_deps", lambda s=None: deps)
        return deps

    return armar


@pytest.mark.parametrize("txt", ["que otras opciones tienen?", "¿Tenés algo parecido?",
                                 "y otra marca?", "qué más tenés?", "alguna alternativa?"])
def test_pide_otras_opciones(txt):
    assert pide_otras_opciones(txt)


@pytest.mark.parametrize("txt", ["gracias", "la 2", "dale, lo retiro", "tenés polvo compacto?"])
def test_no_pide_otras_opciones(txt):
    assert not pide_otras_opciones(txt)


def test_tipo_de_producto_saca_marca_linea_y_medida(entorno):
    sku = entorno({})["sku"]
    t = tipo_de_producto("Maybelline FIT me! Mate & Sin Poros polvo compacto 12g", sku)
    assert "polvo" in t and "compacto" in t
    assert "maybelline" not in t and "12g" not in t and "poros" not in t
    # una forma sola ("gel") no alcanza: traería cualquier cosa
    assert tipo_de_producto("gel lord cheseline", sku) == ""


async def test_otras_opciones_busca_en_el_catalogo_y_no_inventa(entorno):
    deps = entorno({"que otras opciones tienen?": {
        "intencion": "consulta_stock", "entidad_producto": None, "respuesta": _INVENTADA}})
    s = await deps["session"].get(PHONE)
    s["_ultima_busqueda"] = "Maybelline FIT me! Mate & Sin Poros polvo compacto 12g"
    s["history"] = [{"role": "user", "content": "Maybelline FIT me! Mate & Sin Poros polvo compacto 12g"},
                    {"role": "assistant", "content": "Justo no me figura disponible. ¿Lo consulto?"}]
    await deps["session"].save(PHONE, s)

    await wh.procesar_mensajes([_msg("que otras opciones tienen?")])
    enviado = deps["wa"].enviados[-1]
    assert "$3.200" not in enviado and "Revlon" not in enviado and "Infallible" not in enviado
    assert "VOGUE" in enviado.upper() or "IDI" in enviado.upper() or "LUCY" in enviado.upper()


async def test_sin_busqueda_previa_los_precios_inventados_se_bloquean(entorno):
    """consulta_stock sin producto y sin nada antes: el modelo no puede poner
    precios que no salen de ningún dato."""
    deps = entorno({"que otras opciones tienen?": {
        "intencion": "consulta_stock", "entidad_producto": None, "respuesta": _INVENTADA}})
    await wh.procesar_mensajes([_msg("que otras opciones tienen?")])
    enviado = deps["wa"].enviados[-1]
    assert "$3.200" not in enviado and "Revlon" not in enviado
    assert "equipo" in enviado


async def test_consulta_stock_sin_precios_pasa_igual(entorno):
    """El control solo frena precios: una respuesta sin precios sale tal cual."""
    deps = entorno({"¿cómo se usan las pastillas?": {
        "intencion": "consulta_stock", "entidad_producto": None,
        "respuesta": "Se disuelve una pastilla en agua tibia y se deja la prótesis 3 minutos."}})
    await wh.procesar_mensajes([_msg("¿cómo se usan las pastillas?")])
    assert deps["wa"].enviados[-1] == ("Se disuelve una pastilla en agua tibia y se deja "
                                       "la prótesis 3 minutos.")


# ── Nombre del comercio en el saludo (9/10) ────────────────────────────────────
def test_prompt_usa_el_nombre_del_comercio(monkeypatch):
    from app.config import get_settings
    from app.services.intent_service import IntentService
    svc = IntentService("", "", "openai", "farmacia")
    assert "Bienvenido a Remedia" in svc._system_prompt()
    monkeypatch.setattr(get_settings(), "nombre_comercio", "Farmacia Mutual Independencia")
    p = svc._system_prompt()
    assert "Remedia" not in p
    assert "Bienvenido a Farmacia Mutual Independencia" in p
    assert "asistente virtual de Farmacia Mutual Independencia" in p
    # el de la mutual no se toca
    assert "Mutual AMI" in IntentService("", "", "openai", "mutual")._system_prompt()
