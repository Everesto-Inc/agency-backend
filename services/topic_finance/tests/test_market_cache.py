"""Market data is cached under REDIS_KEY_PREFIX (shared platform Redis ACL)."""
import json

import fakeredis.aioredis
import pytest

from agency_platform import cache
from finance.market import YFinanceMarketData

PREFIX = "orchid-agency-backend:"


@pytest.fixture
def redis(monkeypatch):
    monkeypatch.setattr(cache, "_prefix", PREFIX)
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def test_market_data_reads_prefixed_cache_keys(redis):
    await redis.set(f"{PREFIX}mkt:q:AAPL", json.dumps({"symbol": "AAPL", "price": 1.0}))
    await redis.set(f"{PREFIX}mkt:f:AAPL", json.dumps({"symbol": "AAPL", "pe": 30}))
    await redis.set(f"{PREFIX}mkt:h:AAPL:1y", json.dumps([1.0, 2.0]))
    market = YFinanceMarketData(redis)
    assert (await market.quote("AAPL"))["price"] == 1.0
    assert (await market.fundamentals("AAPL"))["pe"] == 30
    assert await market.closes("AAPL", "1y") == [1.0, 2.0]
