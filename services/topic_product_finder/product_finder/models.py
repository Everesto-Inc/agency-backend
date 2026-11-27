from typing import Literal

from pydantic import BaseModel

Condition = Literal["new", "refurbished", "used"]


class PageExtraction(BaseModel):
    """What the cheap model reads from one shop page (the price is then checked on the page by code)."""
    is_product_page: bool = False
    title: str | None = None
    price: float | None = None
    currency: str | None = None
    shipping: float | None = None
    condition: Condition | None = None
    in_stock: bool | None = None
    variant: str | None = None          # e.g. "256 GB" — the version this price is for
    requires_plan: bool | None = None   # price only with a phone plan / subscription / contract / trade-in


class Offer(BaseModel):
    """A shop's offer whose price was verified on its page."""
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
    variant: str | None = None
    requires_plan: bool | None = None
