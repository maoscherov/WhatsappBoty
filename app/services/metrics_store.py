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

    # ── Eventos de negocio ──────────────────────────────────────────────────
    async def evento(self, tipo: str, phone: str = None, dato: str = None,
                     monto: float = None, ref: str = None, extra: dict = None):
        """
        Registra un evento de negocio (embudo, pagos, búsquedas, envíos).
        Best-effort: sin Postgres es no-op y nunca interrumpe el flujo del bot.
        """
        try:
            await self._db.execute(
                "INSERT INTO eventos (tipo, phone, dato, monto, ref, extra) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                tipo, phone, (dato or None), monto, ref,
                json.dumps(extra) if extra else None,
            )
        except Exception as e:
            logger.debug(f"metrics.evento({tipo}): {e}")

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

    async def kpis_conversacionales(self, days: int = 7) -> Optional[dict]:
        """
        KPIs de la especificación 4.5: duración y cantidad de interacciones por
        conversación, resolución en el primer contacto (FCR) y emocionalidad.

        Una "conversación" es un día de actividad de un teléfono. Se cuenta como
        resuelta si no terminó derivada a una persona.
        """
        if not self._db.available():
            return None
        try:
            conv = await self._db.fetch(f"""
                WITH conversaciones AS (
                    SELECT phone,
                           date_trunc('day', created_at)                     AS dia,
                           COUNT(*)                                          AS interacciones,
                           EXTRACT(EPOCH FROM (MAX(created_at) - MIN(created_at)))/60 AS minutos,
                           BOOL_OR(intencion LIKE 'derivado%%')              AS derivada
                    FROM interacciones
                    WHERE created_at >= now() - make_interval(days => $1)
                    GROUP BY phone, date_trunc('day', created_at)
                )
                SELECT COUNT(*)                                              AS total,
                       ROUND(AVG(interacciones), 1)                          AS interacciones_prom,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY interacciones) AS interacciones_mediana,
                       ROUND(AVG(minutos)::numeric, 1)                       AS minutos_prom,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY minutos)  AS minutos_mediana,
                       COUNT(*) FILTER (WHERE NOT derivada)                  AS resueltas_por_el_bot
                FROM conversaciones
            """, days)

            emo = await self._db.fetch("""
                SELECT dato AS sentimiento, COUNT(*) AS cantidad
                FROM eventos
                WHERE tipo = 'sentimiento'
                  AND created_at >= now() - make_interval(days => $1)
                GROUP BY 1
            """, days)

            causales = await self._db.fetch("""
                SELECT intencion, COUNT(*) AS cantidad
                FROM interacciones
                WHERE intencion LIKE 'derivado%%'
                  AND created_at >= now() - make_interval(days => $1)
                GROUP BY 1 ORDER BY 2 DESC
            """, days)

            c = dict(conv[0]) if conv else {}
            total = c.get("total") or 0
            resueltas = c.get("resueltas_por_el_bot") or 0
            emo_total = sum(r["cantidad"] for r in emo) or 1

            def _f(v):
                return round(float(v), 1) if v is not None else None

            return {
                "conversaciones": total,
                "interacciones_promedio": _f(c.get("interacciones_prom")),
                "interacciones_mediana": _f(c.get("interacciones_mediana")),
                "minutos_promedio": _f(c.get("minutos_prom")),
                "minutos_mediana": _f(c.get("minutos_mediana")),
                # FCR: resueltas sin pasar por una persona
                "fcr_pct": round(resueltas / total * 100, 1) if total else None,
                "emocionalidad": {
                    r["sentimiento"]: round(r["cantidad"] / emo_total * 100, 1) for r in emo
                },
                "derivaciones_por_causal": [
                    {"causal": r["intencion"].replace("derivado_", ""), "cantidad": r["cantidad"]}
                    for r in causales
                ],
            }
        except Exception as e:
            logger.warning(f"metrics.kpis_conversacionales: {e}")
            return None

    async def busquedas_sin_resultado(self, days: int = 7, limit: int = 20) -> list[dict]:
        """Términos que los clientes pidieron y el catálogo no tiene."""
        if not self._db.available():
            return []
        try:
            rows = await self._db.fetch("""
                SELECT dato AS termino, COUNT(*) AS veces,
                       COUNT(DISTINCT phone) AS clientes, MAX(created_at) AS ultima_vez
                FROM eventos
                WHERE tipo = 'busqueda_sin_resultado' AND dato IS NOT NULL
                  AND created_at >= now() - make_interval(days => $1)
                GROUP BY 1 ORDER BY 2 DESC, 4 DESC LIMIT $2
            """, days, limit)
            return [
                {"termino": r["termino"], "veces": r["veces"], "clientes": r["clientes"],
                 "ultima_vez": r["ultima_vez"].isoformat()}
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"metrics.busquedas_sin_resultado: {e}")
            return []

    async def pagos_por_marca(self, days: int = 7) -> list[dict]:
        """
        Intentos y aprobaciones por marca de tarjeta. Una marca con tasa baja y
        varios intentos indica un problema de habilitación en el comercio (no
        del cliente) — como el caso Mastercard de agosto 2026.
        """
        if not self._db.available():
            return []
        try:
            rows = await self._db.fetch("""
                SELECT COALESCE(NULLIF(dato, ''), 'sin marca') AS marca,
                       COUNT(*)                                          AS intentos,
                       COUNT(*) FILTER (WHERE tipo = 'pago_aprobado')    AS aprobados,
                       COUNT(*) FILTER (WHERE tipo = 'pago_rechazado')   AS rechazados,
                       MODE() WITHIN GROUP (
                           ORDER BY extra ->> 'motivo'
                       ) FILTER (WHERE tipo = 'pago_rechazado')          AS motivo_top
                FROM eventos
                WHERE tipo IN ('pago_aprobado', 'pago_rechazado')
                  AND created_at >= now() - make_interval(days => $1)
                GROUP BY 1 ORDER BY 2 DESC
            """, days)
            out = []
            for r in rows:
                intentos = r["intentos"] or 0
                aprobados = r["aprobados"] or 0
                tasa = round(aprobados / intentos * 100, 1) if intentos else None
                out.append({
                    "marca": r["marca"],
                    "intentos": intentos,
                    "aprobados": aprobados,
                    "rechazados": r["rechazados"] or 0,
                    "tasa_aprobacion": tasa,
                    "motivo_top": r["motivo_top"],
                    # Señal de alarma: varios intentos y casi ninguno aprueba
                    "alerta": bool(intentos >= 3 and tasa is not None and tasa < 50),
                })
            return out
        except Exception as e:
            logger.warning(f"metrics.pagos_por_marca: {e}")
            return []

    async def envios_fallidos(self, days: int = 7, limit: int = 10) -> dict:
        """
        Envíos de WhatsApp rechazados: total del período y los últimos casos.
        Un valor > 0 significa clientes que se quedaron sin respuesta.
        """
        if not self._db.available():
            return {"total": 0, "ultimos": []}
        try:
            tot = await self._db.fetch("""
                SELECT COUNT(*) AS n FROM eventos
                WHERE tipo = 'wa_send_fallo'
                  AND created_at >= now() - make_interval(days => $1)
            """, days)
            ult = await self._db.fetch("""
                SELECT phone, dato, extra, created_at FROM eventos
                WHERE tipo = 'wa_send_fallo'
                  AND created_at >= now() - make_interval(days => $1)
                ORDER BY created_at DESC LIMIT $2
            """, days, limit)
            return {
                "total": tot[0]["n"] if tot else 0,
                "ultimos": [
                    {"phone": r["phone"], "tipo": r["dato"],
                     "detalle": (json.loads(r["extra"]) if r["extra"] else {}).get("detalle", "")[:160],
                     "ts": r["created_at"].isoformat()}
                    for r in ult
                ],
            }
        except Exception as e:
            logger.warning(f"metrics.envios_fallidos: {e}")
            return {"total": 0, "ultimos": []}

    async def embudo(self, days: int = 7) -> Optional[dict]:
        """
        Embudo de venta: de cuántas conversaciones se llega a ofrecer producto,
        a enviar link y a cobrar. Incluye links abandonados (enviados y nunca
        pagados) con el monto que quedó sin cobrar.
        """
        if not self._db.available():
            return None
        try:
            etapas = await self._db.fetch("""
                SELECT
                  (SELECT COUNT(DISTINCT phone) FROM interacciones
                    WHERE created_at >= now() - make_interval(days => $1))            AS conversaciones,
                  (SELECT COUNT(DISTINCT phone) FROM eventos
                    WHERE tipo = 'producto_ofrecido'
                      AND created_at >= now() - make_interval(days => $1))            AS con_oferta,
                  (SELECT COUNT(DISTINCT phone) FROM eventos
                    WHERE tipo = 'link_enviado'
                      AND created_at >= now() - make_interval(days => $1))            AS con_link,
                  (SELECT COUNT(DISTINCT phone) FROM eventos
                    WHERE tipo = 'pago_aprobado'
                      AND created_at >= now() - make_interval(days => $1))            AS pagados
            """, days)

            links = await self._db.fetch("""
                SELECT COUNT(*)                                   AS enviados,
                       COUNT(*) FILTER (WHERE p.ref IS NOT NULL)  AS pagados,
                       COALESCE(SUM(l.monto) FILTER (WHERE p.ref IS NULL), 0) AS monto_abandonado,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (p.created_at - l.created_at))
                       ) FILTER (WHERE p.ref IS NOT NULL)         AS mediana_seg_pago
                FROM eventos l
                LEFT JOIN eventos p
                       ON p.tipo = 'pago_aprobado' AND p.phone = l.phone
                      AND p.created_at >= l.created_at
                WHERE l.tipo = 'link_enviado'
                  AND l.created_at >= now() - make_interval(days => $1)
            """, days)

            e = dict(etapas[0]) if etapas else {}
            li = dict(links[0]) if links else {}
            conv = e.get("conversaciones") or 0

            def _pct(n, d):
                return round(n / d * 100, 1) if d else None

            enviados = li.get("enviados") or 0
            pagados_link = li.get("pagados") or 0
            mediana = li.get("mediana_seg_pago")
            return {
                "etapas": [
                    {"etapa": "Conversaciones", "cantidad": conv, "pct": 100.0 if conv else None},
                    {"etapa": "Producto ofrecido", "cantidad": e.get("con_oferta") or 0,
                     "pct": _pct(e.get("con_oferta") or 0, conv)},
                    {"etapa": "Link enviado", "cantidad": e.get("con_link") or 0,
                     "pct": _pct(e.get("con_link") or 0, conv)},
                    {"etapa": "Pago aprobado", "cantidad": e.get("pagados") or 0,
                     "pct": _pct(e.get("pagados") or 0, conv)},
                ],
                "conversion_total_pct": _pct(e.get("pagados") or 0, conv),
                "links": {
                    "enviados": enviados,
                    "pagados": pagados_link,
                    "abandonados": enviados - pagados_link,
                    "pct_abandono": _pct(enviados - pagados_link, enviados),
                    "monto_abandonado": round(float(li.get("monto_abandonado") or 0), 2),
                    "mediana_min_hasta_pago": round(float(mediana) / 60, 1) if mediana else None,
                },
            }
        except Exception as e:
            logger.warning(f"metrics.embudo: {e}")
            return None

    # ── Tablero CERCA (/bo/tablero) ───────────────────────────────────────────

    async def _kpis_mes(self, ini: str, fin: str) -> dict:
        """Números base de un mes calendario: conversaciones, pagos, montos."""
        rows = await self._db.fetch("""
            SELECT
              (SELECT COUNT(DISTINCT phone) FROM interacciones
                WHERE created_at >= $1::date AND created_at < $2::date)     AS conversaciones,
              (SELECT COUNT(*) FROM eventos WHERE tipo = 'pago_aprobado'
                AND created_at >= $1::date AND created_at < $2::date)       AS pagos,
              (SELECT COALESCE(SUM(monto), 0) FROM eventos WHERE tipo = 'pago_aprobado'
                AND created_at >= $1::date AND created_at < $2::date)       AS monto,
              (SELECT COUNT(*) FROM eventos WHERE tipo = 'fuera_horario'
                AND created_at >= $1::date AND created_at < $2::date)       AS fuera_horario,
              (SELECT COUNT(*) FROM eventos WHERE tipo = 'derivacion'
                AND created_at >= $1::date AND created_at < $2::date)       AS derivaciones
        """, ini, fin)
        return dict(rows[0]) if rows else {}

    async def tablero(self, vertical: str, mes: str) -> dict:
        """
        Todas las secciones del tablero CERCA para un mes calendario, con
        comparativa contra el mes anterior y badge por métrica. Sin Postgres
        devuelve la estructura completa con badges sin_dato: la página del
        backoffice siempre renderiza.
        """
        ini, fin, ini_ant = _rango_mes(mes)
        base = {"vertical": vertical, "mes": mes}

        if not self._db.available():
            return self._tablero_vacio(base)

        try:
            act = await self._kpis_mes(ini, fin)
            ant = await self._kpis_mes(ini_ant, ini)

            conv, conv_ant = act.get("conversaciones") or 0, ant.get("conversaciones") or 0
            pagos, pagos_ant = act.get("pagos") or 0, ant.get("pagos") or 0
            monto, monto_ant = float(act.get("monto") or 0), float(ant.get("monto") or 0)

            # Derivaciones por motivo (ambos verticales las usan)
            derivaciones = [
                {"motivo": r["dato"] or "sin_motivo", "cantidad": r["n"]}
                for r in await self._db.fetch("""
                    SELECT dato, COUNT(*) AS n FROM eventos
                    WHERE tipo = 'derivacion' AND created_at >= $1::date
                      AND created_at < $2::date
                    GROUP BY dato ORDER BY n DESC LIMIT 15
                """, ini, fin)
            ]

            if vertical == "mutual":
                kc = await self.kpis_conversacionales(days=31) or {}
                intencion = [
                    {"intencion": r["intencion"] or "desconocido", "cantidad": r["n"]}
                    for r in await self._db.fetch("""
                        SELECT intencion, COUNT(*) AS n FROM interacciones
                        WHERE created_at >= $1::date AND created_at < $2::date
                        GROUP BY intencion ORDER BY n DESC LIMIT 12
                    """, ini, fin)
                ]
                sentimiento = [
                    {"sentimiento": r["dato"] or "neutro", "cantidad": r["n"]}
                    for r in await self._db.fetch("""
                        SELECT dato, COUNT(*) AS n FROM eventos
                        WHERE tipo = 'sentimiento' AND created_at >= $1::date
                          AND created_at < $2::date
                        GROUP BY dato ORDER BY n DESC
                    """, ini, fin)
                ]
                extremas = await self._db.fetch("""
                    WITH conv AS (
                        SELECT phone, date_trunc('day', created_at) AS dia,
                               COUNT(*) AS interacciones,
                               EXTRACT(EPOCH FROM MAX(created_at) - MIN(created_at))/60 AS minutos
                        FROM interacciones
                        WHERE created_at >= $1::date AND created_at < $2::date
                        GROUP BY phone, dia)
                    SELECT COUNT(*) FILTER (WHERE interacciones > 30 OR minutos > 90) AS extremas,
                           COUNT(*) AS total
                    FROM conv
                """, ini, fin)
                ex = dict(extremas[0]) if extremas else {}
                extremas_pct = (round(ex["extremas"] / ex["total"] * 100, 1)
                                if ex.get("total") else None)
                rec = await self._db.fetch("""
                    WITH mes AS (SELECT DISTINCT phone FROM interacciones
                                 WHERE created_at >= $1::date AND created_at < $2::date)
                    SELECT COUNT(*) FILTER (WHERE EXISTS (
                             SELECT 1 FROM interacciones i2
                             WHERE i2.phone = mes.phone AND i2.created_at < $1::date)) AS recurrentes,
                           COUNT(*) AS total
                    FROM mes
                """, ini, fin)
                rc = dict(rec[0]) if rec else {}
                recurrentes_pct = (round((rc.get("recurrentes") or 0) / rc["total"] * 100, 1)
                                   if rc.get("total") else None)
                return {**base,
                        "panorama": {
                            "conversaciones": _kpi(conv, conv_ant, "medido"),
                            "duracion_media_min": _kpi(kc.get("minutos_promedio"),
                                                       badge="medido"),
                            "interacciones_promedio": _kpi(kc.get("interacciones_promedio"),
                                                           badge="medido"),
                            "recurrentes_pct": _kpi(recurrentes_pct, badge="medido"),
                            "extremas_pct": _kpi(extremas_pct, badge="medido"),
                        },
                        "distribucion": {"intencion": intencion, "sentimiento": sentimiento},
                        "derivaciones": derivaciones,
                        "propuestos": ["prioridad", "canal", "fcr", "ab_testing"]}

            # ── Farmacia ──────────────────────────────────────────────────────
            fh_tipos: dict[str, int] = {}
            for r in await self._db.fetch("""
                SELECT dato, COUNT(*) AS n FROM eventos
                WHERE tipo = 'fuera_horario' AND created_at >= $1::date
                  AND created_at < $2::date GROUP BY dato
            """, ini, fin):
                t = clasificar_fuera_horario(r["dato"] or "")
                fh_tipos[t] = fh_tipos.get(t, 0) + r["n"]

            socios = await self._db.fetch("""
                SELECT COUNT(*) FILTER (WHERE (extra->>'socio')::boolean) AS socios,
                       COUNT(*) AS total
                FROM eventos WHERE tipo = 'pago_aprobado'
                  AND created_at >= $1::date AND created_at < $2::date
            """, ini, fin)
            so = dict(socios[0]) if socios else {}
            socios_pct = (round((so.get("socios") or 0) / so["total"] * 100, 1)
                          if so.get("total") else None)

            sla = await self._db.fetch("""
                SELECT COUNT(*) FILTER (WHERE monto <= 900) AS dentro, COUNT(*) AS total,
                       ROUND(AVG(monto) / 60.0, 1) AS promedio_min
                FROM eventos WHERE tipo = 'derivacion_atendida'
                  AND created_at >= $1::date AND created_at < $2::date
            """, ini, fin)
            sl = dict(sla[0]) if sla else {}
            sla_pct = (round(sl["dentro"] / sl["total"] * 100, 1) if sl.get("total") else None)

            top_monto = [
                {"producto": r["dato"], "monto": float(r["m"] or 0), "pagos": r["n"]}
                for r in await self._db.fetch("""
                    SELECT dato, SUM(monto) AS m, COUNT(*) AS n FROM eventos
                    WHERE tipo = 'producto_ofrecido' AND created_at >= $1::date
                      AND created_at < $2::date AND dato IS NOT NULL
                    GROUP BY dato ORDER BY m DESC NULLS LAST LIMIT 5
                """, ini, fin)
            ]
            mas_nombrados = [
                {"producto": r["dato"], "veces": r["n"]}
                for r in await self._db.fetch("""
                    SELECT dato, COUNT(*) AS n FROM eventos
                    WHERE tipo = 'producto_ofrecido' AND created_at >= $1::date
                      AND created_at < $2::date AND dato IS NOT NULL
                    GROUP BY dato ORDER BY n DESC LIMIT 5
                """, ini, fin)
            ]

            emb = await self.embudo(days=31)
            fh = act.get("fuera_horario") or 0

            return {**base,
                    "panorama": {
                        "conversaciones": _kpi(conv, conv_ant, "medido"),
                        "conversion_venta": _kpi(
                            round(pagos / conv * 100, 1) if conv else None,
                            round(pagos_ant / conv_ant * 100, 1) if conv_ant else None,
                            "medido"),
                        "monto_vendido": _kpi(round(monto, 2), round(monto_ant, 2), "medido"),
                        "ticket_promedio": _kpi(
                            round(monto / pagos, 2) if pagos else None,
                            round(monto_ant / pagos_ant, 2) if pagos_ant else None,
                            "medido"),
                        "fuera_horario_pct": _kpi(
                            round(fh / (conv + fh) * 100, 1) if (conv + fh) else None,
                            badge="medido"),
                        "sla_receta": _kpi(sla_pct, badge="medido", meta_pct=100,
                                           meta_min=15,
                                           demora_promedio_min=sl.get("promedio_min")),
                    },
                    "embudo": emb,
                    "producto": {"mas_nombrados": mas_nombrados, "top_monto": top_monto},
                    "recetas_horario_socios": {
                        "fuera_horario_tipos": fh_tipos,
                        "socios_pct": socios_pct,
                        "derivaciones_sla_pct": sla_pct,
                    },
                    "pagos": {"por_pasarela": await self.pagos_por_marca(days=31),
                              "bot_vs_derivado": (await self.kpis_conversacionales(days=31)
                                                  or {}).get("fcr_pct")},
                    "derivaciones": derivaciones,
                    "propuestos": ["nps_csat", "emocionalidad"]}
        except Exception as e:
            logger.warning(f"metrics.tablero: {e}")
            return self._tablero_vacio(base)

    @staticmethod
    def _tablero_vacio(base: dict) -> dict:
        """La estructura completa con badges sin_dato: la página siempre renderiza."""
        if base.get("vertical") == "mutual":
            return {**base,
                    "panorama": {k: _kpi() for k in
                                 ("conversaciones", "duracion_media_min",
                                  "interacciones_promedio", "recurrentes_pct",
                                  "extremas_pct")},
                    "distribucion": {"intencion": [], "sentimiento": []},
                    "derivaciones": [],
                    "propuestos": ["prioridad", "canal", "fcr", "ab_testing"]}
        return {**base,
                "panorama": {k: _kpi() for k in
                             ("conversaciones", "conversion_venta", "monto_vendido",
                              "ticket_promedio", "fuera_horario_pct", "sla_receta")},
                "embudo": None,
                "producto": {"mas_nombrados": [], "top_monto": []},
                "recetas_horario_socios": {"fuera_horario_tipos": {}, "socios_pct": None,
                                           "derivaciones_sla_pct": None},
                "pagos": {"por_pasarela": [], "bot_vs_derivado": None},
                "derivaciones": [],
                "propuestos": ["nps_csat", "emocionalidad"]}

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


