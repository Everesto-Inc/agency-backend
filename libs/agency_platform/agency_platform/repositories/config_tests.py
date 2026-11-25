"""config_tests: dataset × (live, draft) runs — queued by the edge, run by the quality worker."""
import json
from typing import Any

from agency_platform.db import Database


def _row(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    for k in ("summary", "results"):
        v = out.get(k)
        out[k] = json.loads(v) if isinstance(v, str | bytes) else v
    return out


class ConfigTestRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def queue(self, *, dataset_id: int, draft_id: int | None, max_cases: int, total: int, by: str) -> int:
        return await self._db.insert(
            """INSERT INTO config_tests (dataset_id, draft_id, max_cases, total_cases, created_by)
               VALUES (%s, %s, %s, %s, %s)""", (dataset_id, draft_id, max_cases, total, by[:255]))

    async def get(self, test_id: int, with_results: bool = True) -> dict[str, Any] | None:
        r = await self._db.fetchrow(
            """SELECT t.id, t.dataset_id, t.draft_id, t.max_cases, t.status, t.created_by, t.created_at, t.started_at,
                      t.finished_at, t.done_cases, t.total_cases, t.summary, t.results, t.error,
                      d.name AS dataset_name, d.version AS dataset_version
               FROM config_tests t JOIN eval_datasets d ON d.id = t.dataset_id WHERE t.id = %s""", (test_id,))
        if r is None:
            return None
        out = _row(r)
        if not with_results:
            out["results"] = None
        return out

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """SELECT t.id, t.dataset_id, t.draft_id, t.max_cases, t.status, t.created_by, t.created_at, t.started_at,
                      t.finished_at, t.done_cases, t.total_cases, t.summary, NULL AS results, t.error,
                      d.name AS dataset_name, d.version AS dataset_version
               FROM config_tests t JOIN eval_datasets d ON d.id = t.dataset_id ORDER BY t.id DESC LIMIT %s""", (limit,))
        return [_row(r) for r in rows]

    async def claim_next(self) -> int | None:
        """The oldest queued test → running (atomic: two workers never take the same one)."""
        test_id = await self._db.fetchval("SELECT id FROM config_tests WHERE status = 'queued' ORDER BY id LIMIT 1")
        if test_id is None:
            return None
        n = await self._db.execute(
            """UPDATE config_tests SET status = 'running', started_at = UTC_TIMESTAMP(6)
               WHERE id = %s AND status = 'queued'""", (test_id,))
        return int(test_id) if n else None

    async def progress(self, test_id: int, done: int, results: list[dict[str, Any]]) -> str:
        """Saves progress; returns the current status (so the worker sees a cancel)."""
        await self._db.execute("UPDATE config_tests SET done_cases = %s, results = %s WHERE id = %s",
                               (done, json.dumps(results), test_id))
        return str(await self._db.fetchval("SELECT status FROM config_tests WHERE id = %s", (test_id,)))

    async def finish(self, test_id: int, *, status: str, summary: dict[str, Any] | None,
                     results: list[dict[str, Any]] | None, error: str | None = None) -> None:
        await self._db.execute(
            """UPDATE config_tests SET status = %s, finished_at = UTC_TIMESTAMP(6), summary = %s,
                      results = COALESCE(%s, results), error = %s
               WHERE id = %s AND status IN ('running', 'cancelling')""",
            (status, json.dumps(summary) if summary is not None else None,
             json.dumps(results) if results is not None else None, (error or "")[:500] or None, test_id))

    async def cancel(self, test_id: int) -> bool:
        n = await self._db.execute(
            """UPDATE config_tests SET status = CASE status WHEN 'queued' THEN 'cancelled' ELSE 'cancelling' END,
                      finished_at = CASE status WHEN 'queued' THEN UTC_TIMESTAMP(6) ELSE finished_at END
               WHERE id = %s AND status IN ('queued', 'running')""", (test_id,))
        return n > 0

    async def avg_cost_per_question(self, days: int = 7) -> dict[str, float]:
        """Recent mean cost per answered question, per assistant (for the cost estimate before a run)."""
        rows = await self._db.fetch(
            """SELECT topic, AVG(cost_usd) AS c FROM agent_runs
               WHERE status = 'success' AND created_at >= UTC_TIMESTAMP(6) - INTERVAL %s DAY GROUP BY topic""", (days,))
        return {r["topic"]: float(r["c"] or 0) for r in rows}
