"""Deterministic tools exposed to the AutoGen Market Data Analyst.

Every figure the team may use originates here; each result is also captured
for the run's `market_data` artifact (auditable provenance of the numbers).
"""
from typing import Any

from agency_platform.errors import AppError, BadRequestError
from finance import analytics
from finance.market import MarketData, clean_symbol
from finance.params import params

_PERIODS = {"1mo", "3mo", "6mo", "1y", "2y", "5y"}


class FinanceTools:
    def __init__(self, market: MarketData) -> None:
        self._m = market
        self.collected: list[dict[str, Any]] = []

    def _record(self, tool: str, args: dict[str, Any], result: Any) -> Any:
        self.collected.append({"tool": tool, "args": args, "result": result})
        return result

    @staticmethod
    def _period(period: str | None) -> str:
        p = period or params().market_data.default_period
        if p not in _PERIODS:
            raise BadRequestError(f"period must be one of {sorted(_PERIODS)}")
        return p

    def _symbols(self, symbols: list[str]) -> list[str]:
        limit = params().market_data.max_symbols_per_request
        if not symbols or len(symbols) > limit:
            raise BadRequestError(f"provide 1-{limit} symbols")
        return [clean_symbol(s) for s in symbols]

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        """Latest price for one ticker symbol (e.g. AAPL, VTI)."""
        try:
            s = clean_symbol(symbol)
            return self._record("get_quote", {"symbol": s}, await self._m.quote(s))
        except AppError as exc:
            return {"error": exc.message}

    async def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        """Company/fund fundamentals: name, sector, market cap, P/E, dividend yield, margins, 52-week range."""
        try:
            s = clean_symbol(symbol)
            return self._record("get_fundamentals", {"symbol": s}, await self._m.fundamentals(s))
        except AppError as exc:
            return {"error": exc.message}

    async def get_risk_return(self, symbol: str, period: str = "1y") -> dict[str, Any]:
        """Historical return, volatility, max drawdown, Sharpe and beta vs benchmark for one symbol over a period (1mo..5y)."""
        try:
            s, p = clean_symbol(symbol), self._period(period)
            a = params().analytics
            prices = await self._m.closes(s, p)
            stats = analytics.series_stats(prices, days_per_year=a.trading_days_per_year, risk_free=a.risk_free_rate)
            if s != a.benchmark:
                stats["beta_vs_" + a.benchmark] = analytics.beta(prices, await self._m.closes(a.benchmark, p))
            return self._record("get_risk_return", {"symbol": s, "period": p}, {"symbol": s, "period": p, **stats})
        except (AppError, ValueError) as exc:
            return {"error": getattr(exc, "message", str(exc))}

    async def get_portfolio_metrics(self, weights: dict[str, float], period: str = "1y") -> dict[str, Any]:
        """Portfolio risk/return for {symbol: weight} (weights are normalised), plus the correlation matrix."""
        try:
            symbols = self._symbols(list(weights))
            w = {clean_symbol(k): float(v) for k, v in weights.items()}
            if any(v < 0 for v in w.values()) or sum(w.values()) <= 0:
                raise BadRequestError("weights must be non-negative and sum to > 0")
            p, a = self._period(period), params().analytics
            prices = {s: await self._m.closes(s, p) for s in symbols}
            series = analytics.portfolio_series(prices, w)
            stats = analytics.series_stats(series, days_per_year=a.trading_days_per_year, risk_free=a.risk_free_rate)
            total = sum(w.values())
            result = {"weights": {k: round(v / total, 4) for k, v in w.items()}, "period": p, **stats,
                      "correlation": analytics.correlation(prices) if len(prices) > 1 else {}}
            return self._record("get_portfolio_metrics", {"weights": w, "period": p}, result)
        except (AppError, ValueError) as exc:
            return {"error": getattr(exc, "message", str(exc))}

    def as_list(self) -> list:
        return [self.get_quote, self.get_fundamentals, self.get_risk_return, self.get_portfolio_metrics]
