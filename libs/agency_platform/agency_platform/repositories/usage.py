"""Usage history per user x topic x day (usage_daily) for the self-service and admin APIs.

Live "today" numbers come from Redis (billing.usage.UsageMeter); this is the
durable, queryable history (billing source of truth).
Pseudo-topics: 'routing' (the /chat topic classifier), 'evals' (quality jobs).
"""
from datetime import UTC, date, datetime, timedelta
from typing import Any

from agency_platform.db import Database

_TOTALS = """SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens,
             SUM(cost_usd) AS cost_usd, SUM(llm_calls) AS llm_calls, SUM(runs) AS runs,
             SUM(cached_runs) AS cached_runs"""

TOTAL_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd", "llm_calls", "runs", "cached_runs")


def since(days: int) -> date:
    return datetime.now(UTC).date() - timedelta(days=days - 1)


def month_start() -> date:
    return datetime.now(UTC).date().replace(day=1)


def totals_row(r: dict[str, Any]) -> dict[str, Any]:
    pt, ct = int(r["prompt_tokens"] or 0), int(r["completion_tokens"] or 0)
    return {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
        "cost_usd": round(float(r["cost_usd"] or 0), 6),
        "llm_calls": int(r["llm_calls"] or 0),
        "runs": int(r["runs"] or 0),
        "cached_runs": int(r["cached_runs"] or 0),
    }


def total(per_topic: dict[str, dict]) -> dict:
    out = {k: sum(v[k] for v in per_topic.values()) for k in TOTAL_KEYS}
    out["cost_usd"] = round(out["cost_usd"], 6)
    return out


class UsageRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def by_topic(self, *, from_day: date, to_day: date | None = None,
                       user_id: str | None = None) -> dict[str, dict]:
        """Totals per topic for from_day <= day < to_day (to_day None = up to today)."""
        rows = await self._db.fetch(
            f"""SELECT topic, {_TOTALS} FROM usage_daily
                WHERE day >= %(from_day)s AND (%(to_day)s IS NULL OR day < %(to_day)s)
                  AND (%(user_id)s IS NULL OR user_id = %(user_id)s)
                GROUP BY topic ORDER BY topic""",
            {"from_day": from_day, "to_day": to_day, "user_id": user_id},
        )
        return {r["topic"]: totals_row(r) for r in rows}

    async def daily_series(self, *, from_day: date, to_day: date | None = None, user_id: str | None = None,
                           topic: str | None = None) -> list[dict]:
        rows = await self._db.fetch(
            f"""SELECT day, topic, {_TOTALS} FROM usage_daily
                WHERE day >= %(from_day)s AND (%(to_day)s IS NULL OR day < %(to_day)s)
                  AND (%(user_id)s IS NULL OR user_id = %(user_id)s)
                  AND (%(topic)s IS NULL OR topic = %(topic)s)
                GROUP BY day, topic ORDER BY day, topic""",
            {"from_day": from_day, "to_day": to_day, "user_id": user_id, "topic": topic},
        )
        return [{"day": r["day"].isoformat(), "topic": r["topic"], **totals_row(r)} for r in rows]

    async def top_users(self, *, from_day: date, limit: int, to_day: date | None = None) -> list[dict]:
        rows = await self._db.fetch(
            f"""WITH per AS (
                    SELECT user_id, topic, {_TOTALS} FROM usage_daily
                    WHERE day >= %s AND (%s IS NULL OR day < %s) GROUP BY user_id, topic
                )
                SELECT p.user_id, u.email, u.plan,
                       SUM(p.cost_usd) AS user_cost,
                       JSON_OBJECTAGG(p.topic, JSON_OBJECT(
                           'tokens', p.prompt_tokens + p.completion_tokens, 'cost_usd', p.cost_usd, 'runs', p.runs
                       )) AS topics
                FROM per p JOIN users u ON u.id = p.user_id
                GROUP BY p.user_id, u.email, u.plan
                ORDER BY user_cost DESC LIMIT %s""",
            (from_day, to_day, to_day, limit),
        )
        return [
            {"user_id": r["user_id"], "email": r["email"], "plan": r["plan"],
             "cost_usd": round(float(r["user_cost"] or 0), 6), "topics": r["topics"]}
            for r in rows
        ]

    async def months(self, user_id: str | None = None, limit: int = 36) -> list[str]:
        """'YYYY-MM' months that have any usage, newest first (for month pickers)."""
        rows = await self._db.fetch(
            """SELECT DISTINCT DATE_FORMAT(day, '%%Y-%%m') AS month FROM usage_daily
               WHERE (%(user_id)s IS NULL OR user_id = %(user_id)s)
               ORDER BY month DESC LIMIT %(limit)s""",
            {"user_id": user_id, "limit": limit},
        )
        return [r["month"] for r in rows]

    async def active_users(self, *, from_day: date, to_day: date) -> int:
        return int(await self._db.fetchval(
            """SELECT COUNT(DISTINCT d.user_id) AS n FROM usage_daily d JOIN users u ON u.id = d.user_id
               WHERE d.day >= %s AND d.day < %s AND u.role <> 'service'""",
            (from_day, to_day),
        ) or 0)
