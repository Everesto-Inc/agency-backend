"""EV tools: typed functions the EV agent may call for questions outside the fixed workflow (lists, rankings, country
costs without a car, charging times, buying advice…).

The model only DECIDES which tool to call with which arguments and WRITES the answer; every fact and number comes
from a tool: our catalogue (Open EV Data), official prices (Eurostat, Oil Bulletin), verified specs, and the web
only inside car_specs as the last resort (numbers checked on the page). Calculations are code, never the model.
"""
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from itertools import pairwise
from typing import Any

from agency_platform.errors import AppError
from ev_research import kb
from ev_research import repository as repo
from ev_research.params import params

logger = logging.getLogger(__name__)

MAX_STEPS = 5          # model ↔ tools rounds before we force an answer
MAX_RESULT_CHARS = 6000


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and v is not None else None


# ------------------------------------------------------------------ pure calculations (tested)
def running_costs(consumption_kwh_100km: float, km_per_year: int, electricity_eur_kwh: float,
                  ice_l_100km: float | None = None, fuel_eur_l: float | None = None) -> dict[str, Any]:
    """Pure: yearly / monthly / per-100-km energy cost of an EV (+ the petrol/diesel car it replaces)."""
    kwh = consumption_kwh_100km * km_per_year / 100
    ev = {"kwh_per_year": round(kwh), "cost_per_year": round(kwh * electricity_eur_kwh, 2),
          "cost_per_month": round(kwh * electricity_eur_kwh / 12, 2),
          "cost_per_100km": round(consumption_kwh_100km * electricity_eur_kwh, 2)}
    out: dict[str, Any] = {"ev": ev}
    if ice_l_100km and fuel_eur_l:
        litres = ice_l_100km * km_per_year / 100
        fuel = {"litres_per_year": round(litres), "cost_per_year": round(litres * fuel_eur_l, 2),
                "cost_per_month": round(litres * fuel_eur_l / 12, 2),
                "cost_per_100km": round(ice_l_100km * fuel_eur_l, 2)}
        out["fuel_car"] = fuel
        out["ev_saves_per_year"] = round(fuel["cost_per_year"] - ev["cost_per_year"], 2)
    return out


def charging_minutes(battery_kwh: float, curve: list[dict[str, float]], from_pct: int, to_pct: int,
                     charger_kw: float | None = None, dc_max_kw: float | None = None) -> float | None:
    """Pure: minutes from from_pct to to_pct, step by step along the car's charging curve (power by state of
    charge), capped by the charger. No curve → the car's max DC power, flat (an optimistic estimate)."""
    if not battery_kwh or to_pct <= from_pct:
        return None
    points = sorted((p["percentage"], p["power"]) for p in curve if p.get("power"))

    def power_at(pct: float) -> float | None:
        if not points:
            return dc_max_kw
        if pct <= points[0][0]:
            return points[0][1]
        for (p0, w0), (p1, w1) in pairwise(points):
            if p0 <= pct <= p1:
                return w0 + (w1 - w0) * (pct - p0) / (p1 - p0) if p1 > p0 else w1
        return points[-1][1]

    minutes = 0.0
    for pct in range(from_pct, to_pct):
        kw = power_at(pct + 0.5)
        if not kw:
            return None
        if charger_kw:
            kw = min(kw, charger_kw)
        minutes += battery_kwh * 0.01 / kw * 60
    return round(minutes)


# ------------------------------------------------------------------ the tools (async, use our data)
def _row_out(r: dict[str, Any]) -> dict[str, Any]:
    return {"vehicle": f"{r['make']} {r['model']} {r['variant']}".strip(), "model_year": r.get("release_year"),
            "usable_battery_kwh": r["usable_battery_kwh"], "estimated_range_km": r.get("est_range_km"),
            "consumption_kwh_100km": r.get("consumption_kwh_100km"), "dc_max_kw": r.get("dc_max_kw"),
            "ac_max_kw": r.get("ac_max_kw")}


async def search_catalog(make: str | None = None, min_battery_kwh: float | None = None,
                         max_battery_kwh: float | None = None, min_range_km: float | None = None,
                         min_dc_kw: float | None = None, max_consumption_kwh_100km: float | None = None,
                         min_model_year: int | None = None, sort_by: str = "range", limit: int = 8,
                         **_: Any) -> dict[str, Any]:
    rows = await repo.catalog_rows()
    if make:
        rows = [r for r in rows if kb.brand_key(r["make"]) == kb.brand_key(make)]
    checks = [("usable_battery_kwh", min_battery_kwh, ">="), ("usable_battery_kwh", max_battery_kwh, "<="),
              ("est_range_km", min_range_km, ">="), ("dc_max_kw", min_dc_kw, ">="),
              ("consumption_kwh_100km", max_consumption_kwh_100km, "<="), ("release_year", min_model_year, ">=")]
    for field, limit_v, op in checks:
        if limit_v is not None:
            rows = [r for r in rows if r.get(field) is not None
                    and (r[field] >= limit_v if op == ">=" else r[field] <= limit_v)]
    key = {"range": ("est_range_km", True), "battery": ("usable_battery_kwh", True), "dc_power": ("dc_max_kw", True),
           "efficiency": ("consumption_kwh_100km", False), "newest": ("release_year", True)}.get(sort_by, ("est_range_km", True))
    rows = sorted((r for r in rows if r.get(key[0]) is not None), key=lambda r: r[key[0]], reverse=key[1])
    return {"source": kb.OPEN_EV_DATA, "note": "range = usable battery ÷ average consumption (estimate, not WLTP)",
            "matches": len(rows), "results": [_row_out(r) for r in rows[: max(1, min(limit, 15))]]}


