"""Market data behind a provider protocol, with Redis caching.

yfinance is the default (free, no key); it is synchronous, so calls run in a
worker thread. Swap providers (Alpha Vantage, Polygon, FMP...) by implementing
MarketData -- the agents and tools don't change.
"""
import asyncio
import json
import re
from typing import Any, Protocol

from redis.asyncio import Redis

from agency_platform.cache import rkey
from agency_platform.errors import BadRequestError, UpstreamError
from finance.params import params

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,11}$")


def clean_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not _SYMBOL_RE.match(s):
        raise BadRequestError(f"Invalid ticker symbol: {symbol!r}")
    return s


class MarketData(Protocol):
    async def quote(self, symbol: str) -> dict[str, Any]: ...
    async def fundamentals(self, symbol: str) -> dict[str, Any]: ...
    async def closes(self, symbol: str, period: str) -> list[float]: ...


_FUNDAMENTAL_KEYS = {
    "shortName": "name", "sector": "sector", "industry": "industry", "marketCap": "market_cap",
    "trailingPE": "trailing_pe", "forwardPE": "forward_pe", "dividendYield": "dividend_yield",
    "beta": "beta_reported", "fiftyTwoWeekHigh": "week52_high", "fiftyTwoWeekLow": "week52_low",
    "profitMargins": "profit_margin", "returnOnEquity": "return_on_equity", "debtToEquity": "debt_to_equity",
    "totalRevenue": "revenue_ttm", "currency": "currency", "quoteType": "instrument_type",
    "expenseRatio": "expense_ratio", "netExpenseRatio": "expense_ratio",
}


class YFinanceMarketData:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def _cached(self, key: str, ttl: int, loader) -> Any:
        if hit := await self._redis.get(key):
            return json.loads(hit)
        try:
            value = await asyncio.to_thread(loader)
        except Exception as exc:
            raise UpstreamError(f"Market data unavailable ({type(exc).__name__})") from exc
        await self._redis.set(key, json.dumps(value, default=str), ex=ttl)
        return value

    async def quote(self, symbol: str) -> dict[str, Any]:
        import yfinance as yf

        def load() -> dict[str, Any]:
            fi = yf.Ticker(symbol).fast_info
            price = fi.get("lastPrice")
            if price is None:
                raise ValueError("no price")
            return {"symbol": symbol, "price": round(float(price), 4), "currency": fi.get("currency"),
                    "previous_close": fi.get("previousClose"), "as_of": "latest available"}

        return await self._cached(rkey(f"mkt:q:{symbol}"), params().market_data.quote_cache_ttl_s, load)

    async def fundamentals(self, symbol: str) -> dict[str, Any]:
        import yfinance as yf

        def load() -> dict[str, Any]:
            info = yf.Ticker(symbol).info or {}
            out = {"symbol": symbol}
            for src, dst in _FUNDAMENTAL_KEYS.items():
                if info.get(src) is not None:
                    out[dst] = info[src]
            if len(out) == 1:
                raise ValueError("no fundamentals")
            return out

        return await self._cached(rkey(f"mkt:f:{symbol}"), params().market_data.history_cache_ttl_s, load)

    async def closes(self, symbol: str, period: str) -> list[float]:
        import yfinance as yf

        def load() -> list[float]:
            hist = yf.Ticker(symbol).history(period=period, auto_adjust=True)
            values = [float(v) for v in hist["Close"].dropna().tolist()]
            if len(values) < 2:
                raise ValueError("no history")
            return values

        return await self._cached(rkey(f"mkt:h:{symbol}:{period}"), params().market_data.history_cache_ttl_s, load)
