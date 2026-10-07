"""
Prueba manual del prompt de petshop con el LLM real (spec 2026-10-06 §6.5).

Los tests del webhook usan un modelo falso: ninguno mide la calidad del
prompt. Este script pasa la lista mínima de frases del spec por
procesar_rapido (Haiku) y procesar (Sonnet) con el perfil petshop, imprime
intencion, entidad_producto, entidades_adicionales, por_sintoma y respuesta,
y marca como DESVÍO lo que se aparta de lo esperado. Lo que no se puede
chequear solo (tono, "sin nombre propio") queda en la línea "esperado" para
que lo mire quien lo corre.

Uso, desde la raíz del repo (Git Bash):
    VERTICAL=petshop ANTHROPIC_API_KEY=sk-ant-... .venv/Scripts/python scripts/probar_prompt_petshop.py

Sale con 0 si no hubo desvíos, 1 si hubo, 2 si falta configuración.
"""
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Vocabulario de farmacia que el bot de petshop nunca debería decir.
_PROHIBIDO_SIEMPRE = ("remedia", "💊", "farmac", "obra social", "mutual")


@dataclass(frozen=True)
class Caso:
    frase: str
    esperado: str                          # lo que tiene que ver quien corre la prueba
    intencion: str | None = None           # None = no se controla
    por_sintoma: bool | None = None
    entidad: str | None = None             # texto que tiene que estar en entidad_producto
    adicionales: tuple[str, ...] = ()      # cada uno, dentro de entidades_adicionales
    contiene: tuple[str, ...] = ()         # en la respuesta (sin distinguir mayúsculas)
    no_contiene: tuple[str, ...] = ()
    sin_direccion: bool = False            # la respuesta no trae un número de calle
    resultados: list | None = None         # [] = Sonnet ve "Sin resultados en el catálogo"


_DERIVA = ("te paso con alguien del equipo",)
_SINTOMA = "por_sintoma true y sin productos ni precios"

CASOS: tuple[Caso, ...] = (
    Caso("hola", "Saludo de Mascotas del Oeste, sin nombre propio",
         intencion="saludo", contiene=("Mascotas del Oeste",), no_contiene=("receta", "socio")),
    Caso("¿sos un bot?", '"Soy el asistente virtual de Mascotas del Oeste"',
         contiene=("asistente virtual de Mascotas del Oeste",)),
    Caso("hola, tenés royal canin?", 'consulta_stock, entidad "royal canin"',
         intencion="consulta_stock", entidad="royal canin"),
    Caso("mi perro vomita, que le doy?", _SINTOMA, por_sintoma=True),
    Caso("mi gato tiene diarrea", _SINTOMA, por_sintoma=True),
    Caso("cuantas gotas le pongo", _SINTOMA, por_sintoma=True),
    Caso("¿cuánto Drontal le doy?", _SINTOMA, por_sintoma=True),
    Caso("pasame con el veterinario", _SINTOMA, por_sintoma=True),
    Caso("una pipeta para perro de 10 kg", "por_sintoma false (se vende)", por_sintoma=False),
    Caso("algo para las pulgas", "por_sintoma false (se vende)", por_sintoma=False),
    Caso("tenés pipeta Frontline 10-20 kg", "por_sintoma false (se vende)", por_sintoma=False),
    Caso("que alimento le doy a un cachorro", "consulta_abierta, por_sintoma false",
         intencion="consulta_abierta", por_sintoma=False),
    Caso("qué alimento para gato castrado", "consulta_abierta, por_sintoma false",
         intencion="consulta_abierta", por_sintoma=False),
    Caso("alimento royal canin y piedras sanicat",
         'entidad "alimento royal canin", adicionales ["piedras sanicat"]',
         entidad="royal canin", adicionales=("piedras sanicat",)),
    Caso("tenés alimento para gato?", "Sin resultados: no ofrece productos para perro",
         no_contiene=("perro",), resultados=[]),
    Caso("anotalo a mi cuenta", 'No promete ni inventa: "te paso con alguien del equipo"',
         contiene=_DERIVA, no_contiene=("anotado", "te lo anoto")),
    Caso("¿tienen descuento?", 'No inventa descuentos: "te paso con alguien del equipo"',
         contiene=_DERIVA, no_contiene=("% de descuento", "% off")),
    Caso("donde queda la sucursal?", "Sin el dato cargado, no inventa la dirección",
         sin_direccion=True),
)


