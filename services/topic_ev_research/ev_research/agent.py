"""TopicAgent adapter: LangGraph events -> platform AgentEvents."""
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI

from agency_platform.agents.base import AgentEvent, RunContext, TopicAgent
from agency_platform.cache import get_redis
from agency_platform.db import get_db
from agency_platform.tools.fx import FXRates
from agency_platform.tools.images import ImageStore
from ev_research.graph import build_graph
from ev_research.params import params
from ev_research.sources import CrawlerPool, FactSources, build_search

_STATUS = {
    "parse_intent": "Understood the question (vehicles, country, comparison)",
    "retrieve": "Retrieved specs (and local energy prices when the country is known) — cache or verified crawl",
    "calculate": "Computed running costs deterministically",
    "synthesize": "Wrote the grounded answer",
    "pictures": "Added a picture",
}


class EVResearchAgent(TopicAgent):
    topic = "ev_research"
    framework = "langgraph"

    def __init__(self) -> None:
        self._crawler = CrawlerPool()
        self._graph = None
        self._sources: FactSources | None = None
        self._fx: FXRates | None = None
        self._images: ImageStore | None = None

    async def startup(self, app: FastAPI) -> None:
        await self._crawler.start()
        self._sources = FactSources(self._crawler, build_search(app.state.http))
        self._fx = FXRates(app.state.http, get_redis(), lambda: (params().fx.url, params().fx.cache_ttl_s))
        self._images = ImageStore(get_db(), app.state.http)
        self._graph = build_graph()

    async def shutdown(self) -> None:
        await self._crawler.stop()

    async def run(self, prompt: str, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        assert self._graph is not None and self._sources is not None
        config = {
            "configurable": {"ctx": ctx, "sources": self._sources, "fx": self._fx, "images": self._images},
            "run_name": "ev_research",
            "metadata": {"request_id": ctx.request_id, "user_plan": ctx.user.plan},  # LangSmith
            "tags": ["topic:ev_research"],
        }
        final: dict[str, Any] = {}
        async for update in self._graph.astream({"query": prompt}, config=config, stream_mode="updates"):
            for node, out in update.items():
                out = out or {}
                final.update(out)
                if out.get("error"):
                    yield AgentEvent.error(out["error"], code="EV_PIPELINE_ERROR", node=node)
                elif node in _STATUS and (node != "pictures" or out):  # no picture asked → no status line
                    yield AgentEvent.status(_STATUS[node], node=node)

        if final.get("error"):
            return
        yield AgentEvent.artifact("ev_data", {
            k: final.get(k) for k in ("vehicle_a", "vehicle_b", "specs_a", "specs_b", "electricity_price",
                                       "fuel_price", "cost_a", "cost_b", "fuel_cost", "comparison")
        })
        if final.get("analysis"):
            yield AgentEvent.answer(final["analysis"])
