from pathlib import Path

import fakeredis.aioredis
import pytest

from agency_platform.config.params import ParamFile
from agency_platform.errors import BadRequestError
from agency_platform.llm.tiers import Tier
from model_gateway.catalog import ModelsFile
from model_gateway.classifier import ComplexityClassifier, extract_signals, score
from model_gateway.policy import RoutingPolicy

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def models() -> ModelsFile:
    return ParamFile(ROOT / "config" / "models.yaml", ModelsFile).get()


@pytest.fixture
def policy() -> RoutingPolicy:
    return RoutingPolicy(ComplexityClassifier(fakeredis.aioredis.FakeRedis(decode_responses=True)))


def user(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


async def decide(policy, models, model, text="hi", plan="pro", min_tier=None, judge=None):
    return await policy.decide(
        requested_model=model, messages=user(text), has_tools=False, client_id="ev_research",
        plan=plan, min_tier=min_tier, models=models, judge=judge,
    )


async def test_task_alias_maps_to_configured_tier(policy, models):
    assert (await decide(policy, models, "task-intent_parsing")).tier == Tier.LOW
    assert (await decide(policy, models, "task-reasoning")).tier == Tier.HIGH
    assert (await decide(policy, models, "openai/task-synthesis")).tier == Tier.MID  # provider prefix tolerated


async def test_plan_ceiling_always_wins(policy, models):
    d = await decide(policy, models, "task-reasoning", plan="free")
    assert d.tier == Tier.MID and "ceiling" in d.reason


async def test_min_tier_escalation(policy, models):
    d = await decide(policy, models, "task-extraction", min_tier=Tier.MID)
    assert d.tier == Tier.MID


async def test_unknown_alias_rejected(policy, models):
    with pytest.raises(BadRequestError):
        await decide(policy, models, "gpt-4o")


async def test_unknown_task_uses_default_tier(policy, models):
    d = await decide(policy, models, "task-does_not_exist")
    assert d.tier == models.routing.default_tier and d.source == "default"


async def test_auto_trivial_prompt_is_low(policy, models):
    d = await decide(policy, models, "auto", text="What is the capital of France?")
    assert d.tier == Tier.LOW and d.source == "classifier"


async def test_auto_complex_prompt_is_high(policy, models):
    text = (
        "Explain step by step why and compare the trade-offs, then recommend a strategy and evaluate the risk:\n"
        + "\n".join(f"{i}. consider option {i} and its implications" for i in range(1, 8))
        + "\n" + "context " * 900
    )
    d = await decide(policy, models, "auto", text=text)
    assert d.tier == Tier.HIGH


async def test_auto_ambiguous_uses_judge_and_caches(policy, models):
    calls = []

    async def judge(text: str):
        calls.append(text)
        return Tier.MID

    text = "Compare these two options and explain the trade-offs briefly. " + "detail " * 300
    cfg = models.routing.classifier
    s = score(extract_signals(user(text), False, cfg), cfg)
    assert cfg.low_max_score < s < cfg.high_min_score, s

    first = await decide(policy, models, "auto", text=text, judge=judge)
    second = await decide(policy, models, "auto", text=text, judge=judge)
    assert first.tier == second.tier == Tier.MID
    assert first.source == second.source == "judge"
    assert len(calls) == 1  # second verdict served from cache


# ---- thinking per task / tier (A) ----------------------------------------------------------------------
def test_thinking_effort_per_task_then_tier():
    from agency_platform.config.models_schema import ThinkingConfig
    from agency_platform.llm.tiers import Tier
    t = ThinkingConfig(tasks={"topic_classification": "minimal", "synthesis": "low", "reasoning": "default"})
    assert t.effort("topic_classification", Tier.MID) == "minimal"     # task wins over tier
    assert t.effort("synthesis", Tier.HIGH) == "low"
    assert t.effort(None, Tier.HIGH) == "medium" and t.effort("other", Tier.LOW) == "minimal"
    assert t.effort("reasoning", Tier.HIGH) is None                     # default = send nothing


def test_executor_sends_reasoning_effort_unless_the_caller_did():
    from types import SimpleNamespace

    from agency_platform.config.models_schema import ThinkingConfig
    from agency_platform.llm.tiers import Tier
    from model_gateway.executor import Executor
    ex = Executor.__new__(Executor)
    ex._settings = SimpleNamespace(provider_key=lambda p: "k")
    spec = SimpleNamespace(litellm_model="gemini/x", provider="gemini", timeout_s=10, tier=Tier.MID)
    meta = SimpleNamespace(client_id="edge", request_id="r", prompt_ref=None)
    d = SimpleNamespace(task="topic_classification", source="task", tier=Tier.MID, reason="")
    models = SimpleNamespace(routing=SimpleNamespace(thinking=ThinkingConfig(tasks={"topic_classification": "minimal"})))
    assert ex._kwargs(spec, [], {}, meta, d, models)["reasoning_effort"] == "minimal"
    assert ex._kwargs(spec, [], {"reasoning_effort": "high"}, meta, d, models)["reasoning_effort"] == "high"
    assert "reasoning_effort" not in ex._kwargs(spec, [], {}, meta, d, None)
