"""Data retention (config/privacy.yaml): null old prompt/answer text, drop old
per-call ledger rows (usage_daily aggregates are kept for billing), old judge
results, stale eval candidates and dead refresh tokens.

Large deletes run in chunks so they never hold long locks or bloat the
HeatWave binlog / replication with one giant transaction.
"""
import logging

from agency_platform.config.schemas import RetentionConfig
from agency_platform.db import Database

logger = logging.getLogger(__name__)

_CHUNK = 5_000


class RetentionRepository:
    def __init__(self, db: Database, chunk: int = _CHUNK) -> None:
        self._db = db
        self._chunk = chunk

    async def _chunked(self, sql: str, days: int) -> int:
        total = 0
        while True:
            n = await self._db.execute(f"{sql} LIMIT {self._chunk}", (days,))
            total += n
            if n < self._chunk:
                return total

    async def apply(self, cfg: RetentionConfig) -> dict[str, int]:
        results = {
            "agent_runs_text": await self._chunked(
                """UPDATE agent_runs SET prompt = NULL, answer = NULL, feedback_comment = NULL
                   WHERE created_at < UTC_TIMESTAMP(6) - INTERVAL %s DAY
                     AND (prompt IS NOT NULL OR answer IS NOT NULL OR feedback_comment IS NOT NULL)""",
                cfg.run_text_days,
            ),
            "eval_cases_text": await self._chunked(
                """DELETE FROM eval_cases WHERE status IN ('candidate', 'rejected')
                   AND created_at < UTC_TIMESTAMP(6) - INTERVAL %s DAY""",
                cfg.run_text_days,
            ),
            "llm_calls": await self._chunked(
                "DELETE FROM llm_calls WHERE created_at < UTC_TIMESTAMP(6) - INTERVAL %s DAY", cfg.llm_calls_days
            ),
            "eval_results": await self._chunked(
                "DELETE FROM eval_results WHERE created_at < UTC_TIMESTAMP(6) - INTERVAL %s DAY",
                cfg.eval_results_days,
            ),
            "refresh_tokens": await self._chunked(
                """DELETE FROM refresh_tokens
                   WHERE (revoked_at IS NOT NULL OR expires_at < UTC_TIMESTAMP(6))
                     AND created_at < UTC_TIMESTAMP(6) - INTERVAL %s DAY""",
                cfg.expired_refresh_tokens_days,
            ),
        }
        logger.info("retention applied", extra=results)
        return results