def make_car_specs(sources, ctx):
    async def car_specs(make: str, model: str, year: int | None = None, trim: str = "", **_: Any) -> dict[str, Any]:
        """Verified cache → catalogue → web (numbers checked on the page)."""
        try:
            specs = await sources.ev_specs(make, model, year or 2025, trim or "", ctx)
        except AppError as exc:
            return {"found": False, "reason": exc.message}
        keep = ("matched_vehicle", "battery_kwh", "range_km", "range_estimated", "efficiency_wh_km", "max_charging_kw",
                "base_price", "price_currency", "source", "source_url", "model_year_note", "other_variants")
        return {"found": True, **{k: specs.get(k) for k in keep if specs.get(k) is not None}}
    return car_specs


def make_energy_prices(sources, ctx):
    async def energy_prices(country_code: str, **_: Any) -> dict[str, Any]:
        cc = (country_code or "").strip().upper()[:2]
        out: dict[str, Any] = {"country": cc}
        for metric in ("electricity_price_per_kwh", "petrol_price_per_liter", "diesel_price_per_liter"):
            try:
                p = await sources.country_price(cc, metric, ctx)
                out[metric] = {"value": p["value"], "currency": p["currency"], "source": p.get("source") or p.get("source_url"),
                               "official": bool(p.get("official")), "period": p.get("period")}
            except AppError as exc:
                out[metric] = {"unavailable": exc.message}
        return out
    return energy_prices


def make_running_costs(energy_prices):
    async def costs(country_code: str, consumption_kwh_100km: float, km_per_year: int | None = None,
                    compare_fuel: str | None = None, ice_l_100km: float | None = None, **_: Any) -> dict[str, Any]:
        prices = await energy_prices(country_code)
        elec = prices.get("electricity_price_per_kwh", {})
        if "value" not in elec:
            return {"error": "no electricity price for this country", "prices": prices}
        km = km_per_year or params().assumptions.default_annual_mileage_km
        assumptions = [] if km_per_year else [f"{km:,} km per year (default)"]
        fuel = None
        if compare_fuel in ("petrol", "diesel"):
            fuel = prices.get(f"{compare_fuel}_price_per_liter", {})
            if not ice_l_100km:
                ice_l_100km = params().assumptions.avg_ice_fuel_consumption_l_per_100km
                assumptions.append(f"{compare_fuel} car uses {ice_l_100km} L/100 km (assumed average)")
        result = running_costs(consumption_kwh_100km, km, elec["value"], ice_l_100km if fuel else None,
                               fuel.get("value") if fuel else None)
        return {**result, "currency": elec["currency"], "km_per_year": km, "assumptions": assumptions,
                "prices_used": {"electricity": elec, **({compare_fuel: fuel} if fuel else {})}}
    return costs


async def charging_time(make: str, model: str, from_pct: int = 10, to_pct: int = 80, charger_kw: float | None = None,
                        year: int | None = None, **_: Any) -> dict[str, Any]:
    row = kb.match_catalog(await repo.catalog_rows(), make, model, year)
    if row is None:
        return {"found": False, "reason": "car not in our catalogue"}
    minutes = charging_minutes(row["usable_battery_kwh"], row.get("charging_curve") or [], int(from_pct), int(to_pct),
                               charger_kw, row.get("dc_max_kw"))
    return {"found": True, "vehicle": kb.catalog_specs(row, year)["matched_vehicle"], "from_pct": from_pct, "to_pct": to_pct,
            "charger_kw": charger_kw, "minutes": minutes, "dc_max_kw": row.get("dc_max_kw"),
            "method": "along the car's charging curve" if row.get("charging_curve") else "flat at max DC power (optimistic)",
            "source": kb.OPEN_EV_DATA}


