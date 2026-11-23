"""Live usage metering, quotas and rate limits (Redis; safe across replicas).

Who writes what:
  * model gateway -> record_llm_usage(): real provider token counts + cost,
    per user/day and per run (request_id). It is the only component that sees
    every LLM call regardless of agent framework, so it is the source of truth.
  * topic runner  -> check_*() before a run, count_topic_request() after it.

Everything is also broken down per topic (user x topic x day), because users
see -- and are billed for -- tokens and cost per assistant.

Postgres keeps the durable, queryable history (llm_calls, agent_runs); Redis
holds the hot counters that every request checks.
"""
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

from agency_platform.cache import rkey
from agency_platform.config.schemas import PlanConfig
from agency_platform.errors import QuotaExceededError, RateLimitedError

_DAY_TTL_S = 2 * 24 * 3600
_RUN_TTL_S = 3600
_MICRO = 1_000_000


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _seconds_to_midnight() -> int:
    now = datetime.now(UTC)
    return int(86400 - (now.hour * 3600 + now.minute * 60 + now.second))


@dataclass(frozen=True)
class DailyUsage:
    tokens: int
    cost_usd: float
    token_limit: int
    cost_limit_usd: float

    @property
    def exceeded(self) -> bool:
        return self.tokens >= self.token_limit or self.cost_usd >= self.cost_limit_usd


