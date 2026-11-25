"""Admin APIs: usage & billing views, model/topic performance, experiments,
canaries, quality (evals + drift), eval-case curation, account management.

Thin HTTP layer: every query lives in agency_platform.repositories."""
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator

from agency_platform.auth.accounts import AccountRepository, RefreshTokenStore
from agency_platform.auth.deps import AdminUser, get_account_directory
from agency_platform.auth.passwords import MAX_PASSWORD_BYTES
from agency_platform.config.registry import plans
from agency_platform.config.settings import get_settings
from agency_platform.db import get_db
from agency_platform.errors import BadRequestError, NotFoundError
from agency_platform.repositories import AdminMetrics, EvalRepository, GuardrailRepository, RunRepository, UsageRepository
from agency_platform.repositories.admin_metrics import Window, f, opt_f
from agency_platform.repositories.guardrails import day_series
from agency_platform.repositories.usage import since, total
from agency_platform.service import openapi
from edge_gateway.schemas import (
    AccountSummary,
    AdminRun,
    CanaryStat,
    EvalCase,
    EvalCaseStatus,
    ExperimentArm,
    LlmCall,
    ModelPerformance,
    PlanChanged,
    RoutingStat,
    TopicPerformance,
    TopicQuality,
    TopicUsageReport,
    UserUsage,
    UserUsageDetail,
)

admin_router = APIRouter(responses=openapi.ADMIN)

USAGE = ["Admin: usage & billing"]
MODELS = ["Admin: models & routing"]
TOPICS = ["Admin: topics & experiments"]
QUALITY = ["Admin: quality & evals"]
ACCOUNTS = ["Admin: accounts & runs"]


# =========================================================== usage / billing
@admin_router.get("/usage/topics", tags=USAGE, summary="Tokens and cost per topic across all users")
async def usage_by_topic(_: AdminUser, days: int = Query(30, ge=1, le=366)) -> TopicUsageReport:
    per_topic = await UsageRepository(get_db()).by_topic(from_day=since(days))
    return {"days": days, "total": total(per_topic), "by_topic": per_topic}


@admin_router.get("/usage/users", tags=USAGE, summary="Top users by cost, with per-topic breakdown")
async def usage_top_users(_: AdminUser, days: int = Query(30, ge=1, le=366), limit: int = Query(50, ge=1, le=500)) -> list[UserUsage]:
    return await UsageRepository(get_db()).top_users(from_day=since(days), limit=limit)


@admin_router.get("/usage/users/{user_id}", tags=USAGE, summary="One user's usage per topic (+ daily series)")
async def usage_for_user(user_id: uuid.UUID, _: AdminUser, days: int = Query(30, ge=1, le=366)) -> UserUsageDetail:
    repo = UsageRepository(get_db())
    per_topic = await repo.by_topic(from_day=since(days), user_id=str(user_id))
    return {
        "user_id": str(user_id),
        "total": total(per_topic),
        "by_topic": per_topic,
        "daily": await repo.daily_series(from_day=since(days), user_id=str(user_id)),
    }


# =========================================================== model / topic performance
@admin_router.get("/metrics/models", tags=MODELS, summary="Per-model performance")
async def model_performance(_: AdminUser, window: Window = "24h") -> list[ModelPerformance]:
    """Per-model performance: volume, latency percentiles, error/fallback rate, cache share, cost."""
    return await AdminMetrics(get_db()).models(window)


@admin_router.get("/metrics/routing", tags=MODELS, summary="How model tiers are being chosen")
async def routing_stats(_: AdminUser, window: Window = "24h") -> list[RoutingStat]:
    """How tiers are being chosen (task / tier / classifier / judge / default) per topic."""
    return await AdminMetrics(get_db()).routing(window)


@admin_router.get("/metrics/topics", tags=TOPICS, summary="Per-topic outcomes")
async def topic_quality(_: AdminUser, window: Window = "7d") -> list[TopicPerformance]:
    """Per-topic outcomes: success rate, latency, cost, cache share, feedback, online judge score."""
    return await AdminMetrics(get_db()).topics(window)


# =========================================================== experiments & canaries
@admin_router.get("/experiments", tags=TOPICS, summary="Prompt A/B results per variant")
async def experiments(_: AdminUser, topic: str | None = None, window: Window = "7d") -> list[ExperimentArm]:
    return await AdminMetrics(get_db()).experiments(topic, window)


