"""Self-service usage: what the signed-in user has spent, in tokens and $, per topic."""
from fastapi import APIRouter, Query

from agency_platform.auth.deps import CurrentUser
from agency_platform.billing.usage import UsageMeter
from agency_platform.cache import get_redis
from agency_platform.db import get_db
from agency_platform.repositories import RunRepository, UsageRepository
from agency_platform.repositories.usage import month_start, since, total
from agency_platform.service import openapi
from edge_gateway.schemas import DailyUsage, MyUsage, RunSummary
from edge_gateway.usage_service import live_today

usage_router = APIRouter(tags=["Usage"], responses=openapi.AUTHED)


@usage_router.get("/me", summary="My tokens and cost per topic: today, month to date, last 30 days")
async def my_usage(user: CurrentUser) -> MyUsage:
    today = await live_today(UsageMeter(get_redis()), user.id, user.plan_config)
    repo = UsageRepository(get_db())
    mtd = await repo.by_topic(from_day=month_start(), user_id=user.id)
    last30 = await repo.by_topic(from_day=since(30), user_id=user.id)
    return {
        "plan": user.plan,
        "today": today,
        "month_to_date": {"total": total(mtd), "by_topic": mtd},
        "last_30_days": {"total": total(last30), "by_topic": last30},
    }


@usage_router.get("/me/daily", summary="My daily usage time series per topic (for charts)")
async def my_daily(user: CurrentUser, days: int = Query(30, ge=1, le=366), topic: str | None = None) -> list[DailyUsage]:
    return await UsageRepository(get_db()).daily_series(from_day=since(days), user_id=user.id, topic=topic)


@usage_router.get("/me/runs", summary="My recent runs with their token and cost usage")
async def my_runs(user: CurrentUser, topic: str | None = None, limit: int = Query(50, ge=1, le=200)) -> list[RunSummary]:
    rows = await RunRepository(get_db()).list_for_user(user.id, topic=topic, limit=limit)
    return [
        r | {"cost_usd": float(r["cost_usd"]), "cache_hit": bool(r["cache_hit"]),
             "total_tokens": r["prompt_tokens"] + r["completion_tokens"]}
        for r in rows
    ]
