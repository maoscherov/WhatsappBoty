"""Pedidos durables y códigos de pedido de Mercurio

`orders`: copia durable de los pedidos que hoy viven solo en Redis con TTL de
7 días (order:{id}). Un pedido COBRADO que se pierde por un reinicio o una
evicción de Redis es plata: acá queda para siempre, con el estado del alta en
el ERP (F5). Redis sigue siendo lo que lee el backoffice; esta tabla es la
fuente durable y la cola de reintentos del ERP.

`mercurio_codigos`: el POST /pedidos de Mercurio exige `variant_id` = codigo
de la variante y `product_id` = codigo_padre (spec 14/9), pero catalog_items
solo guarda external_id (id_articulo_mercurio). El sync llena esta tabla
satélite sin tocar el contrato compartido con el agente de la farmacia.

Numeración (7/10): esta migración fue la 0018 de feature/vertical-petshop,
pero develop agregó su propia 0018 (operadores) y con dos heads el `upgrade
head` del arranque fallaba. Pasó a 0019 sobre la 0018 de develop. La base de
MO ya estaba estampada en "0018" con ESTA migración (orders y
mercurio_codigos, sin operadores), así que se auto-repara: si falta la tabla
operadores corre el upgrade() de la 0018 de develop y después crea lo suyo
con IF NOT EXISTS. Farmacia, mutual (en 0017 o en 0018 = operadores) y MO
llegan solas a 0019 al arrancar, sin `alembic stamp` a mano.

Revisión final (fix-C1, 7/10), también con IF NOT EXISTS para que llegue a
la orders que ya existe en MO:
- `ux_orders_payment`: UNIQUE (payment_id) WHERE payment_id IS NOT NULL. Un
  mismo pago crea una sola orden aunque Redis se pierda (find_by_payment cae
  a esta tabla y un INSERT que choca se trata como duplicado). Reemplaza al
  índice común `ix_orders_payment`. Si la base ya tiene pagos repetidos, la
  migración no falla: avisa (NOTICE) y deja el índice común.
- `erp_proximo_intento`: cuándo reintentar el alta en el ERP (backoff).
- `erp_actualizado_at` (ronda de arreglo 2): cuándo cambió por última vez el
  alta en el ERP (lo escriben marcar_erp y vencer_pendientes). El "último
  error" de /bo/mercurio/estado se ordena por ella: updated_at también se
  mueve con las acciones del operador (preparado, retirado).

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-05
"""
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _reparar_operadores()
    _orders()
    _mercurio_codigos()


def downgrade() -> None:
    # Solo lo de esta migración. `operadores` es de la 0018 (develop): aunque en
    # la base de MO la haya creado _reparar_operadores, bajar a 0018 la deja.
    op.execute("DROP TABLE IF EXISTS mercurio_codigos")
    op.execute("DROP INDEX IF EXISTS ux_orders_payment")
    op.execute("DROP INDEX IF EXISTS ix_orders_payment")
    op.execute("DROP INDEX IF EXISTS ix_orders_erp_pendiente")
    op.execute("DROP TABLE IF EXISTS orders")


# ── Auto-reparación de la base de MO ─────────────────────────────────────────
def _existe_tabla(nombre: str) -> bool:
    # to_regclass resuelve con el search_path, igual que los CREATE TABLE sin
    # esquema de las migraciones.
    return op.get_bind().execute(
        sa.text("SELECT to_regclass(:t)"), {"t": nombre}).scalar() is not None


def _migracion_0018_operadores():
    """La 0018 de develop cargada por ruta (el nombre del archivo empieza con
    un dígito y no se puede importar). Su `op` es el mismo proxy de alembic,
    así que corre en el contexto y la transacción de esta migración."""
    ruta = Path(__file__).resolve().with_name("0018_operadores.py")
    spec = importlib.util.spec_from_file_location("_migracion_0018_operadores", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _reparar_operadores() -> None:
    # Solo si falta la tabla (base de MO). Si existe, la 0018 ya corrió y NO se
    # vuelve a sembrar: la farmacia pudo renombrar o fusionar operadores ("02"
    # -> "María") y la siembra volvería a dar de alta los nombres viejos.
    if not _existe_tabla("operadores"):
        _migracion_0018_operadores().upgrade()


# ── Tablas de esta migración ─────────────────────────────────────────────────
def _orders() -> None:
    # En MO la tabla ya existe (la creó la vieja 0018 de la rama) y este CREATE
    # TABLE IF NOT EXISTS no la toca: dejarlo igual al que corrió allá.
    op.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id            TEXT PRIMARY KEY,
            phone               TEXT NOT NULL,
            estado              TEXT NOT NULL DEFAULT 'pendiente',
            total               NUMERIC(12,2),
            pago                TEXT,
            payment_id          TEXT,
            data                JSONB NOT NULL,
            erp_estado          TEXT,            -- NULL = no aplica | pendiente | enviado | rechazado
            erp_id_comprobante  TEXT,
            erp_numero          TEXT,
            erp_intentos        INT NOT NULL DEFAULT 0,
            erp_ultimo_error    TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_orders_erp_pendiente ON orders (created_at) "
               "WHERE erp_estado = 'pendiente'")
    # Columnas e índices nuevos de orders van ACÁ abajo, para que lleguen
    # también a la tabla que ya existe en MO:
    #   ALTER TABLE orders ADD COLUMN IF NOT EXISTS ...
    #   CREATE [UNIQUE] INDEX IF NOT EXISTS <nombre nuevo> ...
    # Un índice con un nombre que ya existe (ix_orders_payment) no se pisa con
    # IF NOT EXISTS: para cambiarlo, nombre nuevo (o DROP INDEX IF EXISTS antes).
    # Lo que se agregue acá se borra solo en downgrade() con el DROP TABLE.

    # Backoff de los reintentos del alta en el ERP (lo usa el job).
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS erp_proximo_intento TIMESTAMPTZ NULL")
    # Último cambio del alta en el ERP (el "último error" de /bo/mercurio/estado).
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS erp_actualizado_at TIMESTAMPTZ NULL")

    # Un pago, una orden. Sin pagos repetidos: índice único parcial y se va el
    # común (ix_orders_payment, que creó la vieja 0018 en MO). Con repetidos
    # (no debería pasar, pero el arranque corre `upgrade head` y no puede
    # caerse): NOTICE y queda el índice común para las búsquedas por pago.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM orders WHERE payment_id IS NOT NULL
                       GROUP BY payment_id HAVING count(*) > 1) THEN
                RAISE NOTICE 'orders tiene payment_id repetidos - no se crea ux_orders_payment';
                CREATE INDEX IF NOT EXISTS ix_orders_payment ON orders (payment_id);
            ELSE
                CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_payment ON orders (payment_id)
                    WHERE payment_id IS NOT NULL;
                DROP INDEX IF EXISTS ix_orders_payment;
            END IF;
        END
        $$
    """)


def _mercurio_codigos() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS mercurio_codigos (
            branch_id     TEXT NOT NULL,
            external_id   TEXT NOT NULL,
            codigo        TEXT NOT NULL,
            codigo_padre  TEXT NOT NULL,
            PRIMARY KEY (branch_id, external_id)
        )
    """)
