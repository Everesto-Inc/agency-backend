"""Response models of the edge API (requests live next to their routes)."""
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ------------------------------------------------------------------ auth / account


class TokenPair(BaseModel):
    access_token: str = Field(..., description="Bearer token for every other endpoint (short-lived)")
    refresh_token: str = Field(..., description="Single-use; exchange at /auth/refresh for a new pair")
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int = Field(..., description="Access token lifetime in seconds")
    plan: str = Field(..., description="Effective subscription plan")


class Me(BaseModel):
    id: str
    email: str
    role: Literal["user", "admin", "service"]
    plan: str
    plan_display_name: str
    entitled_topics: list[str] = Field(..., description="Topics this plan includes ('*' = all)")
    max_model_tier: str = Field(..., description="Highest model tier this plan may use")


class AccountExport(BaseModel):
    account: dict[str, Any] | None
    runs: list[dict[str, Any]] = Field(..., description="Every run: prompt, answer, tokens, cost, feedback")
    usage_daily: list[dict[str, Any]]


# ------------------------------------------------------------------ topics


class TopicSummary(BaseModel):
    topic: str = Field(..., examples=["ev_research"])
    display_name: str
    framework: str
    description: str
    entitled: bool = Field(..., description="Whether the caller's plan includes it")
    daily_request_limit: int | None = Field(None, description="Runs per day on the caller's plan (null = unlimited)")
    run_path: str = Field(..., description="Run this topic directly (bypassing /chat auto-routing)")
    docs_path: str = Field(..., description="This topic's own API docs")


# ------------------------------------------------------------------ usage