@admin_router.get("/canaries", tags=MODELS, summary="Canary model vs incumbents, per tier")
async def canaries(_: AdminUser, window: Window = "24h") -> list[CanaryStat]:
    return await AdminMetrics(get_db()).canaries(window)


# =========================================================== quality
@admin_router.get("/quality", tags=QUALITY, summary="Latest offline evals, online judge scores and drift per topic")
async def quality(_: AdminUser, topic: str | None = None) -> list[TopicQuality]:
    repo = EvalRepository(get_db())
    out: dict[str, dict] = {}
    for r in await repo.latest_offline(topic):
        out.setdefault(r["topic"], {})["offline"] = r | {
            "pass_rate": f(r["pass_rate"]), "mean_judge_score": f(r["mean_judge_score"]),
            "mean_cost_usd": f(r["mean_cost_usd"]),
            "gate_passed": None if r["gate_passed"] is None else bool(r["gate_passed"]),
        }
    for r in await repo.online_scores(topic):
        out.setdefault(r["topic"], {})["online"] = {
            "judge_score_24h": opt_f(r["score_24h"]), "judge_score_7d": opt_f(r["score_7d"]),
            "judged_7d": int(r["judged_7d"] or 0),
        }
    for r in await repo.latest_drift(topic):
        out.setdefault(r["topic"], {})["drift"] = {"at": r["created_at"], "flagged": r["flagged"], "metrics": r["metrics"]}
    return [{"topic": t, **v} for t, v in sorted(out.items())]


# =========================================================== guardrails
GUARDRAILS = ["Admin: guardrails"]


class GuardrailTotal(BaseModel):
    kind: str
    action: str
    count: int
    last_at: datetime


class GuardrailDay(BaseModel):
    day: str
    total: int
    counts: dict[str, int] = Field(description="kind → events that day")


class GuardrailEvent(BaseModel):
    id: int
    created_at: datetime
    service: str
    kind: str
    action: str
    topic: str | None
    user_id: str | None
    user_email: str | None
    request_id: str | None
    detail: str


class GuardrailReport(BaseModel):
    days: int
    totals: list[GuardrailTotal]
    daily: list[GuardrailDay]
    recent: list[GuardrailEvent]


@admin_router.get("/guardrails", tags=GUARDRAILS, summary="Guardrails that fired: totals, per day, latest events")
async def guardrail_report(
    _: AdminUser,
    days: int = Query(7, ge=1, le=90),
    kind: str | None = Query(None, max_length=32, description="Only this kind in `recent`"),
    limit: int = Query(100, ge=1, le=500),
    before: datetime | None = Query(None, description="Older events (paging): created_at of the last one you have"),
) -> GuardrailReport:
    """Never contains users' text — only what fired, where, and short technical detail."""
    repo = GuardrailRepository(get_db())
    totals = [GuardrailTotal(kind=r["kind"], action=r["action"], count=int(r["n"]), last_at=r["last_at"])
              for r in await repo.totals(days)]
    daily = [GuardrailDay(**d) for d in day_series(await repo.daily(days), days, datetime.now(UTC).date())]
    recent = [GuardrailEvent(**{**r, "user_id": str(r["user_id"]) if r["user_id"] else None})
              for r in await repo.recent(limit=limit, kind=kind, before=before)]
    return GuardrailReport(days=days, totals=totals, daily=daily, recent=recent)


# =========================================================== feedback -> dataset curation
class EvalCaseUpdate(BaseModel):
    status: Literal["approved", "rejected", "candidate"]
    checks: dict[str, Any] | None = Field(
        default=None,
        description="Expectations for the dataset: must_contain, must_not_contain, rubric, reference, ...",
    )


