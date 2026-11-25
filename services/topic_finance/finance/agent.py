"""TopicAgent: AutoGen team events -> platform AgentEvents."""
from collections.abc import AsyncIterator

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
        task = with_history(prompt, ctx.history)

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
            yield AgentEvent.error("Compliance review did not approve the analysis; withholding it.",
                                   code="COMPLIANCE_REJECTED")
            return
        yield AgentEvent.answer(f"{analysis}\n\n---\n_{cfg.disclaimer}_")
