"""EV tools: deterministic calculators, catalogue search, and the model ↔ tools loop (facts only from tools)."""
import json
from types import SimpleNamespace

import pytest

from ev_research import repository as repo
from ev_research import tools

CATALOG = [
    {"make": "Volkswagen", "model": "ID.3", "variant": "Pro S", "release_year": 2024, "usable_battery_kwh": 77.0,
     "consumption_kwh_100km": 16.0, "est_range_km": 481, "dc_max_kw": 175.0, "ac_max_kw": 11.0,
     "charging_curve": [{"percentage": 0, "power": 150}, {"percentage": 50, "power": 150}, {"percentage": 80, "power": 75}]},
    {"make": "Kia", "model": "EV6", "variant": "Long Range", "release_year": 2024, "usable_battery_kwh": 80.0,
     "consumption_kwh_100km": 17.8, "est_range_km": 449, "dc_max_kw": 263.0, "ac_max_kw": 11.0, "charging_curve": []},
    {"make": "Dacia", "model": "Spring", "variant": "", "release_year": 2024, "usable_battery_kwh": 25.0,
     "consumption_kwh_100km": 15.2, "est_range_km": 164, "dc_max_kw": 34.0, "ac_max_kw": 7.0, "charging_curve": []},
]


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    async def rows():
        return CATALOG
    monkeypatch.setattr(repo, "catalog_rows", rows)


def test_running_costs_are_exact():
    out = tools.running_costs(16.0, 15000, 0.3618, ice_l_100km=7.0, fuel_eur_l=1.964)
    assert out["ev"] == {"kwh_per_year": 2400, "cost_per_year": 868.32, "cost_per_month": 72.36, "cost_per_100km": 5.79}
    assert out["fuel_car"]["cost_per_year"] == 2062.2 and out["ev_saves_per_year"] == 1193.88


def test_charging_minutes_follow_the_curve_and_the_charger():
    curve = CATALOG[0]["charging_curve"]
    fast = tools.charging_minutes(77.0, curve, 10, 80)
    slow = tools.charging_minutes(77.0, curve, 10, 80, charger_kw=50)
    assert 20 < fast < 35 and slow > fast                     # 150 kW to 50 %, tapering to 75 kW at 80 %
    assert tools.charging_minutes(25.0, [], 10, 80, dc_max_kw=34) == round(25 * 0.70 / 34 * 60)
    assert tools.charging_minutes(77.0, curve, 80, 10) is None


async def test_search_catalog_filters_and_ranks():
    out = await tools.search_catalog(min_dc_kw=100, sort_by="dc_power")
    assert [r["vehicle"] for r in out["results"]] == ["Kia EV6 Long Range", "Volkswagen ID.3 Pro S"]
    assert (await tools.search_catalog(make="VW"))["matches"] == 1
    assert "Open EV Data" in out["source"] and "estimate" in out["note"]


async def test_charging_time_tool_uses_the_catalogue():
    out = await tools.charging_time("VW", "ID.3", 10, 80)
    assert out["found"] and out["method"] == "along the car's charging curve" and out["minutes"] > 0
    assert (await tools.charging_time("Tesla", "Model 3"))["found"] is False


class ScriptedModel:
    """Asks for tools first, then answers using the tool results it was given."""
    def __init__(self):
        self.round = 0
        self.seen = []

    async def chat(self, messages, **kw):
        self.seen.append((messages, kw))
        self.round += 1
        if self.round == 1:
            calls = [{"id": "c1", "type": "function", "function": {"name": "search_catalog",
                      "arguments": json.dumps({"min_dc_kw": 200, "sort_by": "dc_power"})}},
                     {"id": "c2", "type": "function", "function": {"name": "nope", "arguments": "{}"}}]
            return SimpleNamespace(content="", raw={"choices": [{"message": {"content": None, "tool_calls": calls}}]})
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        found = json.loads(tool_msgs[0]["content"])["results"][0]["vehicle"]
        return SimpleNamespace(content=f"Fastest: {found}", raw={"choices": [{"message": {"content": f"Fastest: {found}"}}]})


async def test_the_loop_runs_tools_and_answers_from_their_results():
    model = ScriptedModel()
    ctx = SimpleNamespace(llm=model)
    out = await tools.run_tools("which EV charges fastest?", ctx, sources=None, system="s", history=[], prompt=None)
    assert out["answer"] == "Fastest: Kia EV6 Long Range"
    assert out["trace"] == [{"tool": "search_catalog", "args": {"min_dc_kw": 200, "sort_by": "dc_power"}, "ok": True},
                            {"tool": "nope", "args": {}, "ok": False}]
    assert model.seen[0][1]["task"] == "tool_use" and model.seen[0][1]["tools"] == tools.TOOL_SPECS


async def test_the_loop_forces_an_answer_after_max_steps():
    class Looping:
        n = 0

        async def chat(self, messages, **kw):
            self.n += 1
            if kw["tool_choice"] == "none":
                return SimpleNamespace(content="done", raw={"choices": [{"message": {"content": "done"}}]})
            call = {"id": f"c{self.n}", "function": {"name": "search_catalog", "arguments": "{}"}}
            return SimpleNamespace(content="", raw={"choices": [{"message": {"tool_calls": [call]}}]})
    model = Looping()
    out = await tools.run_tools("q", SimpleNamespace(llm=model), None, "s", [], None)
    assert out["answer"] == "done" and model.n == tools.MAX_STEPS + 1
