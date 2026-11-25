"""Eval cases (one JSON object per line in a dataset) — shared by the quality worker (runs them) and the edge
(Admin → Experiments: upload, check, preview)."""
import json
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

MAX_DATASET_BYTES = 2_000_000
MAX_DATASET_CASES = 500


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
    id: str = Field(..., min_length=1, max_length=100)
    topic: str
    prompt: str = Field(..., min_length=1, max_length=5000)
    checks: Checks = Checks()
    rubric: str | None = Field(None, description="What a good answer must do, for the judge")
    reference: str | None = Field(None, description="Known-good facts/answer the judge may compare against")
    tags: list[str] = Field(default_factory=list)


@dataclass
class ParsedDataset:
    cases: list[EvalCase] = field(default_factory=list)
    errors: list[dict[str, object]] = field(default_factory=list)   # {"line": n, "message": "..."}

    @property
    def topics(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.cases:
            out[c.topic] = out.get(c.topic, 0) + 1
        return out


def parse_jsonl(data: bytes, known_topics: set[str] | None = None, max_errors: int = 20) -> ParsedDataset:
    """Pure: JSONL bytes → cases + per-line errors (bad JSON, schema, unknown topic, duplicate id, too many)."""
    out = ParsedDataset()

    def err(line: int, msg: str) -> None:
        if len(out.errors) < max_errors:
            out.errors.append({"line": line, "message": msg})

    if len(data) > MAX_DATASET_BYTES:
        err(0, f"file larger than {MAX_DATASET_BYTES // 1_000_000} MB")
        return out
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        err(0, "not UTF-8 text")
        return out
    seen: set[str] = set()
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = EvalCase.model_validate(json.loads(line))
        except json.JSONDecodeError as exc:
            err(n, f"not JSON: {exc.msg}")
            continue
        except ValidationError as exc:
            e = exc.errors()[0]
            err(n, f"{'.'.join(str(p) for p in e['loc']) or 'case'}: {e['msg']}")
            continue
        if known_topics is not None and case.topic not in known_topics:
            err(n, f"unknown topic '{case.topic}' (known: {', '.join(sorted(known_topics))})")
            continue
        if case.id in seen:
            err(n, f"duplicate id '{case.id}'")
            continue
        seen.add(case.id)
        out.cases.append(case)
    if len(out.cases) > MAX_DATASET_CASES:
        err(0, f"more than {MAX_DATASET_CASES} cases")
    if not out.cases and not out.errors:
        err(0, "no cases in the file")
    return out


def to_jsonl(cases: list[EvalCase]) -> bytes:
    return "".join(c.model_dump_json(exclude_defaults=True) + "\n" for c in cases).encode()
