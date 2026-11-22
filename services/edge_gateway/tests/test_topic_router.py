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
    choice = await choose_topic("hello there, how are you?", llm=None)
    assert choice.topic is None and choice.method == "none"
