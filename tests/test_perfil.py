"""
Perfil de rubro (spec 2026-10-06-vertical-petshop-design.md §3, §4.1 y §6.2):
registro de perfiles, get_perfil, prompts armados por bloques, textos del
perfil y corte del arranque con un VERTICAL desconocido.

Los tests que cambian de perfil usan la fixture `usar_perfil` (conftest.py).
"""
import hashlib
import inspect
import logging
import os
import re

import pytest

from app.services import prompts
from app.services.config_service import DEFAULTS
from app.services.mutual_helper import SYSTEM_PROMPT_MUTUAL
from app.services.perfil import (
    CLAVES_TEXTO_RUBRO, MARCAS_AUDIO_FARMACIA, VISION_FARMACIA, VISION_PETSHOP,
    get_perfil, perfil_por_clave,
)

SHA_FARMACIA = "1953a4e6815d855e635406c1da8f97ff83bb9be6a2fd040b69eabfae4c540749"
SHA_MUTUAL = "4377db47f567816d994d1824bd25adb7cb40e5bb9a96d00b74491f81258dc469"
SHA_VISION_FARMACIA = "84fec1f627d68084101c2afe62d8c6c15dc7deb0ae238b30a16cc0e8bf423132"
CAPACIDADES = ("recetas", "obras_sociales", "socios", "cuenta_corriente", "links_como_receta")
BLOQUES_COMPARTIDOS = ("SEGUIMIENTO", "DERIVACION", "RESERVAS", "CONFIRMACIONES",
                       "RESPUESTA_DIRECTA")
LINEAS_REUSABLES = ("CATALOGO_BUSQUEDA", "PAGO_SIN_LINKS", "PAGO_CORRECCION_CANTIDAD",
                    "VARIOS_PRIMERO_Y_DEMAS", "VARIOS_NUNCA_JUNTES")


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ── Prompts ───────────────────────────────────────────────────────────────────

def test_prompt_farmacia_identico_byte_a_byte():
    from app.services import intent_service
    for texto in (perfil_por_clave("farmacia").system_prompt, intent_service.SYSTEM_PROMPT):
        assert len(texto) == 14680
        assert _sha(texto) == SHA_FARMACIA
    # intent_service lo re-exporta: es el mismo objeto, no una copia.
    assert intent_service.SYSTEM_PROMPT is prompts.SYSTEM_PROMPT


def test_bloques_del_prompt_de_farmacia():
    for nombre in BLOQUES_COMPARTIDOS + LINEAS_REUSABLES:
        bloque = getattr(prompts, nombre)
        assert bloque.endswith("\n"), nombre
        assert bloque in prompts.SYSTEM_PROMPT, nombre


def test_prompts_no_importa_nada_de_app():
    fuente = inspect.getsource(prompts)
    assert not re.search(r"^\s*(from|import)\s+app\b", fuente, re.MULTILINE)


def test_resolver_plantilla_usa_replace_y_respeta_las_llaves_del_json():
    plantilla = 'Sos {comercio} {emoji}. Respondé {"intencion": "saludo"} {otra}'
    assert (prompts.resolver_plantilla(plantilla, "MO", "🐾")
            == 'Sos MO 🐾. Respondé {"intencion": "saludo"} {otra}')


def test_prompt_de_vision_de_farmacia_movido_byte_a_byte():
    from app.services import image_service
    assert image_service._PROMPT is prompts.VISION_PROMPT_FARMACIA
    assert _sha(prompts.VISION_PROMPT_FARMACIA) == SHA_VISION_FARMACIA
    assert VISION_FARMACIA.prompt is prompts.VISION_PROMPT_FARMACIA
    assert VISION_FARMACIA.categorias == ("receta", "bono", "credencial", "comprobante",
                                          "producto", "otro")


def test_vision_petshop():
    assert VISION_PETSHOP.prompt is prompts.VISION_PROMPT_PETSHOP
    assert VISION_PETSHOP.categorias == ("producto", "comprobante", "indicacion_veterinaria", "otro")
    # El esquema que se le pide al modelo lista exactamente las categorías.
    assert '"tipo": "producto|comprobante|indicacion_veterinaria|otro"' in VISION_PETSHOP.prompt
    bajo = VISION_PETSHOP.prompt.lower()
    for palabra in ("farmacia", "obra social", "bono", "credencial", "prepaga"):
        assert palabra not in bajo, palabra