@dataclass(frozen=True)
class RunUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class UsageMeter:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    # ---- keys -----------------------------------------------------------
    @staticmethod
    def _tokens_key(user_id: str) -> str:
        return rkey(f"usage:tok:{user_id}:{_today()}")

    @staticmethod
    def _cost_key(user_id: str) -> str:
        return rkey(f"usage:cost:{user_id}:{_today()}")

    @staticmethod
    def _run_key(request_id: str) -> str:
        return rkey(f"usage:run:{request_id}")

    @staticmethod
    def _topic_req_key(user_id: str, topic: str) -> str:
        return rkey(f"usage:req:{topic}:{user_id}:{_today()}")

    @staticmethod
    def _per_topic_key(user_id: str) -> str:
        return rkey(f"usage:topic:{user_id}:{_today()}")

    # ---- writes (model gateway) ----------------------------------------------
    async def record_llm_usage(
        self, *, user_id: str, topic: str, request_id: str, prompt_tokens: int, completion_tokens: int, cost_usd: float
    ) -> None:
        micro = round(cost_usd * _MICRO)
        run_key = self._run_key(request_id)
        topic_key = self._per_topic_key(user_id)
        async with self._r.pipeline(transaction=True) as p:
            p.hincrby(topic_key, f"{topic}:tok", prompt_tokens + completion_tokens)
            p.hincrby(topic_key, f"{topic}:cost", micro)
            p.hincrby(topic_key, f"{topic}:calls", 1)
            p.expire(topic_key, _DAY_TTL_S)
            p.incrby(self._tokens_key(user_id), prompt_tokens + completion_tokens)
            p.expire(self._tokens_key(user_id), _DAY_TTL_S)
            p.incrby(self._cost_key(user_id), micro)
            p.expire(self._cost_key(user_id), _DAY_TTL_S)
            p.hincrby(run_key, "prompt_tokens", prompt_tokens)
            p.hincrby(run_key, "completion_tokens", completion_tokens)
            p.hincrby(run_key, "cost_micro", micro)
            p.hincrby(run_key, "llm_calls", 1)
            p.expire(run_key, _RUN_TTL_S)
            await p.execute()

    # ---- reads -----------------------------------------------------------
    async def daily(self, user_id: str, plan: PlanConfig) -> DailyUsage:
        tokens, micro = await self._r.mget(self._tokens_key(user_id), self._cost_key(user_id))
        return DailyUsage(
            tokens=int(tokens or 0),
            cost_usd=int(micro or 0) / _MICRO,
            token_limit=plan.daily_token_limit,
            cost_limit_usd=plan.daily_cost_limit_usd,
        )

    async def run_usage(self, request_id: str) -> RunUsage:
        data = await self._r.hgetall(self._run_key(request_id))
        return RunUsage(
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            cost_usd=int(data.get("cost_micro", 0)) / _MICRO,
            llm_calls=int(data.get("llm_calls", 0)),
        )

    async def topic_requests_today(self, user_id: str, topic: str) -> int:
        return int(await self._r.get(self._topic_req_key(user_id, topic)) or 0)

    async def per_topic_today(self, user_id: str) -> dict[str, dict[str, float]]:
        """Live {topic: {tokens, cost_usd, llm_calls}} for today (UTC)."""
        raw = await self._r.hgetall(self._per_topic_key(user_id))
        out: dict[str, dict[str, float]] = {}
        for field, value in raw.items():
            topic, _, kind = field.rpartition(":")
            entry = out.setdefault(topic, {"tokens": 0, "cost_usd": 0.0, "llm_calls": 0})
            if kind == "tok":
                entry["tokens"] = int(value)
            elif kind == "cost":
                entry["cost_usd"] = int(value) / _MICRO
            elif kind == "calls":
                entry["llm_calls"] = int(value)
        return out

    async def topic_tokens_today(self, user_id: str, topic: str) -> int:
        return int(await self._r.hget(self._per_topic_key(user_id), f"{topic}:tok") or 0)

    # ---- enforcement -------------------------------------------------------
    async def check_daily_quota(self, user_id: str, plan_name: str, plan: PlanConfig) -> DailyUsage:
        usage = await self.daily(user_id, plan)
        if usage.exceeded:
            raise QuotaExceededError(
                f"Daily usage limit reached on the '{plan_name}' plan. Resets at 00:00 UTC.",
                retry_after_s=_seconds_to_midnight(),
                tokens_used=usage.tokens,
                token_limit=usage.token_limit,
            )
        return usage

    async def check_topic_quota(self, user_id: str, topic: str, plan: PlanConfig) -> None:
        limit = plan.topic_daily_limit(topic)
        if limit is None:
            return
        used = await self.topic_requests_today(user_id, topic)
        if used >= limit:
            raise QuotaExceededError(
                f"Daily request limit for '{topic}' reached ({used}/{limit}). Resets at 00:00 UTC.",
                retry_after_s=_seconds_to_midnight(),
                topic=topic,
            )

    async def check_topic_tokens(self, user_id: str, topic: str, plan: PlanConfig) -> None:
        limit = plan.topic_token_limit(topic)
        if limit is None:
            return
        used = await self.topic_tokens_today(user_id, topic)
        if used >= limit:
            raise QuotaExceededError(
                f"Daily token budget for '{topic}' reached ({used}/{limit}). Resets at 00:00 UTC.",
                retry_after_s=_seconds_to_midnight(),
                topic=topic,
            )

    async def count_topic_request(self, user_id: str, topic: str) -> None:
        key = self._topic_req_key(user_id, topic)
        async with self._r.pipeline(transaction=True) as p:
            p.incr(key)
            p.expire(key, _DAY_TTL_S)
            await p.execute()


class RateLimiter:
    """Fixed-window limiter (INCR + EXPIRE). Cheap and good enough for per-user
    request throttling and login brute-force protection."""

    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def hit(self, key: str, limit: int, window_s: int) -> None:
        if limit <= 0:
            raise RateLimitedError("Requests are not allowed on this plan", retry_after_s=window_s)
        bucket = int(datetime.now(UTC).timestamp()) // window_s
        full_key = rkey(f"rl:{key}:{bucket}")
        async with self._r.pipeline(transaction=True) as p:
            p.incr(full_key)
            p.expire(full_key, window_s + 1)
            count, _ = await p.execute()
        if count > limit:
            raise RateLimitedError("Too many requests, slow down", retry_after_s=window_s)

    async def reset(self, key: str, window_s: int) -> None:
        bucket = int(datetime.now(UTC).timestamp()) // window_s
        await self._r.delete(rkey(f"rl:{key}:{bucket}"))
