"""TopicAgent: Product Finder — a CrewAI research agent with verified prices.

    question (+ the chat) → research agent: search reviews/guides/shops, read pages, search again … → answer
                            every search and page read is streamed as a status line ("Reading ldlc.com")
    → price check: every price in the answer must have come from a tool, else "(not verified)" is added
    → artifact: verified offers, pages read (with why a page gave no price), searches
"""
import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from agency_platform.agents.base import AgentEvent, RunContext, TopicAgent
from agency_platform.agents.sessions import with_history
from agency_platform.cache import get_redis
from agency_platform.security.guardrails import guardrail
from agency_platform.tools.search import WebSearch
from product_finder import repository
from product_finder.crews import CrewAICrews, Crews
from product_finder.fx import FXRates
from product_finder.params import params, settings
from product_finder.research import Research, mark_unverified


class ProductFinderAgent(TopicAgent):
    topic = "product_finder"
    framework = "crewai"

    def __init__(self, crews: Crews | None = None) -> None:
        self._crews = crews or CrewAICrews()
        self._search: WebSearch | None = None
        self._fx: FXRates | None = None

    async def startup(self, app: FastAPI) -> None:
        key = settings().tavily_api_key
        self._search = WebSearch(key.get_secret_value() if key else None, app.state.http)
        self._fx = FXRates(app.state.http, get_redis())

    def bind(self, search: WebSearch, fx: FXRates) -> None:  # tests
        self._search, self._fx = search, fx

    async def run(self, prompt: str, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        assert self._search is not None and self._fx is not None
        research = Research(ctx, self._search, self._fx, params().search.default_country)
        question = with_history(prompt, ctx.history, max_chars=params().research.history_chars, summary=ctx.summary)
        yield AgentEvent.status("Researching", step="research")
        job = asyncio.create_task(self._crews.research(question, research, ctx))
        try:
            while not job.done() or not research.events.empty():
                get = asyncio.ensure_future(research.events.get())
                done, _ = await asyncio.wait({get, job}, return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    yield get.result()
                else:
                    get.cancel()
            answer = job.result()
        finally:
            if not job.done():
                job.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await job

        answer, flagged = mark_unverified(answer, research.evidence_text())
        if flagged:
            await guardrail("grounding", "flagged", topic="product_finder",
                            detail=f"{len(flagged)} price(s) in the answer not seen in any tool result")
        yield AgentEvent.artifact("research", {
            "offers": [o.model_dump() for o in research.offers],
            "trace": research.trace,
            "pages_read": research.pages,
            "searches": research.searches,
            "unverified_prices": flagged,
        })
        await self._persist(ctx, prompt, research)
        if not answer:
            yield AgentEvent.error("The research ended without an answer. Please try again.", code="NO_ANSWER")
            return
        yield AgentEvent.answer(answer)

    async def _persist(self, ctx: RunContext, question: str, research: Research) -> None:
        if not research.offers:
            return
        await repository.record_offers(request_id=ctx.request_id, product=question[:200],
                                       country_code=research.default_country, offers=research.offers)
