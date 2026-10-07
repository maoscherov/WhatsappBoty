"""
Perfil de rubro: qué sabe hacer el bot en este deploy (farmacia, mutual o
petshop). Cada gancho del código pregunta por una CAPACIDAD del perfil
(`get_perfil().recetas`), nunca por el nombre del rubro.

Imports: solo stdlib, app.config, app.services.prompts y
app.services.mutual_helper (que solo usa stdlib). config_service.DEFAULTS se
importa ADENTRO de _registro(), porque config_service importa este módulo.
Nunca importar intent_service, sku_service, image_service ni checkout_helper:
todos importan perfil y se armaría un ciclo.

Regla de uso: get_perfil() se llama en el momento de usarlo, adentro de cada
función. Nunca se guarda en una variable de módulo, en un __init__ ni en un
singleton (IntentService, PaymentService, PaywayService, ConfigService).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from types import MappingProxyType
from typing import Literal

from app.config import get_settings
from app.services import prompts
from app.services.mutual_helper import SYSTEM_PROMPT_MUTUAL


@dataclass(frozen=True)
class VocabularioAudio:
    prefijo: str                    # "Consulta a una farmacia"
    marcas_base: tuple[str, ...]    # marcas fijas, antes de las del catálogo


@dataclass(frozen=True)
class VisionPerfil:
    categorias: tuple[str, ...]     # tipos válidos; cualquier otro pasa a "otro"
    prompt: str = field(repr=False)  # prompt del clasificador de imágenes


@dataclass(frozen=True)
class Perfil:
    clave: str                      # "farmacia" | "mutual" | "petshop"
    comercio: str                   # nombre visible; COMERCIO_NOMBRE lo pisa
    system_prompt: str = field(repr=False)
    emoji: str
    rotulo_kb: str                  # sin corchetes
    vocabulario_audio: VocabularioAudio
    recetas: bool
    obras_sociales: bool
    socios: bool
    cuenta_corriente: bool
    links_como_receta: bool
    sintomas: Literal["farmaceutico", "derivar"]
    vision: VisionPerfil
    textos: Mapping[str, str] = field(repr=False)   # MappingProxyType: un test no lo puede mutar
    # Agregados al diseño (spec §3.7)
    venta: bool                     # False = solo informa (mutual)
    catalogo_csv_base: bool         # puede cargar data/catalogo_base.csv
    descriptor_tarjeta: str = ""    # "" = derivado de comercio
    razon_social: str = ""          # "" = comercio
    wordmark_html: str = ""         # "" = html.escape(comercio)


# Las 13 claves de DEFAULTS con vocabulario o emoji de farmacia (spec §3.4).
# Todo perfil las define todas: un fallback perfil.textos[k] nunca da KeyError.
CLAVES_TEXTO_RUBRO: frozenset[str] = frozenset({
    "pedido_listo_retiro_message", "pedido_listo_envio_message",
    "efectivo_retiro_message", "efectivo_envio_message",
    "sintoma_farmaceutico_message", "receta_recibida_message",
    "socio_discount_message", "socio_discount_info_message",
    "socio_discount_off_message", "bono_recibido_message",
    "bono_no_reconocido_message", "bono_consulta_si_message",
    "comprobante_recibido_message",
})

# Marcas que más se piden por audio en la farmacia (caso real 23/9), en el
# mismo orden que sku_service.MARCAS_AUDIO_BASE.
MARCAS_AUDIO_FARMACIA: tuple[str, ...] = (
    "Aveno", "Atopix", "Actron", "Ibupirac", "Ibuevanol", "Tafirol", "Bayaspirina", "Buscapina",
    "Sertal", "Dermaglos", "Isdin", "La Roche-Posay", "Eucerin", "Cetaphil", "Hyalu C",
    "Bagovit", "Lanzopral", "Omeprazol", "Holomagnesio", "Curflex", "Dioxaflex", "Novalgina",
    "Refrianex", "Mejoral", "Geniol", "Aspirina", "Uvasal", "Sal de frutas Eno", "Loratadina",
    "Allegra", "Cepage", "Cassará", "Bagó", "Roemmers", "Elea", "Vichy", "Avene", "Bioderma",
)

VISION_FARMACIA = VisionPerfil(
    categorias=("receta", "bono", "credencial", "comprobante", "producto", "otro"),
    prompt=prompts.VISION_PROMPT_FARMACIA,
)
VISION_PETSHOP = VisionPerfil(
    categorias=("producto", "comprobante", "indicacion_veterinaria", "otro"),
    prompt=prompts.VISION_PROMPT_PETSHOP,
)

_COMERCIO_PETSHOP = "Mascotas del Oeste"
_EMOJI_PETSHOP = "🐾"
_IMAGEN_AL_EQUIPO = ("¡Hola {nombre}! Recibí tu imagen 🙌 Te paso con alguien del equipo "
                     "que la mira y te ayuda.")
_PRECIO_DE_LISTA = "Por ahora te puedo ofrecer el precio de lista 🙂"


def _textos_petshop(emoji: str) -> dict[str, str]:
    """Las 13 de CLAVES_TEXTO_RUBRO más las dos de capacidades propias del
    petshop (consulta_salud_message e indicacion_veterinaria_message). Varias
    nunca se leen (sus ganchos están apagados por capacidad), pero igual son
    neutras: si un gancho quedara sin gate, el cliente no lee receta/bono/socio."""
    return {
        "pedido_listo_retiro_message": (
            "🎉 *¡Tu pedido está listo para retirar!*\n\n"
            "*{producto}* — ${total}\n"
            "🔑 *Código de retiro: {codigo}*{horario}\n\n"
            "Presentá este código y te lo entregamos. ¡Te esperamos! " + emoji
        ),
        "pedido_listo_envio_message": (
            "🎉 *¡Tu pedido está listo!*\n\n"
            "*{producto}* — ${total}\n"
            "🚚 Sale para *{direccion}*. Te avisamos cuando esté en camino. " + emoji
        ),
        "efectivo_retiro_message": (
            "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
            "*{producto}* — ${total}\n"
            "💵 Lo pagás en efectivo al retirar.{plazo}\n"
            "🔑 *Tu código de retiro es: {codigo}*\n\n¡Muchas gracias! " + emoji
        ),
        "efectivo_envio_message": (
            "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
            "*{producto}* — ${total}{envio}\n"
            "🚚 Te lo enviamos a *{direccion}* y lo pagás en efectivo al recibirlo.\n"
            "📋 Código de pedido: *{codigo}*\n\n¡Muchas gracias! " + emoji
        ),
        "sintoma_farmaceutico_message": "",          # vacío: apaga el agregado
        "receta_recibida_message": _IMAGEN_AL_EQUIPO,
        "bono_recibido_message": _IMAGEN_AL_EQUIPO,
        "bono_no_reconocido_message": _IMAGEN_AL_EQUIPO,
        "socio_discount_message": "🎉 Te aplicamos un {pct}% de descuento (precio de lista: ${antes}).",
        "socio_discount_info_message": _PRECIO_DE_LISTA,
        "socio_discount_off_message": _PRECIO_DE_LISTA,
        "bono_consulta_si_message": "Eso lo confirma el equipo: te paso con alguien para que lo vea con vos 🙂",
        # Sin {nombre}: MO no tiene padrón y hoy saldría "¡Listo !".
        "comprobante_recibido_message": (
            "¡Listo! Recibimos tu comprobante 🙌 Lo verificamos y te confirmamos en un rato."
        ),
        # Solo en perfiles con sintomas == "derivar".
        "consulta_salud_message": (
            "Para temas de salud prefiero que te atienda una persona del equipo, así no te "
            "recomiendo nada a ciegas " + emoji + " Ya te paso. Si lo notás muy decaído o "
            "empeora, no esperes y consultá con un veterinario."
        ),
        # Solo en perfiles con "indicacion_veterinaria" en vision.categorias.
        "indicacion_veterinaria_message": (
            "¡Hola {nombre}! Recibí la indicación del veterinario " + emoji + " Te paso con "
            "alguien del equipo que la revisa y te ayuda con lo que necesita tu mascota."
        ),
    }


@lru_cache
def _registro() -> Mapping[str, Perfil]:
    # Adentro de la función: config_service importa perfil (valores_base).
    from app.services.config_service import DEFAULTS

    farmacia = Perfil(
        clave="farmacia",
        comercio="Remedia",
        system_prompt=prompts.SYSTEM_PROMPT,
        emoji="💊",
        rotulo_kb="INFORMACIÓN DE LA FARMACIA",
        vocabulario_audio=VocabularioAudio("Consulta a una farmacia", MARCAS_AUDIO_FARMACIA),
        recetas=True,
        obras_sociales=True,
        socios=True,
        cuenta_corriente=True,
        links_como_receta=True,
        sintomas="farmaceutico",
        vision=VISION_FARMACIA,
        # Desde DEFAULTS: el merge {**DEFAULTS, **textos} no cambia nada.
        textos=MappingProxyType({k: DEFAULTS[k] for k in CLAVES_TEXTO_RUBRO}),
        venta=True,
        catalogo_csv_base=True,
        descriptor_tarjeta="FARMACIA AMI",
        razon_social="Farmacia Mutual Independencia",
        wordmark_html="Remed<b>IA</b>",
    )
    # La mutual lleva todas las capacidades de farmacia en True: los bloques de
    # imagen, horario, link y padrón corren hoy ANTES del desvío a _flujo_mutual.
    mutual = replace(farmacia, clave="mutual", system_prompt=SYSTEM_PROMPT_MUTUAL, venta=False)
    petshop = Perfil(
        clave="petshop",
        comercio=_COMERCIO_PETSHOP,
        system_prompt=prompts.resolver_plantilla(
            prompts.SYSTEM_PROMPT_PETSHOP_PLANTILLA, _COMERCIO_PETSHOP, _EMOJI_PETSHOP),
        emoji=_EMOJI_PETSHOP,
        rotulo_kb="INFORMACIÓN DEL COMERCIO",
        vocabulario_audio=VocabularioAudio("Consulta a un petshop", ()),
        recetas=False,
        obras_sociales=False,
        socios=False,
        cuenta_corriente=False,
        links_como_receta=False,
        sintomas="derivar",
        vision=VISION_PETSHOP,
        textos=MappingProxyType(_textos_petshop(_EMOJI_PETSHOP)),
        venta=True,
        catalogo_csv_base=False,
    )
    return MappingProxyType({"farmacia": farmacia, "mutual": mutual, "petshop": petshop})


@lru_cache
def get_perfil() -> Perfil:
    """Perfil del deploy según VERTICAL (y COMERCIO_NOMBRE). Un VERTICAL
    desconocido es un ValueError: el lifespan de main.py lo deja subir y el
    arranque se corta."""
    s = get_settings()
    clave = (s.vertical or "").strip().lower() or "farmacia"
    registro = _registro()
    if clave not in registro:
        raise ValueError(f"VERTICAL desconocido: {s.vertical!r}. "
                         "Valores válidos: farmacia, mutual, petshop")
    p = registro[clave]
    nombre = (s.comercio_nombre or "").strip()
    if nombre:
        # Todo sale del nombre: se vacían los tres campos de marca.
        p = replace(p, comercio=nombre, descriptor_tarjeta="",
                    razon_social="", wordmark_html="")
        if p.clave == "petshop":
            # Farmacia y mutual conservan su prompt constante (los bytes).
            p = replace(p, system_prompt=prompts.resolver_plantilla(
                prompts.SYSTEM_PROMPT_PETSHOP_PLANTILLA, nombre, p.emoji))
    return p


def perfil_por_clave(clave: str) -> Perfil:
    """Perfil del registro sin mirar el entorno ni el cache (para tests)."""
    return _registro()[clave]
