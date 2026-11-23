"""Exact-match response cache for first-turn questions (Redis).

The key covers everything that can change the answer: topic, normalised
question, the prompt fingerprint (versions, content hash and the user's A/B
arms) and the plan's model-tier ceiling (a free user's mid-tier answer must not
be served to a pro user, or vice versa). TTL is per topic (topics.yaml), short
for volatile data like prices and quotes.

Not cached: follow-ups in a conversation (history changes the meaning), runs
that emitted errors.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis

from agency_platform.cache import rkey

_WS = re.compile(r"\s+")


@dataclass
class CachedAnswer:
    answer: str
    artifacts: dict[str, Any] = field(default_factory=dict)


def normalise_question(text: str) -> str:
    return _WS.sub(" ", text.strip().lower()).rstrip("?!. ")


class ResponseCache:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    @staticmethod
    def key(topic: str, fingerprint: str, tier_ceiling: str, prompt: str) -> str:
        raw = "\x1f".join([topic, fingerprint, str(tier_ceiling), normalise_question(prompt)])
        return rkey(f"rcache:{topic}:{hashlib.sha256(raw.encode()).hexdigest()}")

    async def get(self, key: str) -> CachedAnswer | None:
        raw = await self._r.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        return CachedAnswer(answer=data["answer"], artifacts=data.get("artifacts", {}))

    async def put(self, key: str, value: CachedAnswer, ttl_s: int) -> None:
        payload = json.dumps({"answer": value.answer, "artifacts": value.artifacts}, default=str)
        if len(payload) <= 512_000:
            await self._r.set(key, payload, ex=ttl_s)
