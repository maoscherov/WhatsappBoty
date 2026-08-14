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

    async def save(self, phone: str, role: str, content: str):
        if not content:
            return
        await self._db.execute(
            "INSERT INTO messages (phone, role, content) VALUES ($1, $2, $3)",
            phone, role, content,
        )

    async def history(self, phone: str, limit: int = 200) -> list[dict]:
        rows = await self._db.fetch(
            "SELECT role, content, created_at FROM messages "
            "WHERE phone = $1 ORDER BY created_at ASC LIMIT $2",
            phone, limit,
        )
        return [
            {"role": r["role"], "content": r["content"], "ts": r["created_at"].isoformat()}
            for r in rows
        ]

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


_instance: Optional[MessageStore] = None


def get_message_store(db) -> MessageStore:
    global _instance
    if _instance is None:
        _instance = MessageStore(db)
    return _instance
