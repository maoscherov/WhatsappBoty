"""
Cliente HTTP de WAHA de Radar (§6.1). Reimplementa el cliente del spike
(scripts/radar_spike/client.py) sin importarlo.

- Lista blanca de rutas: cualquier otra lanza RutaNoPermitida ANTES de tocar la
  red. Nada de enviar, marcar leído, presencia, typing, archivar ni logout.
  Las lecturas de chats y mensajes llegan en el tramo 3.
- Solo sesiones de Radar: `v_` + 12 hex (una por vínculo). Una sesión ajena
  (p. ej. la personal del dueño) no se puede leer, crear ni borrar.
- httpx y httpcore quedan en WARNING: a nivel INFO loguean la URL completa.
- Este módulo no loguea nada: ni cuerpos, ni QR, ni códigos, ni claves. Un
  error de red se convierte en WahaError sin el mensaje de httpx (trae la URL).
"""

import logging
import re
from typing import Any, Optional

import httpx

# Siempre con fullmatch: con `$` y re.match un salto de línea final pasaba el control.
PATRON_SESION = re.compile(r"^v_[0-9a-f]{12}$")
_S = r"(?P<sesion>[^/]+)"
RUTAS_PERMITIDAS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/api/server/version$")),
    ("POST", re.compile(r"^/api/sessions$")),
    ("GET", re.compile(rf"^/api/sessions/{_S}$")),
    ("DELETE", re.compile(rf"^/api/sessions/{_S}$")),
    ("POST", re.compile(rf"^/api/sessions/{_S}/(start|stop|restart)$")),
    ("GET", re.compile(rf"^/api/{_S}/auth/qr$")),
    ("POST", re.compile(rf"^/api/{_S}/auth/request-code$")),
    ("POST", re.compile(r"^/api/keys$")),
    ("GET", re.compile(r"^/api/keys$")),
    ("DELETE", re.compile(r"^/api/keys/[A-Za-z0-9_-]{1,80}$")),
)
_CODIGO = re.compile(r"^[A-Z0-9]{4}-?[A-Z0-9]{4}$")


class WahaError(RuntimeError):
    pass


class RutaNoPermitida(WahaError):
    pass


class SesionProhibida(WahaError):
    pass


class WahaHttpError(WahaError):
    def __init__(self, metodo: str, status: int) -> None:
        super().__init__(f"{metodo} -> HTTP {status}")
        self.status = status


def verificar_ruta(metodo: str, ruta: str) -> Optional[str]:
    """Devuelve la sesión embebida en la ruta (o None) si está permitida; si no, lanza."""
    for m, patron in RUTAS_PERMITIDAS:
        if m == metodo:
            hallado = patron.fullmatch(ruta)
            if hallado:
                return hallado.groupdict().get("sesion")
    raise RutaNoPermitida(f"{metodo} fuera de la lista blanca")


def verificar_nombre_sesion(nombre: Any) -> str:
    if not isinstance(nombre, str) or not PATRON_SESION.fullmatch(nombre):
        raise SesionProhibida("solo sesiones de Radar (v_ + 12 hex)")
    return nombre


def _vista(d: dict[str, Any]) -> dict[str, Any]:
    me = d.get("me")
    return {
        "name": d.get("name"),
        "status": d.get("status"),
        "engine": d.get("engine"),
        "config": d.get("config") or {},
        "me_id": me.get("id") if isinstance(me, dict) else None,
    }


