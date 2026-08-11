"""
Métricas históricas del bot en Postgres (tabla interacciones).

Cada respuesta del bot registra: tipo de mensaje, intención detectada, tiempo
total y por paso. Sobre eso se calculan las agregaciones del dashboard del
backoffice. Best-effort: sin Postgres, no-op (el vivo sigue en PerfService/Redis).
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Intenciones que implican que la conversación pasó a una persona
_DERIVACIONES = (
    "derivado_humano", "derivado_receta", "pago_manual", "sin_stock_derivado",
    "receta_link", "imagen_receta", "imagen_credencial", "cambio_postventa",
)


class MetricsStore:
    def __init__(self, db):
        self._db = db

    async def record(self, phone: str, tipo: str, intencion: str,
                     total_ms: int, steps: dict, apis: list):
        try:
            await self._db.execute(
                "INSERT INTO interacciones (phone, tipo, intencion, total_ms, steps, apis) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                phone, tipo, intencion, total_ms, json.dumps(steps), json.dumps(apis),
            )
        except Exception as e:
            logger.debug(f"metrics.record: {e}")

    async def dashboard(self, days: int = 7) -> Optional[dict]:
        """Todas las agregaciones del dashboard en una sola respuesta."""
        if not self._db.available():
            return None
        try:
            derivadas_sql = "(" + ",".join(f"'{d}'" for d in _DERIVACIONES) + ")"

            serie = await self._db.fetch(f"""
                SELECT date_trunc('day', created_at) AS dia,
                       COUNT(*)                       AS mensajes,
                       COUNT(DISTINCT phone)          AS conversaciones,
                       COUNT(*) FILTER (WHERE intencion IN {derivadas_sql}) AS derivaciones,
                       ROUND(AVG(total_ms))           AS avg_ms,
                       percentile_cont(0.5)  WITHIN GROUP (ORDER BY total_ms) AS p50_ms,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) AS p95_ms
                FROM interacciones
                WHERE created_at >= now() - make_interval(days => $1)
                GROUP BY 1 ORDER BY 1
            """, days)

            intenciones = await self._db.fetch("""
                SELECT COALESCE(intencion, 'desconocido') AS intencion, COUNT(*) AS cantidad
                FROM interacciones
                WHERE created_at >= now() - make_interval(days => $1)
                GROUP BY 1 ORDER BY 2 DESC LIMIT 15
            """, days)

            tipos = await self._db.fetch("""
                SELECT COALESCE(tipo, 'text') AS tipo, COUNT(*) AS cantidad
                FROM interacciones
                WHERE created_at >= now() - make_interval(days => $1)
                GROUP BY 1 ORDER BY 2 DESC
            """, days)

            horas = await self._db.fetch("""
                SELECT EXTRACT(hour FROM created_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::int AS hora,
                       COUNT(*) AS cantidad
                FROM interacciones
                WHERE created_at >= now() - make_interval(days => $1)
                GROUP BY 1 ORDER BY 1
            """, days)

            totales = await self._db.fetch(f"""
                SELECT COUNT(*)                        AS mensajes,
                       COUNT(DISTINCT phone)           AS conversaciones,
                       COUNT(*) FILTER (WHERE intencion IN {derivadas_sql}) AS derivaciones,
                       COUNT(*) FILTER (WHERE intencion = 'pedido_confirmado') AS pedidos_confirmados,
                       ROUND(AVG(total_ms))            AS avg_ms,
                       percentile_cont(0.5)  WITHIN GROUP (ORDER BY total_ms) AS p50_ms,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) AS p95_ms
                FROM interacciones
                WHERE created_at >= now() - make_interval(days => $1)
            """, days)

            def _f(v):
                return round(float(v), 1) if v is not None else None

            t = dict(totales[0]) if totales else {}
            return {
                "dias": days,
                "totales": {
                    "mensajes": t.get("mensajes", 0),
                    "conversaciones": t.get("conversaciones", 0),
                    "derivaciones": t.get("derivaciones", 0),
                    "pedidos_confirmados": t.get("pedidos_confirmados", 0),
                    "avg_ms": _f(t.get("avg_ms")),
                    "p50_ms": _f(t.get("p50_ms")),
                    "p95_ms": _f(t.get("p95_ms")),
                },
                "por_dia": [
                    {"dia": r["dia"].date().isoformat(), "mensajes": r["mensajes"],
                     "conversaciones": r["conversaciones"], "derivaciones": r["derivaciones"],
                     "avg_ms": _f(r["avg_ms"]), "p50_ms": _f(r["p50_ms"]), "p95_ms": _f(r["p95_ms"])}
                    for r in serie
                ],
                "intenciones": [{"intencion": r["intencion"], "cantidad": r["cantidad"]} for r in intenciones],
                "tipos_mensaje": [{"tipo": r["tipo"], "cantidad": r["cantidad"]} for r in tipos],
                "por_hora": [{"hora": r["hora"], "cantidad": r["cantidad"]} for r in horas],
            }
        except Exception as e:
            logger.warning(f"metrics.dashboard: {e}")
            return None

    async def conversaciones(self, days: int = 30, q: str = "", limit: int = 50) -> list[dict]:
        """
        Conversaciones históricas desde Postgres (tabla messages): una fila por
        teléfono con actividad en el rango, con conteo y último mensaje.
        `q` filtra por teléfono (contiene).
        """
        if not self._db.available():
            return []
        try:
            filtro_q = "AND phone LIKE $2" if q else ""
            args = [days] + ([f"%{q}%"] if q else []) + [limit]
            limit_idx = 3 if q else 2
            rows = await self._db.fetch(f"""
                SELECT phone,
                       COUNT(*)                                          AS mensajes,
                       COUNT(*) FILTER (WHERE role = 'user')             AS mensajes_cliente,
                       MIN(created_at)                                   AS primera_actividad,
                       MAX(created_at)                                   AS ultima_actividad,
                       (ARRAY_AGG(content ORDER BY created_at DESC))[1]  AS ultimo_mensaje
                FROM messages
                WHERE created_at >= now() - make_interval(days => $1) {filtro_q}
                GROUP BY phone
                ORDER BY MAX(created_at) DESC
                LIMIT ${limit_idx}
            """, *args)
            return [
                {"phone": r["phone"], "mensajes": r["mensajes"],
                 "mensajes_cliente": r["mensajes_cliente"],
                 "primera_actividad": r["primera_actividad"].isoformat(),
                 "ultima_actividad": r["ultima_actividad"].isoformat(),
                 "ultimo_mensaje": (r["ultimo_mensaje"] or "")[:100]}
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"metrics.conversaciones: {e}")
            return []


_instance: Optional[MetricsStore] = None


def get_metrics_store(db) -> MetricsStore:
    global _instance
    if _instance is None:
        _instance = MetricsStore(db)
    return _instance
