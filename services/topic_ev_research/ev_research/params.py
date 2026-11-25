from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr

from agency_platform.config.params import ParamFile
from agency_platform.config.settings import PlatformSettings, get_settings


class EVSettings(PlatformSettings):
    service_name: str = "topic-ev-research"
    tavily_api_key: SecretStr | None = None


class CountryData(BaseModel):
    staleness_days: int = Field(30, ge=1)


class EVSpecs(BaseModel):
    staleness_days: int = Field(90, ge=1)


class Crawl(BaseModel):
    candidate_sources: int = Field(3, ge=1, le=10)
    max_concurrent_crawls: int = Field(2, ge=1, le=16)
    page_timeout_s: int = Field(30, ge=5)
    chunk_token_threshold: int = 4000
    verify_numbers_on_page: bool = True


class Assumptions(BaseModel):
    avg_ice_fuel_consumption_l_per_100km: float = Field(7.0, gt=0)
    default_annual_mileage_km: int = Field(15000, gt=0)


class FX(BaseModel):
    url: str = "https://api.frankfurter.app/latest"   # ECB reference rates, no key
    cache_ttl_s: int = Field(21600, ge=60)


class EVParams(BaseModel):
    country_data: CountryData
    ev_specs: EVSpecs
    crawl: Crawl
    assumptions: Assumptions
    fx: FX = FX()


def settings() -> EVSettings:
    return get_settings(EVSettings)


@lru_cache
def _file() -> ParamFile[EVParams]:
    return ParamFile(settings().config_dir / "agents" / "ev_research.yaml", EVParams)


def params() -> EVParams:
    return _file().get()
