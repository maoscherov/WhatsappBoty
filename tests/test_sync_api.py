"""
Integración HTTP de /v1/sync/* y /bo/branches* contra el Postgres embebido
(pgserver + migración 0005), con httpx.AsyncClient sobre la app real — mismo
event loop que el pool de asyncpg.
"""

import pytest
from httpx import ASGITransport, AsyncClient

import app.services.db as dbmod
from app.services.db import Database
from app.services.branch_auth import generar_token
from app.services.branch_store import BranchStore, estado_derivado


BRANCH = "farmacia-test"


def _hash(n: int) -> str:
    return f"{n:064x}"


def _item(external_id: str = "p1", n: int = 1, **kw) -> dict:
    base = {
        "external_id": external_id, "hash": _hash(n),
        "barcodes": ["7790000000017"], "troquel": None,
        "name": f"Producto {external_id}", "brand": "Lab X",
        "drug": None, "form": None, "category": "Medicamentos",
        "rubro": "", "subrubro": "", "therapeutic_actions": [],
        "price": "1234.50", "stock": 3, "visible": True, "active": True,
    }
    base.update(kw)
    return base


def _batch(items: list[dict], batch: int = 1, total: int = 1) -> dict:
    return {"schema_version": 1, "branch_id": BRANCH, "source": "observer-gestion",
            "mode": "delta", "batch": batch, "total_batches": total,
            "generated_at": "2026-09-08T03:12:00-03:00", "items": items}


@pytest.fixture
async def db(pg_dsn):
    import app.services.catalog_store as csmod
    import app.services.branch_store as bsmod

    d = Database(pg_dsn)
    ok = await d.connect()
    assert ok, "no se pudo conectar al Postgres de test"
    await d.execute("TRUNCATE catalog_extras, catalog_items, branches")
    prev = dbmod._instance
    dbmod._instance = d          # el singleton que usan los endpoints
    # Los stores cachean la Database: sin reset quedaría el pool del test anterior.
    csmod._instance = None
    bsmod._instance = None
    yield d
    dbmod._instance = prev
    csmod._instance = None
    bsmod._instance = None
    await d.close()


@pytest.fixture
async def branch_token(db) -> str:
    token, token_hash = generar_token()
    assert await BranchStore(db).crear(BRANCH, "Farmacia Test", token_hash)
    return token


@pytest.fixture
async def client():
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as ac:
        yield ac


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    async def test_sin_token_401(self, client, db):
        r = await client.post("/v1/sync/catalog", json=_batch([_item()]))
        assert r.status_code == 401

    async def test_token_invalido_401(self, client, db):
        r = await client.post("/v1/sync/catalog", json=_batch([_item()]),
                              headers=_auth("token-falso"))
        assert r.status_code == 401

    async def test_branch_id_ajeno_403(self, client, branch_token):
        body = _batch([_item()])
        body["branch_id"] = "otra-sucursal"
        r = await client.post("/v1/sync/catalog", json=body,
                              headers=_auth(branch_token))
        assert r.status_code == 403

    async def test_sucursal_inactiva_403(self, client, db, branch_token):
        await db.execute("UPDATE branches SET activa = false WHERE branch_id = $1", BRANCH)
        r = await client.post("/v1/sync/catalog", json=_batch([_item()]),
                              headers=_auth(branch_token))
        assert r.status_code == 403

    async def test_schema_version_no_soportada_422(self, client, branch_token):
        body = _batch([_item()])
        body["schema_version"] = 2
        r = await client.post("/v1/sync/catalog", json=body,
                              headers=_auth(branch_token))
        assert r.status_code == 422


