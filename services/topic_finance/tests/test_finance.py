import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("PROMPTS_DIR", str(ROOT / "prompts"))

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from agency_platform.testing import FakeLLM, fake_context
from finance import analytics
from finance.agent import FinanceAgent
from finance.team import GATEWAY_MODEL_INFO, gateway_client
from finance.tools import FinanceTools


class FakeMarket:
    async def quote(self, symbol):
        return {"symbol": symbol, "price": 100.0, "currency": "USD"}

    async def fundamentals(self, symbol):
        return {"symbol": symbol, "trailing_pe": 20.0}

    async def closes(self, symbol, period):
        growth = 1.0004 if symbol != "BND" else 1.0001
        return [100 * growth**i * (1 + 0.01 * ((i % 5) - 2)) for i in range(253)]


def tool_call(name: str, args: str) -> CreateResult:
    return CreateResult(content=[FunctionCall(id="c1", name=name, arguments=args)], finish_reason="function_calls",
                        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)


def replay_factory(scripts: dict[str, list]):
    used: list[str] = []

    def factory(ctx, task):
        used.append(task)
        return ReplayChatCompletionClient(scripts[task], model_info=GATEWAY_MODEL_INFO)

    factory.used = used
    return factory


async def run(factory):
    agent = FinanceAgent(market=FakeMarket(), client_factory=factory)
    ctx = fake_context("finance", FakeLLM({}), ROOT / "prompts")
    return [e async for e in agent.run("How risky is VTI?", ctx)]


async def test_team_uses_tools_mixes_tiers_and_gates_on_compliance():
    factory = replay_factory({
        "data_analysis": [tool_call("get_risk_return", '{"symbol": "VTI", "period": "1y"}'), "VTI: volatility reported."],
        "investment_analysis": ["## VTI risk\nBased on the reported volatility..."],
        "compliance_review": ["APPROVED"],
    })
    events = await run(factory)
    assert factory.used == ["data_analysis", "investment_analysis", "compliance_review"]
    assert any(e.type == "tool_call" and e.content == "get_risk_return" for e in events)
    data = next(e for e in events if e.type == "artifact").data["payload"]
    assert data["compliance_approved"] is True
    assert data["tool_results"][0]["result"]["symbol"] == "VTI"
    answer = next(e for e in events if e.type == "token").content
    assert answer.startswith("## VTI risk") and "not personalised" in answer  # disclaimer appended deterministically


async def test_unapproved_analysis_is_withheld():
    factory = replay_factory({
        "data_analysis": ["no data", "no data", "no data", "no data"],
        "investment_analysis": ["Buy now, guaranteed 50%!"] * 4,
        "compliance_review": ["REVISE: promises returns"] * 4,
    })
    events = await run(factory)
    assert not any(e.type == "token" for e in events)
    assert any(e.type == "error" and e.data["code"] == "COMPLIANCE_REJECTED" for e in events)


async def test_tools_validate_inputs():
    tools = FinanceTools(FakeMarket())
    assert "error" in await tools.get_quote("rm -rf /")
    assert "error" in await tools.get_portfolio_metrics({"VTI": -1, "BND": 1})
    pm = await tools.get_portfolio_metrics({"VTI": 60, "BND": 40})
    assert pm["weights"] == {"VTI": 0.6, "BND": 0.4} and "correlation" in pm


def test_analytics_math():
    prices = [100, 110, 99, 120]
    assert analytics.max_drawdown(prices) == pytest.approx(-0.1)
    stats = analytics.series_stats([100 * 1.001**i for i in range(253)], days_per_year=252, risk_free=0.0)
    assert stats["annualised_volatility_pct"] == pytest.approx(0, abs=0.01)
    assert stats["annualised_return_pct"] == pytest.approx(28.6, abs=0.2)


def test_autogen_client_points_at_model_gateway():
    client = gateway_client(fake_context("finance", FakeLLM({}), ROOT / "prompts"), "investment_analysis")
    assert client._raw_config["base_url"] == "http://fake-gateway/v1"
    assert client._raw_config["model"] == "task-investment_analysis"
