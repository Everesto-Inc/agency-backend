"""EV research as a deterministic LangGraph state machine.

    parse_intent (LLM, low tier) -> retrieve (crawl+verify, cached) -> calculate (pure Python)
        -> synthesize (LLM, mid tier, grounded only in state) -> log_run

Never guesses what changes the answer: without a stated country there are no local prices or running
costs (the answer asks for the country); every default it used (model year, km/year) is listed as an
assumption; a car price found for another market is converted to the local currency, never mixed.

Only two nodes touch an LLM, each through the model gateway with a *task*
(not a model), so tiering, plan ceilings, tracking and fallback are uniform.
Any failure routes straight to log_run so failed runs are audited too.
"""
import asyncio
import json
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from agency_platform.agents.base import RunContext
from agency_platform.errors import AppError
from agency_platform.tools.fx import RATE_SOURCE
from agency_platform.tools.images import markdown as images_markdown
from ev_research import calc, repository
from ev_research import tools as ev_tools
from ev_research.params import params
from ev_research.sources import FactSources


class EVState(TypedDict, total=False):
    query: str
    comparison_mode: str
    country_code: str | None
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
    wants_images: bool
    pictures: list[dict[str, str]]
    unavailable: list[str]
    assumptions: list[str]
    needs: list[str]
    error: str | None
    out_of_scope: bool
    question_kind: str
    tool_trace: list[dict[str, Any]]


class ParsedIntent(BaseModel):
    # car: one named EV · compare: two EVs / EV vs fuel · general: any other EV question (lists, country costs
    # without a car, charging times, buying advice…) → the tool agent · not_ev: outside EVs
    question_kind: Literal["car", "compare", "general", "not_ev"] = "car"
    comparison_mode: Literal["single", "ev_vs_ev", "ev_vs_fuel"] = "single"
    country_code: str | None = None          # null unless the user said it (this message or the conversation)
    annual_mileage_km: int | None = None
    vehicle_a_make: str | None = None
    vehicle_a_model: str | None = None
    vehicle_a_year: int | None = None
    vehicle_a_year_stated: bool = True
    vehicle_a_trim: str = ""
    vehicle_b_make: str | None = None
    vehicle_b_model: str | None = None
    vehicle_b_year: int | None = None
    fuel_type: Literal["petrol", "diesel"] | None = None
    wants_images: bool = False


def country_of(code: str | None) -> str | None:
    """Pure: a valid ISO alpha-2 code, else None (no country is better than a wrong one)."""
    code = (code or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else None


def _deps(config: RunnableConfig) -> tuple[RunContext, FactSources]:
    c = config["configurable"]
    return c["ctx"], c["sources"]


async def parse_intent(state: EVState, config: RunnableConfig) -> dict[str, Any]:
    ctx, _ = _deps(config)
    p = ctx.prompt("parse_intent")
    history = [{"role": m["role"], "content": m["content"]} for m in ctx.history[-4:] if m["role"] in ("user", "assistant")]
    system = p.system + (f"\n\nSummary of the earlier conversation (context only):\n{ctx.summary}" if ctx.summary else "")
    try:
        intent = await ctx.llm.structured(
            [{"role": "system", "content": system}, *history, {"role": "user", "content": state["query"]}],
            ParsedIntent,
            task="intent_parsing",
            prompt=p,
        )
    except AppError as exc:
        return {"error": f"Could not understand the request: {exc.message}"}
    if intent.question_kind == "not_ev" or (intent.vehicle_a_make or "").upper() == "UNKNOWN":
        return {"error": "This assistant answers electric-vehicle questions.", "out_of_scope": True}
    if intent.question_kind == "general" or not (intent.vehicle_a_make and intent.vehicle_a_model):
        # no single named car to look up: the tool agent picks what it needs (catalogue, prices, calculators)
        return {"question_kind": "general", "country_code": country_of(intent.country_code),
                "annual_mileage_km": intent.annual_mileage_km}
    intent.vehicle_a_year = intent.vehicle_a_year or 2025

    vehicle_b = None
    if intent.comparison_mode == "ev_vs_ev" and intent.vehicle_b_make and intent.vehicle_b_model:
        vehicle_b = {"make": intent.vehicle_b_make, "model": intent.vehicle_b_model,
                     "year": intent.vehicle_b_year or intent.vehicle_a_year, "trim": ""}
    assumptions: list[str] = []
    if intent.comparison_mode == "ev_vs_fuel" and intent.fuel_type is None:
        intent.fuel_type = "petrol"
        assumptions.append("compared with a petrol car (fuel type not given)")
    if not intent.vehicle_a_year_stated:
        assumptions.append(f"model year {intent.vehicle_a_year} (latest — year not given)")
    km = intent.annual_mileage_km or params().assumptions.default_annual_mileage_km
    if not intent.annual_mileage_km:
        assumptions.append(f"{km:,} km per year (default)")
    return {
        "comparison_mode": intent.comparison_mode,
        "country_code": country_of(intent.country_code),
        "annual_mileage_km": km,
        "assumptions": assumptions,
        "wants_images": intent.wants_images,
        "vehicle_a": {"make": intent.vehicle_a_make, "model": intent.vehicle_a_model,
                      "year": intent.vehicle_a_year, "trim": intent.vehicle_a_trim},
        "vehicle_b": vehicle_b,
        "fuel_type": intent.fuel_type,
    }


async def retrieve(state: EVState, config: RunnableConfig) -> dict[str, Any]:
    """All lookups in parallel; each is cache-first."""
    ctx, sources = _deps(config)
    country = state.get("country_code")
    jobs: dict[str, Any] = {"specs_a": sources.ev_specs(**state["vehicle_a"], ctx=ctx)}
    if state.get("vehicle_b"):
        jobs["specs_b"] = sources.ev_specs(**state["vehicle_b"], ctx=ctx)
    if country:  # local prices only for a country the user gave — never a default country
        jobs["electricity_price"] = sources.country_price(country, "electricity_price_per_kwh", ctx)
        if state.get("comparison_mode") == "ev_vs_fuel":
            jobs["fuel_price"] = sources.country_price(country, f"{state['fuel_type']}_price_per_liter", ctx)

    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    out: dict[str, Any] = {"unavailable": [], "needs": [] if country else ["country"],
                           "electricity_price": None, "fuel_price": None}
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
    await _local_prices(out, config)
    return out


async def _local_prices(out: dict[str, Any], config: RunnableConfig) -> None:
    """A car price found in another currency → add the local amount (ECB rate); the original is always kept."""
    fx = config["configurable"].get("fx")
    local = (out.get("electricity_price") or out.get("fuel_price") or {}).get("currency")
    for key in ("specs_a", "specs_b"):
        specs = out.get(key)
        if not (fx and local and specs and specs.get("base_price") and specs.get("price_currency")):
            continue
        if specs["price_currency"].upper() == local.upper():
            continue
        try:
            amount = await fx.convert(float(specs["base_price"]), specs["price_currency"], local)
        except AppError:
            amount = None
        if amount is not None:
            out[key] = {**specs, "base_price_local": {"amount": amount, "currency": local.upper(), "rate_source": RATE_SOURCE}}


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
                 "electricity_price", "fuel_price", "cost_a", "cost_b", "fuel_cost", "comparison", "unavailable",
                 "assumptions", "needs")


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


