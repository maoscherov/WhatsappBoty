"""
Tanda 2 de bugs (14/10): búsqueda. "No lo tengo" con stock y opciones de
menos (C-4760, C-4694, C-4736, C-4232, C-4455, C-4299, C-4873, C-4855).
"""
import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as ch
from app.services.sku_service import SKUService
from tests.test_webhook_secuencias import (PHONE, _Cfg, _Img, _Intent, _Rag, _Rec, _Socios,
                                           _Wa, _msg)

_FILAS = [
    ("1", "Nosotras multiestilo con aloe protector ENV x 20", 432.3, "Nosotras", 0),
    ("2", "NOSOTRAS PROT.MULTIES ENV x 20", 4160.7, "Nosotras", 2),
    ("3", "NOSOTRAS PROT.MULTIES ENV x 60", 11231.68, "Nosotras", 6),
    ("4", "NOSOTRAS PROT.LARGO ENV x 50", 8377.66, "Nosotras", 5),
    ("5", "fluorogel blanqueador ENV x 60", 10540.29, "Fluorogel", 4),
    ("6", "FLUOROGEL DIENTES SENSIBLES GEL x 60", 9499.0, "Fluorogel", 2),
    ("7", "DOVE ANTIBACTERIAL JLI x 250", 7667.0, "Dove", 4),
    ("8", "DOVE ORIGINAL JAB x 90", 2627.57, "Dove", 8),
    ("9", "OB ORIGINAL MEDIO TAM x 8", 5310.27, "Ob", 3),
    ("10", "GUANTES DESCARTABLES DE NITRILO MEDIANOS ENV x 100", 8955.66, "Guantes", 0),
    ("11", "COREGA TABS PRO ORTODONCIA EFERV TAB x 30", 11081.0, "Corega", 66),
    ("12", "ULTRA COREGA CREMA MENTA PMO x 70", 17439.38, "Corega", 10),
    ("13", "CAVIAHUE SOLAR FPS50 PMO x 150", 49894.18, "Caviahue", 3),
    ("14", "CEPAGE FILTRES FLUIDE COLOR EMU x 50", 36200.0, "Cepage", 8),
    ("15", "QUELAT CITRATO DE MAGNESIO 400 COM x 30", 31495.17, "Quelat", 0),
    ("16", "QUELAT MAGNESIO COM x 30", 36618.79, "Quelat", 1),
    ("17", "GARNIER SKIN ACT BIFA AGUA MIC LOC x 400", 29616.73, "Garnier", 3),
    ("18", "EXIMIA AQUA MAT MICELAR P OLEOSA LOC x 200", 36416.0, "Eximia", 2),
    ("19", "KOREIA AGUA MICELAR ENV x 200", 11341.88, "Koreia", 5),
    ("20", "DERMAGLOS AGUA MICELAR ENV x 200", 26244.58, "Dermaglos", 4),
    ("21", "LA PASIONARIA AGUA MICELAR ENV x 200", 22000.0, "La Pasionaria", 0),
    ("22", "BIODERMA SENSIBIO AGUA MICELAR ENV x 250", 41000.0, "Bioderma", 2),
    ("23", "NIVEA AGUA MICELAR ENV x 400", 12000.0, "Nivea", 6),
    ("24", "LOREAL AGUA MICELAR ENV x 200", 15000.0, "Loreal", 3),
    ("25", "CETAPHIL AGUA MICELAR ENV x 300", 30000.0, "Cetaphil", 2),
]


def _sku():
    return SKUService.from_rows([
        {"external_id": i, "name": n, "price": p, "brand": m, "stock": st,
         "category": "Perfumeria", "requiere_receta": "no"} for i, n, p, m, st in _FILAS])


@pytest.fixture(scope="module")
def sku():
    return _sku()


def _ids(res, n=3):
    return [r["sku_id"] for r in res[:n]]


def test_multiestilo_encuentra_prot_multies(sku):
    assert {"2", "3"} & set(_ids(sku.buscar("nosotras multiestilo")))
    assert "3" in _ids(sku.buscar("nosotras diarios multiforma 60"), 1)


def test_pasta_blanqueadora_encuentra_el_blanqueador(sku):
    assert _ids(sku.buscar("fluorogel pasta blanqueadora"), 1) == ["5"]


def test_jabon_en_barra_no_da_el_liquido(sku):
    assert _ids(sku.buscar("jabon dove azul en barra"), 1) == ["8"]


def test_tampones_medianos_no_son_guantes(sku):
    res = sku.buscar("tampones medianos")
    assert _ids(res, 1) == ["9"]


def test_corega_tap_y_pastillas_para_la_protesis(sku):
    assert "11" in _ids(sku.buscar("corega tap"))
    assert _ids(sku.buscar("pastillas para limpiar protesis"), 1) == ["11"]


# "protector solar con color" se mide contra el catálogo real en
# scripts/busqueda_casos.json (con 20 productos todo término parece una marca).


def test_consulta_generica(sku):
    assert ch.es_consulta_generica("agua micelar", sku)
    assert not ch.es_consulta_generica("agua micelar koreia", sku)


def test_aviso_sin_stock_del_pedido(sku):
    res = sku.buscar("citrato de magnesio quelat")
    ofrecido = next(r for r in res if r["sku_id"] == "16")
    aviso = ch.aviso_sin_stock_del_pedido(
        "citrato de magnesio quelat", res, ofrecido,
        "Tengo disponible el Quelat Magnesio Com x 30 a $29.295,03. ¿Te gustaría?")
    assert aviso and "CITRATO" in aviso
    # si ya lo dijo, no se repite
    assert ch.aviso_sin_stock_del_pedido(
        "citrato de magnesio quelat", res, ofrecido,
        "No tengo el citrato en este momento. Tengo el Quelat Magnesio a $29.295,03.") is None


@pytest.fixture
def entorno(monkeypatch):
    from app.services.session_service import SessionService

    def armar(guion):
        deps = {
            "wa": _Wa(), "sku": _sku(),
            "session": SessionService("redis://127.0.0.1:1"),
            "intent": _Intent(guion), "payment": _Rec(), "audio": _Rec(),
            "image": _Img(), "perf": _Rec(), "config": _Cfg(),
            "socios": _Socios(), "msgs": _Rec(), "metrics": _Rec(), "rag": _Rag(),
        }
        monkeypatch.setattr(wh, "_deps", lambda s=None: deps)
        return deps

    return armar


async def test_otra_marca_trae_las_que_no_se_mostraron(entorno):
    """C-4855: "¿No tenés otra marca?" repitió Garnier y Eximia y dijo que no
    había otras, habiendo Koreia y Dermaglós."""
    guion = {
        "Quiero un agua micelar": {"intencion": "consulta_stock", "entidad_producto": "agua micelar",
                                   "respuesta": "Tengo estas opciones."},
        "No tenes otra marca ?": {"intencion": "consulta_stock", "entidad_producto": "agua micelar",
                                  "respuesta": "Tengo estas otras."},
    }
    deps = entorno(guion)
    vistos = []
    real = deps["intent"].procesar

    async def _proc(mensaje, **k):
        vistos.append([r["sku_id"] for r in (k.get("resultados_sku") or [])])
        return await real(mensaje, **k)
    deps["intent"].procesar = _proc
    await wh.procesar_mensajes([_msg("Quiero un agua micelar")])
    primera = set(vistos[-1])
    await wh.procesar_mensajes([_msg("No tenes otra marca ?")])
    segunda = set(vistos[-1])
    assert segunda and not (segunda & primera)
