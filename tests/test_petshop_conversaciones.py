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
