"""Live (Redis) usage view shared by /usage/me and the dashboards."""
from agency_platform.billing.usage import UsageMeter
from agency_platform.config.registry import topics
from agency_platform.config.schemas import PlanConfig
from edge_gateway.schemas import Today, TopicToday


async def live_today(meter: UsageMeter, user_id: str, plan: PlanConfig) -> Today:
    """Today's tokens/cost against the plan limits, per topic (resets 00:00 UTC)."""
    today = await meter.daily(user_id, plan)
    live = await meter.per_topic_today(user_id)
    enabled = topics().enabled()
    by_topic: dict[str, TopicToday] = {}
    for t in sorted(set(enabled) | set(live)):
        if t in enabled and not plan.allows_topic(t) and t not in live:
            continue
        spent = live.get(t, {"tokens": 0, "cost_usd": 0.0, "llm_calls": 0})
        by_topic[t] = TopicToday(
            tokens=int(spent["tokens"]),
            cost_usd=round(spent["cost_usd"], 6),
            llm_calls=int(spent["llm_calls"]),
            runs=await meter.topic_requests_today(user_id, t),
            run_limit=plan.topic_daily_limit(t),
            token_limit=plan.topic_token_limit(t),
        )
    return Today(
        tokens=today.tokens,
        token_limit=today.token_limit,
        cost_usd=round(today.cost_usd, 6),
        cost_limit_usd=today.cost_limit_usd,
        by_topic=by_topic,
    )
