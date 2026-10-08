"""
Listas numeradas pegadas al texto (8/10): cada ítem en su renglón.
"""
from app.services.checkout_helper import separar_lista_numerada as sep


def test_polvos_primer_item_pegado():
    t = ("Te puedo ofrecer estas opciones de polvos compactos: 1. Polvo Compacto Maybelline "
         "Superstay a $3.600, ya con tu 20% de empleado.\n2. Polvo Compacto L'Oréal Infallible "
         "a $3.200, ya con tu 20% de empleado.\n3. Polvo Compacto Rimmel Stay Matte a $2.800, "
         "ya con tu 20% de empleado. ¿Cuál te gustaría llevar?")
    assert sep(t) == (
        "Te puedo ofrecer estas opciones de polvos compactos:\n\n"
        "1. Polvo Compacto Maybelline Superstay a $3.600, ya con tu 20% de empleado.\n"
        "2. Polvo Compacto L'Oréal Infallible a $3.200, ya con tu 20% de empleado.\n"
        "3. Polvo Compacto Rimmel Stay Matte a $2.800, ya con tu 20% de empleado.\n\n"
        "¿Cuál te gustaría llevar?")


def test_tafirol_todo_en_un_renglon():
    t = ("Tengo varias opciones de Tafirol disponibles para vos: 1. Tafirol Resaca Comprimidos x 8 "
         "a $8.145,04, ya con tu 20% de descuento de empleado. 2. Tafirol Fem Comprimidos x 10 a "
         "$7.075,00, con el descuento incluido. 3. Tafirol Forte 650 mg Comprimidos x 30 a "
         "$12.474,25, también con el descuento. ¿Cuál preferís?")
    r = sep(t).split("\n")
    assert r[0] == "Tengo varias opciones de Tafirol disponibles para vos:"
    assert r[2].startswith("1. Tafirol Resaca") and r[2].endswith("empleado.")
    assert r[3].startswith("2. Tafirol Fem") and r[4].startswith("3. Tafirol Forte")
    assert "$12.474,25" in r[4] and r[-1] == "¿Cuál preferís?"


def test_bloque_de_productos_despues_queda_aparte():
    t = ("Opciones: 1. Raquiferol gotas a $12.439 2. Raquiferol ampolla a $10.267\n\n"
         "Lo que tengo disponible:\n• RAQUIFEROL D3 Oral GTS x 4 — $12,439.00")
    assert sep(t) == ("Opciones:\n\n1. Raquiferol gotas a $12.439\n2. Raquiferol ampolla a $10.267"
                      "\n\nLo que tengo disponible:\n• RAQUIFEROL D3 Oral GTS x 4 — $12,439.00")


def test_no_toca_lo_que_no_es_lista():
    for t in ("Sale $3.600, ya con tu 20% de descuento.",
              "El Clonagin 2 mg x 30. ¿Te lo reservo?",
              "Tomá 1. después otra cosa",                # un solo ítem
              "Precio: $1.500 y $2.300",
              "", None):
        assert sep(t) == t


def test_lista_ya_bien_armada_no_cambia():
    t = "Tengo estas:\n\n1. Uno a $10\n2. Dos a $20\n\n¿Cuál querés?"
    assert sep(t) == t
    assert sep(sep(t)) == t
