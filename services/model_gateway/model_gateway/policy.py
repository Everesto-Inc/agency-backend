"""Routing policy: requested alias -> tier, bounded by the caller's plan.

Resolution order:
    "task-<name>" -> topic override (topics.yaml task_tiers) -> models.yaml tasks
    "tier-<t>"    -> that tier
    "auto"        -> complexity classifier (heuristics, then LLM judge)
then: raise to X-Agency-Min-Tier (structured-output escalation), then clamp to
the plan's max_tier. The plan ceiling always wins: a free user can never be
routed to the most expensive tier, whatever the agent asks for.
"""
from dataclasses import dataclass
from typing import Any

from agency_platform.config.registry import plans, topics
from agency_platform.errors import BadRequestError
from agency_platform.llm.tiers import AUTO_ALIAS, TASK_PREFIX, TIER_PREFIX, Tier
from model_gateway.catalog import ModelsFile
from model_gateway.classifier import ComplexityClassifier, JudgeFn


@dataclass(frozen=True)
class RouteDecision:
    requested: str
    tier: Tier
    ceiling: Tier
    source: str  # task|tier|classifier|judge|default
    reason: str
    task: str | None = None


def _normalise_alias(model: str) -> str:
    # Frameworks sometimes keep a provider prefix ("openai/task-x").
    return model.rsplit("/", 1)[-1].strip().lower()


class RoutingPolicy:
    def __init__(self, classifier: ComplexityClassifier) -> None:
        self._classifier = classifier

    async def decide(
        self,
        *,
        requested_model: str,
        messages: list[dict[str, Any]],
        has_tools: bool,
        client_id: str,
        plan: str,
        min_tier: Tier | None,
        models: ModelsFile,
        judge: JudgeFn | None = None,
    ) -> RouteDecision:
        alias = _normalise_alias(requested_model)
        routing = models.routing
        task: str | None = None

        if alias == AUTO_ALIAS:
            c = await self._classifier.classify(messages, has_tools, routing, judge)
            tier, source, reason = c.tier, c.source, c.reason
        elif alias.startswith(TIER_PREFIX):
            try:
                tier = Tier(alias.removeprefix(TIER_PREFIX))
            except ValueError:
                raise BadRequestError(f"Unknown tier alias '{requested_model}'") from None
            source, reason = "tier", "explicit tier"
        elif alias.startswith(TASK_PREFIX):
            task = alias.removeprefix(TASK_PREFIX)
            topic_cfg = topics().topics.get(client_id)
            override = topic_cfg.task_tiers.get(task) if topic_cfg else None
            if override is not None:
                tier, source, reason = override, "task", f"task '{task}' (topic override)"
            elif task in routing.tasks:
                tier, source, reason = routing.tasks[task], "task", f"task '{task}'"
            else:
                tier, source, reason = routing.default_tier, "default", f"unknown task '{task}'"
        else:
            raise BadRequestError(
                f"Unknown model '{requested_model}'. Use 'auto', 'tier-<low|mid|high>' or 'task-<name>'."
            )

        if min_tier is not None and min_tier.rank > tier.rank:
            tier, reason = min_tier, f"{reason}; raised to min tier {min_tier}"

        ceiling = plans().get(plan).max_tier
        if tier.rank > ceiling.rank:
            tier, reason = ceiling, f"{reason}; clamped to plan ceiling {ceiling}"

        return RouteDecision(
            requested=requested_model, tier=tier, ceiling=ceiling, source=source, reason=reason, task=task
        )
