"""Quality data: offline eval runs, online judge results, drift reports and the
feedback-derived eval cases. Used by the quality worker (writes) and the edge
gateway (admin views + Prometheus quality gauges)."""
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pymysql

from agency_platform.db import Database, placeholders

_ER_DUP_ENTRY = 1062


def _latest_per_topic(table: str, columns: str, where: str = "TRUE") -> str:
    """MySQL has no DISTINCT ON: newest row per topic via ROW_NUMBER()."""
    return f"""SELECT {columns} FROM (
                   SELECT {columns}, ROW_NUMBER() OVER (PARTITION BY topic ORDER BY created_at DESC, id DESC) AS rn
                   FROM {table} WHERE {where}
               ) latest WHERE rn = 1 ORDER BY topic"""


class EvalRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------ offline evals
    async def save_offline_run(self, summary: dict[str, Any], gate_passed: bool | None,
                               results: Sequence[dict[str, Any]]) -> int:
        async with self._db.transaction() as conn:
            run_id = await conn.insert(
                """INSERT INTO eval_runs (kind, topic, git_sha, label, cases, pass_rate, mean_judge_score,
                       mean_cost_usd, p95_latency_ms, gate_passed, summary)
                   VALUES ('offline', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (summary["topic"], summary.get("git_sha"), summary.get("label"), summary["cases"],
                 summary["pass_rate"], summary.get("mean_judge_score"), summary["mean_cost_usd"],
                 summary["p95_latency_ms"], gate_passed, summary),
            )
            await conn.executemany(
                """INSERT INTO eval_results (kind, eval_run_id, topic, case_id, request_id, passed, judge_score,
                       scores, failures, rationale, cost_usd, latency_ms)
                   VALUES ('offline', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(run_id, summary["topic"], r["case_id"], r.get("request_id"), r["passed"], r.get("judge_score"),
                  r.get("scores") or {}, r.get("failures") or [], r.get("rationale"), r.get("cost_usd"),
                  r.get("latency_ms")) for r in results],
            )
        return run_id

    async def latest_offline(self, topic: str | None = None) -> list[dict[str, Any]]:
        return await self._db.fetch(
            _latest_per_topic(
                "eval_runs",
                "id, topic, created_at, git_sha, label, cases, pass_rate, mean_judge_score, mean_cost_usd, "
                "p95_latency_ms, gate_passed",
                "kind = 'offline' AND (%(topic)s IS NULL OR topic = %(topic)s)",
            ),
            {"topic": topic},
        )

    # ------------------------------------------------------------ online judging
    async def pending_online(self, limit: int, max_age_hours: int = 48) -> list[dict[str, Any]]:
        """Sampled successful runs not judged yet."""
        return await self._db.fetch(
            """SELECT r.request_id, r.topic, r.prompt, r.answer, r.cost_usd, r.latency_ms
               FROM agent_runs r
               LEFT JOIN eval_results e ON e.online_request_id = r.request_id
               WHERE r.eval_sampled AND r.status = 'success' AND r.answer IS NOT NULL AND r.prompt IS NOT NULL
                 AND e.id IS NULL AND r.created_at > UTC_TIMESTAMP(6) - INTERVAL %s HOUR
               ORDER BY r.created_at
               LIMIT %s""",
            (max_age_hours, limit),
        )

    async def save_online_result(self, *, topic: str, request_id: str, passed: bool, judge_score: float,
                                 scores: dict, rationale: str, cost_usd: Any, latency_ms: int | None) -> bool:
        """Idempotent: a run judged twice (overlapping jobs) keeps its first verdict; returns
        False for the duplicate. (Not ON DUPLICATE KEY: with CLIENT_FOUND_ROWS a no-op
        duplicate also reports 1 row, so it can't tell insert from duplicate.)"""
        try:
            await self._db.execute(
                """INSERT INTO eval_results (kind, eval_run_id, topic, request_id, passed, judge_score, scores,
                       rationale, cost_usd, latency_ms)
                   VALUES ('online', NULL, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (topic, request_id, passed, judge_score, scores, rationale, cost_usd, latency_ms),
            )
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == _ER_DUP_ENTRY:
                return False
            raise
        return True

    async def online_scores(self, topic: str | None = None) -> list[dict[str, Any]]:
        return await self._db.fetch(
            """SELECT topic,
                      AVG(CASE WHEN created_at > UTC_TIMESTAMP(6) - INTERVAL 24 HOUR THEN judge_score END) AS score_24h,
                      AVG(CASE WHEN created_at > UTC_TIMESTAMP(6) - INTERVAL 7 DAY THEN judge_score END) AS score_7d,
                      SUM(created_at > UTC_TIMESTAMP(6) - INTERVAL 7 DAY) AS judged_7d
               FROM eval_results WHERE kind = 'online' AND (%(topic)s IS NULL OR topic = %(topic)s)
               GROUP BY topic""",
            {"topic": topic},
        )

    # ------------------------------------------------------------ drift
    async def window_stats(self, topic: str, start: datetime, end: datetime) -> dict[str, Any]:
        """Raw per-window aggregates for drift detection."""
        runs = await self._db.fetchrow(
            """SELECT COUNT(*) AS runs,
                      AVG(status <> 'success') AS error_rate,
                      AVG(CASE WHEN NOT cache_hit THEN cost_usd END) AS cost_per_run,
                      AVG(CASE WHEN feedback_score IS NOT NULL THEN feedback_score < 0 END) AS neg_rate
               FROM agent_runs
               WHERE topic = %s AND created_at >= %s AND created_at < %s AND status <> 'cancelled'""",
            (topic, start, end),
        ) or {}
        judge = await self._db.fetchval(
            """SELECT AVG(judge_score) AS s FROM eval_results
               WHERE kind = 'online' AND topic = %s AND created_at >= %s AND created_at < %s""",
            (topic, start, end),
        )
        tiers = await self._db.fetch(
            """SELECT tier, COUNT(*) AS n FROM llm_calls
               WHERE client_id = %s AND status = 'success' AND created_at >= %s AND created_at < %s
               GROUP BY tier""",
            (topic, start, end),
        )
        return {**runs, "judge_score": judge, "tiers": {t["tier"]: float(t["n"]) for t in tiers}}

    async def save_drift(self, *, topic: str, window_start: datetime, window_end: datetime,
                         metrics: dict, flagged: list[str]) -> None:
        await self._db.execute(
            """INSERT INTO drift_reports (topic, window_start, window_end, metrics, flagged)
               VALUES (%s, %s, %s, %s, %s)""",
            (topic, window_start, window_end, metrics, flagged),
        )

    async def latest_drift(self, topic: str | None = None) -> list[dict[str, Any]]:
        return await self._db.fetch(
            _latest_per_topic("drift_reports", "id, topic, created_at, flagged, metrics",
                              "(%(topic)s IS NULL OR topic = %(topic)s)"),
            {"topic": topic},
        )

    # ------------------------------------------------------------ feedback -> dataset curation
    async def list_cases(self, *, topic: str | None, status: str, label: str | None,
                         limit: int) -> list[dict[str, Any]]:
        return await self._db.fetch(
            """SELECT id, topic, source_request_id, prompt, answer, label, comment, status, checks, created_at
               FROM eval_cases
               WHERE (%(topic)s IS NULL OR topic = %(topic)s) AND status = %(status)s
                 AND (%(label)s IS NULL OR label = %(label)s)
               ORDER BY created_at DESC LIMIT %(limit)s""",
            {"topic": topic, "status": status, "label": label, "limit": limit},
        )

    async def update_case(self, case_id: int, *, status: str, checks: dict | None) -> dict[str, Any] | None:
        matched = await self._db.execute(
            "UPDATE eval_cases SET status = %s, checks = COALESCE(%s, checks) WHERE id = %s",
            (status, checks, case_id),
        )
        if not matched:
            return None
        return await self._db.fetchrow("SELECT id, status, checks FROM eval_cases WHERE id = %s", (case_id,))

    async def approved_cases(self) -> list[dict[str, Any]]:
        return await self._db.fetch(
            "SELECT id, topic, prompt, label, comment, checks FROM eval_cases WHERE status = 'approved' ORDER BY id"
        )

    async def mark_exported(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        return await self._db.execute(
            f"UPDATE eval_cases SET status = 'exported' WHERE id IN ({placeholders(len(ids))})", tuple(ids)
        )