@admin_router.get("/eval-cases", tags=QUALITY, summary="Feedback-derived eval case candidates")
async def list_eval_cases(
    _: AdminUser, topic: str | None = None, status: str = "candidate", label: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[EvalCase]:
    return await EvalRepository(get_db()).list_cases(topic=topic, status=status, label=label, limit=limit)


@admin_router.put("/eval-cases/{case_id}", tags=QUALITY, summary="Approve or reject an eval case",
                  responses=openapi.errors(404, 422))
async def update_eval_case(case_id: int, body: EvalCaseUpdate, _: AdminUser) -> EvalCaseStatus:
    """Approved cases are appended to evals/datasets by the quality worker (export-cases)."""
    row = await EvalRepository(get_db()).update_case(case_id, status=body.status, checks=body.checks)
    if row is None:
        raise NotFoundError("Eval case not found")
    return row


# =========================================================== accounts & runs
class PlanChange(BaseModel):
    plan: str
    expires_at: datetime | None = Field(default=None, description="None = no expiry")


@admin_router.put("/users/{user_id}/plan", tags=ACCOUNTS, summary="Change a user's plan",
                  responses=openapi.errors(400, 404, 422))
async def set_plan(user_id: uuid.UUID, body: PlanChange, _: AdminUser) -> PlanChanged:
    """Integration point for a billing provider webhook (Stripe etc.)."""
    if body.plan not in plans().plans:
        raise BadRequestError(f"Unknown plan '{body.plan}'", available=list(plans().plans))
    account = await AccountRepository(get_db()).set_plan(str(user_id), body.plan, body.expires_at)
    if account is None:
        raise NotFoundError("User not found")
    await get_account_directory().invalidate(str(user_id))  # takes effect on the next request
    return PlanChanged(id=account.id, plan=account.plan, plan_expires_at=account.plan_expires_at)


@admin_router.get("/users", tags=ACCOUNTS, summary="List accounts (plan=none: waiting for activation)")
async def list_users(
    _: AdminUser,
    plan: str | None = Query(default=None, max_length=64, description="Only this plan, e.g. none"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AccountSummary]:
    """Activate an account with PUT /users/{user_id}/plan."""
    rows = await AccountRepository(get_db()).list_accounts(plan, limit)
    return [AccountSummary(**{**r, "is_active": bool(r["is_active"])}) for r in rows]


class PasswordReset(BaseModel):
    new_password: str = Field(..., min_length=10, description="Tell the user; they can change it later")

    @field_validator("new_password")
    @classmethod
    def _bcrypt_limit(cls, v: str) -> str:
        if len(v.encode()) > MAX_PASSWORD_BYTES:
            raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
        return v


@admin_router.put("/users/{user_id}/password", status_code=204, tags=ACCOUNTS,
                  summary="Reset a user's password and revoke their sessions", responses=openapi.errors(404, 422))
async def reset_password(user_id: uuid.UUID, body: PasswordReset, _: AdminUser) -> None:
    if not await AccountRepository(get_db()).set_password(str(user_id), body.new_password):
        raise NotFoundError("User not found")
    await RefreshTokenStore(get_db(), get_settings().refresh_token_ttl_days).revoke_all(str(user_id))


@admin_router.post("/users/{user_id}/disable", status_code=204, tags=ACCOUNTS,
                   summary="Disable a user and revoke their sessions")
async def disable_user(user_id: uuid.UUID, _: AdminUser) -> None:
    await AccountRepository(get_db()).set_active(str(user_id), False)
    await RefreshTokenStore(get_db(), get_settings().refresh_token_ttl_days).revoke_all(str(user_id))
    await get_account_directory().invalidate(str(user_id))


@admin_router.get("/runs", tags=ACCOUNTS, summary="Recent runs across all users")
async def recent_runs(
    _: AdminUser, topic: str | None = None, status: str | None = None, limit: int = Query(50, ge=1, le=500)
) -> list[AdminRun]:
    rows = await RunRepository(get_db()).recent(topic=topic, status=status, limit=limit)
    return [r | {"cost_usd": f(r["cost_usd"]), "cache_hit": bool(r["cache_hit"])} for r in rows]


@admin_router.get("/runs/{request_id}/calls", tags=ACCOUNTS, summary="Every LLM call of one run")
async def run_calls(request_id: str, _: AdminUser) -> list[LlmCall]:
    """Every LLM attempt in one run: which tier/model, why, cost, latency, cache/canary."""
    rows = await RunRepository(get_db()).llm_calls(request_id)
    return [r | {"cost_usd": f(r["cost_usd"]), "cached": bool(r["cached"]), "canary": bool(r["canary"])} for r in rows]