class TestSyncCatalog:
    async def test_upsert_e_idempotencia(self, client, db, branch_token):
        r = await client.post("/v1/sync/catalog", json=_batch([_item("p1"), _item("p2", n=2)]),
                              headers=_auth(branch_token))
        assert r.status_code == 200
        assert r.json() == {"received": 2, "upserted": 2, "unchanged": 0}

        # Reintento del mismo lote (timeout de red del agente): 0 upserted.
        r2 = await client.post("/v1/sync/catalog", json=_batch([_item("p1"), _item("p2", n=2)]),
                               headers=_auth(branch_token))
        assert r2.json() == {"received": 2, "upserted": 0, "unchanged": 2}

        # Cambio de stock → hash nuevo → 1 upserted.
        r3 = await client.post("/v1/sync/catalog", json=_batch([_item("p1", n=9, stock=0)]),
                               headers=_auth(branch_token))
        assert r3.json()["upserted"] == 1
        row = await db.fetchrow(
            "SELECT stock, price::text AS price, requiere_receta FROM catalog_items "
            "WHERE branch_id = $1 AND external_id = 'p1'", BRANCH)
        assert row["stock"] == 0 and row["price"] == "1234.50"

    async def test_receta_derivada_al_escribir(self, client, db, branch_token):
        items = [
            _item("r1", n=11, category="Medicamentos Bajo Receta", name="Clonagin 2mg"),
            _item("r2", n=12, category="Medicamentos Bajo Receta", name="Ibupirac 600"),
            _item("r3", n=13, category="Perfumería", name="Shampoo X"),
        ]
        r = await client.post("/v1/sync/catalog", json=_batch(items),
                              headers=_auth(branch_token))
        assert r.status_code == 200
        rows = await db.fetch(
            "SELECT external_id, requiere_receta FROM catalog_items "
            "WHERE branch_id = $1 ORDER BY external_id", BRANCH)
        flags = {r["external_id"]: r["requiere_receta"] for r in rows}
        assert flags["r1"] == "si"
        assert flags["r2"] == "no"     # whitelist OTC blindada
        assert flags["r3"] == "no"

    async def test_precio_null_no_es_gratis(self, client, db, branch_token):
        r = await client.post("/v1/sync/catalog",
                              json=_batch([_item("sinprecio", n=21, price=None)]),
                              headers=_auth(branch_token))
        assert r.status_code == 200
        row = await db.fetchrow(
            "SELECT price FROM catalog_items WHERE external_id = 'sinprecio'")
        assert row["price"] is None

    async def test_lote_vacio_422(self, client, branch_token):
        r = await client.post("/v1/sync/catalog", json=_batch([]),
                              headers=_auth(branch_token))
        assert r.status_code == 422


class TestFullManifest:
    async def test_desactiva_ausentes_y_pide_resend(self, client, db, branch_token):
        await client.post("/v1/sync/catalog",
                          json=_batch([_item("a", n=1), _item("b", n=2), _item("c", n=3)]),
                          headers=_auth(branch_token))
        # Manifiesto: a igual, b con hash nuevo, d desconocido; c ausente.
        r = await client.post("/v1/sync/full-manifest", json={
            "branch_id": BRANCH, "generated_at": "x",
            "items": [{"external_id": "a", "hash": _hash(1)},
                      {"external_id": "b", "hash": _hash(99)},
                      {"external_id": "d", "hash": _hash(4)}],
        }, headers=_auth(branch_token))
        assert r.status_code == 200
        body = r.json()
        assert sorted(body["resend"]) == ["b", "d"]
        assert body["deactivated"] == 1
        row = await db.fetchrow(
            "SELECT active FROM catalog_items WHERE external_id = 'c'")
        assert row["active"] is False

    async def test_reactivacion_por_reenvio(self, client, db, branch_token):
        await client.post("/v1/sync/catalog", json=_batch([_item("z", n=5)]),
                          headers=_auth(branch_token))
        await client.post("/v1/sync/full-manifest", json={
            "branch_id": BRANCH, "generated_at": "x",
            "items": [{"external_id": "otro", "hash": _hash(6)}],
        }, headers=_auth(branch_token))
        # z quedó inactivo; el agente lo reenvía con el MISMO hash → reactiva.
        r = await client.post("/v1/sync/catalog", json=_batch([_item("z", n=5)]),
                              headers=_auth(branch_token))
        assert r.json()["upserted"] == 1
        row = await db.fetchrow(
            "SELECT active FROM catalog_items WHERE external_id = 'z'")
        assert row["active"] is True

    async def test_manifiesto_vacio_422(self, client, db, branch_token):
        await client.post("/v1/sync/catalog", json=_batch([_item("k", n=7)]),
                          headers=_auth(branch_token))
        r = await client.post("/v1/sync/full-manifest",
                              json={"branch_id": BRANCH, "generated_at": "x", "items": []},
                              headers=_auth(branch_token))
        assert r.status_code == 422
        row = await db.fetchrow(
            "SELECT active FROM catalog_items WHERE external_id = 'k'")
        assert row["active"] is True   # NO desactivó todo


