"""TopicAgent: CrewAI planning/matching + deterministic verified-price pipeline."""
from collections.abc import AsyncIterator

from fastapi import FastAPI

from agency_platform.agents.base import AgentEvent, RunContext, TopicAgent
from agency_platform.agents.sessions import with_history
from agency_platform.cache import get_redis
from agency_platform.tools.search import WebSearch
from product_finder import offers as pipeline
from product_finder import repository
from product_finder.crews import CrewAICrews, Crews
from product_finder.fx import FXRates
from product_finder.models import Offer, SearchPlan
from product_finder.params import params, settings
from product_finder.ranking import rank, render_answer


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
        # the chat's last messages go along: "and in France?" refers to the product asked before
        plan = await self._crews.plan(with_history(prompt, ctx.history, summary=ctx.summary),
                                      default_country=params().search.default_country, ctx=ctx)
        if plan.intent == "about_previous" and ctx.history:
            # "which storage was that?" is about the answer already given: no new search, no changed product
            yield AgentEvent.status("Answering from the results already shown", step="follow_up")
            yield AgentEvent.answer(await self._follow_up(prompt, ctx))
            return
        yield AgentEvent.status(f"Looking for: {plan.product} ({plan.country_code}, {plan.currency})",
                                step="plan", must_match=plan.must_match)

        urls = await pipeline.candidate_urls(plan, self._search)
        yield AgentEvent.status(f"Checking {len(urls)} retailer pages", step="search")
        found = await pipeline.gather_offers(urls, ctx)
        yield AgentEvent.status(f"Verified prices on {len(found)} pages", step="extract")

        if found:
            await pipeline.normalise(found, plan.currency, self._fx)
            report = await self._crews.match(plan, found, ctx)
            verdicts = {v.id: v for v in report.verdicts}
            for o in found:
                if v := verdicts.get(o.id):
                    o.verdict, o.verdict_note = v.verdict, v.note
            caveats = report.caveats
            yield AgentEvent.status("Matched offers against the exact product", step="match")
        else:
            caveats = []

        ranked = rank(found, plan)
        await self._persist(ctx, plan, found)
        yield AgentEvent.artifact("offers", {
            "plan": plan.model_dump(),
            "ranked": [o.model_dump() for o in ranked],
            "all_verified": [o.model_dump() for o in found],
        })
        yield AgentEvent.answer(render_answer(plan, ranked, len(urls), caveats, found))

    async def _follow_up(self, prompt: str, ctx: RunContext) -> str:
        p = ctx.prompt("follow_up")
        history = [{"role": m["role"], "content": m["content"]} for m in ctx.history[-6:]
                   if m["role"] in ("user", "assistant")]
        result = await ctx.llm.chat([{"role": "system", "content": p.system}, *history,
                                     {"role": "user", "content": prompt}], task="direct", prompt=p)
        return (result.content or "").strip() or "I don't have that detail in the results above."

    async def _persist(self, ctx: RunContext, plan: SearchPlan, found: list[Offer]) -> None:
        if not found:
            return
        await repository.record_offers(
            request_id=ctx.request_id, product=plan.product, country_code=plan.country_code, offers=found
        )
