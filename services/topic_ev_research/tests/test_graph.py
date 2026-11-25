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
    def __init__(self, fail: str | None = None, currency: str = "EUR") -> None:
        self.fail = fail
        self.currency = currency
        self.price_calls: list[tuple[str, str]] = []

    async def ev_specs(self, make, model, year, trim, ctx):
        if self.fail == "specs":
            raise UpstreamError("no verifiable data")
        return dict(SPECS)

    async def image_candidates(self, make, model, year):
        return ["https://img.example/search-hit.jpg"]

    async def country_price(self, country, metric, ctx):
        self.price_calls.append((country, metric))
        if self.fail == "price":
            raise UpstreamError("no verifiable data")
        value = 0.25 if metric.startswith("electricity") else 1.80
        return {"value": value, "currency": self.currency, "source_url": f"https://example.com/{metric}"}


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    logged = []

    async def fake_log(**kw):
        logged.append(kw)

    monkeypatch.setattr(repository, "log_run", fake_log)
    return logged


def intent(mode="ev_vs_fuel"):
    return {"comparison_mode": mode, "country_code": "fr", "annual_mileage_km": 20000, "vehicle_a_make": "Tesla",
            "vehicle_a_model": "Model 3", "vehicle_a_year": 2024, "vehicle_a_year_stated": True, "vehicle_a_trim": "",
            "fuel_type": "diesel"}


class FakeFX:
    async def convert(self, amount, from_ccy, to_ccy):
        return round(amount * 1.17, 2) if (from_ccy, to_ccy) == ("EUR", "GBP") else None


class FakeImages:
    def __init__(self, usable=True):
        self.usable, self.saved = usable, []

    async def save_from_url(self, url, alt=""):
        self.saved.append((url, alt))
        return f"/api/v1/images/{len(self.saved):064d}" if self.usable else None


async def run(llm, sources, query="Model 3 vs diesel in France, 20000 km", fx=None, images=None):
    ctx = fake_context("ev_research", llm, ROOT / "prompts")
    g = graph.build_graph()
    return await g.ainvoke({"query": query},
                           config={"configurable": {"ctx": ctx, "sources": sources, "fx": fx, "images": images}})


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


async def test_no_country_means_no_guessed_prices_and_a_question(no_db):
    vague = intent("single") | {"country_code": None, "annual_mileage_km": None, "vehicle_a_year_stated": False}
    llm = FakeLLM({"intent_parsing": vague, "synthesis": "specs + which country?"})
    sources = FakeSources()
    state = await run(llm, sources, query="help on Tesla Model 3?")
    assert sources.price_calls == []                       # never a default country's prices
    assert state["needs"] == ["country"] and state["cost_a"] is None
    assert state["assumptions"] == ["model year 2024 (latest — year not given)", "15,000 km per year (default)"]
    told = llm.calls[-1]["messages"][-1]["content"]
    assert '"needs"' in told and '"country"' in told and "latest" in told   # the writer must ask + list assumptions


async def test_stated_country_and_year_no_assumptions(no_db):
    llm = FakeLLM({"intent_parsing": intent(), "synthesis": "ok"})
    state = await run(llm, FakeSources())
    assert state["assumptions"] == [] and state["needs"] == []


async def test_foreign_car_price_converted_original_kept(no_db):
    llm = FakeLLM({"intent_parsing": intent("single") | {"country_code": "GB"}, "synthesis": "ok"})
    state = await run(llm, FakeSources(currency="GBP"), fx=FakeFX())
    specs = state["specs_a"]
    assert specs["base_price"] == 42990.0 and specs["price_currency"] == "EUR"   # as found
    assert specs["base_price_local"]["amount"] == 50298.3 and specs["base_price_local"]["currency"] == "GBP"
    # same currency → nothing added; no FX configured → nothing added (never a mixed total)
    same = await run(FakeLLM({"intent_parsing": intent("single"), "synthesis": "ok"}), FakeSources(), fx=FakeFX())
    assert "base_price_local" not in same["specs_a"]
    none = await run(FakeLLM({"intent_parsing": intent("single") | {"country_code": "GB"}, "synthesis": "ok"}),
                     FakeSources(currency="GBP"))
    assert "base_price_local" not in none["specs_a"]


def test_country_codes_are_validated():
    assert (graph.country_of("fr"), graph.country_of(" de "), graph.country_of(None), graph.country_of("USA")) == ("FR", "DE", None, None)


async def test_parse_gets_summary_and_clean_memory_messages(no_db):
    from agency_platform.testing import fake_context
    llm = FakeLLM({"intent_parsing": intent(), "synthesis": "ok"})
    ctx = fake_context("ev_research", llm, ROOT / "prompts",
                       history=[{"role": "user", "content": "I live in France", "topic": "finance", "at": "2026-11-25T10:00:00+00:00"}])
    ctx.summary = "- user drives 20,000 km a year"
    await graph.build_graph().ainvoke({"query": "and the Model 3?"},
                                      config={"configurable": {"ctx": ctx, "sources": FakeSources(), "fx": None}})
    parse = llm.calls[0]["messages"]
    assert "drives 20,000 km" in parse[0]["content"]                        # summary in the instructions
    assert parse[1] == {"role": "user", "content": "I live in France"}      # only role + content to the model



async def test_picture_from_the_cached_specs_first(no_db, monkeypatch):
    remembered = []

    async def set_image_url(*a):
        remembered.append(a)
    monkeypatch.setattr(repository, "set_image_url", set_image_url)
    monkeypatch.setitem(SPECS, "image_url", "https://cdn.example/model-3.jpg")
    images = FakeImages()
    llm = FakeLLM({"intent_parsing": intent("single") | {"wants_images": True}, "synthesis": "specs"})
    state = await run(llm, FakeSources(), images=images, query="show me the Model 3")
    assert images.saved == [("https://cdn.example/model-3.jpg", "2024 Tesla Model 3")]   # searched already → no search
    assert state["analysis"].startswith("specs") and "![2024 Tesla Model 3](/api/v1/images/" in state["analysis"]
    assert "*Picture: cdn.example*" in state["analysis"] and remembered == []


async def test_picture_search_fallback_is_remembered(no_db, monkeypatch):
    remembered = []

    async def set_image_url(*a):
        remembered.append(a)
    monkeypatch.setattr(repository, "set_image_url", set_image_url)
    images = FakeImages()
    llm = FakeLLM({"intent_parsing": intent("single") | {"wants_images": True}, "synthesis": "specs"})
    state = await run(llm, FakeSources(), images=images)
    assert images.saved[0][0] == "https://img.example/search-hit.jpg"
    assert remembered == [("Tesla", "Model 3", 2024, "", "https://img.example/search-hit.jpg")]
    assert len(state["pictures"]) == 1


async def test_no_picture_unless_asked_and_honest_when_none(no_db):
    images = FakeImages()
    state = await run(FakeLLM({"intent_parsing": intent("single"), "synthesis": "specs"}), FakeSources(), images=images)
    assert images.saved == [] and state["analysis"] == "specs"
    none = await run(FakeLLM({"intent_parsing": intent("single") | {"wants_images": True}, "synthesis": "specs"}),
                     FakeSources(), images=FakeImages(usable=False))
    assert "couldn't find a usable picture" in none["analysis"]
