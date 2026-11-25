"""Currency normalisation for ranking (ECB rates via Frankfurter) — the shared agency_platform.tools.fx, configured
from config/agents/product_finder.yaml (fx.url, fx.cache_ttl_s; hot-reloaded).

Totals are converted only for *ranking*; the offer's own price/currency is
always shown as listed on the retailer page.
"""
import httpx
from redis.asyncio import Redis

from agency_platform.tools.fx import FXRates as _SharedFX
from product_finder.params import params


class FXRates(_SharedFX):
    def __init__(self, http: httpx.AsyncClient, redis: Redis) -> None:
        super().__init__(http, redis, lambda: (params().fx.url, params().fx.cache_ttl_s))
