"""
Secretos por tenant fuera de la base y de sus backups (§6.4).

k_tenant: 32 bytes aleatorios por tenant.
  contact_hmac = HMAC-SHA256(k_tenant, teléfono E.164)
  lid_hmac     = HMAC-SHA256(k_tenant, lid)
Un hash sin clave se revierte por enumeración (~10^10 teléfonos); por eso la
clave es obligatoria y nunca se loguea ni se guarda en Postgres.

k_tenant se destruye SOLO en la baja del cliente (tramo 6): "borrar todo" es
por línea y `suppressions` (por tenant) tiene que sobrevivirla.

Bindings:
- FileSecretStore(dir): un archivo por secreto, base64, permisos 0600. Dev y
  tests usan un directorio temporal; producción, un volumen montado en
  RADAR_SECRETS_DIR, fuera de los backups de Postgres (docs/radar-despliegue.md).
- MemorySecretStore: solo tests unitarios.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets as _secrets
import uuid
from pathlib import Path
from typing import Optional, Protocol

from app.radar.telefonos import normalizar_e164

_NOMBRE = re.compile(r"^[a-z0-9_:-]{1,120}$")
_LID = re.compile(r"^[0-9]{5,20}$")


class KTenantAusente(KeyError):
    pass


class KTenantYaExiste(ValueError):
    pass


class SecretStore(Protocol):
    def get(self, nombre: str) -> Optional[bytes]: ...
    def set(self, nombre: str, valor: bytes) -> None: ...
    def delete(self, nombre: str) -> None: ...


def _validar_nombre(nombre: str) -> str:
    if not _NOMBRE.match(nombre or ""):
        raise ValueError("nombre de secreto inválido")
    return nombre


class MemorySecretStore:
    def __init__(self):
        self._datos: dict[str, bytes] = {}

    def get(self, nombre: str) -> Optional[bytes]:
        return self._datos.get(_validar_nombre(nombre))

    def set(self, nombre: str, valor: bytes) -> None:
        self._datos[_validar_nombre(nombre)] = bytes(valor)

    def delete(self, nombre: str) -> None:
        self._datos.pop(_validar_nombre(nombre), None)


class FileSecretStore:
    def __init__(self, dir: str | Path):
        self._dir = Path(dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _ruta(self, nombre: str) -> Path:
        return self._dir / (_validar_nombre(nombre).replace(":", "__") + ".b64")

    def get(self, nombre: str) -> Optional[bytes]:
        ruta = self._ruta(nombre)
        if not ruta.exists():
            return None
        return base64.b64decode(ruta.read_bytes())

    def set(self, nombre: str, valor: bytes) -> None:
        ruta = self._ruta(nombre)
        fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64encode(bytes(valor)))

    def delete(self, nombre: str) -> None:
        self._ruta(nombre).unlink(missing_ok=True)


def nombre_k_tenant(tenant_id: uuid.UUID) -> str:
    return f"k_tenant:{tenant_id}"


def crear_k_tenant(store: SecretStore, tenant_id: uuid.UUID) -> None:
    nombre = nombre_k_tenant(tenant_id)
    if store.get(nombre) is not None:
        raise KTenantYaExiste(str(tenant_id))
    store.set(nombre, _secrets.token_bytes(32))


def obtener_k_tenant(store: SecretStore, tenant_id: uuid.UUID) -> bytes:
    valor = store.get(nombre_k_tenant(tenant_id))
    if valor is None:
        raise KTenantAusente(str(tenant_id))
    if len(valor) != 32:
        raise ValueError("k_tenant corrupta: no tiene 32 bytes")
    return valor


def destruir_k_tenant(store: SecretStore, tenant_id: uuid.UUID) -> None:
    store.delete(nombre_k_tenant(tenant_id))


def contact_hmac(k_tenant: bytes, telefono_e164: str) -> str:
    if not telefono_e164.startswith("+"):
        raise ValueError("contact_hmac requiere E.164 ('+549...'): usar normalizar_e164")
    return hmac.new(k_tenant, telefono_e164.encode(), hashlib.sha256).hexdigest()


def lid_hmac(k_tenant: bytes, lid: str) -> str:
    lid = (lid or "").strip()
    if lid.endswith("@lid"):
        lid = lid[:-4]
    if not _LID.match(lid):
        raise ValueError("lid inválido")
    return hmac.new(k_tenant, lid.encode(), hashlib.sha256).hexdigest()


class Seudonimizador:
    """Fachada por tenant: normaliza y firma con la k_tenant del store."""

    def __init__(self, store: SecretStore):
        self._store = store

    def contact_hmac(self, tenant_id: uuid.UUID, telefono: str) -> str:
        return contact_hmac(obtener_k_tenant(self._store, tenant_id), normalizar_e164(telefono))

    def lid_hmac(self, tenant_id: uuid.UUID, lid: str) -> str:
        return lid_hmac(obtener_k_tenant(self._store, tenant_id), lid)
