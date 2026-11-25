import json
from pathlib import Path

import pytest

from edge_gateway.topic_router import choose_topic

DATASET = Path(__file__).resolve().parents[3] / "evals" / "datasets" / "topic_routing.jsonl"
CASES = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("case", [c for c in CASES if c.get("keyword_routable", True)], ids=lambda c: c["prompt"][:40])
async def test_keyword_routing_golden_set(case):
    choice = await choose_topic(case["prompt"], llm=None)
    assert choice.topic == case["expected_topic"], choice


async def test_unroutable_prompt_without_llm_returns_none():
    choice = await choose_topic("tell me something interesting about volcanoes", llm=None)
    assert choice.topic is None and choice.method == "none"


class Triage:
    """A fake triage model: answers what the test says, records what it was shown."""
    def __init__(self, route, confidence=0.9, fail=None):
        self.route, self.confidence, self.fail, self.seen = route, confidence, fail, None

    async def structured(self, messages, schema, **kw):
        self.seen = messages
        if self.fail:
            raise self.fail
        return schema(route=self.route, confidence=self.confidence, reason="test")


async def test_triage_decides_direct_for_any_wording():
    llm = Triage("direct")
    c = await choose_topic("qui es-tu ?", llm=llm, last_topic="ev_research")
    assert (c.topic, c.method, c.direct) == ("ev_research", "triage-direct", True)
    assert "direct" in llm.seen[0]["content"] and "ev_research" in llm.seen[0]["content"]


async def test_triage_sees_the_conversation_for_follow_ups():
    llm = Triage("ev_research")
    history = [{"role": "user", "content": "Range of the Kia EV6?", "topic": "ev_research"},
               {"role": "assistant", "content": "About 528 km WLTP.", "topic": "ev_research"}]
    c = await choose_topic("and in winter?", llm=llm, last_topic="ev_research", history=history)
    assert (c.topic, c.method) == ("ev_research", "triage")
    assert "Range of the Kia EV6?" in llm.seen[1]["content"] and "and in winter?" in llm.seen[1]["content"]


async def test_unknown_route_or_failure_falls_back_to_keywords():
    from agency_platform.errors import UpstreamError
    c = await choose_topic("What is the dividend of Microsoft stock?", llm=Triage("weather"))
    assert (c.topic, c.method) == ("finance", "keywords")
    c = await choose_topic("and in winter?", llm=Triage("x", fail=UpstreamError("gateway down")), last_topic="ev_research")
    assert (c.topic, c.method) == ("ev_research", "follow-up")


async def test_slow_triage_never_blocks(monkeypatch):
    import asyncio

    from edge_gateway import topic_router as tr

    class Slow:
        async def structured(self, *a, **k):
            await asyncio.sleep(5)
    cfg = tr.topics()
    monkeypatch.setattr(cfg.routing, "triage_timeout_s", 0.05)
    c = await choose_topic("What is the dividend of Microsoft stock?", llm=Slow())
    assert c.method == "keywords"
