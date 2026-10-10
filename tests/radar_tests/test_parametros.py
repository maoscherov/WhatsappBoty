"""
Tabla de parámetros de §2.1 y la regla "más estricto".
"""
from decimal import Decimal

import pytest

from app.radar.parametros import (
    DE_LINEA, DE_TENANT, PARAMETROS, ValorInvalido, clasificar_cambios, comparar,
    es_mas_estricto, iniciales, perfil_por_rubro, propuesta_para_perfil, validar,
)


def test_tabla_exacta_del_spec():
    assert {n: (p.ambito, p.inicial) for n, p in PARAMETROS.items()} == {
        "duracion_vinculo_dias": ("linea", 0),
        "retencion_fuente_dias": ("linea", 0),
        "retencion_tras_desvinculo_dias": ("linea", 0),
        "retencion_fichas_meses": ("tenant", 12),
        "tope_ia_mensual_usd": ("linea", None),
        "perfil_de_datos": ("tenant", "estandar"),
        "retener_fragmentos": ("tenant", False),
        "ia_habilitada": ("tenant", True),
        "via_llm": ("tenant", "lotes"),
    }
    assert DE_LINEA == ["duracion_vinculo_dias", "retencion_fuente_dias",
                        "retencion_tras_desvinculo_dias", "tope_ia_mensual_usd"]
    assert DE_TENANT == ["retencion_fichas_meses", "perfil_de_datos", "retener_fragmentos",
                         "ia_habilitada", "via_llm"]


def test_iniciales_por_ambito():
    assert iniciales("linea") == {"duracion_vinculo_dias": 0, "retencion_fuente_dias": 0,
                                  "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None}
    assert iniciales("tenant")["retencion_fichas_meses"] == 12


@pytest.mark.parametrize("nombre,actual,nuevo,esperado", [
    # plazos: N > 0 es más estricto que 0; entre > 0 gana el menor
    ("duracion_vinculo_dias", 0, 30, "mas_estricto"),
    ("duracion_vinculo_dias", 30, 0, "mas_laxo"),
    ("duracion_vinculo_dias", 30, 7, "mas_estricto"),
    ("duracion_vinculo_dias", 7, 30, "mas_laxo"),
    ("duracion_vinculo_dias", 7, 7, "igual"),
    ("retencion_fuente_dias", 0, 7, "mas_estricto"),
    ("retencion_fichas_meses", 12, 0, "mas_laxo"),
    ("retencion_fichas_meses", 12, 6, "mas_estricto"),
    # monto: NULL = sin tope (lo más laxo); entre montos gana el menor
    ("tope_ia_mensual_usd", None, Decimal("50"), "mas_estricto"),
    ("tope_ia_mensual_usd", Decimal("50"), None, "mas_laxo"),
    ("tope_ia_mensual_usd", Decimal("50"), Decimal("20"), "mas_estricto"),
    # booleanos: apagar es más estricto
    ("ia_habilitada", True, False, "mas_estricto"),
    ("ia_habilitada", False, True, "mas_laxo"),
    ("retener_fragmentos", False, True, "mas_laxo"),
    # enums
    ("perfil_de_datos", "estandar", "sensible", "mas_estricto"),
    ("perfil_de_datos", "sensible", "estandar", "mas_laxo"),
    ("via_llm", "lotes", "sincronica", "mas_estricto"),
    ("via_llm", "sincronica", "lotes", "mas_laxo"),
])
def test_comparar(nombre, actual, nuevo, esperado):
    assert comparar(nombre, actual, nuevo) == esperado


def test_es_mas_estricto_es_estricto_no_reflexivo():
    assert es_mas_estricto("duracion_vinculo_dias", 7, 30)
    assert not es_mas_estricto("duracion_vinculo_dias", 30, 7)
    assert not es_mas_estricto("duracion_vinculo_dias", 7, 7)


@pytest.mark.parametrize("nombre,valor,esperado", [
    ("duracion_vinculo_dias", "30", 30),
    ("tope_ia_mensual_usd", "12.50", Decimal("12.50")),
    ("tope_ia_mensual_usd", None, None),
    ("ia_habilitada", False, False),
    ("via_llm", "lotes", "lotes"),
])
def test_validar_coacciona(nombre, valor, esperado):
    assert validar(nombre, valor) == esperado


@pytest.mark.parametrize("nombre,valor", [
    ("duracion_vinculo_dias", -1),
    ("duracion_vinculo_dias", None),
    ("duracion_vinculo_dias", "treinta"),
    ("duracion_vinculo_dias", True),
    ("tope_ia_mensual_usd", Decimal("-1")),
    ("ia_habilitada", "si"),
    ("via_llm", "streaming"),
    ("perfil_de_datos", "farmacia"),
    ("inexistente", 1),
])
def test_validar_rechaza(nombre, valor):
    with pytest.raises(ValorInvalido):
        validar(nombre, valor)


def test_perfil_sensible_propone_no_fuerza():
    assert propuesta_para_perfil("sensible") == {
        "retencion_fuente_dias": 7, "ia_habilitada": False,
        "via_llm": "sincronica", "retener_fragmentos": False,
    }
    assert propuesta_para_perfil("estandar") == {}
    with pytest.raises(ValorInvalido):
        propuesta_para_perfil("otro")


@pytest.mark.parametrize("rubro,perfil", [
    ("farmacia", "sensible"), ("salud", "sensible"), ("mutual_salud", "sensible"),
    ("comercio", "estandar"), ("pet_shop", "estandar"), ("servicios", "estandar"), ("otro", "estandar"),
])
def test_perfil_por_rubro(rubro, perfil):
    assert perfil_por_rubro(rubro) == perfil


def test_clasificar_cambios_solo_los_que_cambian():
    actuales = {"duracion_vinculo_dias": 0, "retencion_fuente_dias": 0,
                "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None}
    cambios = clasificar_cambios(actuales, {"duracion_vinculo_dias": "30", "retencion_fuente_dias": 0})
    assert cambios == {"duracion_vinculo_dias": "mas_estricto"}