# ── Registro de perfiles ──────────────────────────────────────────────────────

def test_perfil_farmacia():
    f = perfil_por_clave("farmacia")
    assert (f.clave, f.comercio, f.emoji, f.rotulo_kb) == (
        "farmacia", "Remedia", "💊", "INFORMACIÓN DE LA FARMACIA")
    assert all(getattr(f, c) is True for c in CAPACIDADES)
    assert f.sintomas == "farmaceutico"
    assert f.vision is VISION_FARMACIA
    assert f.venta is True and f.catalogo_csv_base is True
    assert (f.descriptor_tarjeta, f.razon_social, f.wordmark_html) == (
        "FARMACIA AMI", "Farmacia Mutual Independencia", "Remed<b>IA</b>")


def test_perfil_mutual_es_farmacia_sin_venta_con_su_prompt():
    f, m = perfil_por_clave("farmacia"), perfil_por_clave("mutual")
    assert m.clave == "mutual"
    assert m.system_prompt is SYSTEM_PROMPT_MUTUAL
    assert _sha(m.system_prompt) == SHA_MUTUAL
    assert m.venta is False
    assert all(getattr(m, c) is True for c in CAPACIDADES)
    assert m.catalogo_csv_base is True
    assert m.sintomas == "farmaceutico"
    assert m.vision is f.vision is VISION_FARMACIA
    assert m.rotulo_kb == "INFORMACIÓN DE LA FARMACIA"
    assert dict(m.textos) == dict(f.textos)
    assert m.vocabulario_audio == f.vocabulario_audio
    assert (m.comercio, m.emoji) == ("Remedia", "💊")
    assert (m.descriptor_tarjeta, m.razon_social, m.wordmark_html) == (
        f.descriptor_tarjeta, f.razon_social, f.wordmark_html)


def test_perfil_petshop():
    p = perfil_por_clave("petshop")
    assert (p.clave, p.comercio, p.emoji, p.rotulo_kb) == (
        "petshop", "Mascotas del Oeste", "🐾", "INFORMACIÓN DEL COMERCIO")
    assert all(getattr(p, c) is False for c in CAPACIDADES)
    assert p.sintomas == "derivar"
    assert p.vision is VISION_PETSHOP
    assert p.venta is True and p.catalogo_csv_base is False
    assert (p.descriptor_tarjeta, p.razon_social, p.wordmark_html) == ("", "", "")
    assert p.vocabulario_audio.prefijo == "Consulta a un petshop"
    assert p.vocabulario_audio.marcas_base == ()
    assert "{comercio}" not in p.system_prompt and "{emoji}" not in p.system_prompt
    assert "Soy el asistente virtual de Mascotas del Oeste" in p.system_prompt


def test_marcas_de_audio_de_farmacia_son_las_de_hoy():
    from app.services.sku_service import MARCAS_AUDIO_BASE
    f = perfil_por_clave("farmacia")
    assert f.vocabulario_audio.prefijo == "Consulta a una farmacia"
    assert f.vocabulario_audio.marcas_base is MARCAS_AUDIO_FARMACIA
    assert len(MARCAS_AUDIO_FARMACIA) == 38
    assert MARCAS_AUDIO_FARMACIA[:3] == ("Aveno", "Atopix", "Actron")
    assert MARCAS_AUDIO_FARMACIA == tuple(MARCAS_AUDIO_BASE)


# ── Textos del perfil (§3.4) ──────────────────────────────────────────────────

def test_claves_texto_rubro_son_las_13_de_la_spec():
    assert CLAVES_TEXTO_RUBRO == frozenset({
        "pedido_listo_retiro_message", "pedido_listo_envio_message",
        "efectivo_retiro_message", "efectivo_envio_message",
        "sintoma_farmaceutico_message", "receta_recibida_message",
        "socio_discount_message", "socio_discount_info_message",
        "socio_discount_off_message", "bono_recibido_message",
        "bono_no_reconocido_message", "bono_consulta_si_message",
        "comprobante_recibido_message",
    })
    assert CLAVES_TEXTO_RUBRO <= set(DEFAULTS)


