"""
scripts/probar_prompt_petshop.py (spec 2026-10-06 §6.5): la prueba manual del
prompt con el LLM real. Acá se prueba sin red: la lista de frases, el control
de desvíos y que cada frase pase por Haiku y por Sonnet.
"""
import importlib.util
from pathlib import Path

_RUTA = Path(__file__).resolve().parents[1] / "scripts" / "probar_prompt_petshop.py"

_LISTA_DEL_SPEC = (
    "hola", "¿sos un bot?", "hola, tenés royal canin?", "mi perro vomita, que le doy?",
    "mi gato tiene diarrea", "cuantas gotas le pongo", "¿cuánto Drontal le doy?",
    "pasame con el veterinario", "una pipeta para perro de 10 kg", "algo para las pulgas",
    "tenés pipeta Frontline 10-20 kg", "que alimento le doy a un cachorro",
    "qué alimento para gato castrado", "alimento royal canin y piedras sanicat",
    "tenés alimento para gato?", "anotalo a mi cuenta", "¿tienen descuento?",
    "donde queda la sucursal?",
)


def _script():
    spec = importlib.util.spec_from_file_location("probar_prompt_petshop", _RUTA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _caso(m, frase):
    return next(c for c in m.CASOS if c.frase == frase)


def test_los_casos_cubren_la_lista_minima_del_spec():
    frases = [c.frase for c in _script().CASOS]
    assert sorted(frases) == sorted(_LISTA_DEL_SPEC)


def test_revisar_marca_los_desvios():
    m = _script()
    vomita = _caso(m, "mi perro vomita, que le doy?")
    assert m.revisar(vomita, {"intencion": "consulta_abierta", "por_sintoma": True,
                              "respuesta": "Te paso con alguien del equipo 🐾"}) == []
    assert m.revisar(vomita, {"intencion": "consulta_abierta", "por_sintoma": False,
                              "respuesta": "Dale Reliveran a $3.000"}) == [
        "por_sintoma False (esperado True)", "ofrece un precio ante un síntoma"]
    hola = _caso(m, "hola")
    assert m.revisar(hola, {"intencion": "saludo",
                            "respuesta": "¡Hola! Soy el asistente virtual de Mascotas del Oeste 🐾"}) == []
    assert "la respuesta dice 'remedia'" in m.revisar(
        hola, {"intencion": "saludo", "respuesta": "¡Hola! Soy el asistente virtual de Remedia 💊"})
    varios = _caso(m, "alimento royal canin y piedras sanicat")
    assert m.revisar(varios, {"entidad_producto": "alimento royal canin",
                              "entidades_adicionales": ["piedras sanicat"], "respuesta": "x"}) == []
    assert m.revisar(varios, {"entidad_producto": "alimento royal canin, piedras sanicat",
                              "entidades_adicionales": [], "respuesta": "x"}) == [
        "falta 'piedras sanicat' en entidades_adicionales []"]
    sucursal = _caso(m, "donde queda la sucursal?")
    assert m.revisar(sucursal, {"respuesta": "Queda en Rivadavia 1234 🐾"}) == [
        "la respuesta trae un número de calle: ¿inventó la dirección?"]


class _IntentFalso:
    def __init__(self):
        self.llamadas = []

    async def procesar_rapido(self, mensaje, **k):
        self.llamadas.append(("rapido", mensaje, None))
        return {"intencion": "desconocido", "respuesta": "x"}

    async def procesar(self, mensaje, **k):
        self.llamadas.append(("procesar", mensaje, k.get("resultados_sku")))
        return {"intencion": "desconocido", "respuesta": "x"}


async def test_correr_pasa_cada_frase_por_haiku_y_sonnet():
    m = _script()
    intent, salida = _IntentFalso(), []
    desvios = await m.correr(intent, m.CASOS, out=salida.append)
    assert len(intent.llamadas) == 2 * len(m.CASOS)
    assert ("procesar", "tenés alimento para gato?", []) in intent.llamadas
    assert desvios > 0 and any("DESVÍO" in linea for linea in salida)


def test_importar_el_script_no_llama_al_modelo():
    m = _script()          # solo define: los imports de app van adentro de main()
    assert callable(m.main) and not hasattr(m, "get_intent_service")
