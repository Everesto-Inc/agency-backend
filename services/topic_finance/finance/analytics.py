"""Deterministic risk/return math on price series (numpy). No LLM involved."""
import math

import numpy as np


def _returns(prices: list[float]) -> np.ndarray:
    p = np.asarray(prices, dtype=float)
    if p.size < 2:
        raise ValueError("need at least two prices")
    return p[1:] / p[:-1] - 1.0


def max_drawdown(prices: list[float]) -> float:
    p = np.asarray(prices, dtype=float)
    peaks = np.maximum.accumulate(p)
    return float(((p - peaks) / peaks).min())


def series_stats(prices: list[float], *, days_per_year: int, risk_free: float) -> dict[str, float]:
    r = _returns(prices)
    years = len(r) / days_per_year
    total = prices[-1] / prices[0] - 1.0
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else total
    vol = float(r.std(ddof=1) * math.sqrt(days_per_year)) if r.size > 1 else 0.0
    sharpe = (cagr - risk_free) / vol if vol > 0 else 0.0
    return {
        "total_return_pct": round(total * 100, 2),
        "annualised_return_pct": round(cagr * 100, 2),
        "annualised_volatility_pct": round(vol * 100, 2),
        "max_drawdown_pct": round(max_drawdown(prices) * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "observations": len(prices),
    }


def beta(asset_prices: list[float], benchmark_prices: list[float]) -> float | None:
    n = min(len(asset_prices), len(benchmark_prices))
    if n < 20:
        return None
    a, b = _returns(asset_prices[-n:]), _returns(benchmark_prices[-n:])
    var = float(np.var(b, ddof=1))
    return round(float(np.cov(a, b, ddof=1)[0, 1]) / var, 2) if var > 0 else None


def portfolio_series(price_map: dict[str, list[float]], weights: dict[str, float]) -> list[float]:
    """Daily-rebalanced portfolio value series (start = 1.0) on the common date tail."""
    n = min(len(v) for v in price_map.values())
    total_w = sum(weights.values())
    w = {k: v / total_w for k, v in weights.items()}
    rets = sum(w[s] * _returns(price_map[s][-n:]) for s in w)
    return [1.0, *np.cumprod(1 + rets).tolist()]


def correlation(price_map: dict[str, list[float]]) -> dict[str, dict[str, float]]:
    n = min(len(v) for v in price_map.values())
    symbols = list(price_map)
    m = np.corrcoef(np.vstack([_returns(price_map[s][-n:]) for s in symbols]))
    return {a: {b: round(float(m[i, j]), 2) for j, b in enumerate(symbols)} for i, a in enumerate(symbols)}
