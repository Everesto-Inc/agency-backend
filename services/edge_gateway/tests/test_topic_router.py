import json
from pathlib import Path

import pytest

from edge_gateway.topic_router import choose_topic, is_smalltalk

DATASET = Path(__file__).resolve().parents[3] / "evals" / "datasets" / "topic_routing.jsonl"
CASES = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("case", [c for c in CASES if c.get("keyword_routable", True)], ids=lambda c: c["prompt"][:40])
async def test_keyword_routing_golden_set(case):
    choice = await choose_topic(case["prompt"], llm=None)
    assert choice.topic == case["expected_topic"], choice


async def test_unroutable_prompt_without_llm_returns_none():
    choice = await choose_topic("tell me something interesting about volcanoes", llm=None)
    assert choice.topic is None and choice.method == "none"


@pytest.mark.parametrize("text", ["who are you?", "Hi!", "danke", "what can you do", "Bonjour", "hello there, how are you?"])
def test_obvious_smalltalk(text):
    assert is_smalltalk(text)


@pytest.mark.parametrize("text", [
    "hi, cheapest Model Y wallbox?",                       # keywords → a real question
    "who are you and is Tesla stock cheap?",
    "tell me something interesting about volcanoes",        # no phrase
    "hello " + "word " * 12,                                # too long
])
def test_not_smalltalk(text):
    assert not is_smalltalk(text)


async def test_smalltalk_shortcut_keeps_the_chats_assistant():
    c = await choose_topic("thanks!", llm=None, last_topic="finance")
    assert (c.topic, c.method, c.smalltalk) == ("finance", "smalltalk", True)
    c = await choose_topic("who are you", llm=None)                 # new chat: the edge picks one
    assert c.topic is None and c.smalltalk


async def test_follow_up_stays_without_a_routing_call():
    class NoLLM:
        async def structured(self, *a, **k):
            raise AssertionError("no routing call for a follow-up")
    c = await choose_topic("and in winter?", llm=NoLLM(), last_topic="ev_research")
    assert (c.topic, c.method) == ("ev_research", "follow-up")


async def test_tie_stays_with_the_chats_assistant():
    # "tesla" (EV) + "stock" (finance): not a clear switch → the chat's assistant keeps it
    c = await choose_topic("is Tesla stock expensive?", llm=None, last_topic="ev_research")
    assert c.topic == "ev_research"


async def test_clear_keywords_switch_away_from_the_chats_assistant():
    c = await choose_topic("What is the dividend of Microsoft stock?", llm=None, last_topic="ev_research")
    assert c.topic == "finance" and c.method == "keywords"


async def test_routing_model_can_say_smalltalk():
    class LLM:
        async def structured(self, messages, schema, **k):
            assert "smalltalk" in messages[0]["content"]
            return schema(topic="smalltalk", confidence=0.9)
    c = await choose_topic("could you introduce yourself briefly", llm=LLM())
    assert c.smalltalk and c.method == "llm-smalltalk"
