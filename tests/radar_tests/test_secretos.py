"""
k_tenant fuera de la base (§6.4) y HMAC con clave para contactos y lids.
"""
import hashlib
import hmac
import os
import stat
import sys
import uuid

import pytest

from app.radar.secrets import (
    FileSecretStore, KTenantAusente, KTenantYaExiste, MemorySecretStore, Seudonimizador,
    contact_hmac, crear_k_tenant, destruir_k_tenant, lid_hmac, nombre_k_tenant, obtener_k_tenant,
)
from app.radar.telefonos import TelefonoNoSoportado

K = bytes(range(32))


def test_contact_hmac_es_hmac_sha256_del_e164():
    esperado = hmac.new(K, b"+5493411234567", hashlib.sha256).hexdigest()
    assert contact_hmac(K, "+5493411234567") == esperado
    assert len(esperado) == 64


def test_contact_hmac_exige_e164():
    with pytest.raises(ValueError):
        contact_hmac(K, "5493411234567")


def test_claves_distintas_dan_hmac_distinto():
    assert contact_hmac(K, "+5493411234567") != contact_hmac(bytes(32), "+5493411234567")


def test_lid_hmac_acepta_con_y_sin_sufijo():
    assert lid_hmac(K, "123456789012345@lid") == lid_hmac(K, "123456789012345")
    assert lid_hmac(K, "123456789012345") == hmac.new(K, b"123456789012345", hashlib.sha256).hexdigest()


def test_lid_hmac_rechaza_lid_no_numerico():
    with pytest.raises(ValueError):
        lid_hmac(K, "5493411234567@c.us")


@pytest.mark.parametrize("store_factory", [
    lambda tmp: MemorySecretStore(),
    lambda tmp: FileSecretStore(tmp / "secretos"),
])
def test_ciclo_de_vida_de_k_tenant(tmp_path, store_factory):
    store = store_factory(tmp_path)
    tid = uuid.uuid4()
    with pytest.raises(KTenantAusente):
        obtener_k_tenant(store, tid)
    crear_k_tenant(store, tid)
    k = obtener_k_tenant(store, tid)
    assert len(k) == 32
    with pytest.raises(KTenantYaExiste):
        crear_k_tenant(store, tid)
    assert obtener_k_tenant(store, tid) == k
    destruir_k_tenant(store, tid)
    with pytest.raises(KTenantAusente):
        obtener_k_tenant(store, tid)


def test_file_store_persiste_entre_instancias(tmp_path):
    tid = uuid.uuid4()
    crear_k_tenant(FileSecretStore(tmp_path / "s"), tid)
    assert len(obtener_k_tenant(FileSecretStore(tmp_path / "s"), tid)) == 32


@pytest.mark.skipif(sys.platform == "win32", reason="permisos POSIX")
def test_file_store_archivo_solo_dueno(tmp_path):
    store = FileSecretStore(tmp_path / "s")
    tid = uuid.uuid4()
    crear_k_tenant(store, tid)
    archivo = next((tmp_path / "s").iterdir())
    assert stat.S_IMODE(os.stat(archivo).st_mode) == 0o600


def test_nombre_de_secreto_invalido(tmp_path):
    store = FileSecretStore(tmp_path / "s")
    with pytest.raises(ValueError):
        store.set("../fuera", b"x")


def test_seudonimizador_normaliza_y_usa_la_clave_del_tenant():
    store = MemorySecretStore()
    tid = uuid.uuid4()
    crear_k_tenant(store, tid)
    s = Seudonimizador(store)
    k = obtener_k_tenant(store, tid)
    assert s.contact_hmac(tid, "0341 15 123 4567") == contact_hmac(k, "+5493411234567")
    assert s.contact_hmac(tid, "5493411234567@c.us") == contact_hmac(k, "+5493411234567")
    assert s.lid_hmac(tid, "123456789012345@lid") == lid_hmac(k, "123456789012345")
    with pytest.raises(TelefonoNoSoportado):
        s.contact_hmac(tid, "+1 555 123 4567")
    assert nombre_k_tenant(tid) == f"k_tenant:{tid}"
