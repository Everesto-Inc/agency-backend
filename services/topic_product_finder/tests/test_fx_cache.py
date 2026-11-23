"""FX rates are cached under REDIS_KEY_PREFIX (shared platform Redis ACL)."""
import json

import fakeredis.aioredis
import httpx

from agency_platform import cache
from product_finder.fx import FXRates

PREFIX = "orchid-agency-backend:"


async def test_fx_rates_read_the_prefixed_cache_key(monkeypatch):
    monkeypatch.setattr(cache, "_prefix", PREFIX)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.set(f"{PREFIX}fx:EUR", json.dumps({"EUR": 1.0, "USD": 1.1}))
    async with httpx.AsyncClient() as http:  # never called: cache hit
        assert (await FXRates(http, redis).rates("eur"))["USD"] == 1.1