class WahaCliente:
    def __init__(self, base_url: str, admin_key: str, *, transport: Optional[httpx.AsyncBaseTransport] = None,
                 timeout: float = 20.0) -> None:
        for nombre in ("httpx", "httpcore"):
            logging.getLogger(nombre).setLevel(logging.WARNING)
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout,
                                       headers={"X-Api-Key": admin_key, "Accept": "application/json"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "WahaCliente":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _request(self, metodo: str, ruta: str, *, params: Optional[dict] = None, json: Any = None,
                       esperar: Optional[tuple[int, ...]] = (200, 201),
                       headers: Optional[dict] = None) -> httpx.Response:
        sesion = verificar_ruta(metodo, ruta)
        if sesion is not None:
            verificar_nombre_sesion(sesion)
        if metodo == "POST" and ruta == "/api/sessions":
            verificar_nombre_sesion((json or {}).get("name"))
        if metodo == "POST" and ruta == "/api/keys":
            verificar_nombre_sesion((json or {}).get("session"))
        try:
            resp = await self._http.request(metodo, ruta, params=params, json=json, headers=headers)
        except httpx.HTTPError as e:
            # Sin el mensaje: httpx lo arma con la URL completa.
            raise WahaError(f"{metodo}: error de red ({type(e).__name__})") from None
        if esperar and resp.status_code not in esperar:
            raise WahaHttpError(metodo, resp.status_code)
        return resp

    # --- servidor ---------------------------------------------------------------
    async def version_servidor(self) -> dict[str, Any]:
        resp = await self._request("GET", "/api/server/version")
        try:
            d = resp.json()
        except ValueError:
            raise WahaError("GET: la respuesta de versión no es JSON") from None
        if not isinstance(d, dict):                # un 200 que no es de WAHA (HTML de un proxy, un número, null)
            raise WahaError("GET: la respuesta de versión no es un objeto")
        return {k: d.get(k) for k in ("version", "engine", "tier")}

    # --- sesiones ---------------------------------------------------------------
    async def crear_sesion(self, cuerpo: dict[str, Any]) -> dict[str, Any]:
        return _vista((await self._request("POST", "/api/sessions", json=cuerpo)).json() or {})

    async def leer_sesion(self, nombre: str) -> Optional[dict[str, Any]]:
        r = await self._request("GET", f"/api/sessions/{nombre}", esperar=None)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise WahaHttpError("GET", r.status_code)
        return _vista(r.json() or {})

    async def borrar_sesion(self, nombre: str) -> int:
        return (await self._request("DELETE", f"/api/sessions/{nombre}", esperar=None)).status_code

    async def _accion(self, nombre: str, accion: str) -> dict[str, Any]:
        return _vista((await self._request("POST", f"/api/sessions/{nombre}/{accion}")).json() or {})

    async def iniciar_sesion(self, nombre: str) -> dict[str, Any]:
        return await self._accion(nombre, "start")

    async def detener_sesion(self, nombre: str) -> dict[str, Any]:
        return await self._accion(nombre, "stop")

    async def reiniciar_sesion(self, nombre: str) -> dict[str, Any]:
        return await self._accion(nombre, "restart")

    # --- vinculación ------------------------------------------------------------
    async def qr_png(self, nombre: str) -> Optional[bytes]:
        r = await self._request("GET", f"/api/{nombre}/auth/qr", params={"format": "image"}, esperar=None,
                                headers={"Accept": "image/png"})
        return r.content if r.status_code == 200 else None

    async def pedir_codigo(self, nombre: str, telefono_digitos: str) -> Optional[str]:
        r = await self._request("POST", f"/api/{nombre}/auth/request-code",
                                json={"phoneNumber": telefono_digitos}, esperar=None)
        if r.status_code not in (200, 201):
            return None
        try:
            codigo = (r.json() or {}).get("code")
        except ValueError:
            return None
        return codigo if isinstance(codigo, str) and _CODIGO.fullmatch(codigo) else None

    # --- claves -----------------------------------------------------------------
    async def crear_clave(self, sesion: str, *, actions: dict[str, bool]) -> tuple[str, str]:
        """Clave de sesión con actions EXPLÍCITO: con null WAHA aplica todos los permisos (§6.1)."""
        if not actions:
            raise ValueError("actions explícito: con null WAHA aplica todos los permisos")
        r = await self._request("POST", "/api/keys", json={"isAdmin": False, "session": sesion, "isActive": True,
                                                           "actions": dict(actions)})
        d = r.json() or {}
        kid, valor = d.get("id"), d.get("key")
        if not kid or not valor:
            raise WahaError("WAHA no devolvió id y valor de la clave")
        return str(kid), str(valor)

    async def listar_claves(self) -> list[dict[str, Any]]:
        crudo = (await self._request("GET", "/api/keys")).json()
        crudo = crudo if isinstance(crudo, list) else []
        return [{"id": str(k.get("id")), "session": k.get("session"), "isAdmin": bool(k.get("isAdmin"))}
                for k in crudo]

    async def borrar_clave(self, key_id: str) -> int:
        clave = next((k for k in await self.listar_claves() if k["id"] == key_id), None)
        if clave is None:
            raise WahaError("clave inexistente")
        verificar_nombre_sesion(clave["session"])
        return (await self._request("DELETE", f"/api/keys/{key_id}", esperar=None)).status_code
