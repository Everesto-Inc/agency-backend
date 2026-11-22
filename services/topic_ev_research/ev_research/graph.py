"""EV research as a deterministic LangGraph state machine.

    parse_intent (LLM, low tier) -> retrieve (crawl+verify, cached) -> calculate (pure Python)
        -> synthesize (LLM, mid tier, grounded only in state) -> log_run

Only two nodes touch an LLM, each through the model gateway with a *task*
(not a model), so tiering, plan ceilings, tracking and fallback are uniform.
Any failure routes straight to log_run so failed runs are audited too.
"""
import asyncio
import json
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from agency_platform.agents.base import RunContext
from agency_platform.errors import AppError
from ev_research import calc, repository
from ev_research.params import params
from ev_research.sources import FactSources


class EVState(TypedDict, total=False):
    query: str
    comparison_mode: str
    country_code: str
    annual_mileage_km: int
    vehicle_a: dict[str, Any]
    vehicle_b: dict[str, Any] | None
    fuel_type: str | None
    specs_a: dict[str, Any] | None
    specs_b: dict[str, Any] | None
    electricity_price: dict[str, Any] | None
    fuel_price: dict[str, Any] | None
    cost_a: dict[str, Any] | None
    cost_b: dict[str, Any] | None
    fuel_cost: dict[str, Any] | None
    comparison: dict[str, Any] | None
    analysis: str | None
    unavailable: list[str]
    error: str | None


class ParsedIntent(BaseModel):
    comparison_mode: Literal["single", "ev_vs_ev", "ev_vs_fuel"]
    country_code: str = "US"
    annual_mileage_km: int | None = None
    vehicle_a_make: str
    vehicle_a_model: str
    vehicle_a_year: int
    vehicle_a_trim: str = ""
    vehicle_b_make: str | None = None
    vehicle_b_model: str | None = None
    vehicle_b_year: int | None = None
    fuel_type: Literal["petrol", "diesel"] | None = None


def _deps(config: RunnableConfig) -> tuple[RunContext, FactSources]:
    c = config["configurable"]
    return c["ctx"], c["sources"]


async def parse_intent(state: EVState, config: RunnableConfig) -> dict[str, Any]:
    ctx, _ = _deps(config)
    p = ctx.prompt("parse_intent")
    history = [m for m in ctx.history[-4:] if m["role"] in ("user", "assistant")]
    try:
        intent = await ctx.llm.structured(
            [{"role": "system", "content": p.system}, *history, {"role": "user", "content": state["query"]}],
            ParsedIntent,
            task="intent_parsing",
            prompt=p,
        )
    except AppError as exc:
        return {"error": f"Could not understand the request: {exc.message}"}
    if intent.vehicle_a_make.upper() == "UNKNOWN":
        return {"error": "This assistant answers electric-vehicle questions. Please name an EV model."}

    vehicle_b = None
    if intent.comparison_mode == "ev_vs_ev" and intent.vehicle_b_make and intent.vehicle_b_model:
        vehicle_b = {"make": intent.vehicle_b_make, "model": intent.vehicle_b_model,
                     "year": intent.vehicle_b_year or intent.vehicle_a_year, "trim": ""}
    if intent.comparison_mode == "ev_vs_fuel" and intent.fuel_type is None:
        intent.fuel_type = "petrol"
    return {
        "comparison_mode": intent.comparison_mode,
        "country_code": intent.country_code.upper()[:2],
        "annual_mileage_km": intent.annual_mileage_km or params().assumptions.default_annual_mileage_km,
        "vehicle_a": {"make": intent.vehicle_a_make, "model": intent.vehicle_a_model,
                      "year": intent.vehicle_a_year, "trim": intent.vehicle_a_trim},
        "vehicle_b": vehicle_b,
        "fuel_type": intent.fuel_type,
    }


async def retrieve(state: EVState, config: RunnableConfig) -> dict[str, Any]:
    """All lookups in parallel; each is cache-first."""
    ctx, sources = _deps(config)
    country = state["country_code"]
    jobs: dict[str, Any] = {
        "specs_a": sources.ev_specs(**state["vehicle_a"], ctx=ctx),
        "electricity_price": sources.country_price(country, "electricity_price_per_kwh", ctx),
    }
    if state.get("vehicle_b"):
        jobs["specs_b"] = sources.ev_specs(**state["vehicle_b"], ctx=ctx)
    if state.get("comparison_mode") == "ev_vs_fuel":
        jobs["fuel_price"] = sources.country_price(country, f"{state['fuel_type']}_price_per_liter", ctx)

    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    out: dict[str, Any] = {"unavailable": []}
    for key, result in zip(jobs, results, strict=True):
        if isinstance(result, BaseException):
            msg = result.message if isinstance(result, AppError) else type(result).__name__
            if key.startswith("specs"):  # no verified specs -> nothing grounded to say
                return {"error": f"Could not retrieve {key.replace('_', ' ')}: {msg}"}
            # Prices only feed the cost comparison: answer the rest, say what's missing.
            out["unavailable"].append(f"{key.replace('_', ' ')} ({msg})")
            out[key] = None
            continue
        out[key] = result
    return out


