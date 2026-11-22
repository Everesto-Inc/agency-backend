"""Read-only analytics for the admin API: per-model performance, routing mix,
per-topic outcomes, prompt A/B experiments and canary comparisons.

MySQL has no percentile_cont: latency percentiles use CUME_DIST() over the
window (discrete percentile = smallest value whose cumulative share >= p).
These scans are what HeatWave accelerates if llm_calls / agent_runs are
loaded into the RAPID secondary engine later.
"""
from datetime import date, datetime
from typing import Any, Literal

from agency_platform.db import Database

Window = Literal["1h", "24h", "7d", "30d"]
# Whitelisted -> safe to interpolate into SQL.
INTERVALS: dict[str, str] = {"1h": "1 HOUR", "24h": "24 HOUR", "7d": "7 DAY", "30d": "30 DAY"}


def _since(window: str) -> str:
    return f"UTC_TIMESTAMP(6) - INTERVAL {INTERVALS[window]}"


def _pct(source: str, keys: str, value: str = "latency_ms") -> str:
    """CTE body: p50/p95 of `value` per `keys` over the rows of `source`."""
    return f"""SELECT {keys},
                      MIN(CASE WHEN cd >= 0.5 THEN {value} END) AS p50_ms,
                      MIN(CASE WHEN cd >= 0.95 THEN {value} END) AS p95_ms
               FROM (SELECT {keys}, {value}, CUME_DIST() OVER (PARTITION BY {keys} ORDER BY {value}) AS cd
                     FROM {source}) ranked
               GROUP BY {keys}"""


def _on(keys: str, left: str, right: str) -> str:
    return " AND ".join(f"{left}.{k.strip()} <=> {right}.{k.strip()}" for k in keys.split(","))


def f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def opt_f(v: Any) -> float | None:
    return float(v) if v is not None else None


def i(v: Any) -> int:
    return int(v) if v is not None else 0


