"""Picks the topic (agent service) for a free-form /chat prompt.

Keyword scoring from config/topics.yaml first (free, instant). Only when that is
ambiguous does a low-tier LLM classify -- through the model gateway like every
other call, so it is tracked and billed the same way.
"""
import re
from dataclasses import dataclass

from pydantic import BaseModel

from agency_platform.config.registry import prompt_store, topics
from agency_platform.errors import AppError
from agency_platform.llm.client import ModelGatewayClient


@dataclass(frozen=True)
class TopicChoice:
    topic: str | None
    confidence: float
    method: str  # keywords|smalltalk|follow-up|llm|llm-smalltalk|default|none
    scores: dict[str, int]
    smalltalk: bool = False   # answer briefly, no assistant workflow (topic may be None → caller picks one)


SMALLTALK = "smalltalk"   # the routing model's answer for greetings / "who are you" / thanks


def is_smalltalk(prompt: str) -> bool:
    """Pure (given the config): short, matches a small-talk phrase, and has no assistant keyword."""
    cfg = topics().routing.smalltalk
    if not cfg.enabled:
        return False
    text = re.sub(r"[^\w\s'-]", " ", prompt.lower())
    words = text.split()
    if not words or len(words) > cfg.max_words or any(keyword_scores(prompt).values()):
        return False
    padded = f" {' '.join(words)} "
    return any(f" {p.lower()} " in padded for p in cfg.phrases)


class _LLMVerdict(BaseModel):
    topic: str
    confidence: float = 0.5


def keyword_scores(prompt: str) -> dict[str, int]:
    text = prompt.lower()
    scores: dict[str, int] = {}
    for name, cfg in topics().enabled().items():
        hits = 0
        for kw in cfg.keywords:
            pattern = r"\b" + re.escape(kw.lower()) + r"\b"
            hits += len(re.findall(pattern, text))
        scores[name] = hits
    return scores


async def choose_topic(prompt: str, llm: ModelGatewayClient | None, last_topic: str | None = None) -> TopicChoice:
    """keywords (clear winner) → small-talk shortcut → follow-up stays with the chat's assistant → routing model
    (which may also say "smalltalk") → default. Only the routing model costs a call."""
    cfg = topics()
    scores = keyword_scores(prompt)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if ranked and ranked[0][1] >= cfg.routing.keyword_min_hits:
        best, best_hits = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if best_hits > runner_up:  # clear winner
            confidence = round(min(0.5 + 0.15 * (best_hits - runner_up), 0.95), 2)
            return TopicChoice(best, confidence, "keywords", scores)

    enabled_now = cfg.enabled()
    follow = last_topic if last_topic in enabled_now else None
    if is_smalltalk(prompt):
        return TopicChoice(follow, 0.9, "smalltalk", scores, smalltalk=True)
    if follow and cfg.routing.sticky_follow_ups:
        return TopicChoice(follow, 0.6, "follow-up", scores)

    if cfg.routing.llm_fallback and llm is not None:
        enabled = cfg.enabled()
        catalogue = "\n".join(f"- {name}: {t.description}" for name, t in enabled.items())
        p = prompt_store().get("edge", "topic_classifier")
        try:
            verdict = await llm.structured(
                [
                    {"role": "system", "content": p.system.format(topics=catalogue)},
                    {"role": "user", "content": prompt[:2000]},
                ],
                _LLMVerdict,
                task="topic_classification",
                prompt=p,
                max_escalations=0,
            )
            if verdict.topic in enabled:
                return TopicChoice(verdict.topic, round(verdict.confidence, 2), "llm", scores)
            if verdict.topic == SMALLTALK:
                return TopicChoice(follow, round(verdict.confidence, 2), "llm-smalltalk", scores, smalltalk=True)
        except AppError:
            pass  # fall through to default

    if cfg.routing.default_topic:
        return TopicChoice(cfg.routing.default_topic, 0.3, "default", scores)
    return TopicChoice(None, 0.0, "none", scores)
