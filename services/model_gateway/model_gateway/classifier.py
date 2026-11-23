"""Complexity classifier for model="auto": decides which tier a request needs.

Two stages, cheapest first:
  1. Heuristic score in [0, 1] from request signals (length, reasoning terms,
     multi-step structure, code, tools, conversation depth). Clear cases are
     decided here for free, in microseconds.
  2. Only the ambiguous middle band goes to a low-tier LLM judge, with the
     verdict cached in Redis by content hash, so repeated prompts cost nothing.
All weights/thresholds/terms are parameters in config/models.yaml.
"""
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from agency_platform.cache import rkey
from agency_platform.llm.tiers import Tier
from model_gateway.catalog import ClassifierConfig, RoutingConfig

_CODE_RE = re.compile(r"```|Traceback \(most recent call last\)|\bdef \w+\(|\bclass \w+[:(]|;\s*$|\{\s*\"", re.M)
_STEP_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*])\s+|\b(?:then|after that|finally|first|second|next)\b", re.I)

JudgeFn = Callable[[str], Awaitable[Tier | None]]


@dataclass(frozen=True)
class Signals:
    chars: int
    reasoning_hits: int
    step_markers: int
    has_code: bool
    has_tools: bool
    turns: int


@dataclass(frozen=True)
class Classification:
    tier: Tier
    source: str  # classifier|judge|default
    reason: str
    score: float


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI content parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def extract_signals(messages: list[dict[str, Any]], has_tools: bool, cfg: ClassifierConfig) -> Signals:
    user_msgs = [m for m in messages if m.get("role") == "user"]
    last_user = message_text(user_msgs[-1]) if user_msgs else ""
    lowered = last_user.lower()
    total_chars = sum(len(message_text(m)) for m in messages)
    return Signals(
        chars=total_chars,
        reasoning_hits=sum(1 for term in cfg.reasoning_terms if term in lowered),
        step_markers=len(_STEP_RE.findall(last_user)),
        has_code=bool(_CODE_RE.search(last_user)),
        has_tools=has_tools,
        turns=len(user_msgs),
    )


def score(signals: Signals, cfg: ClassifierConfig) -> float:
    w = cfg.weights
    features = {
        "length": min(signals.chars / max(cfg.length_saturation_chars, 1), 1.0),
        "reasoning_terms": min(signals.reasoning_hits / 3, 1.0),
        "multi_step": min(signals.step_markers / 4, 1.0),
        "code": 1.0 if signals.has_code else 0.0,
        "tools": 1.0 if signals.has_tools else 0.0,
        "conversation_depth": min(signals.turns / 6, 1.0),
    }
    total_weight = sum(w.get(k, 0.0) for k in features) or 1.0
    return round(sum(features[k] * w.get(k, 0.0) for k in features) / total_weight, 4)


class ComplexityClassifier:
    def __init__(self, redis: Redis | None) -> None:
        self._redis = redis

    async def classify(
        self, messages: list[dict[str, Any]], has_tools: bool, routing: RoutingConfig, judge: JudgeFn | None = None
    ) -> Classification:
        cfg = routing.classifier
        signals = extract_signals(messages, has_tools, cfg)
        s = score(signals, cfg)
        if s <= cfg.low_max_score:
            return Classification(Tier.LOW, "classifier", f"score {s} <= {cfg.low_max_score}", s)
        if s >= cfg.high_min_score:
            return Classification(Tier.HIGH, "classifier", f"score {s} >= {cfg.high_min_score}", s)

        if cfg.llm_judge.enabled and judge is not None:
            text = "\n".join(f"{m.get('role')}: {message_text(m)}" for m in messages[-4:])[-cfg.llm_judge.max_input_chars :]
            cache_key = rkey("route:judge:" + hashlib.sha256(text.encode()).hexdigest()[:40])
            if self._redis is not None and (cached := await self._redis.get(cache_key)):
                return Classification(Tier(cached), "judge", f"cached judge verdict (score {s})", s)
            verdict = await judge(text)
            if verdict is not None:
                if self._redis is not None:
                    await self._redis.set(cache_key, verdict.value, ex=cfg.llm_judge.cache_ttl_s)
                return Classification(verdict, "judge", f"llm judge (score {s})", s)

        return Classification(routing.default_tier, "default", f"ambiguous score {s}; default tier", s)