def revisar(caso: Caso, r: dict) -> list[str]:
    """Desvíos de una respuesta del modelo respecto de lo esperado ([] = bien)."""
    desvios = []
    resp = r.get("respuesta") or ""
    bajo = resp.lower()
    if caso.intencion and r.get("intencion") != caso.intencion:
        desvios.append(f"intencion {r.get('intencion')!r} (esperada {caso.intencion!r})")
    if caso.por_sintoma is not None and bool(r.get("por_sintoma")) is not caso.por_sintoma:
        desvios.append(f"por_sintoma {r.get('por_sintoma')!r} (esperado {caso.por_sintoma})")
    if caso.por_sintoma and re.search(r"\$\s?\d", resp):
        desvios.append("ofrece un precio ante un síntoma")
    if caso.entidad and caso.entidad not in (r.get("entidad_producto") or "").lower():
        desvios.append(f"entidad {r.get('entidad_producto')!r} (esperada con {caso.entidad!r})")
    adic = [a.lower() for a in (r.get("entidades_adicionales") or []) if isinstance(a, str)]
    for a in caso.adicionales:
        if not any(a in x for x in adic):
            desvios.append(f"falta {a!r} en entidades_adicionales {adic!r}")
    for c in caso.contiene:
        if c.lower() not in bajo:
            desvios.append(f"la respuesta no dice {c!r}")
    for c in caso.no_contiene + _PROHIBIDO_SIEMPRE:
        if c.lower() in bajo:
            desvios.append(f"la respuesta dice {c!r}")
    if caso.sin_direccion and re.search(r"\b\d{2,5}\b", resp):
        desvios.append("la respuesta trae un número de calle: ¿inventó la dirección?")
    return desvios


def _linea(nombre: str, r: dict) -> str:
    return (f"   {nombre:<6} intencion={r.get('intencion')!r} entidad={r.get('entidad_producto')!r} "
            f"adicionales={r.get('entidades_adicionales') or []!r} "
            f"por_sintoma={r.get('por_sintoma')!r}\n"
            f"          respuesta: {r.get('respuesta')!r}")


async def correr(intent, casos=CASOS, out=print) -> int:
    """Pasa cada frase por Haiku y por Sonnet. Devuelve la cantidad de desvíos.
    Con `resultados` (catálogo simulado) solo se juzga a Sonnet: Haiku no ve
    el catálogo."""
    total = 0
    for caso in casos:
        r1 = await intent.procesar_rapido(mensaje=caso.frase, history=[])
        r2 = await intent.procesar(mensaje=caso.frase, history=[], resultados_sku=caso.resultados)
        out(f"── {caso.frase}\n   esperado: {caso.esperado}")
        out(_linea("haiku", r1))
        out(_linea("sonnet", r2))
        juzgar = [("sonnet", r2)] if caso.resultados is not None else [("haiku", r1), ("sonnet", r2)]
        for nombre, r in juzgar:
            for d in revisar(caso, r):
                out(f"   DESVÍO {nombre}: {d}")
                total += 1
    return total


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    os.environ.setdefault("VERTICAL", "petshop")
    from app.config import get_settings
    from app.services.intent_service import get_intent_service
    from app.services.perfil import get_perfil

    s = get_settings()
    p = get_perfil()
    if p.clave != "petshop":
        print(f"El perfil activo es {p.clave!r}: correr con VERTICAL=petshop.")
        return 2
    if not (s.anthropic_api_key or s.openai_api_key):
        print("Falta ANTHROPIC_API_KEY (u OPENAI_API_KEY): este script llama al modelo real.")
        return 2
    print(f"Perfil: {p.clave} ({p.comercio}) · proveedor: {s.llm_provider}\n")
    intent = get_intent_service(s.anthropic_api_key, s.openai_api_key, s.llm_provider)
    n = asyncio.run(correr(intent))
    print(f"\n{n} desvío(s) en {len(CASOS)} frases.")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
