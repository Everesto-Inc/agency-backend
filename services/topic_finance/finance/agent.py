"""TopicAgent: AutoGen team events -> platform AgentEvents."""
from collections.abc import AsyncIterator
from typing import Any

from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import TextMessage, ToolCallExecutionEvent, ToolCallRequestEvent
from fastapi import FastAPI

from agency_platform.agents.base import AgentEvent, RunContext, TopicAgent
from agency_platform.agents.sessions import with_history
from agency_platform.cache import get_redis
from finance.market import MarketData, YFinanceMarketData
from finance.params import params
from finance.team import COMPLIANCE, STRATEGIST, ClientFactory, build_team, gateway_client
from finance.tools import FinanceTools


def _pct(v: Any) -> str:
    return f"{v:+.1f} %" if isinstance(v, int | float) else "–"


def data_only_answer(collected: list[dict[str, Any]]) -> str:
    """Pure: the tool results as a table — every number comes from a tool, nothing from a model."""
    quotes = {c["result"]["symbol"]: c["result"] for c in collected if c["tool"] == "get_quote"}
    funds = {c["result"]["symbol"]: c["result"] for c in collected if c["tool"] == "get_fundamentals"}
    risk = {c["result"]["symbol"]: c["result"] for c in collected if c["tool"] == "get_risk_return"}
    symbols = list(dict.fromkeys([*quotes, *funds, *risk]))
    if not symbols:
        return ("I can compare specific stocks or funds with real market data, but I can't tell you what to buy. "
                "Name one or more (e.g. *Amundi MSCI World vs S&P 500 ETF*) and I'll compare return, risk and costs.")
    lines = ["I can't recommend what to buy, but here is the verified market data to compare:", "",
             "| Symbol | Name | Price | Return | Volatility | Max drawdown | Fees |", "|---|---|---|---|---|---|---|"]
    for s in symbols:
        q, f, r = quotes.get(s, {}), funds.get(s, {}), risk.get(s, {})
        price = f"{q['price']:,.2f} {q.get('currency') or ''}".strip() if q.get("price") is not None else "–"
        fee = f"{f['expense_ratio']:.2f} %" if isinstance(f.get("expense_ratio"), int | float) else "–"
        period = f" ({r['period']})" if r.get("period") else ""
        lines.append(f"| {s} | {f.get('name') or '–'} | {price} | {_pct(r.get('total_return_pct'))}{period} | "
                     f"{_pct(r.get('annualised_volatility_pct'))} | {_pct(r.get('max_drawdown_pct'))} | {fee} |")
    return "\n".join(lines)


class FinanceAgent(TopicAgent):
    topic = "finance"
    framework = "autogen"

    def __init__(self, market: MarketData | None = None, client_factory: ClientFactory = gateway_client) -> None:
        self._market = market
        self._client_factory = client_factory

    async def startup(self, app: FastAPI) -> None:
        if self._market is None:
            self._market = YFinanceMarketData(get_redis())

    async def run(self, prompt: str, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        assert self._market is not None
        tools = FinanceTools(self._market)
        team = build_team(ctx, tools, self._client_factory)
        task = with_history(prompt, ctx.history, summary=ctx.summary)

        analysis: str | None = None
        approved = False
        async for msg in team.run_stream(task=task):
            if isinstance(msg, TaskResult):
                break
            if isinstance(msg, ToolCallRequestEvent):
                for call in msg.content:
                    yield AgentEvent(type="tool_call", content=call.name, data={"arguments": call.arguments})
            elif isinstance(msg, ToolCallExecutionEvent):
                for res in msg.content:
                    yield AgentEvent(type="tool_result", content=res.name,
                                     data={"is_error": bool(getattr(res, "is_error", False))})
            elif isinstance(msg, TextMessage):
                if msg.source == STRATEGIST:
                    analysis = msg.content
                    yield AgentEvent.status("Strategist drafted the analysis", agent=STRATEGIST)
                elif msg.source == COMPLIANCE:
                    approved = msg.content.strip().upper().startswith("APPROVED")
                    yield AgentEvent.status("Compliance approved" if approved else "Compliance requested a revision",
                                            agent=COMPLIANCE, verdict=msg.content[:200])
                elif msg.source != "user":
                    yield AgentEvent.status(f"{msg.source} reported data", agent=msg.source)

        yield AgentEvent.artifact("market_data", {"tool_results": tools.collected, "compliance_approved": approved})
        cfg = params().team
        if analysis is None:
            yield AgentEvent.error("The analysis team could not produce an answer.", code="NO_ANALYSIS")
            return
        if cfg.require_compliance_approval and not approved:
            # Never a dead end: the unapproved text is withheld, the verified data is still shown (numbers from tools).
            yield AgentEvent.status("Compliance did not approve the analysis; showing the verified data only",
                                    agent=COMPLIANCE)
            yield AgentEvent.answer(f"{data_only_answer(tools.collected)}\n\n---\n_{cfg.disclaimer}_")
            return
        yield AgentEvent.answer(f"{analysis}\n\n---\n_{cfg.disclaimer}_")
