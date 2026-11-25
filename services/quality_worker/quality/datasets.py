"""Golden datasets: evals/datasets/<topic>.jsonl, one EvalCase per line."""
import json
from pathlib import Path

from agency_platform.evals import Checks, EvalCase  # noqa: F401 — schema shared with the edge


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
