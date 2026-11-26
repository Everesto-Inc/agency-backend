from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field

from agency_platform.config.params import ParamFile
from agency_platform.config.settings import PlatformSettings, get_settings

Period = Literal["1mo", "3mo", "6mo", "1y", "2y", "5y"]


class FinanceSettings(PlatformSettings):
    service_name: str = "topic-finance"


class MarketDataParams(BaseModel):
    provider: Literal["yfinance"] = "yfinance"
    quote_cache_ttl_s: int = 300
    history_cache_ttl_s: int = 3600
    default_period: Period = "1y"
    max_symbols_per_request: int = Field(10, ge=1, le=50)


class Market(BaseModel):
    exchanges: list[str]          # yfinance exchange codes listed in this country (search filter)
    currency: str


class MarketsParams(BaseModel):
    # country when the user names none — the answer says it was assumed
    default_country: str = Field("FR", min_length=2, max_length=2)
    by_country: dict[str, Market] = Field(default_factory=dict)


class AnalyticsParams(BaseModel):
    risk_free_rate: float = Field(0.04, ge=0, lt=1)
    benchmark: str = "SPY"
    trading_days_per_year: int = 252


class TeamParams(BaseModel):
    max_messages: int = Field(10, ge=3, le=30)
    max_tool_iterations: int = Field(6, ge=1, le=12)
    require_compliance_approval: bool = True
    disclaimer: str


class FinanceParams(BaseModel):
    market_data: MarketDataParams
    markets: MarketsParams = Field(default_factory=MarketsParams)
    analytics: AnalyticsParams
    team: TeamParams


def settings() -> FinanceSettings:
    return get_settings(FinanceSettings)


@lru_cache
def _file() -> ParamFile[FinanceParams]:
    return ParamFile(settings().config_dir / "agents" / "finance.yaml", FinanceParams)


def params() -> FinanceParams:
    return _file().get()
