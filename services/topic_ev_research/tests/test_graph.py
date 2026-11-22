import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("PROMPTS_DIR", str(ROOT / "prompts"))

import pytest

from agency_platform.errors import UpstreamError
from agency_platform.testing import FakeLLM, fake_context
from ev_research import calc, graph, repository

SPECS = {"battery_kwh": 75.0, "range_km": 500.0, "max_charging_kw": 250.0, "efficiency_wh_km": None,
         "base_price": 42990.0, "price_currency": "EUR", "source_url": "https://example.com/m3", "from_cache": False}


class FakeSources:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail

    async def ev_specs(self, make, model, year, trim, ctx):
        if self.fail == "specs":
            raise UpstreamError("no verifiable data")
        return dict(SPECS)

    async def country_price(self, country, metric, ctx):
        if self.fail == "price":
            raise UpstreamError("no verifiable data")
        value = 0.25 if metric.startswith("electricity") else 1.80
        return {"value": value, "currency": "EUR", "source_url": f"https://example.com/{metric}"}


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    logged = []

    async def fake_log(**kw):
        logged.append(kw)

    monkeypatch.setattr(repository, "log_run", fake_log)
    return logged


def intent(mode="ev_vs_fuel"):
    return {"comparison_mode": mode, "country_code": "fr", "annual_mileage_km": 20000, "vehicle_a_make": "Tesla",
            "vehicle_a_model": "Model 3", "vehicle_a_year": 2024, "vehicle_a_trim": "", "fuel_type": "diesel"}


async def run(llm, sources, query="Model 3 vs diesel in France, 20000 km"):
    ctx = fake_context("ev_research", llm, ROOT / "prompts")
    g = graph.build_graph()
    return await g.ainvoke({"query": query}, config={"configurable": {"ctx": ctx, "sources": sources}})


async def test_full_pipeline_grounded_and_tiered(no_db):
    llm = FakeLLM({"intent_parsing": intent(), "synthesis": "grounded answer"})
    state = await run(llm, FakeSources())
    assert state["analysis"] == "grounded answer"
    # efficiency derived from battery/range (75 kWh / 500 km = 150 Wh/km), clearly labelled
    assert state["cost_a"]["efficiency_wh_km"] == 150.0
    assert state["cost_a"]["efficiency_source"].startswith("derived")
    assert state["cost_a"]["annual_energy_cost"] == 750.0  # 150 Wh/km * 20000 km * 0.25
    assert state["fuel_cost"]["annual_fuel_cost"] == 2520.0  # 7 L/100km * 20000 * 1.80
    assert state["comparison"] == {"annual_savings_vs_fuel": 1770.0, "ev_cheaper": True}
    # LLM calls asked for tasks (cheap parse, mid synthesis), never for concrete models
    assert [c["task"] for c in llm.calls] == ["intent_parsing", "synthesis"]
    assert len(no_db) == 1  # audited


async def test_failure_skips_to_audit_without_synthesis(no_db):
    llm = FakeLLM({"intent_parsing": intent()})  # no 'synthesis' scripted: must not be called
    state = await run(llm, FakeSources(fail="specs"))
    assert "Could not retrieve specs a" in state["error"]
    assert state.get("analysis") is None
    assert len(no_db) == 1 and no_db[0]["final_output"].startswith("Could not retrieve")


async def test_missing_price_still_answers_specs(no_db):
    llm = FakeLLM({"intent_parsing": intent(), "synthesis": "specs answer, costs unavailable"})
    state = await run(llm, FakeSources(fail="price"))
    assert state["analysis"] == "specs answer, costs unavailable" and not state.get("error")
    assert state["cost_a"] is None and state["comparison"] is None
    assert {u.split(" (")[0] for u in state["unavailable"]} == {"electricity price", "fuel price"}
    assert "unavailable" in llm.calls[-1]["messages"][-1]["content"]  # the answer is told what's missing


def test_country_codes_become_names_for_search():
    from ev_research.sources import country_name
    assert (country_name("DE"), country_name("fr"), country_name("ZZ")) == ("Germany", "France", "ZZ")


async def test_off_topic_rejected_early(no_db):
    bad = intent() | {"vehicle_a_make": "UNKNOWN"}
    state = await run(FakeLLM({"intent_parsing": bad}), FakeSources(), query="bake me a cake")
    assert "electric-vehicle" in state["error"]


def test_calc_is_exact():
    assert calc.ev_energy_cost(15000, 0.3, 140.3) == {"annual_kwh": 2104.5, "annual_energy_cost": 631.35}
    assert calc.derived_efficiency(None, 400) is None
