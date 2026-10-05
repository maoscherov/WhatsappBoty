"""
Alta de un admin de KIS desde la shell del servidor (Railway shell), con las
variables RADAR_* del entorno:

    python scripts/radar_admin.py crear-admin --email mariano@keepitsimple.com.ar --nombre "Mariano"

Imprime UNA vez el link de invitación (7 días, un solo uso) en esta terminal
en lugar de mandarlo por email, aunque RADAR_MAILER=smtp esté en el entorno:
es el camino sin correo saliente. Con SMTP no hace falta la shell: el arranque
crea los admins de RADAR_ADMINS_INICIALES y cada uno pide su link en
/radar/login. Es idempotente: repetirlo actualiza el nombre y reenvía la
invitación (máximo 3 cada 15 minutos), también a un admin que creó el arranque.
"""

import argparse
import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.radar.admin_kis import crear_admin_kis          # noqa: E402
from app.radar.app import construir_contexto, validar_settings   # noqa: E402
from app.radar.mailer import MemoryMailer                # noqa: E402
from app.radar.settings import get_radar_settings        # noqa: E402


async def _crear_admin(email: str, nombre: str) -> None:
    rs = get_radar_settings()
    validar_settings(rs)
    ctx = await construir_contexto(rs)
    ctx.mailer = MemoryMailer()
    try:
        uid = await crear_admin_kis(ctx, email=email, nombre=nombre)
        print(f"admin {uid} listo")
        if ctx.mailer.enviados:
            print(ctx.mailer.enviados[0].texto)
        else:
            print("límite de links alcanzado para este usuario: esperá 15 minutos y repetí")
    finally:
        await ctx.cerrar()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Administración de Radar")
    sub = parser.add_subparsers(dest="comando", required=True)
    c = sub.add_parser("crear-admin", help="crea (o actualiza) un admin de KIS e imprime su invitación")
    c.add_argument("--email", required=True)
    c.add_argument("--nombre", default="")
    args = parser.parse_args(argv)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_crear_admin(args.email, args.nombre))
    else:
        # El test del script corre dentro de un loop de pytest-asyncio: asyncio.run()
        # no puede anidarse, así que se ejecuta en un thread aparte con su propio loop.
        excepcion: list[BaseException] = []

        def _correr():
            try:
                asyncio.run(_crear_admin(args.email, args.nombre))
            except BaseException as e:  # noqa: BLE001
                excepcion.append(e)

        hilo = threading.Thread(target=_correr)
        hilo.start()
        hilo.join()
        if excepcion:
            raise excepcion[0]


if __name__ == "__main__":
    main()
