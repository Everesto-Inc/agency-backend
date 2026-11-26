"""Triage: what is this question, and who should handle it?

One fast call (cheap model, thinking off) reads the question AND the last messages of the chat and decides:
  direct          a short answer is enough (greeting, "who are you", thanks, a simple general question) →
                  the assistant answers with one short call, no research workflow
  <assistant>     EV Helper / Product Finder / Finance Analyst → its full workflow (data, web, calculations)

Nothing hard-coded decides: any wording, any language; follow-ups are judged from the conversation.
Keywords (config/topics.yaml) are only the safety net when triage fails or is too slow, so routing never hangs.
"""
import asyncio
import re
from dataclasses import dataclass

from pydantic import BaseModel

from agency_platform.config.registry import prompt_store, scope, topics
from agency_platform.errors import AppError
from agency_platform.llm.client import ModelGatewayClient

DIRECT = "direct"
OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True)
class TopicChoice:
    topic: str | None
    confidence: float
    method: str  # triage|triage-direct|keywords|follow-up|default|none
    scores: dict[str, int]
    direct: bool = False   # one short answer, no assistant workflow (topic may be None → the caller picks one)
    reason: str = ""
    out_of_scope: bool = False   # outside config/scope.yaml → polite refusal (topic may be None → the caller picks one)


class _Triage(BaseModel):
    route: str
    confidence: float = 0.5
    reason: str = ""


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


def conversation_lines(history: list[dict[str, str]] | None) -> str:
    """Pure: the last messages as short lines for the triage prompt."""
    lines = [f"{m['role']} ({m.get('topic') or '-'}): {m['content'][:300]}"
             for m in (history or [])[-4:] if m.get("role") in ("user", "assistant")]
    return "\n".join(lines) or "(new conversation)"


async def triage(prompt: str, llm: ModelGatewayClient, history: list[dict[str, str]] | None,
                 last_topic: str | None) -> _Triage:
    cfg = topics()
    enabled = cfg.enabled()
    sc = scope()
    domains = "\n".join(f"- {d.id} ({d.label}): {d.covers}" for d in sc.domains if d.id in enabled)
    p = prompt_store().get("edge", "triage")
    return await asyncio.wait_for(llm.structured(
        [{"role": "system", "content": p.system.format(domains=domains, not_allowed="; ".join(sc.not_allowed) or "-")},
         {"role": "user", "content": f"Conversation so far (last assistant: {last_topic or 'none'}):\n"
                                     f"{conversation_lines(history)}\n\nNew message:\n{prompt[:2000]}"}],
        _Triage, task="topic_classification", prompt=p, max_escalations=0,
    ), timeout=cfg.routing.triage_timeout_s)


def by_keywords(prompt: str, last_topic: str | None) -> TopicChoice | None:
    """Safety net only: a clear keyword winner, else the chat's last assistant."""
    cfg = topics()
    scores = keyword_scores(prompt)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if ranked and ranked[0][1] >= cfg.routing.keyword_min_hits:
        best, best_hits = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if best_hits > runner_up:
            return TopicChoice(best, round(min(0.5 + 0.15 * (best_hits - runner_up), 0.95), 2), "keywords", scores)
    if last_topic in cfg.enabled():
        return TopicChoice(last_topic, 0.4, "follow-up", scores)
    return None


async def choose_topic(prompt: str, llm: ModelGatewayClient | None, last_topic: str | None = None,
                       history: list[dict[str, str]] | None = None) -> TopicChoice:
    cfg = topics()
    enabled = cfg.enabled()
    follow = last_topic if last_topic in enabled else None
    if cfg.routing.triage and llm is not None:
        try:
            t = await triage(prompt, llm, history, follow)
            if t.route == DIRECT:
                return TopicChoice(follow, round(t.confidence, 2), "triage-direct", {}, direct=True, reason=t.reason)
            if t.route == OUT_OF_SCOPE:
                return TopicChoice(follow, round(t.confidence, 2), "triage-out-of-scope", {}, out_of_scope=True,
                                   reason=t.reason)
            if t.route in enabled:
                return TopicChoice(t.route, round(t.confidence, 2), "triage", {}, reason=t.reason)
        except (AppError, TimeoutError):
            pass  # safety net below — routing never hangs on the triage call
    if (choice := by_keywords(prompt, follow)) is not None:
        return choice
    if cfg.routing.default_topic:
        return TopicChoice(cfg.routing.default_topic, 0.3, "default", keyword_scores(prompt))
    return TopicChoice(None, 0.0, "none", keyword_scores(prompt))
