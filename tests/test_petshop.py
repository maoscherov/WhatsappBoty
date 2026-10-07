"""
Petshop por capacidad (spec §6.2, `tests/test_petshop.py`): cada test de
petshop tiene su par de farmacia "igual que hoy". Los perfiles se eligen con la
fixture `usar_perfil` (tests/conftest.py).
"""

# ── Recetas: _formatear_productos (intent_service) ──────────────────────────────
_PIPETA = {"nombre": "PIPETA FRONTLINE PLUS PERRO 10-20KG", "precio": 15000.0, "estado": "disponible",
           "cantidad_visible": 4, "sku_id": "20", "requiere_receta": "si"}
_PIPETA_URGENTE = {**_PIPETA, "sku_id": "21", "requiere_receta": "ambiguo", "urgente": True}


def test_formatear_productos_petshop_no_marca_receta(usar_perfil):
    from app.services.intent_service import IntentService
    usar_perfil("petshop")
    txt = IntentService("")._formatear_productos([_PIPETA, _PIPETA_URGENTE])
    assert txt == (
        "1. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4) | ID: 20\n"
        "2. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | STOCK BAJO - ofrecer con urgencia | ID: 21")


def test_formatear_productos_farmacia_marca_receta_como_hoy(usar_perfil):
    from app.services.intent_service import IntentService
    usar_perfil("farmacia")
    txt = IntentService("")._formatear_productos([_PIPETA, _PIPETA_URGENTE])
    assert txt == (
        "1. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | REQUIERE RECETA | ID: 20\n"
        "2. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | STOCK BAJO - ofrecer con urgencia | REQUIERE RECETA | ID: 21")
