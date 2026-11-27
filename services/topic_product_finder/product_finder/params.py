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
    exclude_domains: list[str] = Field(default_factory=list)
    # the country's main shops: search_web(shops_only=true) searches only these
    retailer_hints: dict[str, list[str]] = Field(default_factory=dict)
    # mobile carriers: their device prices are often only with a plan → always read by the model (requires_plan)
    carrier_domains: list[str] = Field(default_factory=list)


class ResearchParams(BaseModel):
    max_iterations: int = Field(12, ge=2, le=30)     # CrewAI agent steps (one model call each)
    max_searches: int = Field(8, ge=1, le=20)
    max_pages: int = Field(15, ge=1, le=40)
    results_per_search: int = Field(6, ge=1, le=10)
    snippet_chars: int = Field(400, ge=100, le=2000)
    page_text_chars: int = Field(5000, ge=500, le=20000)   # page text the agent reads (tokens!)
    tool_timeout_s: float = Field(90, gt=5)
    history_chars: int = Field(4000, ge=500, le=20000)     # per earlier message (follow-ups need the old table)


class FetchParams(BaseModel):
    timeout_s: float = Field(12, gt=0)
    max_bytes: int = Field(1_500_000, gt=10_000)
    max_text_chars: int = Field(12_000, gt=1000)


class ExtractionParams(BaseModel):
    prefer_structured_data: bool = True
    verify_price_on_page: bool = True


class FXParams(BaseModel):
    url: str
    cache_ttl_s: int = 21600


class FinderParams(BaseModel):
    search: SearchParams
    research: ResearchParams = Field(default_factory=ResearchParams)
    fetch: FetchParams
    extraction: ExtractionParams
    fx: FXParams


def settings() -> FinderSettings:
    return get_settings(FinderSettings)


@lru_cache
def _file() -> ParamFile[FinderParams]:
    return ParamFile(settings().config_dir / "agents" / "product_finder.yaml", FinderParams)


def params() -> FinderParams:
    return _file().get()
