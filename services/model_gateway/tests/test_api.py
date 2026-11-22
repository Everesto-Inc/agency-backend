"""End-to-end through the HTTP API with a fake provider (no network, no DB)."""
import uuid
from pathlib import Path
from types import SimpleNamespace

import fakeredis.aioredis
import litellm
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agency_platform.billing.usage import UsageMeter
from agency_platform.config.params import ParamFile
from agency_platform.config.prompts import PromptStore
from agency_platform.llm.call_token import CallClaims, mint_call_token
from model_gateway.api import GatewayState, router
from model_gateway.catalog import ModelsFile
from model_gateway.classifier import ComplexityClassifier
from model_gateway.executor import Executor
from model_gateway.llm_cache import LLMCache
from model_gateway.policy import RoutingPolicy
from model_gateway.recorder import daily_rollup, usage_topic
from model_gateway.settings import GatewaySettings

ROOT = Path(__file__).resolve().parents[3]
SECRET = "s" * 40


class FakeRecorder:
    def __init__(self, meter: UsageMeter) -> None:
        self.records = []
        self.meter = meter

    async def record(self, rec) -> None:
        self.records.append(rec)
        if rec.status == "success" and rec.user_id:
            await self.meter.record_llm_usage(
                user_id=rec.user_id, topic=usage_topic(rec.client_id), request_id=rec.request_id,
                prompt_tokens=rec.prompt_tokens, completion_tokens=rec.completion_tokens, cost_usd=rec.cost_usd,
            )


