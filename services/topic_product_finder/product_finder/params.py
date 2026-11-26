from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr

from agency_platform.config.params import ParamFile
from agency_platform.config.settings import PlatformSettings, get_settings


class FinderSettings(PlatformSettings):
    service_name: str = "topic-product-finder"
    tavily_api_key: SecretStr | None = None


class SearchParams(BaseModel):
    # country when the shopper names none (request or conversation) — said in the answer, never silent
    default_country: str = Field("FR", min_length=2, max_length=2)
    max_queries: int = Field(4, ge=1, le=8)
    results_per_query: int = Field(6, ge=1, le=20)
    max_pages_to_check: int = Field(12, ge=1, le=40)
    exclude_domains: list[str] = Field(default_factory=list)
    retailer_hints: dict[str, list[str]] = Field(default_factory=dict)


class FetchParams(BaseModel):
    concurrency: int = Field(6, ge=1, le=20)
    timeout_s: float = Field(12, gt=0)
    max_bytes: int = Field(1_500_000, gt=10_000)
    max_text_chars: int = Field(12_000, gt=1000)


class ExtractionParams(BaseModel):
    prefer_structured_data: bool = True
    verify_price_on_page: bool = True


class FXParams(BaseModel):
    url: str
    cache_ttl_s: int = 21600


class RankingParams(BaseModel):
    allowed_verdicts: list[str] = Field(default_factory=lambda: ["exact", "variant"])
    top_n: int = Field(5, ge=1, le=20)


class FinderParams(BaseModel):
    search: SearchParams
    fetch: FetchParams
    extraction: ExtractionParams
    fx: FXParams
    ranking: RankingParams


def settings() -> FinderSettings:
    return get_settings(FinderSettings)


@lru_cache
def _file() -> ParamFile[FinderParams]:
    return ParamFile(settings().config_dir / "agents" / "product_finder.yaml", FinderParams)


def params() -> FinderParams:
    return _file().get()