@pytest.mark.parametrize("clave", ["farmacia", "mutual", "petshop"])
def test_todo_perfil_define_las_13_claves(clave):
    assert CLAVES_TEXTO_RUBRO <= set(perfil_por_clave(clave).textos)


@pytest.mark.parametrize("clave", ["farmacia", "mutual"])
def test_textos_de_farmacia_y_mutual_salen_de_defaults(clave):
    p = perfil_por_clave(clave)
    assert set(p.textos) == CLAVES_TEXTO_RUBRO
    assert {**DEFAULTS, **p.textos} == DEFAULTS


def test_textos_del_perfil_son_de_solo_lectura():
    with pytest.raises(TypeError):
        perfil_por_clave("petshop").textos["pedido_listo_retiro_message"] = "X"


def test_textos_petshop():
    p = perfil_por_clave("petshop")
    assert set(p.textos) == CLAVES_TEXTO_RUBRO | {"consulta_salud_message",
                                                  "indicacion_veterinaria_message"}
    for k, v in p.textos.items():
        assert not re.search(r"farmac|receta|socio|mutual|bono|obra social", v, re.I), k
        assert "💊" not in v, k
    assert p.textos["pedido_listo_retiro_message"].endswith("¡Te esperamos! 🐾")
    assert p.textos["sintoma_farmaceutico_message"] == ""
    assert p.textos["comprobante_recibido_message"] == (
        "¡Listo! Recibimos tu comprobante 🙌 Lo verificamos y te confirmamos en un rato.")
    assert p.textos["consulta_salud_message"] == (
        "Para temas de salud prefiero que te atienda una persona del equipo, así no te "
        "recomiendo nada a ciegas 🐾 Ya te paso. Si lo notás muy decaído o empeora, no "
        "esperes y consultá con un veterinario.")
    assert p.textos["indicacion_veterinaria_message"] == (
        "¡Hola {nombre}! Recibí la indicación del veterinario 🐾 Te paso con alguien del "
        "equipo que la revisa y te ayuda con lo que necesita tu mascota.")


@pytest.mark.parametrize("clave", ["farmacia", "mutual", "petshop"])
def test_invariantes_de_capacidades_y_textos(clave):
    p = perfil_por_clave(clave)
    if p.sintomas == "derivar":
        texto = p.textos["consulta_salud_message"]
        assert p.emoji in texto
        assert not re.search(r"farmac|receta|socio|obra social", texto, re.I)
    if "indicacion_veterinaria" in p.vision.categorias:
        assert "indicacion_veterinaria_message" in p.textos
    if not p.recetas:
        assert "receta" not in p.vision.categorias
    if not p.obras_sociales:
        assert "bono" not in p.vision.categorias
        assert "credencial" not in p.vision.categorias


# ── get_perfil: VERTICAL y COMERCIO_NOMBRE (§3.5) ─────────────────────────────

def test_vertical_desconocido_es_value_error_con_mensaje_claro(usar_perfil):
    with pytest.raises(ValueError) as e:
        usar_perfil("veterinaria")
    assert "veterinaria" in str(e.value)
    assert "farmacia, mutual, petshop" in str(e.value)


@pytest.mark.parametrize("valor,clave", [
    ("", "farmacia"), ("farmacia", "farmacia"), (" Petshop ", "petshop"), ("MUTUAL", "mutual"),
])
def test_vertical_se_normaliza(usar_perfil, valor, clave):
    assert usar_perfil(valor).clave == clave


def test_settings_sin_vertical_ni_comercio_es_farmacia(monkeypatch, usar_perfil):
    from app.config import Settings
    monkeypatch.delenv("VERTICAL", raising=False)
    monkeypatch.delenv("COMERCIO_NOMBRE", raising=False)
    s = Settings(_env_file=None)
    assert (s.vertical, s.comercio_nombre) == ("farmacia", "")
    assert usar_perfil(s.vertical).clave == "farmacia"


