"""Exact-match LLM response cache (Redis) for deterministic calls.

Only temperature-0, tool-free, non-streaming calls are cached: same input ->
same output is a reasonable contract there (extraction, classification, judge
verdicts). The key includes the concrete model chosen, so changing the catalog
never serves an answer from a different model.
"""
import hashlib
import json
from typing import Any

from redis.asyncio import Redis

from agency_platform.cache import rkey
from model_gateway.catalog import LLMCacheConfig


def cacheable(params: dict[str, Any], cfg: LLMCacheConfig) -> bool:
    return cfg.enabled and params.get("temperature") == 0 and not params.get("tools")


def cache_key(model_id: str, messages: list[dict[str, Any]], params: dict[str, Any]) -> str:
    material = json.dumps({"m": model_id, "msg": messages, "p": params}, sort_keys=True, default=str)
    return rkey("llmcache:" + hashlib.sha256(material.encode()).hexdigest())


class LLMCache:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    async def put(self, key: str, payload: dict[str, Any], cfg: LLMCacheConfig) -> None:
        data = json.dumps(payload, default=str)
        if len(data) <= cfg.max_response_chars:
            await self._r.set(key, data, ex=cfg.ttl_s)