class TestHeartbeat:
    async def test_204_y_estado_derivado(self, client, db, branch_token):
        r = await client.post("/v1/sync/heartbeat", json={
            "branch_id": BRANCH, "agent_version": "0.1.0",
            "erp_version": "2.5.5293.5", "erp_status": "ok",
            "last_sync_ok_at": "2026-09-08T03:12:00-03:00",
            "catalog_count": 54235, "pending_batches": 0,
        }, headers=_auth(branch_token))
        assert r.status_code == 204
        row = await db.fetchrow("SELECT * FROM branches WHERE branch_id = $1", BRANCH)
        assert row["catalog_count"] == 54235
        assert estado_derivado(dict(row)) == "ok"

    async def test_acepta_erp_version_null(self, client, db, branch_token):
        """El agente manda erp_version: null hasta conocerla (contrato §2)."""
        r = await client.post("/v1/sync/heartbeat", json={
            "branch_id": BRANCH, "agent_version": "0.2.0", "erp_version": None,
            "erp_status": "inalcanzable", "last_sync_ok_at": None,
            "catalog_count": 0, "pending_batches": 0,
            "metrics": {"erp_fetch_ms": None, "ws_connected": False},
        }, headers=_auth(branch_token))
        assert r.status_code == 204, r.text
        row = await db.fetchrow("SELECT erp_version, erp_status FROM branches WHERE branch_id = $1", BRANCH)
        assert row["erp_version"] in ("", None)
        assert row["erp_status"] == "inalcanzable"

    async def test_estados_derivados(self, client, db, branch_token):
        assert estado_derivado({"last_heartbeat_at": None}) == "sin_agente"
        await client.post("/v1/sync/heartbeat", json={
            "branch_id": BRANCH, "erp_status": "no_autorizado",
        }, headers=_auth(branch_token))
        row = dict(await db.fetchrow("SELECT * FROM branches WHERE branch_id = $1", BRANCH))
        assert estado_derivado(row) == "erp_no_autorizado"
        await client.post("/v1/sync/heartbeat", json={
            "branch_id": BRANCH, "erp_status": "ok", "pending_batches": 4,
        }, headers=_auth(branch_token))
        row = dict(await db.fetchrow("SELECT * FROM branches WHERE branch_id = $1", BRANCH))
        assert estado_derivado(row) == "atrasada"


class TestBoBranches:
    async def test_crear_listar_rotar(self, client, db):
        r = await client.post("/bo/branches",
                              json={"branch_id": "farmacia-dos", "nombre": "Sucursal 2"})
        assert r.status_code == 200
        token = r.json()["token"]
        assert len(token) > 30

        # 409 si ya existe
        r2 = await client.post("/bo/branches",
                               json={"branch_id": "farmacia-dos", "nombre": "Otra"})
        assert r2.status_code == 409

        # branch_id inválido
        r3 = await client.post("/bo/branches", json={"branch_id": "X!", "nombre": "n"})
        assert r3.status_code == 422

        lista = (await client.get("/bo/branches")).json()
        assert any(b["branch_id"] == "farmacia-dos" and b["estado"] == "sin_agente"
                   for b in lista)

        # Rotar token: el viejo deja de servir, el nuevo sirve.
        r4 = await client.post("/bo/branches/farmacia-dos/rotate-token")
        nuevo = r4.json()["token"]
        assert nuevo != token
        rv = await client.post("/v1/sync/heartbeat",
                               json={"branch_id": "farmacia-dos"}, headers=_auth(token))
        assert rv.status_code == 401
        rn = await client.post("/v1/sync/heartbeat",
                               json={"branch_id": "farmacia-dos"}, headers=_auth(nuevo))
        assert rn.status_code == 204

    async def test_sync_now_sin_conexion_409(self, client, db, branch_token):
        r = await client.post(f"/bo/branches/{BRANCH}/sync-now")
        assert r.status_code == 409


class TestDegradacion:
    async def test_sin_postgres_503(self, client):
        prev = dbmod._instance
        dbmod._instance = Database("")   # sin DSN → no disponible
        try:
            r = await client.post("/v1/sync/catalog", json=_batch([_item()]),
                                  headers=_auth("cualquiera"))
            assert r.status_code == 503
        finally:
            dbmod._instance = prev
