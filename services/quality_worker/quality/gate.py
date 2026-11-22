"""Regression gate: a change to prompts / models / topic code must not make
quality, cost or latency meaningfully worse than the committed baseline.

Baselines live in evals/baselines/<topic>.json (the summary of an accepted
run), versioned with the code -- promoting a new baseline is a reviewed change.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from agency_platform.config.schemas import GateConfig


@dataclass
class GateResult:
    topic: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    baseline_found: bool = True


def load_baseline(evals_dir: Path, topic: str) -> dict | None:
    path = evals_dir / "baselines" / f"{topic}.json"
    return json.loads(path.read_text()) if path.exists() else None


def save_baseline(evals_dir: Path, summary: dict) -> Path:
    path = evals_dir / "baselines" / f"{summary['topic']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2) + "\n")
    return path


def _pct(new: float, old: float) -> float:
    return (new - old) / old * 100 if old else 0.0


def evaluate_gate(current: dict, baseline: dict | None, cfg: GateConfig) -> GateResult:
    topic = current["topic"]
    reasons: list[str] = []
    if current["pass_rate"] < cfg.min_pass_rate:
        reasons.append(f"pass rate {current['pass_rate']:.0%} below floor {cfg.min_pass_rate:.0%}")
    if baseline is None:
        return GateResult(topic, not reasons, [*reasons, "no baseline yet (floor checks only)"], baseline_found=False)

    if baseline["pass_rate"] - current["pass_rate"] > cfg.max_pass_rate_drop:
        reasons.append(f"pass rate dropped {baseline['pass_rate']:.0%} -> {current['pass_rate']:.0%}")
    cur_j, base_j = current.get("mean_judge_score"), baseline.get("mean_judge_score")
    if cur_j is not None and base_j is not None and base_j - cur_j > cfg.max_judge_score_drop:
        reasons.append(f"judge score dropped {base_j:.2f} -> {cur_j:.2f}")
    if _pct(current["mean_cost_usd"], baseline["mean_cost_usd"]) > cfg.max_cost_increase_pct:
        reasons.append(
            f"cost per case up {_pct(current['mean_cost_usd'], baseline['mean_cost_usd']):.0f}% "
            f"(${baseline['mean_cost_usd']:.4f} -> ${current['mean_cost_usd']:.4f})"
        )
    if _pct(current["p95_latency_ms"], baseline["p95_latency_ms"]) > cfg.max_p95_latency_increase_pct:
        reasons.append(f"p95 latency {baseline['p95_latency_ms']}ms -> {current['p95_latency_ms']}ms")
    return GateResult(topic, not reasons, reasons)


def markdown(results: list[GateResult], summaries: dict[str, dict]) -> str:
    """PR-comment friendly summary."""
    lines = ["## LLM quality gate", "", "| Topic | Pass rate | Judge | $/case | p95 | Gate |", "|---|---|---|---|---|---|"]
    for r in results:
        s = summaries[r.topic]
        judge = f"{s['mean_judge_score']:.2f}" if s.get("mean_judge_score") is not None else "-"
        lines.append(f"| {r.topic} | {s['pass_rate']:.0%} | {judge} | ${s['mean_cost_usd']:.4f} | "
                     f"{s['p95_latency_ms']}ms | {'PASS' if r.passed else 'FAIL'} |")
    for r in results:
        for reason in r.reasons:
            lines.append(f"- **{r.topic}**: {reason}")
    return "\n".join(lines)
