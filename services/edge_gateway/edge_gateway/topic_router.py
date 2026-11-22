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
    method: str  # keywords|llm|default|none
    scores: dict[str, int]


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


async def choose_topic(prompt: str, llm: ModelGatewayClient | None) -> TopicChoice:
    cfg = topics()
    scores = keyword_scores(prompt)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if ranked and ranked[0][1] >= cfg.routing.keyword_min_hits:
        best, best_hits = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if best_hits > runner_up:  # clear winner
            confidence = round(min(0.5 + 0.15 * (best_hits - runner_up), 0.95), 2)
            return TopicChoice(best, confidence, "keywords", scores)

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
        except AppError:
            pass  # fall through to default

    if cfg.routing.default_topic:
        return TopicChoice(cfg.routing.default_topic, 0.3, "default", scores)
    return TopicChoice(None, 0.0, "none", scores)
