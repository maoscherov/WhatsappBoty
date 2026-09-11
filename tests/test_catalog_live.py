"""
Corrección del catálogo con datos en vivo del ERP al OFRECER (catalog_live).
Caso real 11/9: el lote de Observer trae precio 0 / stock 0 para el Aveno
solar que la consulta individual devuelve con 2 unidades y $32.409.
"""

import pytest

import app.services.agent_registry as reg_mod
import app.services.catalog_source as cs
from app.services.agent_registry import LookupResult
from app.services.catalog_live import es_dudoso, refrescar_ofertas_en_vivo
from app.services.sku_service import SKUService

BRANCH = "farmacia-mutual"


def _fila(external_id, name, price=None, stock=0, barcodes=None):
    return {"external_id": external_id, "hash": "a" * 64, "barcodes": barcodes or [],
            "troquel": None, "name": name, "brand": "Andromaco", "drug": None,
            "form": "Crema", "category": "Cosméticos", "rubro": "Perfumeria",
            "subrubro": "Perfumeria", "therapeutic_actions": [], "price": price,
            "stock": stock, "visible": True, "active": True, "requiere_receta": "no",
            "source": "observer-gestion"}


class _FakeRegistry:
    def __init__(self, resultado):
        self._resultado = resultado
        self.consultas = []

    def connected(self, branch_id):
        return True

    async def lookup(self, branch_id, barcodes=None, ids=None, timeout=5.0):
        self.consultas.append(list(ids or []))
        return self._resultado


@pytest.fixture
def erp(monkeypatch):
    async def _branch(forzar=False):
        return BRANCH
    monkeypatch.setattr(cs, "resolver_branch_default", _branch)

    def _instalar(resultado):
        fake = _FakeRegistry(resultado)
        monkeypatch.setattr(reg_mod, "_instance", fake)
        return fake
    return _instalar


class TestEsDudoso:
    def test_precio_cero_o_sin_stock(self):
        assert es_dudoso({"precio": 0, "estado": "consultar"})
        assert es_dudoso({"precio": 1000, "sin_stock": True, "estado": "sin_stock"})
        assert not es_dudoso({"precio": 1000, "sin_stock": False, "estado": "disponible"})


class TestRefrescarOfertas:
    async def test_corrige_precio_y_stock_con_dato_vivo(self, erp):
        svc = SKUService.from_rows([
            _fila("182288", "AVENO SOLAR F65 infantil CRE x 175", price=None, stock=0)])
        antes = svc.buscar("aveno solar")
        assert antes and antes[0]["estado"] == "sin_stock" and antes[0]["precio"] == 0.0

        fake = erp(LookupResult(items=[{"external_id": "182288", "stock": 2,
                                        "price": "32409.09"}]))
        despues = await refrescar_ofertas_en_vivo(antes, svc)
        assert fake.consultas == [["182288"]]
        assert despues[0]["precio"] == 32409.09
        assert despues[0]["estado"] == "disponible"
        assert despues[0]["cantidad_visible"] == 2
        # y quedó corregido en memoria para la próxima búsqueda
        assert svc.get_by_id("182288").precio_venta == 32409.09

    async def test_sin_dudosos_no_consulta(self, erp):
        svc = SKUService.from_rows([_fila("1", "Producto OK", price=1500.0, stock=5)])
        fake = erp(LookupResult(items=[]))
        res = svc.buscar("producto ok")
        assert await refrescar_ofertas_en_vivo(res, svc) == res
        assert fake.consultas == []

    async def test_sin_respuesta_del_agente_sigue_con_cache(self, erp):
        svc = SKUService.from_rows([_fila("2", "Dudoso", price=None, stock=0)])
        erp(None)
        res = svc.buscar("dudoso")
        assert await refrescar_ofertas_en_vivo(res, svc) == res

    async def test_missing_no_altera(self, erp):
        svc = SKUService.from_rows([_fila("3", "Fantasma", price=None, stock=0)])
        erp(LookupResult(items=[], missing=["3"]))
        res = svc.buscar("fantasma")
        despues = await refrescar_ofertas_en_vivo(res, svc)
        assert despues[0]["estado"] == "sin_stock"

    async def test_maximo_tres_ids_por_oferta(self, erp):
        filas = [_fila(str(i), f"Dudoso {i} crema", price=None, stock=0) for i in range(1, 6)]
        svc = SKUService.from_rows(filas)
        fake = erp(LookupResult(items=[]))
        res = svc.buscar("dudoso crema", top_n=5)
        await refrescar_ofertas_en_vivo(res, svc)
        assert len(fake.consultas[0]) <= 3

    async def test_sin_sucursal_erp_no_consulta(self, monkeypatch):
        async def _none(forzar=False):
            return None
        monkeypatch.setattr(cs, "resolver_branch_default", _none)
        fake = _FakeRegistry(LookupResult(items=[]))
        monkeypatch.setattr(reg_mod, "_instance", fake)
        svc = SKUService.from_rows([_fila("4", "Dudoso", price=None, stock=0)])
        res = svc.buscar("dudoso")
        assert await refrescar_ofertas_en_vivo(res, svc) == res
        assert fake.consultas == []
