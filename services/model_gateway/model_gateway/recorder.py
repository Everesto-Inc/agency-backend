"""Records every LLM attempt:

  * Redis (inline): live daily quota, per-run usage, per-user x per-topic counters
  * Prometheus (inline): calls, latency, tokens, cost, canary outcomes
  * MySQL (batched, off the request path):
      - llm_calls    one row per attempt (per-model performance history)
      - usage_daily  per-user x per-topic x day token/cost rollup (billing, "my usage")
"""
import asyncio
import contextlib
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from agency_platform.billing.usage import UsageMeter
from agency_platform.observability.metrics import CANARY_CALLS, LLM_CALLS, LLM_COST, LLM_LATENCY, LLM_TOKENS
from agency_platform.repositories import LlmCallRepository

logger = logging.getLogger(__name__)

# Calls that aren't made by a topic agent are attributed to pseudo-topics so
# per-topic usage always adds up to the user's total.
USAGE_TOPIC_ALIASES = {"edge": "routing", "quality": "evals"}


def usage_topic(client_id: str) -> str:
    return USAGE_TOPIC_ALIASES.get(client_id, client_id)


@dataclass(frozen=True)
class CallRecord:
    request_id: str
    user_id: str | None
    client_id: str
    task: str | None
    prompt_ref: str | None
    requested_model: str
    route_source: str
    route_reason: str
    tier: str
    model: str
    attempt: int
    status: str
    error_type: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    streamed: bool
    cached: bool = False
    canary: bool = False
    pii_redactions: int = 0


def _uuid_or_none(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(value)) if value else None
    except ValueError:
        return None


def daily_rollup(batch: list[CallRecord], day=None) -> list[tuple]:
    """Aggregate a batch into usage_daily upserts (one row per user x topic)."""
    day = day or datetime.now(UTC).date()
    agg: dict[tuple, list] = defaultdict(lambda: [0, 0, 0, 0.0])
    for r in batch:
        uid = _uuid_or_none(r.user_id)
        if r.status != "success" or uid is None:
            continue
        a = agg[(uid, usage_topic(r.client_id))]
        a[0] += 1
        a[1] += r.prompt_tokens
        a[2] += r.completion_tokens
        a[3] += r.cost_usd
    return [(uid, topic, day, *vals) for (uid, topic), vals in agg.items()]


class CallRecorder:
    def __init__(self, repo: LlmCallRepository, meter: UsageMeter, batch_size: int = 200, flush_s: float = 1.0) -> None:
        self._repo = repo
        self._meter = meter
        self._batch_size = batch_size
        self._flush_s = flush_s
        self._queue: asyncio.Queue[CallRecord] = asyncio.Queue(maxsize=10_000)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="llm-call-recorder")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        while not self._queue.empty():  # don't lose the tail on shutdown
            await self._flush(self._drain())

    async def record(self, rec: CallRecord) -> None:
        LLM_CALLS.labels(rec.client_id, rec.tier, rec.model, "cached" if rec.cached else rec.status).inc()
        if not rec.cached:
            LLM_LATENCY.labels(rec.tier, rec.model).observe(rec.latency_ms / 1000)
        if rec.canary:
            CANARY_CALLS.labels(rec.tier, rec.model, rec.status).inc()
        if rec.status == "success":
            LLM_TOKENS.labels(rec.client_id, rec.model, "prompt").inc(rec.prompt_tokens)
            LLM_TOKENS.labels(rec.client_id, rec.model, "completion").inc(rec.completion_tokens)
            LLM_COST.labels(rec.client_id, rec.model).inc(rec.cost_usd)
            if rec.user_id:
                await self._meter.record_llm_usage(
                    user_id=rec.user_id,
                    topic=usage_topic(rec.client_id),
                    request_id=rec.request_id,
                    prompt_tokens=rec.prompt_tokens,
                    completion_tokens=rec.completion_tokens,
                    cost_usd=rec.cost_usd,
                    model=rec.model,
                )
        try:
            self._queue.put_nowait(rec)
        except asyncio.QueueFull:
            logger.error("llm call record dropped: recorder queue full")

    def _drain(self) -> list[CallRecord]:
        items: list[CallRecord] = []
        while not self._queue.empty() and len(items) < self._batch_size:
            items.append(self._queue.get_nowait())
        return items

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            await asyncio.sleep(self._flush_s)
            await self._flush([first, *self._drain()])

    async def _flush(self, batch: list[CallRecord]) -> None:
        if not batch:
            return
        rows = [
            (
                r.request_id, _uuid_or_none(r.user_id), r.client_id, r.task, r.prompt_ref, r.requested_model,
                r.route_source, r.route_reason, r.tier, r.model, r.attempt, r.status, r.error_type,
                r.prompt_tokens, r.completion_tokens, r.cost_usd, r.latency_ms, r.streamed, r.cached, r.canary,
                r.pii_redactions,
            )
            for r in batch
        ]
        try:
            await self._repo.insert_batch(rows, daily_rollup(batch))
        except Exception:
            logger.exception("failed to persist llm call batch", extra={"size": len(rows)})
