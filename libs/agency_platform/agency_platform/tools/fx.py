"""Currency conversion with ECB reference rates (Frankfurter, no key), cached in Redis. Shared by the topics.

    fx = FXRates(http, redis, lambda: (url, cache_ttl_s))   # settings read on every call → hot-reloadable
    await fx.convert(46990, "GBP", "EUR")                   # → 54123.45 (None if a rate is missing)
"""
import json
from collections.abc import Callable
from decimal import Decimal

import httpx
from redis.asyncio import Redis

from agency_platform.cache import rkey
from agency_platform.errors import UpstreamError

DEFAULT_URL = "https://api.frankfurter.app/latest"
RATE_SOURCE = "ECB reference rates (frankfurter.app)"


class FXRates:
    def __init__(self, http: httpx.AsyncClient, redis: Redis,
                 settings: Callable[[], tuple[str, int]] = lambda: (DEFAULT_URL, 21600)) -> None:
        self._http = http
        self._redis = redis
        self._settings = settings

    async def rates(self, base: str) -> dict[str, float]:
        base = base.upper()
        key = rkey(f"fx:{base}")
        if cached := await self._redis.get(key):
            return json.loads(cached)
        url, ttl = self._settings()
        try:
            resp = await self._http.get(url, params={"base": base}, timeout=10)
            resp.raise_for_status()
            rates = {k.upper(): float(v) for k, v in resp.json()["rates"].items()} | {base: 1.0}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise UpstreamError(f"FX rates unavailable for {base}") from exc
        await self._redis.set(key, json.dumps(rates), ex=ttl)
        return rates

    async def convert(self, amount: float, from_ccy: str, to_ccy: str) -> float | None:
        from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
        if from_ccy == to_ccy:
            return round(amount, 2)
        rates = await self.rates(to_ccy)  # rates relative to target: 1 TO = r FROM
        rate = rates.get(from_ccy)
        if not rate:
            return None
        return float((Decimal(str(amount)) / Decimal(str(rate))).quantize(Decimal("0.01")))