def test_comercio_nombre_sale_del_entorno(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("COMERCIO_NOMBRE", "MO Prueba")
    assert Settings(_env_file=None).comercio_nombre == "MO Prueba"


def test_comercio_nombre_en_petshop_resuelve_de_nuevo_el_prompt(usar_perfil):
    p = usar_perfil("petshop", comercio="MO Prueba")
    assert p.comercio == "MO Prueba"
    assert "Soy el asistente virtual de MO Prueba" in p.system_prompt
    assert "Mascotas del Oeste" not in p.system_prompt
    assert (p.descriptor_tarjeta, p.razon_social, p.wordmark_html) == ("", "", "")


def test_comercio_nombre_en_farmacia_no_toca_el_prompt(usar_perfil):
    p = usar_perfil("farmacia", comercio="Farmacia Centro")
    assert p.comercio == "Farmacia Centro"
    assert _sha(p.system_prompt) == SHA_FARMACIA
    assert (p.descriptor_tarjeta, p.razon_social, p.wordmark_html) == ("", "", "")


def test_comercio_nombre_en_blanco_no_pisa_nada(usar_perfil):
    assert usar_perfil("farmacia", comercio="   ") is perfil_por_clave("farmacia")


def test_get_perfil_cachea_y_perfil_por_clave_no_mira_el_entorno(usar_perfil):
    p = usar_perfil("petshop")
    assert get_perfil() is p
    assert perfil_por_clave("farmacia").clave == "farmacia"


def test_usar_perfil_a_cambia_a_petshop(usar_perfil):
    assert usar_perfil("petshop").clave == "petshop"


def test_usar_perfil_b_el_teardown_restauro_el_perfil_del_entorno():
    # Corre después del anterior (orden del archivo): sin el teardown de la
    # fixture, el petshop se filtraría acá.
    esperado = (os.environ.get("VERTICAL") or "").strip().lower() or "farmacia"
    assert get_perfil().clave == esperado


# ── Arranque (§3.5) ───────────────────────────────────────────────────────────

def test_arranque_con_vertical_desconocido_falla_antes_de_tocar_redis(usar_perfil, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    llamadas = []
    monkeypatch.setattr(main, "get_blob_store", lambda *a, **k: llamadas.append("redis"))
    with pytest.raises(ValueError):
        usar_perfil("veterinaria")          # deja VERTICAL=veterinaria hasta el teardown
    with pytest.raises(ValueError, match="veterinaria"):
        with TestClient(main.app):
            pass
    assert llamadas == []


def test_arranque_loguea_el_perfil_antes_de_tocar_redis(usar_perfil, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    import app.main as main

    class _Corte(BaseException):
        """Corta el lifespan en el primer acceso a Redis (el except del
        lifespan es `except Exception` y no la atrapa)."""

    def _cortar(*a, **k):
        raise _Corte()

    monkeypatch.setattr(main, "get_blob_store", _cortar)
    usar_perfil("petshop")
    with caplog.at_level(logging.INFO, logger="app.main"):
        with pytest.raises(_Corte):
            with TestClient(main.app):
                pass
    assert "Perfil de rubro: petshop (Mascotas del Oeste)" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════
# Task 4 — prompt de petshop (spec §4.1 y §6.2)
# ══════════════════════════════════════════════════════════════════════════════
import hashlib as _hashlib
import re as _re

_PROHIBIDAS_PROMPT_PETSHOP = (
    "farmac", "receta", "socio", "obra social", "remedia", "mercado pago", "mutual",
    "medicament", "remedio", "semanalmente", "{comercio}", "{emoji}", "ibuprofeno",
    "lotrial", "bayer", "aveno", "talco",
)
_OBLIGATORIAS_PROMPT_PETSHOP = (
    "Soy el asistente virtual de Mascotas del Oeste",
    "¡Hola! Soy el asistente virtual de Mascotas del Oeste 🐾 ¿En qué te puedo ayudar?",
    "El sistema envía el link de pago después de que confirme.",
    "SALUD DE LA MASCOTA",
    "DESCUENTOS, PROMOCIONES Y CUPONES",
    "Nunca inventes la dirección",
    "especie",
)
# Bloques de farmacia que el prompt de petshop reusa tal cual (prompts.py).
_BLOQUES_COMPARTIDOS = (
    "SEGUIMIENTO", "DERIVACION", "RESERVAS", "CONFIRMACIONES", "RESPUESTA_DIRECTA",
    "CATALOGO_BUSQUEDA", "PAGO_SIN_LINKS", "PAGO_CORRECCION_CANTIDAD",
    "VARIOS_PRIMERO_Y_DEMAS", "VARIOS_NUNCA_JUNTES",
)
_SHA_PROMPT_FARMACIA = "1953a4e6815d855e635406c1da8f97ff83bb9be6a2fd040b69eabfae4c540749"


def _prompt_petshop() -> str:
    from app.services.perfil import perfil_por_clave
    return perfil_por_clave("petshop").system_prompt


def _claves_json(prompt: str) -> list[str]:
    """Claves del esquema JSON de FORMATO DE RESPUESTA, en orden."""
    bloque = prompt[prompt.index("FORMATO DE RESPUESTA:"):]
    bloque = bloque[bloque.index("{"): bloque.index("}") + 1]
    return _re.findall(r'^\s*"(\w+)":', bloque, _re.M)


def test_intent_service_reexporta_el_prompt_de_farmacia():
    from app.services import intent_service, prompts
    assert intent_service.SYSTEM_PROMPT is prompts.SYSTEM_PROMPT
    assert intent_service._SYSTEM_CACHED is prompts.SYSTEM_PROMPT
    assert _hashlib.sha256(intent_service.SYSTEM_PROMPT.encode("utf-8")).hexdigest() == _SHA_PROMPT_FARMACIA


def test_prompt_petshop_sin_vocabulario_de_farmacia():
    low = _prompt_petshop().lower()
    assert [w for w in _PROHIBIDAS_PROMPT_PETSHOP if w in low] == []


def test_prompt_petshop_frases_obligatorias():
    p = _prompt_petshop()
    assert [f for f in _OBLIGATORIAS_PROMPT_PETSHOP if f not in p] == []


def test_prompt_petshop_contrato_con_farmacia():
    from app.services import prompts
    farm, pet = prompts.SYSTEM_PROMPT, _prompt_petshop()
    # Cada bloque compartido está, tal cual, en los dos prompts
    for nombre in _BLOQUES_COMPARTIDOS:
        bloque = getattr(prompts, nombre)
        assert bloque and bloque in farm and bloque in pet, nombre
    # El enum de `intencion` es la misma línea en los dos
    enum_farm = [x for x in farm.splitlines() if x.lstrip().startswith('"intencion":')]
    enum_pet = [x for x in pet.splitlines() if x.lstrip().startswith('"intencion":')]
    assert len(enum_farm) == 1 and enum_farm == enum_pet
    # Mismas claves del JSON, en el mismo orden
    assert _claves_json(farm) == _claves_json(pet) == [
        "intencion", "entidad_producto", "entidades_adicionales", "agregar_al_pedido", "cantidad",
        "sku_seleccionado_index", "confirmacion", "solicita_imagen", "por_sintoma", "respuesta"]
    # Reglas duras de venta, idénticas
    for frase in (
        "- REGLA ESTRICTA: solo podés ofrecer productos que aparezcan en [RESULTADOS DEL CATÁLOGO] u [OPCIONES MOSTRADAS].",
        "- NUNCA incluyas URLs, links ni texto que parezca un link en tu respuesta.",
    ):
        assert frase in farm and frase in pet
    # Las filas de la matriz que no son del rubro (todas menos saludo y consulta_abierta)
    def _filas_fijas(p):
        return [x for x in p.splitlines() if x.startswith("| ")
                and not x.startswith(("| saludo |", "| consulta_abierta |"))]
    assert len(_filas_fijas(farm)) == 8 and _filas_fijas(farm) == _filas_fijas(pet)


def test_prompt_petshop_resuelve_comercio_nombre_y_farmacia_conserva_el_hash(usar_perfil):
    p = usar_perfil("petshop", comercio="MO Prueba")
    assert p.comercio == "MO Prueba"
    assert "Soy el asistente virtual de MO Prueba" in p.system_prompt
    assert "¡Hola! Soy el asistente virtual de MO Prueba 🐾 ¿En qué te puedo ayudar?" in p.system_prompt
    assert "Mascotas del Oeste" not in p.system_prompt
    f = usar_perfil("farmacia", comercio="MO Prueba")
    assert _hashlib.sha256(f.system_prompt.encode("utf-8")).hexdigest() == _SHA_PROMPT_FARMACIA