class AdminMetrics:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def models(self, window: Window) -> list[dict]:
        keys = "model, tier, canary"
        rows = await self._db.fetch(
            f"""WITH base AS (
                    SELECT model, tier, canary, status, attempt, cached, latency_ms,
                           prompt_tokens, completion_tokens, cost_usd
                    FROM llm_calls WHERE created_at > {_since(window)}
                ),
                pct AS ({_pct("base WHERE status = 'success' AND NOT cached", keys)})
                SELECT b.model, b.tier, b.canary,
                       COUNT(*) AS attempts,
                       SUM(b.status = 'error') AS errors,
                       SUM(b.attempt > 1) AS fallback_served,
                       SUM(b.cached) AS cached,
                       MAX(p.p50_ms) AS p50_ms, MAX(p.p95_ms) AS p95_ms,
                       SUM(b.prompt_tokens) AS prompt_tokens, SUM(b.completion_tokens) AS completion_tokens,
                       SUM(b.cost_usd) AS cost_usd
                FROM base b LEFT JOIN pct p ON {_on(keys, "p", "b")}
                GROUP BY b.model, b.tier, b.canary ORDER BY attempts DESC"""
        )
        return [
            {
                "model": r["model"], "tier": r["tier"], "canary": bool(r["canary"]), "attempts": r["attempts"],
                "error_rate": round(i(r["errors"]) / r["attempts"], 4) if r["attempts"] else 0,
                "fallback_served": i(r["fallback_served"]),
                "cache_hit_rate": round(i(r["cached"]) / r["attempts"], 4) if r["attempts"] else 0,
                "p50_ms": r["p50_ms"], "p95_ms": r["p95_ms"],
                "prompt_tokens": i(r["prompt_tokens"]), "completion_tokens": i(r["completion_tokens"]),
                "cost_usd": f(r["cost_usd"]),
            }
            for r in rows
        ]

    async def routing(self, window: Window) -> list[dict]:
        """How tiers are being chosen (task / tier / classifier / judge / default) per calling service."""
        rows = await self._db.fetch(
            f"""SELECT client_id, route_source, tier, COUNT(*) AS calls, SUM(cost_usd) AS cost_usd
                FROM llm_calls WHERE status = 'success' AND created_at > {_since(window)}
                GROUP BY client_id, route_source, tier ORDER BY client_id, calls DESC"""
        )
        return [r | {"cost_usd": f(r["cost_usd"])} for r in rows]

    async def topics(self, window: Window) -> list[dict]:
        """Per-topic outcomes: success rate, latency, cost, cache share, feedback, online judge score."""
        rows = await self._db.fetch(
            f"""WITH base AS (
                    SELECT request_id, topic, status, cache_hit, latency_ms, cost_usd, feedback_score
                    FROM agent_runs WHERE created_at > {_since(window)}
                ),
                pct AS ({_pct("base WHERE latency_ms IS NOT NULL", "topic")})
                SELECT r.topic,
                       COUNT(*) AS runs,
                       SUM(r.status = 'success') AS successes,
                       SUM(r.cache_hit) AS cached,
                       MAX(p.p95_ms) AS p95_ms,
                       AVG(r.cost_usd) AS avg_cost_usd,
                       AVG(r.feedback_score) AS avg_feedback,
                       COUNT(r.feedback_score) AS feedback_count,
                       AVG(e.judge_score) AS judge_score
                FROM base r
                LEFT JOIN pct p ON p.topic = r.topic
                LEFT JOIN eval_results e ON e.online_request_id = r.request_id
                GROUP BY r.topic ORDER BY r.topic"""
        )
        return [
            {
                "topic": r["topic"], "runs": r["runs"],
                "success_rate": round(i(r["successes"]) / r["runs"], 4) if r["runs"] else 0,
                "cache_hit_rate": round(i(r["cached"]) / r["runs"], 4) if r["runs"] else 0,
                "p95_ms": r["p95_ms"], "avg_cost_usd": f(r["avg_cost_usd"]),
                "avg_feedback": opt_f(r["avg_feedback"]), "feedback_count": r["feedback_count"],
                "online_judge_score": opt_f(r["judge_score"]),
            }
            for r in rows
        ]

    async def experiments(self, topic: str | None, window: Window) -> list[dict]:
        """Per prompt ref (topic/prompt@version~variant): volume, cost, latency, errors, and the
        feedback + online judge score of the runs that used it."""
        rows = await self._db.fetch(
            f"""WITH calls AS (
                    SELECT prompt_ref, request_id, cost_usd, latency_ms, status
                    FROM llm_calls
                    WHERE prompt_ref IS NOT NULL AND created_at > {_since(window)}
                      AND (%(topic)s IS NULL OR client_id = %(topic)s)
                ),
                pct AS ({_pct("calls", "prompt_ref")})
                SELECT c.prompt_ref,
                       COUNT(*) AS calls,
                       COUNT(DISTINCT c.request_id) AS runs,
                       AVG(c.cost_usd) AS avg_call_cost,
                       MAX(p.p95_ms) AS p95_ms,
                       AVG(c.status = 'error') AS error_rate,
                       AVG(r.feedback_score) AS avg_feedback,
                       COUNT(r.feedback_score) AS feedback_count,
                       AVG(e.judge_score) AS judge_score,
                       COUNT(e.judge_score) AS judged
                FROM calls c
                LEFT JOIN pct p ON p.prompt_ref = c.prompt_ref
                LEFT JOIN agent_runs r ON r.request_id = c.request_id
                LEFT JOIN eval_results e ON e.online_request_id = c.request_id
                GROUP BY c.prompt_ref ORDER BY c.prompt_ref""",
            {"topic": topic},
        )
        return [
            {
                "prompt_ref": r["prompt_ref"], "variant": r["prompt_ref"].partition("~")[2] or "control",
                "calls": r["calls"], "runs": r["runs"], "avg_call_cost_usd": f(r["avg_call_cost"]),
                "p95_ms": r["p95_ms"], "error_rate": round(f(r["error_rate"]), 4),
                "avg_feedback": opt_f(r["avg_feedback"]), "feedback_count": r["feedback_count"],
                "judge_score": opt_f(r["judge_score"]), "judged_runs": r["judged"],
            }
            for r in rows
        ]

    async def canaries(self, window: Window) -> list[dict]:
        """Canary model vs incumbents, for tiers that had canary traffic in the window."""
        keys = "tier, model, canary"
        rows = await self._db.fetch(
            f"""WITH base AS (
                    SELECT request_id, tier, model, canary, status, latency_ms, cost_usd
                    FROM llm_calls
                    WHERE created_at > {_since(window)} AND NOT cached
                      AND tier IN (SELECT DISTINCT tier FROM llm_calls WHERE canary AND created_at > {_since(window)})
                ),
                pct AS ({_pct("base WHERE status = 'success'", keys)})
                SELECT c.tier, c.model, c.canary,
                       COUNT(*) AS attempts,
                       AVG(c.status = 'success') AS success_rate,
                       MAX(p.p95_ms) AS p95_ms,
                       AVG(CASE WHEN c.status = 'success' THEN c.cost_usd END) AS avg_cost_usd,
                       AVG(e.judge_score) AS judge_score
                FROM base c
                LEFT JOIN pct p ON {_on(keys, "p", "c")}
                LEFT JOIN eval_results e ON e.online_request_id = c.request_id
                GROUP BY c.tier, c.model, c.canary ORDER BY c.tier, c.canary DESC"""
        )
        return [
            {
                "tier": r["tier"], "model": r["model"], "canary": bool(r["canary"]), "attempts": r["attempts"],
                "success_rate": round(f(r["success_rate"]), 4), "p95_ms": r["p95_ms"],
                "avg_cost_usd": f(r["avg_cost_usd"]), "judge_score": opt_f(r["judge_score"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------ calendar ranges (dashboards)
    async def models_between(self, start: datetime, end: datetime) -> list[dict]:
        """Tokens, cost and reliability per model in [start, end)."""
        rows = await self._db.fetch(
            """SELECT model, tier,
                      COUNT(*) AS calls,
                      SUM(status = 'error') AS errors,
                      SUM(cached) AS cached,
                      SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens,
                      SUM(cost_usd) AS cost_usd
               FROM llm_calls WHERE created_at >= %s AND created_at < %s
               GROUP BY model, tier ORDER BY cost_usd DESC""",
            (start, end),
        )
        return [
            {
                "model": r["model"], "tier": r["tier"], "calls": r["calls"],
                "error_rate": round(i(r["errors"]) / r["calls"], 4) if r["calls"] else 0.0,
                "cache_hit_rate": round(i(r["cached"]) / r["calls"], 4) if r["calls"] else 0.0,
                "prompt_tokens": i(r["prompt_tokens"]), "completion_tokens": i(r["completion_tokens"]),
                "total_tokens": i(r["prompt_tokens"]) + i(r["completion_tokens"]),
                "cost_usd": round(f(r["cost_usd"]), 6),
            }
            for r in rows
        ]

    async def user_counts(self, start: date, end: date) -> dict[str, Any]:
        """Accounts (service accounts excluded): total, signed up in [start, end), per plan (now)."""
        totals = await self._db.fetchrow(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(created_at >= %s AND created_at < %s), 0) AS new_signups,
                      COALESCE(SUM(NOT is_active), 0) AS disabled
               FROM users WHERE role <> 'service'""",
            (start, end),
        ) or {}
        plans = await self._db.fetch(
            "SELECT plan, COUNT(*) AS n FROM users WHERE role <> 'service' AND is_active GROUP BY plan ORDER BY plan"
        )
        return {
            "total": i(totals.get("total")), "new_signups": i(totals.get("new_signups")),
            "disabled": i(totals.get("disabled")), "by_plan": {p["plan"]: i(p["n"]) for p in plans},
        }
