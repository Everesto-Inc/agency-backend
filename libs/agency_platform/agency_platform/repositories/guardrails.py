"""guardrail_events: written by security.guardrails.guardrail(), read by Admin → Guardrails."""
from datetime import date, datetime
from typing import Any

from agency_platform.db import Database


class GuardrailRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, *, service: str, kind: str, action: str, topic: str | None, user_id: str | None,
                     request_id: str | None, detail: str) -> None:
        await self._db.execute(
            """INSERT INTO guardrail_events (service, kind, action, topic, user_id, request_id, detail)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (service[:40], kind[:32], action[:16], topic, user_id, request_id, detail[:500]),
        )

    async def totals(self, days: int) -> list[dict[str, Any]]:
        """Per kind + action over the window (most frequent first)."""
        return await self._db.fetch(
            """SELECT kind, action, COUNT(*) AS n, MAX(created_at) AS last_at FROM guardrail_events
               WHERE created_at >= UTC_TIMESTAMP(6) - INTERVAL %s DAY
               GROUP BY kind, action ORDER BY n DESC""",
            (days,),
        )

    async def daily(self, days: int) -> list[dict[str, Any]]:
        return await self._db.fetch(
            """SELECT DATE(created_at) AS day, kind, COUNT(*) AS n FROM guardrail_events
               WHERE created_at >= UTC_TIMESTAMP(6) - INTERVAL %s DAY
               GROUP BY DATE(created_at), kind ORDER BY day""",
            (days,),
        )

    async def recent(self, *, limit: int, kind: str | None = None, before: datetime | None = None) -> list[dict[str, Any]]:
        """Newest first; joins the account email so admins see who (the log itself stores only the id)."""
        return await self._db.fetch(
            """SELECT g.id, g.created_at, g.service, g.kind, g.action, g.topic, g.user_id, a.email AS user_email,
                      g.request_id, g.detail
               FROM guardrail_events g LEFT JOIN users a ON a.id = g.user_id
               WHERE (%s IS NULL OR g.kind = %s) AND (%s IS NULL OR g.created_at < %s)
               ORDER BY g.created_at DESC LIMIT %s""",
            (kind, kind, before, before, limit),
        )


def day_series(rows: list[dict[str, Any]], days: int, today: date) -> list[dict[str, Any]]:
    """Pure: one entry per day (oldest first, zero-filled) with counts per kind."""
    from datetime import timedelta
    out = {(today - timedelta(days=i)).isoformat(): {} for i in range(days - 1, -1, -1)}
    for r in rows:
        d = r["day"].isoformat() if hasattr(r["day"], "isoformat") else str(r["day"])
        if d in out:
            out[d][r["kind"]] = int(r["n"])
    return [{"day": d, "counts": c, "total": sum(c.values())} for d, c in out.items()]