def calculate(state: EVState) -> dict[str, Any]:
    km = state["annual_mileage_km"]
    price = state.get("electricity_price")
    kwh_price = float(price["value"]) if price else None

    def ev_side(specs: dict[str, Any] | None) -> dict[str, Any] | None:
        if not specs or kwh_price is None:
            return None
        eff, source = specs.get("efficiency_wh_km"), "stated"
        if eff is None:
            eff, source = calc.derived_efficiency(specs.get("battery_kwh"), specs.get("range_km")), "derived (battery / range)"
        if eff is None:
            return None
        return {**calc.ev_energy_cost(km, kwh_price, eff), "efficiency_wh_km": eff, "efficiency_source": source}

    cost_a, cost_b = ev_side(state.get("specs_a")), ev_side(state.get("specs_b"))
    fuel = None
    if state.get("comparison_mode") == "ev_vs_fuel" and state.get("fuel_price"):
        consumption = params().assumptions.avg_ice_fuel_consumption_l_per_100km
        fuel = {**calc.fuel_cost(km, float(state["fuel_price"]["value"]), consumption),
                "assumed_consumption_l_per_100km": consumption}

    comparison = None
    if cost_a and cost_b:
        diff = round(cost_a["annual_energy_cost"] - cost_b["annual_energy_cost"], 2)
        comparison = {"annual_cost_difference": diff, "cheaper": "vehicle_a" if diff < 0 else "vehicle_b"}
    elif cost_a and fuel:
        savings = round(fuel["annual_fuel_cost"] - cost_a["annual_energy_cost"], 2)
        comparison = {"annual_savings_vs_fuel": savings, "ev_cheaper": savings > 0}
    return {"cost_a": cost_a, "cost_b": cost_b, "fuel_cost": fuel, "comparison": comparison}


GROUNDED_KEYS = ("vehicle_a", "vehicle_b", "country_code", "annual_mileage_km", "specs_a", "specs_b",
                 "electricity_price", "fuel_price", "cost_a", "cost_b", "fuel_cost", "comparison", "unavailable")


async def synthesize(state: EVState, config: RunnableConfig) -> dict[str, Any]:
    ctx, _ = _deps(config)
    p = ctx.prompt("synthesize")
    grounded = {k: state.get(k) for k in GROUNDED_KEYS}
    try:
        result = await ctx.llm.chat(
            [
                {"role": "system", "content": p.system},
                {"role": "user", "content": f"User asked: {state['query']}\n\nGrounded data (the only facts you may use):\n"
                                            f"{json.dumps(grounded, indent=2, default=str)}"},
            ],
            task="synthesis",
            prompt=p,
        )
    except AppError as exc:
        return {"error": f"Could not write the answer: {exc.message}"}
    return {"analysis": result.content}


async def log_run(state: EVState, config: RunnableConfig) -> dict[str, Any]:
    ctx, _ = _deps(config)
    va = state.get("vehicle_a") or {}
    await repository.log_run(
        request_id=ctx.request_id, user_id=ctx.user.id, session_id=ctx.session_id, query=state["query"],
        parsed_model=f"{va.get('make', '')} {va.get('model', '')}".strip() or None, parsed_year=va.get("year"),
        specs_source_url=(state.get("specs_a") or {}).get("source_url"),
        tco_inputs={k: state.get(k) for k in ("annual_mileage_km", "country_code", "electricity_price", "fuel_price")},
        tco_result={k: state.get(k) for k in ("cost_a", "cost_b", "fuel_cost", "comparison")},
        final_output=state.get("analysis") or state.get("error") or "",
    )
    return {}


def _ok_or_log(next_node: str):
    def route(state: EVState) -> str:
        return "log_run" if state.get("error") else next_node
    return route


def build_graph():
    g = StateGraph(EVState)
    g.add_node("parse_intent", parse_intent)
    g.add_node("retrieve", retrieve)
    g.add_node("calculate", calculate)
    g.add_node("synthesize", synthesize)
    g.add_node("log_run", log_run)
    g.set_entry_point("parse_intent")
    g.add_conditional_edges("parse_intent", _ok_or_log("retrieve"), ["retrieve", "log_run"])
    g.add_conditional_edges("retrieve", _ok_or_log("calculate"), ["calculate", "log_run"])
    g.add_edge("calculate", "synthesize")
    g.add_edge("synthesize", "log_run")
    g.add_edge("log_run", END)
    return g.compile()
