"""
Workers de WAHA de Radar (se corre en la shell del servicio de Radar).

  WAHA_ADMIN_KEY=... python scripts/radar_workers.py registrar --nombre w1 \
      --base-url http://waha-w1.railway.internal:3000 --engine GOWS --max-sesiones 50 --disco-max-gb 20
  python scripts/radar_workers.py disco --nombre w1 --usado-gb 3.5
  python scripts/radar_workers.py listar

La clave admin se lee SOLO de la variable de entorno WAHA_ADMIN_KEY (nunca por
argumento, para que no quede en el historial de la shell) y va al SecretStore,
no a la base. Nunca se imprime.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.radar.app import construir_contexto, validar_settings  # noqa: E402
from app.radar.settings import get_radar_settings  # noqa: E402
from app.radar.workers import actualizar_disco, listar_workers, registrar_worker  # noqa: E402


async def _con_contexto(fn):
    rs = get_radar_settings()
    validar_settings(rs)
    ctx = await construir_contexto(rs)
    try:
        return await fn(ctx)
    finally:
        await ctx.cerrar()


async def _registrar(a: argparse.Namespace) -> None:
    clave = os.environ.get("WAHA_ADMIN_KEY", "")
    if not clave:
        raise SystemExit("Falta WAHA_ADMIN_KEY en el entorno")

    async def fn(ctx):
        wid = await registrar_worker(ctx, nombre=a.nombre, base_url=a.base_url, engine=a.engine,
                                     max_sesiones=a.max_sesiones, disco_max_gb=a.disco_max_gb, admin_key=clave)
        print(f"worker registrado: {a.nombre} ({wid})")
    await _con_contexto(fn)


async def _disco(a: argparse.Namespace) -> None:
    async def fn(ctx):
        w = next((w for w in await listar_workers(ctx) if w.nombre == a.nombre), None)
        if w is None:
            raise SystemExit(f"no existe el worker {a.nombre}")
        await actualizar_disco(ctx, w.id, a.usado_gb)
        print(f"{a.nombre}: {a.usado_gb} GB usados de {w.disco_max_gb}")
    await _con_contexto(fn)


async def _listar(a: argparse.Namespace) -> None:
    async def fn(ctx):
        for w in await listar_workers(ctx):
            print(f"{w.nombre}\t{w.engine}\t{w.sesiones}/{w.max_sesiones} sesiones\t"
                  f"{w.disco_usado_gb}/{w.disco_max_gb} GB\t{'activo' if w.activo else 'inactivo'}")
    await _con_contexto(fn)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Workers de WAHA de Radar")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("registrar")
    r.add_argument("--nombre", required=True)
    r.add_argument("--base-url", required=True)
    r.add_argument("--engine", choices=["NOWEB", "GOWS"], required=True)
    r.add_argument("--max-sesiones", type=int, required=True)
    r.add_argument("--disco-max-gb", type=float, required=True)
    d = sub.add_parser("disco")
    d.add_argument("--nombre", required=True)
    d.add_argument("--usado-gb", type=float, required=True)
    sub.add_parser("listar")
    a = p.parse_args(argv)
    asyncio.run({"registrar": _registrar, "disco": _disco, "listar": _listar}[a.cmd](a))


if __name__ == "__main__":
    main()
