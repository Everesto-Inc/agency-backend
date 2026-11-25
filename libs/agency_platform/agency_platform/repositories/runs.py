"""agent_runs: one row per topic run, plus the per-user x per-topic run counters
in usage_daily, user feedback and the feedback -> eval-case hand-off."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agency_platform.db import Database


@dataclass(frozen=True)
class RunResult:
    status: str
    error_code: str | None
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    llm_calls: int
    answer_chars: int
    answer: str | None
    cache_hit: bool
    eval_sampled: bool


class RunRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self, *, request_id: str, user_id: str, topic: str, session_id: str, prompt: str,
                    prompt_fingerprint: str) -> None:
        await self._db.execute(
            """INSERT INTO agent_runs (request_id, user_id, topic, session_id, prompt, status, prompt_fingerprint)
               VALUES (%s, %s, %s, %s, %s, 'running', %s)
               ON DUPLICATE KEY UPDATE request_id = request_id""",
            (request_id, user_id, topic, session_id, prompt, prompt_fingerprint),
        )

    async def finish(self, request_id: str, r: RunResult) -> None:
        await self._db.execute(
            """UPDATE agent_runs
               SET status = %s, error_code = %s, latency_ms = %s, prompt_tokens = %s, completion_tokens = %s,
                   cost_usd = %s, llm_calls = %s, answer_chars = %s, answer = %s, cache_hit = %s,
                   eval_sampled = %s, finished_at = UTC_TIMESTAMP(6)
               WHERE request_id = %s""",
            (r.status, r.error_code, r.latency_ms, r.prompt_tokens, r.completion_tokens, r.cost_usd, r.llm_calls,
             r.answer_chars, r.answer, r.cache_hit, r.eval_sampled, request_id),
        )

    async def count_daily_run(self, *, user_id: str, topic: str, success: bool, cached: bool) -> None:
        """Per-user x per-topic daily rollup (tokens/cost are added by the model gateway)."""
        await self._db.execute(
            """INSERT INTO usage_daily (user_id, topic, day, runs, successful_runs, cached_runs)
               VALUES (%s, %s, UTC_DATE(), 1, %s, %s) AS new
               ON DUPLICATE KEY UPDATE
                   runs = usage_daily.runs + 1,
                   successful_runs = usage_daily.successful_runs + new.successful_runs,
                   cached_runs = usage_daily.cached_runs + new.cached_runs""",
            (user_id, topic, int(success), int(cached)),
        )

    async def set_feedback(self, *, request_id: str, user_id: str, topic: str, score: int,
                           comment: str | None) -> bool:
        matched = await self._db.execute(
            """UPDATE agent_runs SET feedback_score = %s, feedback_comment = %s
               WHERE request_id = %s AND user_id = %s AND topic = %s""",
            (score, comment, request_id, user_id, topic),
        )
        return matched > 0

    async def propose_eval_case(self, *, request_id: str, label: str, comment: str | None) -> None:
        """Feedback -> dataset: a rated run becomes an eval-case candidate for an admin to
        curate. Re-rating updates the candidate, but never a case already curated."""
        await self._db.execute(
            """INSERT INTO eval_cases (topic, source_request_id, prompt, answer, label, comment, status)
               SELECT * FROM (
                   SELECT topic, request_id, prompt, answer, %s AS label, %s AS comment, 'candidate' AS status
                   FROM agent_runs WHERE request_id = %s AND prompt IS NOT NULL
               ) AS new
               ON DUPLICATE KEY UPDATE
                   label = IF(eval_cases.status = 'candidate', new.label, eval_cases.label),
                   comment = IF(eval_cases.status = 'candidate', new.comment, eval_cases.comment)""",
            (label, comment, request_id),
        )

    async def chat_runs(self, *, user_id: str, session_id: str, exclude_request_id: str, limit: int) -> list[dict[str, Any]]:
        """The last answered questions of one chat, oldest first (memory reload; text is PII-redacted)."""
        rows = await self._db.fetch(
            """SELECT topic, prompt, answer FROM agent_runs
               WHERE user_id = %s AND session_id = %s AND request_id <> %s AND status = 'success'
                 AND prompt IS NOT NULL AND answer IS NOT NULL
               ORDER BY created_at DESC LIMIT %s""",
            (user_id, session_id, exclude_request_id, limit),
        )
        return list(reversed(rows))

    async def list_for_user(self, user_id: str, *, topic: str | None, limit: int,
                            start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
        return await self._db.fetch(
            """SELECT request_id, topic, status, cache_hit, prompt_tokens, completion_tokens, cost_usd,
                      llm_calls, latency_ms, feedback_score, created_at
               FROM agent_runs
               WHERE user_id = %(user_id)s AND (%(topic)s IS NULL OR topic = %(topic)s)
                 AND (%(start)s IS NULL OR created_at >= %(start)s) AND (%(end)s IS NULL OR created_at < %(end)s)
               ORDER BY created_at DESC LIMIT %(limit)s""",
            {"user_id": user_id, "topic": topic, "limit": limit, "start": start, "end": end},
        )

    async def stats(self, *, start: datetime, end: datetime, user_id: str | None = None) -> dict[str, Any]:
        """Run outcomes in [start, end): volume, success, cache, latency, feedback."""
        return await self._db.fetchrow(
            """SELECT COUNT(*) AS runs,
                      COALESCE(SUM(status = 'success'), 0) AS successful,
                      COALESCE(SUM(status IN ('error', 'timeout')), 0) AS failed,
                      COALESCE(SUM(cache_hit), 0) AS cached,
                      AVG(CASE WHEN status = 'success' THEN latency_ms END) AS avg_latency_ms,
                      COALESCE(SUM(feedback_score > 0), 0) AS positive_feedback,
                      COALESCE(SUM(feedback_score < 0), 0) AS negative_feedback
               FROM agent_runs
               WHERE created_at >= %(start)s AND created_at < %(end)s
                 AND (%(user_id)s IS NULL OR user_id = %(user_id)s)""",
            {"start": start, "end": end, "user_id": user_id},
        ) or {}

    async def recent(self, *, topic: str | None, status: str | None, limit: int) -> list[dict[str, Any]]:
        return await self._db.fetch(
            """SELECT request_id, user_id, topic, status, error_code, cache_hit, latency_ms, cost_usd, llm_calls,
                      feedback_score, prompt_fingerprint, created_at
               FROM agent_runs
               WHERE (%(topic)s IS NULL OR topic = %(topic)s) AND (%(status)s IS NULL OR status = %(status)s)
               ORDER BY created_at DESC LIMIT %(limit)s""",
            {"topic": topic, "status": status, "limit": limit},
        )

    async def llm_calls(self, request_id: str) -> list[dict[str, Any]]:
        """Every LLM attempt in one run: which tier/model, why, cost, latency, cache/canary."""
        return await self._db.fetch(
            """SELECT task, prompt_ref, requested_model, route_source, route_reason, tier, model, attempt,
                      status, error_type, cached, canary, pii_redactions, prompt_tokens, completion_tokens,
                      cost_usd, latency_ms, created_at
               FROM llm_calls WHERE request_id = %s ORDER BY id""",
            (request_id,),
        )