async def pictures(state: EVState, config: RunnableConfig) -> dict[str, Any]:
    """Asked for a picture → the one cached with the specs (searched already), else image search; stored as our
    copy and appended under the answer as markdown (so it stays in the chat history)."""
    store = config["configurable"].get("images")
    if not state.get("wants_images") or store is None:
        return {}
    _, sources = _deps(config)
    found: list[dict[str, str]] = []
    for vkey, skey in (("vehicle_a", "specs_a"), ("vehicle_b", "specs_b")):
        vehicle, specs = state.get(vkey), state.get(skey) or {}
        if not vehicle:
            continue
        alt = f"{vehicle.get('year', '')} {vehicle.get('make', '')} {vehicle.get('model', '')}".strip()
        candidates = [specs["image_url"]] if specs.get("image_url") else []
        from_search = not candidates
        if from_search:
            try:
                candidates = await sources.image_candidates(vehicle["make"], vehicle["model"], vehicle["year"])
            except AppError:
                candidates = []
        for url in candidates:
            path = await store.save_from_url(url, alt=alt)
            if path:
                found.append({"path": path, "alt": alt, "source": urlsplit(url).hostname or ""})
                if from_search:
                    await repository.set_image_url(vehicle["make"], vehicle["model"], vehicle["year"],
                                                   vehicle.get("trim", ""), url)
                break
    if not found:
        note = "\n\n*I couldn't find a usable picture this time.*"
        return {"analysis": (state.get("analysis") or "") + note, "pictures": []}
    return {"analysis": f"{state.get('analysis') or ''}\n\n{images_markdown(found)}", "pictures": found}


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


async def tool_agent(state: EVState, config: RunnableConfig) -> dict[str, Any]:
    """Any EV question the fixed path doesn't cover: the model calls our tools (catalogue, prices, calculators,
    verified specs) and writes the answer from their results only."""
    ctx, sources = _deps(config)
    p = ctx.prompt("tool_agent")
    known = []
    if state.get("country_code"):
        known.append(f"country: {state['country_code']}")
    if state.get("annual_mileage_km"):
        known.append(f"km per year: {state['annual_mileage_km']}")
    system = p.system + (f"\n\nKnown from the conversation: {', '.join(known)}" if known else "") + \
        (f"\n\nSummary of the earlier conversation (context only):\n{ctx.summary}" if ctx.summary else "")
    history = [{"role": m["role"], "content": m["content"]} for m in ctx.history[-4:] if m["role"] in ("user", "assistant")]
    try:
        out = await ev_tools.run_tools(state["query"], ctx, sources, system, history, p)
    except AppError as exc:
        return {"error": f"Could not answer: {exc.message}"}
    if not out["answer"]:
        return {"error": "Could not put an answer together from our data."}
    return {"analysis": out["answer"], "tool_trace": out["trace"]}


def _after_intent(state: EVState) -> str:
    if state.get("error"):
        return "log_run"
    return "tool_agent" if state.get("question_kind") == "general" else "retrieve"


def _ok_or_log(next_node: str):
    def route(state: EVState) -> str:
        return "log_run" if state.get("error") else next_node
    return route


def build_graph():
    g = StateGraph(EVState)
    g.add_node("parse_intent", parse_intent)
    g.add_node("tool_agent", tool_agent)
    g.add_node("retrieve", retrieve)
    g.add_node("calculate", calculate)
    g.add_node("synthesize", synthesize)
    g.add_node("pictures", pictures)
    g.add_node("log_run", log_run)
    g.set_entry_point("parse_intent")
    g.add_conditional_edges("parse_intent", _after_intent, ["retrieve", "tool_agent", "log_run"])
    g.add_edge("tool_agent", "log_run")
    g.add_conditional_edges("retrieve", _ok_or_log("calculate"), ["calculate", "log_run"])
    g.add_edge("calculate", "synthesize")
    g.add_edge("synthesize", "pictures")
    g.add_edge("pictures", "log_run")
    g.add_edge("log_run", END)
    return g.compile()