class UsageTotals(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    llm_calls: int
    runs: int
    cached_runs: int


class UsagePeriod(BaseModel):
    total: UsageTotals
    by_topic: dict[str, UsageTotals] = Field(..., description="Keyed by topic; 'routing' = /chat topic selection")


class TopicToday(BaseModel):
    tokens: int
    cost_usd: float
    llm_calls: int
    runs: int
    run_limit: int | None = Field(None, description="Daily run limit on your plan (null = unlimited)")
    token_limit: int | None = Field(None, description="Daily token limit for this topic (null = only the plan-wide limit)")


class Today(BaseModel):
    tokens: int
    token_limit: int
    cost_usd: float
    cost_limit_usd: float
    by_topic: dict[str, TopicToday]


class MyUsage(BaseModel):
    plan: str
    today: Today = Field(..., description="Live counters; reset at 00:00 UTC")
    month_to_date: UsagePeriod
    last_30_days: UsagePeriod


class DailyUsage(UsageTotals):
    day: date
    topic: str


class RunSummary(BaseModel):
    request_id: str
    topic: str
    status: str
    cache_hit: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    llm_calls: int
    latency_ms: int | None
    feedback_score: int | None
    created_at: datetime


# ------------------------------------------------------------------ admin: usage


class TopicUsageReport(BaseModel):
    days: int
    total: UsageTotals
    by_topic: dict[str, UsageTotals]


class UserUsage(BaseModel):
    user_id: str
    email: str
    plan: str
    cost_usd: float
    topics: dict[str, dict[str, float]] = Field(..., description="{topic: {tokens, cost_usd, runs}}")


class UserUsageDetail(BaseModel):
    user_id: str
    total: UsageTotals
    by_topic: dict[str, UsageTotals]
    daily: list[DailyUsage]


# ------------------------------------------------------------------ admin: models, topics, experiments


class ModelPerformance(BaseModel):
    model: str
    tier: str
    canary: bool
    attempts: int
    error_rate: float
    fallback_served: int = Field(..., description="Calls this model served after another candidate failed")
    cache_hit_rate: float
    p50_ms: float | None
    p95_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class RoutingStat(BaseModel):
    client_id: str = Field(..., description="Calling service (topic, edge, quality)")
    route_source: str = Field(..., description="task | tier | classifier | judge | default")
    tier: str
    calls: int
    cost_usd: float


class CanaryStat(BaseModel):
    tier: str
    model: str
    canary: bool
    attempts: int
    success_rate: float
    p95_ms: float | None
    avg_cost_usd: float
    judge_score: float | None


class TopicPerformance(BaseModel):
    topic: str
    runs: int
    success_rate: float
    cache_hit_rate: float
    p95_ms: float | None
    avg_cost_usd: float
    avg_feedback: float | None
    feedback_count: int
    online_judge_score: float | None


class ExperimentArm(BaseModel):
    prompt_ref: str = Field(..., description="topic/prompt@version~variant", examples=["ev_research/synthesize@2.0~b"])
    variant: str
    calls: int
    runs: int
    avg_call_cost_usd: float
    p95_ms: float | None
    error_rate: float
    avg_feedback: float | None
    feedback_count: int
    judge_score: float | None
    judged_runs: int


# ------------------------------------------------------------------ admin: quality


class OfflineEval(BaseModel):
    id: int
    created_at: datetime
    git_sha: str | None
    label: str | None
    cases: int
    pass_rate: float
    mean_judge_score: float
    mean_cost_usd: float
    p95_latency_ms: int | None
    gate_passed: bool | None


class OnlineScores(BaseModel):
    judge_score_24h: float | None
    judge_score_7d: float | None
    judged_7d: int


class DriftSummary(BaseModel):
    at: datetime
    flagged: list[str]
    metrics: dict[str, Any]


class TopicQuality(BaseModel):
    topic: str
    offline: OfflineEval | None = None
    online: OnlineScores | None = None
    drift: DriftSummary | None = None


class EvalCase(BaseModel):
    id: int
    topic: str
    source_request_id: str | None
    prompt: str
    answer: str | None
    label: Literal["positive", "negative"]
    comment: str | None
    status: Literal["candidate", "approved", "rejected", "exported"]
    checks: dict[str, Any] | None
    created_at: datetime


class EvalCaseStatus(BaseModel):
    id: int
    status: str
    checks: dict[str, Any] | None


# ------------------------------------------------------------------ admin: accounts & runs


class PlanChanged(BaseModel):
    id: str
    plan: str
    plan_expires_at: datetime | None


class AccountSummary(BaseModel):
    id: str
    email: str
    role: str
    plan: str = Field(description="'none' = waiting for activation by an admin")
    plan_expires_at: datetime | None
    is_active: bool
    created_at: datetime


class AdminRun(BaseModel):
    request_id: str
    user_id: str
    topic: str
    status: str
    error_code: str | None
    cache_hit: bool
    latency_ms: int | None
    cost_usd: float
    llm_calls: int
    feedback_score: int | None
    prompt_fingerprint: str | None = Field(None, description="Prompt versions + A/B arms used")
    created_at: datetime


class LlmCall(BaseModel):
    task: str | None
    prompt_ref: str | None
    requested_model: str
    route_source: str
    route_reason: str | None
    tier: str
    model: str
    attempt: int
    status: str
    error_type: str | None
    cached: bool
    canary: bool
    pii_redactions: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    created_at: datetime


# ------------------------------------------------------------------ dashboards
# Shapes are frontend-first: every number a card or chart needs is precomputed,
# series have no gaps (one point per day, zeros included), topics carry display names.

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


class MonthOption(BaseModel):
    month: str = Field(..., examples=["2026-11"], description="Value to pass as ?month=")
    label: str = Field(..., examples=["November 2026"])
    is_current: bool
    has_data: bool


class MonthsResponse(BaseModel):
    """Options for the month picker, newest first. The current month is always included."""

    current: str
    months: list[MonthOption]


class Period(BaseModel):
    month: str = Field(..., examples=["2026-11"])
    label: str = Field(..., examples=["November 2026"])
    start: date = Field(..., description="First day of the month")
    end: date = Field(..., description="Last day covered: today for the current month, else the month's last day")
    days: int = Field(..., description="Number of days covered")
    is_current: bool


class Change(BaseModel):
    """% change vs the previous month (null when the previous month was 0)."""

    total_tokens_pct: float | None
    cost_usd_pct: float | None
    runs_pct: float | None


class TopicBreakdown(BaseModel):
    topic: str = Field(..., examples=["ev_research"])
    display_name: str = Field(..., examples=["EV research"])
    kind: Literal["assistant", "routing", "evals"] = Field(
        ..., description="assistant = a topic agent; routing = /chat topic selection; evals = quality jobs")
    totals: UsageTotals
    share_of_cost: float = Field(..., description="0..1 share of the period's total cost")
    avg_cost_per_run: float | None = Field(None, description="cost_usd / runs (null when no runs)")


class DailyTopicPoint(BaseModel):
    total_tokens: int
    cost_usd: float
    runs: int


class DailyPoint(BaseModel):
    day: date
    total_tokens: int
    cost_usd: float
    runs: int
    llm_calls: int
    by_topic: dict[str, DailyTopicPoint] = Field(..., description="Only topics with activity that day")


class AccountRef(BaseModel):
    id: str
    email: str
    plan: str


class UserDashboard(BaseModel):
    period: Period
    currency: Literal["USD"] = "USD"
    account: AccountRef
    totals: UsageTotals
    previous_month: UsageTotals
    change: Change
    by_topic: list[TopicBreakdown] = Field(..., description="Sorted by cost, highest first")
    daily: list[DailyPoint]
    runs: "RunStats"
    recent_runs: list[RunSummary] = Field(..., description="Latest runs in the period (max 10)")
    today: Today | None = Field(None, description="Live quota status; only for the current month")


class ModelUsage(BaseModel):
    model: str
    tier: str
    calls: int
    error_rate: float
    cache_hit_rate: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    share_of_cost: float


class UserCounts(BaseModel):
    total: int = Field(..., description="All accounts (service accounts excluded)")
    active: int = Field(..., description="Accounts with any usage in the period")
    new_signups: int
    disabled: int
    by_plan: dict[str, int] = Field(..., description="Active accounts per plan (current snapshot)")


class RunStats(BaseModel):
    runs: int
    successful: int
    failed: int
    success_rate: float | None
    cache_hit_rate: float | None
    avg_latency_ms: float | None
    positive_feedback: int
    negative_feedback: int


class AdminDashboard(BaseModel):
    period: Period
    currency: Literal["USD"] = "USD"
    totals: UsageTotals
    previous_month: UsageTotals
    change: Change
    by_topic: list[TopicBreakdown]
    by_model: list[ModelUsage]
    daily: list[DailyPoint]
    users: UserCounts
    runs: RunStats
    top_users: list[UserUsage] = Field(..., description="Highest cost first (max 10)")


UserDashboard.model_rebuild()
