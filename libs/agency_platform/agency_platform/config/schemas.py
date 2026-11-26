"""Schemas for the shared parameter files: plans.yaml, topics.yaml, privacy.yaml, evals.yaml."""
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from agency_platform.llm.tiers import Tier

ALL_TOPICS = "*"


class PlanConfig(BaseModel):
    display_name: str
    max_tier: Tier = Tier.MID
    topics: list[str] = Field(default_factory=list, description="Entitled topics; '*' = all")
    daily_token_limit: int = Field(ge=0)
    daily_cost_limit_usd: float = Field(ge=0)
    requests_per_minute: int = Field(ge=0)
    topic_daily_requests: dict[str, int] = Field(default_factory=dict)
    # Optional per-topic daily token budget (on top of the plan-wide one).
    topic_daily_tokens: dict[str, int] = Field(default_factory=dict)

    def allows_topic(self, topic: str) -> bool:
        return ALL_TOPICS in self.topics or topic in self.topics

    def topic_daily_limit(self, topic: str) -> int | None:
        return self.topic_daily_requests.get(topic)

    def topic_token_limit(self, topic: str) -> int | None:
        return self.topic_daily_tokens.get(topic)


class PlansFile(BaseModel):
    signup_plan: str
    lapsed_plan: str = Field(description="Effective plan when a paid subscription has expired")
    plans: dict[str, PlanConfig]

    @model_validator(mode="after")
    def _references_exist(self) -> "PlansFile":
        for ref in (self.signup_plan, self.lapsed_plan):
            if ref not in self.plans:
                raise ValueError(f"plan '{ref}' referenced but not defined")
        return self

    def effective_plan(self, plan: str, expires_at: datetime | None) -> str:
        """Resolve the plan actually in force (expired or unknown -> lapsed plan)."""
        if plan not in self.plans:
            return self.lapsed_plan
        if expires_at is not None and expires_at <= datetime.now(UTC):
            return self.lapsed_plan
        return plan

    def get(self, plan: str) -> PlanConfig:
        return self.plans.get(plan) or self.plans[self.lapsed_plan]


class TopicConfig(BaseModel):
    display_name: str
    framework: str
    service_url: HttpUrl
    description: str
    enabled: bool = True
    keywords: list[str] = Field(default_factory=list)
    # Per-topic override of the gateway's task -> tier mapping.
    task_tiers: dict[str, Tier] = Field(default_factory=dict)
    run_timeout_s: int = Field(default=180, ge=5)
    max_prompt_chars: int = Field(default=4000, ge=100)
    # Identical first-turn questions are answered from cache for this long
    # (0 disables). Keep short for volatile data (prices, quotes).
    response_cache_ttl_s: int = Field(default=0, ge=0)


class TopicRoutingConfig(BaseModel):
    keyword_min_hits: int = 1
    llm_fallback: bool = True
    default_topic: str | None = None
    triage: bool = Field(True, description="one fast call decides: direct answer or which assistant (with the chat's context)")
    triage_timeout_s: float = Field(3.0, gt=0, le=15, description="slower than this → keywords decide (routing never hangs)")


class TopicsFile(BaseModel):
    routing: TopicRoutingConfig = TopicRoutingConfig()
    topics: dict[str, TopicConfig]

    def enabled(self) -> dict[str, TopicConfig]:
        return {k: v for k, v in self.topics.items() if v.enabled}


PIIEntity = Literal["email", "phone", "credit_card", "iban", "ip_address", "ssn"]


class PIIConfig(BaseModel):
    redact_before_llm: bool = True
    redact_in_storage: bool = True
    entities: list[PIIEntity] = Field(default_factory=lambda: ["email", "phone", "credit_card", "iban", "ssn"])


class RetentionConfig(BaseModel):
    run_text_days: int = Field(30, ge=1)
    llm_calls_days: int = Field(180, ge=1)
    eval_results_days: int = Field(365, ge=1)
    expired_refresh_tokens_days: int = Field(7, ge=1)
    guardrail_events_days: int = Field(90, ge=1)


class InjectionConfig(BaseModel):
    mode: Literal["off", "flag", "block"] = "flag"
    threshold: float = Field(0.6, gt=0, le=1)
    llm_check: bool = False


class PictureLimits(BaseModel):
    per_message: int = Field(4, ge=1, le=10)
    per_day: int = Field(60, ge=1, le=10_000)
    max_upload_mb: float = Field(2.0, gt=0, le=10)


class GuardrailsFile(BaseModel):
    injection: InjectionConfig = InjectionConfig()
    pictures: PictureLimits = PictureLimits()


class ScopeDomain(BaseModel):
    id: str = Field(..., description="the assistant (topic) that handles it")
    label: str
    covers: str


class ScopeFile(BaseModel):
    """What the agency talks about (config/scope.yaml) — the triage prompt is built from it."""
    domains: list[ScopeDomain] = Field(..., min_length=1)
    not_allowed: list[str] = Field(default_factory=list)
    reply_style: str = "Say kindly that this is outside what the agency does and suggest 2-3 example questions."


class MemoryFile(BaseModel):
    recent: int = Field(4, ge=0, le=20)
    fold_at_least: int = Field(6, ge=2, le=50)
    max_turns: int = Field(20, ge=2, le=200)
    ttl_hours: int = Field(24, ge=1, le=24 * 30)


class PrivacyFile(BaseModel):
    pii: PIIConfig = PIIConfig()
    retention: RetentionConfig = RetentionConfig()


class OfflineEvalConfig(BaseModel):
    concurrency: int = Field(3, ge=1, le=20)
    request_timeout_s: int = 300
    judge_task: str = "eval_judge"


class GateConfig(BaseModel):
    min_pass_rate: float = Field(0.8, ge=0, le=1)
    max_pass_rate_drop: float = Field(0.05, ge=0, le=1)
    max_judge_score_drop: float = Field(0.3, ge=0)
    max_cost_increase_pct: float = Field(25, ge=0)
    max_p95_latency_increase_pct: float = Field(50, ge=0)


class OnlineEvalConfig(BaseModel):
    sample_rate: float = Field(0.05, ge=0, le=1)
    max_judged_per_job: int = Field(200, ge=1)


class DriftThresholds(BaseModel):
    judge_score_drop: float = 0.4
    negative_feedback_rate_increase: float = 0.10
    error_rate_increase: float = 0.05
    cost_per_run_increase_pct: float = 30
    tier_distribution_jsd: float = 0.10


class DriftConfig(BaseModel):
    window_hours: int = Field(24, ge=1)
    baseline_days: int = Field(7, ge=1)
    min_runs: int = Field(20, ge=1)
    thresholds: DriftThresholds = DriftThresholds()
    alert_webhook_env: str = "ALERT_WEBHOOK_URL"


class EvalsFile(BaseModel):
    offline: OfflineEvalConfig = OfflineEvalConfig()
    gate: GateConfig = GateConfig()
    online: OnlineEvalConfig = OnlineEvalConfig()
    drift: DriftConfig = DriftConfig()
