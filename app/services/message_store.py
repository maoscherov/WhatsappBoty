"""
Historial permanente de conversaciones en Postgres.

Best-effort: si Postgres no está disponible, no-op (el bot sigue con Redis,
que mantiene el contexto de la charla en curso).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MessageStore:
    def __init__(self, db):
        self._db = db

    async def save(self, phone: str, role: str, content: str, autor: Optional[str] = None,
                   origen: Optional[str] = None, media: Optional[str] = None):
        if not content:
            return
        await self._db.execute(
            "INSERT INTO messages (phone, role, content, autor, origen, media) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            phone, role, content, (autor or None), (origen or None), (media or None),
        )

    async def history(self, phone: str, limit: int = 200,
                      before_id: Optional[int] = None) -> list[dict]:
        """
        Los ÚLTIMOS `limit` mensajes del teléfono, en orden cronológico. Antes
        traía los primeros 200 (ASC LIMIT): en un cliente con mucha historia se
        veía lo más viejo y se cortaba lo reciente. `before_id` pagina hacia
        atrás ("cargar anteriores").
        """
        if before_id:
            rows = await self._db.fetch(
                "SELECT id, role, content, autor, origen, media, created_at FROM messages "
                "WHERE phone = $1 AND id < $2 ORDER BY id DESC LIMIT $3",
                phone, int(before_id), limit,
            )
        else:
            rows = await self._db.fetch(
                "SELECT id, role, content, autor, origen, media, created_at FROM messages "
                "WHERE phone = $1 ORDER BY id DESC LIMIT $2",
                phone, limit,
            )
        return [mensaje_a_dict(r) for r in reversed(rows)]

    async def contar(self, phone: str) -> int:
        rows = await self._db.fetch("SELECT COUNT(*) AS n FROM messages WHERE phone = $1", phone)
        return int(rows[0]["n"]) if rows else 0

    async def recurrencia(self, phone: str) -> dict:
        """
        Si el cliente ya escribió antes y con qué frecuencia (spec 4.3).
        Una "conversación" es un día con actividad: alcanza para distinguir a
        quien nos escribe por primera vez del que vuelve seguido.
        """
        rows = await self._db.fetch(
            "SELECT COUNT(*) AS mensajes, "
            "COUNT(DISTINCT date_trunc('day', created_at)) AS dias, "
            "MAX(created_at) AS ultima "
            "FROM messages WHERE phone = $1 AND role = 'user'",
            phone,
        )
        if not rows:
            return {"tipo": "primera_vez", "mensajes": 0, "conversaciones": 0}
        r = rows[0]
        dias = r["dias"] or 0
        tipo = "primera_vez" if dias == 0 else ("frecuente" if dias >= 3 else "ocasional")
        return {
            "tipo": tipo,
            "mensajes": r["mensajes"] or 0,
            "conversaciones": dias,
            "ultima": r["ultima"].isoformat() if r["ultima"] else None,
        }

    async def recent_phones(self, limit: int = 100) -> list[dict]:
        """Teléfonos con actividad reciente + último mensaje (para el backoffice)."""
        rows = await self._db.fetch(
            "SELECT DISTINCT ON (phone) phone, content, created_at "
            "FROM messages ORDER BY phone, created_at DESC LIMIT $1",
            limit,
        )
        return [
            {"phone": r["phone"], "ultimo": r["content"], "ts": r["created_at"].isoformat()}
            for r in rows
        ]


_MEDIA_PREFIX = "📷 /media/chat/"


def mensaje_a_dict(r) -> dict:
    """Fila de `messages` → dict del backoffice. Las fotos del cliente se
    guardan como "📷 /media/chat/{id}": se exponen en `media` para que la
    pantalla las muestre como imagen (el archivo vence a los 7 días)."""
    content = r["content"] or ""
    media = _col(r, "media")
    origen = _col(r, "origen")
    if not media and content.startswith(_MEDIA_PREFIX):
        media = content[len("📷 "):].strip()
    if not origen:
        origen = "imagen" if content.startswith(_MEDIA_PREFIX) else ("texto" if r["role"] == "user" else None)
    return {"id": r["id"], "role": r["role"], "content": content,
            "autor": r["autor"], "origen": origen, "media": media,
            "ts": r["created_at"].isoformat()}


def _col(r, nombre):
    try:
        return r[nombre]
    except (KeyError, IndexError):
        return None


async def guardar_historico(phone: str, role: str, content: str, autor: Optional[str] = None) -> None:
    """
    Guarda un mensaje en el historial permanente desde cualquier punto de envío
    (operador, cotización, pedido listo, avisos automáticos). Best-effort: el
    historial nunca puede romper un envío.
    """
    try:
        from app.config import get_settings
        from app.services.db import get_db
        db = get_db(get_settings().database_url)
        if db.available():
            await get_message_store(db).save(phone, role, content, autor)
    except Exception as e:
        logger.debug(f"historial: no se pudo guardar mensaje de {phone}: {e}")


_instance: Optional[MessageStore] = None


def get_message_store(db) -> MessageStore:
    global _instance
    if _instance is None:
        _instance = MessageStore(db)
    return _instance
