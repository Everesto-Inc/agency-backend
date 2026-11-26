from typing import Literal

from pydantic import BaseModel, Field

Condition = Literal["new", "refurbished", "used"]
Verdict = Literal["exact", "variant", "accessory", "mismatch"]


class SearchPlan(BaseModel):
    product: str = Field(..., description="Canonical product name incl. exact model")
    must_match: list[str] = Field(default_factory=list, description="Attributes an offer must have")
    country_code: str = Field("FR", min_length=2, max_length=2)
    country_stated: bool = Field(True, description="false = the shopper named no country; the default was used")
    currency: str = Field("EUR", min_length=3, max_length=3)
    acceptable_conditions: list[Condition] = Field(default_factory=lambda: ["new"])
    queries: list[str] = Field(..., min_length=1, max_length=8)


class PageExtraction(BaseModel):
    is_product_page: bool = False
    title: str | None = None
    price: float | None = None
    currency: str | None = None
    shipping: float | None = None
    condition: Condition | None = None
    in_stock: bool | None = None


class Offer(BaseModel):
    id: str
    retailer: str
    url: str
    title: str
    price: float
    currency: str
    shipping: float | None = None
    condition: Condition | None = None
    in_stock: bool | None = None
    source: Literal["structured_data", "llm_verified"]
    total_in_target: float | None = None
    target_currency: str | None = None
    verdict: Verdict | None = None
    verdict_note: str | None = None


class OfferVerdict(BaseModel):
    id: str
    verdict: Verdict
    note: str = Field("", max_length=200)


class MatchReport(BaseModel):
    verdicts: list[OfferVerdict]
    caveats: list[str] = Field(default_factory=list, max_length=3)
