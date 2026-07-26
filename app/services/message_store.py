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
