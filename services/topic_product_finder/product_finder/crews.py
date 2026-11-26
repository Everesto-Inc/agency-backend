"""The CrewAI part: two small crews, each bound to a model-gateway *task*.

  planner crew  (task-query_planning   -> low tier)  request -> SearchPlan
  analyst crew  (task-product_matching -> mid tier)  offers  -> MatchReport

The crews reason; deterministic code does search, fetching, price extraction
verification, FX and ranking. CrewAI's LLM is pointed at the model gateway
with the run's call token, so its calls are tiered, plan-capped and tracked
exactly like every other framework's.
"""
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import md5
from typing import Protocol

import portalocker
from crewai import LLM, Agent, Crew, Process, Task
from crewai_core.lock_store import set_lock_backend

from agency_platform.agents.base import RunContext
from agency_platform.security.content import UNTRUSTED_DATA_RULES, fence_untrusted
from product_finder.models import MatchReport, Offer, SearchPlan
from product_finder.params import params


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
    async def plan(self, request: str, default_country: str, ctx: RunContext) -> SearchPlan: ...
    async def match(self, plan: SearchPlan, offers: list[Offer], ctx: RunContext) -> MatchReport: ...


def _llm(ctx: RunContext, task: str) -> LLM:
    cfg = ctx.llm.openai_compat(task=task)
    return LLM(model=f"openai/{cfg.model}", base_url=cfg.base_url, api_key=cfg.api_key, temperature=0)


class CrewAICrews:
    async def plan(self, request: str, default_country: str, ctx: RunContext) -> SearchPlan:
        role = ctx.prompt("planner_role").system
        strategist = Agent(
            role="Shopping Research Strategist",
            goal="Define the exact product and the best search queries to find its cheapest offers",
            backstory=role,
            llm=_llm(ctx, "query_planning"),
            allow_delegation=False,
            max_iter=2,
            verbose=False,
        )
        task = Task(
            description=ctx.prompt("planner_task").system.format(
                request=request.replace("{", "(").replace("}", ")"),
                default_country=default_country,
                max_queries=params().search.max_queries,
            ),
            expected_output="A SearchPlan object",
            output_pydantic=SearchPlan,
            agent=strategist,
        )
        out = await Crew(agents=[strategist], tasks=[task], process=Process.sequential, verbose=False).kickoff_async()
        return _as(out, SearchPlan)

    async def match(self, plan: SearchPlan, offers: list[Offer], ctx: RunContext) -> MatchReport:
        role = ctx.prompt("analyst_role").system
        analyst = Agent(
            role="Product Match Analyst",
            goal="Classify each offer as exact, variant, accessory or mismatch for the wanted product",
            backstory=role,
            llm=_llm(ctx, "product_matching"),
            allow_delegation=False,
            max_iter=2,
            verbose=False,
        )
        listing = json.dumps(
            [{"id": o.id, "retailer": o.retailer, "title": o.title, "condition": o.condition} for o in offers],
            ensure_ascii=False,
        )
        task = Task(
            description=ctx.prompt("analyst_task").system.format(
                product=plan.product,
                must_match=", ".join(plan.must_match) or "the exact model",
                untrusted_rules=UNTRUSTED_DATA_RULES,
                offers=fence_untrusted(listing, "retailer pages").replace("{", "(").replace("}", ")"),
            ),
            expected_output="A MatchReport object with one verdict per offer id",
            output_pydantic=MatchReport,
            agent=analyst,
        )
        out = await Crew(agents=[analyst], tasks=[task], process=Process.sequential, verbose=False).kickoff_async()
        return _as(out, MatchReport)


def _as(output, model):
    if isinstance(getattr(output, "pydantic", None), model):
        return output.pydantic
    raw = getattr(output, "json_dict", None) or getattr(output, "raw", "{}")
    return model.model_validate(raw if isinstance(raw, dict) else json.loads(raw))
