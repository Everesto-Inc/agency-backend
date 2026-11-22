"""Topic 1 service: EV helper (LangGraph)."""
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from agency_platform.agents import create_topic_service
from agency_platform.auth.deps import CurrentUser
from agency_platform.errors import NotFoundError
from agency_platform.service import openapi
from ev_research import calc, repository
from ev_research.agent import EVResearchAgent
from ev_research.params import settings

tools_router = APIRouter(responses={**openapi.AUTHED, **openapi.errors(403, 422)})


class EVSpecs(BaseModel):
    battery_kwh: float | None = Field(None, description="Usable battery capacity")
    range_km: float | None = None
    max_charging_kw: float | None = None
    efficiency_wh_km: float | None = None
    base_price: float | None = None
    price_currency: str | None = None
    image_url: str | None = None
    source_url: str | None = Field(None, description="Page every number was verified against")
    fetched_at: datetime


class EnergyCost(BaseModel):
    annual_kwh: float
    annual_energy_cost: float = Field(..., description="In the currency of the electricity price given")


@tools_router.get("/specifications", summary="Verified EV specs already researched (no crawl, no LLM)",
                  responses=openapi.errors(404))
async def specifications(user: CurrentUser, make: str = Query(..., max_length=60, examples=["Hyundai"]),
                         model: str = Query(..., max_length=80, examples=["Ioniq 5"]),
                         year: int = Query(..., ge=2010, le=2035, examples=[2024]),
                         trim: str = Query("", max_length=60)) -> EVSpecs:
    user.require_topic("ev_research")
    specs = await repository.get_cached_specs(make, model, year, trim)
    if specs is None:
        raise NotFoundError("Not cached yet; ask the assistant via /run to research this model")
    return EVSpecs.model_validate(specs)


class CostBody(BaseModel):
    efficiency_wh_km: float = Field(..., gt=0, lt=1000)
    annual_mileage_km: int = Field(15000, gt=0, le=500_000)
    electricity_price_per_kwh: float = Field(..., gt=0, lt=10)


@tools_router.post("/cost-calculator", summary="Annual charging cost (deterministic)")
async def cost_calculator(body: CostBody, user: CurrentUser) -> EnergyCost:
    user.require_topic("ev_research")
    return EnergyCost(**calc.ev_energy_cost(body.annual_mileage_km, body.electricity_price_per_kwh, body.efficiency_wh_km))


app = create_topic_service(EVResearchAgent(), settings(), extra_routers=[(tools_router, "/tools")])
