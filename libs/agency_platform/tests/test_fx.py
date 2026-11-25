"""Shared currency conversion: ECB rates (fake HTTP), Redis cache, same-currency shortcut, missing rates."""
import os
from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from agency_platform.errors import UpstreamError
from agency_platform.tools.fx import FXRates


def http(status=200, rates=None, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(status, json={"rates": rates or {}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_convert_uses_rates_of_the_target_and_caches_them():
    calls: list[str] = []
    fx = FXRates(http(rates={"GBP": 0.85}, calls=calls), fakeredis.aioredis.FakeRedis(decode_responses=True),
                 lambda: ("https://fx.example/latest", 600))
    assert await fx.convert(85, "gbp", "eur") == 100.0          # 1 EUR = 0.85 GBP
    assert await fx.convert(42, "EUR", "EUR") == 42.0           # same currency: no lookup
    assert await fx.convert(10, "XXX", "EUR") is None           # unknown currency
    assert len(calls) == 1 and "base=EUR" in calls[0]            # second EUR lookup served from Redis


async def test_upstream_failure_is_an_upstream_error():
    fx = FXRates(http(status=503), fakeredis.aioredis.FakeRedis(decode_responses=True))
    with pytest.raises(UpstreamError):
        await fx.rates("EUR")