def clasificar_fuera_horario(dato: str) -> str:
    """
    '<weekday>:<hour>' del evento fuera_horario → tipo de cierre del tablero.
    finde (sáb/dom) > mediodía (12-16 hs) > nocturno (resto). 'otro' si el
    dato no parsea.
    """
    try:
        dia, hora = (int(x) for x in (dato or "").split(":"))
    except (ValueError, TypeError):
        return "otro"
    if dia >= 5:
        return "finde"
    if 12 <= hora <= 16:
        return "mediodia"
    return "nocturno"


def variacion_pct(actual, anterior) -> Optional[float]:
    """% de variación vs. el período anterior; None si no hay base."""
    if actual is None or not anterior:
        return None
    return round((actual - anterior) / anterior * 100, 1)


def _rango_mes(mes: str) -> tuple[str, str, str]:
    """'YYYY-MM' → (inicio_mes, inicio_mes_siguiente, inicio_mes_anterior)."""
    from datetime import date
    anio, m = (int(x) for x in mes.split("-"))
    ini = date(anio, m, 1)
    sig = date(anio + 1, 1, 1) if m == 12 else date(anio, m + 1, 1)
    ant = date(anio - 1, 12, 1) if m == 1 else date(anio, m - 1, 1)
    return ini.isoformat(), sig.isoformat(), ant.isoformat()


# Estructura del tablero cuando no hay datos: la página siempre renderiza,
# con el badge "sin_dato" (mismo criterio del diseño: medido/propuesto/sin dato).
def _kpi(valor=None, anterior=None, badge="sin_dato", **extra) -> dict:
    d = {"valor": valor, "anterior": anterior,
         "variacion_pct": variacion_pct(valor, anterior),
         "badge": badge if valor is not None else "sin_dato"}
    d.update(extra)
    return d


_instance: Optional[MetricsStore] = None


def get_metrics_store(db) -> MetricsStore:
    global _instance
    if _instance is None:
        _instance = MetricsStore(db)
    return _instance
