"""The CrewAI part: one research agent with tools, bound to the model gateway's task-tool_use (mid tier).

The agent decides each step — search reviews/guides to discover candidates, search shops, read pages, search again —
and writes the answer. Our code does the rest: fetching (SSRF-safe), price extraction + verification on the page,
currency conversion, budgets. CrewAI runs the crew in a worker thread (kickoff_async) and calls tools synchronously;
each tool hands its coroutine back to the service's event loop (shared HTTP / Redis clients) and waits for it.
"""
import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import md5
from typing import Any, Protocol

import portalocker
from crewai import LLM, Agent, Crew, Process, Task
from crewai.tools import BaseTool
from crewai_core.lock_store import set_lock_backend
from pydantic import BaseModel, ConfigDict, Field

from agency_platform.agents.base import RunContext
from product_finder.params import params
from product_finder.research import Research


@contextmanager
def _file_lock(name: str, *, timeout: float = 120) -> Iterator[None]:
    """CrewAI's internal locks (its local SQLite task store) on a file in /tmp — never on Redis.

    CrewAI otherwise uses REDIS_URL when it is set, and needs CLIENT SETNAME there: our Redis user is restricted
    by ACL (own key prefix, no admin commands), so every crew crashed before its first model call."""
    path = os.path.join(tempfile.gettempdir(), f"crewai-{md5(name.encode(), usedforsecurity=False).hexdigest()}.lock")
    with portalocker.Lock(path, timeout=timeout):
        yield


set_lock_backend(_file_lock)


class Crews(Protocol):
    async def research(self, question: str, research: Research, ctx: RunContext) -> str: ...


def _llm(ctx: RunContext, task: str) -> LLM:
    cfg = ctx.llm.openai_compat(task=task)
    return LLM(model=f"openai/{cfg.model}", base_url=cfg.base_url, api_key=cfg.api_key, temperature=0.2)


# ------------------------------------------------------------------ tools (sync for CrewAI → our async code)
class _SearchArgs(BaseModel):
    query: str = Field(..., description="What to search, e.g. 'best leather folio iPad Pro 13 M4 review' or a model name")
    shops_only: bool = Field(False, description="true = only the country's main online shops (for prices)")
    country: str = Field("", description="ISO country code if the shopper named one (FR, DE, US...), else empty")


class _ReadArgs(BaseModel):
    url: str = Field(..., description="A page address from the search results")


class _FxArgs(BaseModel):
    amount: float
    from_currency: str = Field(..., description="ISO 4217, e.g. USD")
    to_currency: str = Field(..., description="ISO 4217, e.g. EUR")


class _BridgeTool(BaseTool):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    fn: Callable[..., Awaitable[dict[str, Any]]] = Field(exclude=True)
    loop: Any = Field(exclude=True)

    def _run(self, **kwargs: Any) -> str:
        future = asyncio.run_coroutine_threadsafe(self.fn(**kwargs), self.loop)
        try:
            result = future.result(timeout=params().research.tool_timeout_s)
        except TimeoutError:
            future.cancel()
            result = {"error": "the tool took too long"}
        return json.dumps(result, ensure_ascii=False, default=str)


def research_tools(research: Research, loop: asyncio.AbstractEventLoop) -> list[BaseTool]:
    return [
        _BridgeTool(name="search_web", args_schema=_SearchArgs, fn=research.search_web, loop=loop,
                    description="Search the web: reviews, buying guides and comparisons to discover products, or "
                                "shops (shops_only=true) to find offers. Returns titles, addresses and snippets."),
        _BridgeTool(name="read_page", args_schema=_ReadArgs, fn=research.read_page, loop=loop,
                    description="Read one page: its text, and the shop's offer with a price verified on the page "
                                "(offer.price_verified_on_page). Review pages have text but no offer."),
        _BridgeTool(name="convert_currency", args_schema=_FxArgs, fn=research.convert_currency, loop=loop,
                    description="Convert an amount between currencies (ECB rate, for comparison only)."),
    ]


class CrewAICrews:
    async def research(self, question: str, research: Research, ctx: RunContext) -> str:
        cfg = params().research
        p_role, p_task = ctx.prompt("research_role"), ctx.prompt("research_task")
        agent = Agent(
            role="Product Research Specialist",
            goal="Find the products that fit the shopper's request and their best current prices, with proof",
            backstory=p_role.system,
            llm=_llm(ctx, "tool_use"),
            tools=research_tools(research, asyncio.get_running_loop()),
            allow_delegation=False,
            max_iter=cfg.max_iterations,
            verbose=False,
        )
        task = Task(
            description=p_task.system.format(
                request=question.replace("{", "(").replace("}", ")"),
                default_country=research.default_country,
                today=datetime.now(UTC).date().isoformat(),
                max_pages=cfg.max_pages,
                max_searches=cfg.max_searches,
            ) + ctx.skill_instructions().replace("{", "(").replace("}", ")"),
            expected_output="The answer for the shopper in Markdown",
            agent=agent,
        )
        out = await Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff_async()
        return str(getattr(out, "raw", out) or "").strip()