# ------------------------------------------------------------------ schemas the model sees
def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOL_SPECS = [
    _fn("search_catalog", "Find / rank EVs in our catalogue (~1,300 cars, Open EV Data) by filters. Use for lists, "
        "'which EV has…', 'best range under X kWh', 'fastest charging'.",
        {"make": {"type": "string"}, "min_battery_kwh": {"type": "number"}, "max_battery_kwh": {"type": "number"},
         "min_range_km": {"type": "number"}, "min_dc_kw": {"type": "number"},
         "max_consumption_kwh_100km": {"type": "number"}, "min_model_year": {"type": "integer"},
         "sort_by": {"type": "string", "enum": ["range", "battery", "dc_power", "efficiency", "newest"]},
         "limit": {"type": "integer"}}, []),
    _fn("car_specs", "Specs of ONE named EV (battery, range, consumption, charging power, price if known), with source.",
        {"make": {"type": "string"}, "model": {"type": "string"}, "year": {"type": "integer"}, "trim": {"type": "string"}},
        ["make", "model"]),
    _fn("energy_prices", "Current electricity (per kWh) and petrol/diesel (per litre) prices of a country, official "
        "sources first.", {"country_code": {"type": "string", "description": "ISO alpha-2, e.g. DE, FR"}}, ["country_code"]),
    _fn("running_costs", "Calculate an EV's energy cost per year/month/100 km in a country, optionally vs a petrol or "
        "diesel car. Get the consumption from car_specs or search_catalog first.",
        {"country_code": {"type": "string"}, "consumption_kwh_100km": {"type": "number"},
         "km_per_year": {"type": "integer"}, "compare_fuel": {"type": "string", "enum": ["petrol", "diesel"]},
         "ice_l_100km": {"type": "number"}}, ["country_code", "consumption_kwh_100km"]),
    _fn("charging_time", "Minutes to charge a named EV from one % to another, along its charging curve, optionally "
        "limited by the charger's kW.",
        {"make": {"type": "string"}, "model": {"type": "string"}, "from_pct": {"type": "integer"},
         "to_pct": {"type": "integer"}, "charger_kw": {"type": "number"}, "year": {"type": "integer"}},
        ["make", "model"]),
]


SKILL_FILE_SPEC = _fn("read_skill_file", "Read a reference file of a skill in use for this question (listed under "
                      "'Skills for this question'). Long files come in parts: call again with offset = next_offset.",
                      {"skill": {"type": "string"}, "path": {"type": "string"}, "offset": {"type": "integer"}},
                      ["skill", "path"])


def toolbox(sources, ctx) -> dict[str, Callable[..., Awaitable[dict[str, Any]]]]:
    prices = make_energy_prices(sources, ctx)
    tools = {"search_catalog": search_catalog, "car_specs": make_car_specs(sources, ctx), "energy_prices": prices,
             "running_costs": make_running_costs(prices), "charging_time": charging_time}
    if getattr(ctx, "skills", None):
        tools["read_skill_file"] = ctx.read_skill_file
    return tools


# ------------------------------------------------------------------ the loop
async def run_tools(question: str, ctx, sources, system: str, history: list[dict[str, str]], prompt) -> dict[str, Any]:
    """Model ↔ tools until it answers (max MAX_STEPS rounds). Returns the answer + every tool call (trace)."""
    tools = toolbox(sources, ctx)
    specs = TOOL_SPECS + ([SKILL_FILE_SPEC] if "read_skill_file" in tools else [])
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]
    trace: list[dict[str, Any]] = []
    for step in range(MAX_STEPS + 1):
        force_answer = step == MAX_STEPS
        result = await ctx.llm.chat(messages, task="tool_use", prompt=prompt, tools=specs,
                                    tool_choice="none" if force_answer else "auto")
        msg = (result.raw.get("choices") or [{}])[0].get("message") or {}
        calls = [] if force_answer else (msg.get("tool_calls") or [])
        if not calls:
            return {"answer": (msg.get("content") or result.content or "").strip(), "trace": trace, "steps": step}
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})

        async def call(c: dict[str, Any]) -> dict[str, Any]:
            name = c.get("function", {}).get("name", "")
            try:
                args = json.loads(c.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = None
            fn = tools.get(name)
            if fn is None or not isinstance(args, dict):     # the model's mistake: tell it, and keep it in the trace
                trace.append({"tool": name, "args": args if isinstance(args, dict) else {}, "ok": False})
                return {"error": f"unknown tool {name}" if fn is None else "arguments were not valid JSON"}
            try:
                out = await fn(**args)
            except TypeError as exc:
                out = {"error": f"bad arguments: {exc}"}
            except Exception as exc:
                logger.warning("ev tool failed", extra={"tool": name}, exc_info=True)
                out = {"error": f"{type(exc).__name__}"}
            trace.append({"tool": name, "args": args, "ok": "error" not in out})
            return out

        outputs = await asyncio.gather(*(call(c) for c in calls))
        for c, out in zip(calls, outputs, strict=True):
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "content": json.dumps(out, default=str)[:MAX_RESULT_CHARS]})
    return {"answer": "", "trace": trace, "steps": MAX_STEPS}
