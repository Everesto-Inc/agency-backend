"""llm_calls ledger + its usage_daily token/cost rollup, written by the model gateway."""
from collections.abc import Sequence

from agency_platform.db import Database

CALL_COLUMNS = (
    "request_id", "user_id", "client_id", "task", "prompt_ref", "requested_model", "route_source",
    "route_reason", "tier", "model", "attempt", "status", "error_type", "prompt_tokens", "completion_tokens",
    "cost_usd", "latency_ms", "streamed", "cached", "canary", "pii_redactions",
)

_INSERT_CALLS = (
    f"INSERT INTO llm_calls ({', '.join(CALL_COLUMNS)}) VALUES ({', '.join(['%s'] * len(CALL_COLUMNS))})"
)

_UPSERT_DAILY = """
INSERT INTO usage_daily (user_id, topic, day, llm_calls, prompt_tokens, completion_tokens, cost_usd)
VALUES (%s, %s, %s, %s, %s, %s, %s) AS new
ON DUPLICATE KEY UPDATE
    llm_calls = usage_daily.llm_calls + new.llm_calls,
    prompt_tokens = usage_daily.prompt_tokens + new.prompt_tokens,
    completion_tokens = usage_daily.completion_tokens + new.completion_tokens,
    cost_usd = usage_daily.cost_usd + new.cost_usd
"""


class LlmCallRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert_batch(self, calls: Sequence[tuple], daily: Sequence[tuple]) -> None:
        """calls: tuples in CALL_COLUMNS order; daily: (user_id, topic, day, calls, pt, ct, cost).
        One transaction, so the ledger and the rollup never disagree."""
        async with self._db.transaction() as conn:
            await conn.executemany(_INSERT_CALLS, calls)
            if daily:
                await conn.executemany(_UPSERT_DAILY, daily)
