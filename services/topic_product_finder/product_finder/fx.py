"""Currency normalisation using ECB reference rates (Frankfurter), cached in Redis.

Totals are converted only for *ranking*; the offer's own price/currency is
always shown as listed on the retailer page.
"""
import json
from decimal import Decimal

import httpx
from redis.asyncio import Redis

from agency_platform.errors import UpstreamError
from product_finder.params import params


class FXRates:
    def __init__(self, http: httpx.AsyncClient, redis: Redis) -> None:
        self._http = http
        self._redis = redis

    async def rates(self, base: str) -> dict[str, float]:
        base = base.upper()
        key = f"fx:{base}"
        if cached := await self._redis.get(key):
            return json.loads(cached)
        cfg = params().fx
        try:
            resp = await self._http.get(cfg.url, params={"base": base}, timeout=10)
            resp.raise_for_status()
            rates = {k.upper(): float(v) for k, v in resp.json()["rates"].items()} | {base: 1.0}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise UpstreamError(f"FX rates unavailable for {base}") from exc
        await self._redis.set(key, json.dumps(rates), ex=cfg.cache_ttl_s)
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
