"""Deterministic answer checks (no LLM)."""
import re
from dataclasses import dataclass, field

from quality.datasets import Checks

_URL = re.compile(r"https?://\S+")


@dataclass
class RunOutput:
    status: str
    answer: str
    latency_s: float
    cost_usd: float
    total_tokens: int = 0
    request_id: str | None = None
    artifacts: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)


def run_checks(checks: Checks, out: RunOutput) -> list[str]:
    failures: list[str] = []
    text = out.answer.lower()

    if checks.expect_refusal:
        if out.answer and out.status == "success":
            failures.append("expected a refusal but the agent answered")
        return failures
    if out.status != "success" or not out.answer:
        failures.append(f"run did not succeed (status={out.status})")
        return failures

    failures += [f"missing required text: {s!r}" for s in checks.must_contain if s.lower() not in text]
    if checks.must_contain_any and not any(s.lower() in text for s in checks.must_contain_any):
        failures.append(f"none of {checks.must_contain_any!r} present")
    failures += [f"contains forbidden text: {s!r}" for s in checks.must_not_contain if s.lower() in text]
    if checks.require_sources and not _URL.search(out.answer):
        failures.append("no source URL cited")
    if checks.max_latency_s is not None and out.latency_s > checks.max_latency_s:
        failures.append(f"latency {out.latency_s:.1f}s > {checks.max_latency_s}s")
    if checks.max_cost_usd is not None and out.cost_usd > checks.max_cost_usd:
        failures.append(f"cost ${out.cost_usd:.4f} > ${checks.max_cost_usd}")
    return failures