def fake_response(model: str, content: str = "hello"):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        model_dump=lambda exclude_none=True: {
            "id": "x", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


@pytest.fixture
def env(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    meter = UsageMeter(redis)
    recorder = FakeRecorder(meter)
    settings = GatewaySettings()
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    from fastapi.responses import JSONResponse

    from agency_platform.errors import AppError

    @app.exception_handler(AppError)
    async def _h(_, exc: AppError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)

    app.state.gateway = GatewayState(
        models=ParamFile(ROOT / "config" / "models.yaml", ModelsFile),
        policy=RoutingPolicy(ComplexityClassifier(redis)),
        executor=Executor(settings, recorder, LLMCache(redis)),  # type: ignore[arg-type]
        meter=meter,
        prompts=PromptStore(ROOT / "prompts"),
        client_secrets=settings.client_secrets(),
    )
    calls: list[str] = []
    failing: set[str] = set()

    sent: list[list[dict]] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        sent.append(kwargs["messages"])
        if kwargs["model"] in failing:
            raise litellm.exceptions.RateLimitError("rate limited", llm_provider="x", model=kwargs["model"])
        return fake_response(kwargs["model"])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return SimpleNamespace(client=TestClient(app), calls=calls, failing=failing, recorder=recorder, meter=meter,
                           sent=sent, models=app.state.gateway.models)


def token(plan: str = "pro", client: str = "ev_research", secret: str = SECRET, user_id: str | None = None) -> str:
    claims = CallClaims(client_id=client, user_id=user_id or str(uuid.uuid4()), plan=plan, request_id="req-123456")
    return mint_call_token(claims, secret, ttl_s=60)


def post(env, model: str, tok: str, content: str = "hi", **extra):
    return env.client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": content}], **extra},
        headers={"Authorization": f"Bearer {tok}"},
    )


def test_routes_task_to_low_tier_primary(env):
    r = post(env, "task-intent_parsing", token())
    assert r.status_code == 200, r.text
    assert r.headers["x-agency-tier"] == "low"
    assert r.headers["x-agency-model"] == "gemini-flash-lite"
    assert env.calls == ["gemini/gemini-3.1-flash-lite"]


def test_fallback_within_tier_then_records_both_attempts(env):
    env.failing.add("gemini/gemini-3.1-flash-lite")
    r = post(env, "task-intent_parsing", token())
    assert r.status_code == 200
    assert r.headers["x-agency-model"] == "gemini-flash"
    statuses = [(rec.model, rec.status) for rec in env.recorder.records]
    assert statuses == [("gemini-flash-lite", "error"), ("gemini-flash", "success")]


def test_escalates_to_next_tier_when_whole_tier_fails(env):
    env.failing.update({"gemini/gemini-3.8-flash", "gemini/gemini-3.1-flash-lite"})
    r = post(env, "task-synthesis", token())
    assert r.status_code == 200
    assert r.headers["x-agency-tier"] == "high"


def test_free_plan_never_reaches_high_tier(env):
    r = post(env, "task-reasoning", token(plan="free"))
    assert r.headers["x-agency-tier"] == "mid"


def test_rejects_forged_and_foreign_tokens(env):
    assert post(env, "auto", token(secret="x" * 40)).status_code == 401
    assert post(env, "auto", token(client="unknown")).status_code == 401
    assert post(env, "auto", "garbage").status_code == 401


def test_usage_metered_per_run_and_quota_hard_stop(env):
    uid = str(uuid.uuid4())
    assert post(env, "tier-low", token(plan="none", user_id=uid)).status_code == 429  # 'none' plan: zero budget
    r = post(env, "tier-low", token(plan="free", user_id=uid))
    assert r.status_code == 200
    import asyncio
    usage = asyncio.new_event_loop().run_until_complete(env.meter.run_usage("req-123456"))
    assert usage.total_tokens == 15 and usage.llm_calls == 1 and usage.cost_usd > 0


def run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_per_topic_usage_counters_and_rollup(env):
    uid = str(uuid.uuid4())
    post(env, "tier-low", token(user_id=uid))
    post(env, "tier-low", token(user_id=uid, client="edge", secret="e" * 40))  # /chat routing call
    per_topic = run(env.meter.per_topic_today(uid))
    assert per_topic["ev_research"]["tokens"] == 15 and per_topic["routing"]["tokens"] == 15
    assert per_topic["ev_research"]["cost_usd"] > 0
    rows = daily_rollup(env.recorder.records)
    assert {r[1] for r in rows} == {"ev_research", "routing"}


def test_pii_redacted_before_provider(env):
    r = post(env, "tier-low", token(), content="email me at jane@example.com, card 4111 1111 1111 1111")
    assert r.status_code == 200
    outbound = env.sent[-1][0]["content"]
    assert "jane@example.com" not in outbound and "[EMAIL]" in outbound and "[CARD]" in outbound
    assert env.recorder.records[-1].pii_redactions == 2


def test_deterministic_calls_are_cached(env):
    first = post(env, "tier-low", token(), content="classify this", temperature=0)
    second = post(env, "tier-low", token(), content="classify this", temperature=0)
    assert first.status_code == second.status_code == 200
    assert second.headers.get("x-agency-cache") == "hit"
    assert len(env.calls) == 1  # provider called once
    assert env.recorder.records[-1].cached and env.recorder.records[-1].cost_usd == 0
    post(env, "tier-low", token(), content="classify this", temperature=0.7)  # non-deterministic: not cached
    assert len(env.calls) == 2


def test_canary_gets_configured_share_sticky_per_run(env, monkeypatch):
    from model_gateway.catalog import Canary

    cfg = env.models.get()
    monkeypatch.setattr(cfg, "canaries", [Canary(tier="low", model="gemini-pro", percent=50)])
    served = []
    for i in range(40):
        claims = CallClaims(client_id="ev_research", user_id=str(uuid.uuid4()), plan="pro", request_id=f"req-{i:08d}")
        r = post(env, "tier-low", mint_call_token(claims, SECRET, ttl_s=60))
        served.append(r.headers["x-agency-model"])
    share = served.count("gemini-pro") / len(served)
    assert 0.2 < share < 0.8, share
    assert any(rec.canary for rec in env.recorder.records)
