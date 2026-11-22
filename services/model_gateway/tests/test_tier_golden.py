"""Offline eval: the free heuristic stage must send clear-cut prompts to the
right tier (ambiguous ones are the LLM judge's job and are not asserted here)."""
import json
from pathlib import Path

import pytest

from agency_platform.config.params import ParamFile
from agency_platform.llm.tiers import Tier
from model_gateway.catalog import ModelsFile
from model_gateway.classifier import ComplexityClassifier

ROOT = Path(__file__).resolve().parents[3]
CASES = [json.loads(x) for x in (ROOT / "evals/datasets/model_tiers.jsonl").read_text().splitlines() if x.strip()]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["prompt"][:30])
async def test_heuristic_tiering_golden_set(case):
    routing = ParamFile(ROOT / "config/models.yaml", ModelsFile).get().routing
    text = case["prompt"] + (" context" * (case.get("pad_chars", 0) // 8))
    result = await ComplexityClassifier(None).classify([{"role": "user", "content": text}], False, routing)
    assert result.tier == Tier(case["expected_tier"]), result
