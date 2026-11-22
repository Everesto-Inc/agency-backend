"""Golden datasets: evals/datasets/<topic>.jsonl, one EvalCase per line."""
import json
from pathlib import Path

from pydantic import BaseModel, Field


class Checks(BaseModel):
    """Deterministic expectations -- cheap, exact, run before the LLM judge."""

    must_contain: list[str] = Field(default_factory=list, description="all must appear (case-insensitive)")
    must_contain_any: list[str] = Field(default_factory=list, description="at least one must appear")
    must_not_contain: list[str] = Field(default_factory=list)
    expect_refusal: bool = False          # off-topic / unsafe: the agent should decline (error event, no answer)
    require_sources: bool = False         # answer must cite at least one URL
    max_latency_s: float | None = None
    max_cost_usd: float | None = None
    min_judge_score: float = 3.5          # 1-5 overall judge score needed to pass


class EvalCase(BaseModel):
    id: str
    topic: str
    prompt: str
    checks: Checks = Checks()
    rubric: str | None = Field(None, description="What a good answer must do, for the judge")
    reference: str | None = Field(None, description="Known-good facts/answer the judge may compare against")
    tags: list[str] = Field(default_factory=list)


def load_cases(evals_dir: Path, topic: str, tags: list[str] | None = None) -> list[EvalCase]:
    path = evals_dir / "datasets" / f"{topic}.jsonl"
    if not path.exists():
        return []
    cases = [EvalCase.model_validate(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    if tags:
        cases = [c for c in cases if set(tags) & set(c.tags)]
    return cases


def append_cases(evals_dir: Path, topic: str, cases: list[EvalCase]) -> int:
    path = evals_dir / "datasets" / f"{topic}.jsonl"
    existing = {c.id for c in load_cases(evals_dir, topic)}
    new = [c for c in cases if c.id not in existing]
    if new:
        with path.open("a", encoding="utf-8") as f:
            for c in new:
                f.write(c.model_dump_json(exclude_defaults=True) + "\n")
    return len(new)
