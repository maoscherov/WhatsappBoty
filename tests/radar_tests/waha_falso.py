"""
Servidor WAHA falso para los tests de Radar (httpx.MockTransport). Los tests
NUNCA hablan con un WAHA real: la prueba contra el contenedor de staging es
manual (docs/radar-waha-runbook-tramo2.md).

Guarda sesiones y claves en memoria, registra cada llamada como "METODO ruta"
y permite simular ecos distintos, estados, errores de claves y de código.
"""
import copy
import json

import httpx

PNG = b"\x89PNG\r\n\x1a\nqr-falso"


class WahaFalso:
    def __init__(self):
        self.sesiones: dict[str, dict] = {}
        self.claves: list[dict] = []
        self.llamadas: list[str] = []
        self.cuerpos: list[dict] = []
        self.mutar_eco = None                      # callable(config) que altera lo que "guarda" WAHA
        self.estados: dict[str, list[str]] = {}    # estados a devolver, en orden, en cada GET de sesión
        self.start_da = "WORKING"                  # estado tras POST .../start
        self.falla_claves = False
        self.falla_codigo = False
        self.falla_crear = False
        self.falla_leer = False
        self.me_id = "5493411234567@c.us"
        self._n = 0

    def transporte(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        m, p = req.method, req.url.path
        self.llamadas.append(f"{m} {p}")
        partes = p.split("/")
        if m == "GET" and p == "/api/server/version":
            return httpx.Response(200, json={"version": "2026.8.2", "engine": "NOWEB", "tier": "CORE"})
        if m == "POST" and p == "/api/sessions":
            if self.falla_crear:
                return httpx.Response(500, json={})
            cuerpo = json.loads(req.content)
            self.cuerpos.append(cuerpo)
            config = copy.deepcopy(cuerpo["config"])
            if self.mutar_eco:
                self.mutar_eco(config)
            self.sesiones[cuerpo["name"]] = {"name": cuerpo["name"], "status": "STARTING", "engine": "NOWEB",
                                             "config": config, "me": None}
            return httpx.Response(201, json=self.sesiones[cuerpo["name"]])
        if p.startswith("/api/sessions/"):
            nombre = partes[3]
            s = self.sesiones.get(nombre)
            if len(partes) == 4 and m == "GET":
                if self.falla_leer:
                    return httpx.Response(500, json={})
                if s is None:
                    return httpx.Response(404, json={})
                if self.estados.get(nombre):
                    s["status"] = self.estados[nombre].pop(0)
                if s["status"] == "WORKING":
                    s["me"] = {"id": self.me_id, "pushName": "Negocio"}
                return httpx.Response(200, json=s)
            if len(partes) == 4 and m == "DELETE":
                if s is None:
                    return httpx.Response(404, json={})
                del self.sesiones[nombre]
                return httpx.Response(200, json={})
            if len(partes) == 5 and m == "POST":
                if s is None:
                    return httpx.Response(404, json={})
                s["status"] = {"start": self.start_da, "restart": "SCAN_QR_CODE", "stop": "STOPPED"}[partes[4]]
                return httpx.Response(201, json=s)
        if len(partes) == 5 and partes[3] == "auth":
            if partes[2] not in self.sesiones:
                return httpx.Response(404, json={})
            if partes[4] == "qr" and m == "GET":
                return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
            if partes[4] == "request-code" and m == "POST":
                if self.falla_codigo:
                    return httpx.Response(500, json={})
                return httpx.Response(200, json={"code": "ABCD-EFGH"})
        if p == "/api/keys":
            if self.falla_claves:
                return httpx.Response(403, json={})
            if m == "POST":
                cuerpo = json.loads(req.content)
                self._n += 1
                clave = {"id": f"k{self._n}", "session": cuerpo["session"], "isAdmin": cuerpo["isAdmin"],
                         "actions": cuerpo["actions"], "key": f"valor-secreto-{self._n}"}
                self.claves.append(clave)
                return httpx.Response(201, json={"id": clave["id"], "key": clave["key"]})
            if m == "GET":
                return httpx.Response(200, json=[{"id": k["id"], "session": k["session"], "isAdmin": k["isAdmin"]}
                                                 for k in self.claves])
        if p.startswith("/api/keys/") and m == "DELETE":
            if self.falla_claves:
                return httpx.Response(403, json={})
            self.claves = [k for k in self.claves if k["id"] != partes[3]]
            return httpx.Response(200, json={})
        return httpx.Response(500, json={})
