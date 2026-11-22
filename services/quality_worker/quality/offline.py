"""Offline evaluation: run every dataset case against the real topic service,
apply deterministic checks, score with the LLM judge, aggregate, persist."""
import asyncio
import json
import logging
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agency_platform.errors import AppError
from agency_platform.repositories import EvalRepository
from quality.checks import RunOutput, run_checks
from quality.datasets import EvalCase
from quality.judge import JudgeVerdict

logger = logging.getLogger(__name__)

RunFn = Callable[[EvalCase], Awaitable[RunOutput]]
JudgeFn = Callable[[EvalCase, RunOutput], Awaitable[JudgeVerdict | None]]


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    judge_score: float | None
    scores: dict[str, int]
    failures: list[str]
    rationale: str
    cost_usd: float
    latency_ms: int
    request_id: str | None


@dataclass
class Report:
    topic: str
    created_at: str
    git_sha: str | None
    label: str | None
    results: list[CaseResult] = field(default_factory=list)

    @property
    def cases(self) -> int:
        return len(self.results)

    @property
    def pass_rate(self) -> float:
        return round(sum(r.passed for r in self.results) / self.cases, 4) if self.cases else 0.0

    @property
    def mean_judge_score(self) -> float | None:
        s = [r.judge_score for r in self.results if r.judge_score is not None]
        return round(statistics.fmean(s), 3) if s else None

    @property
    def mean_cost_usd(self) -> float:
        return round(statistics.fmean([r.cost_usd for r in self.results]), 6) if self.results else 0.0

    @property
    def p95_latency_ms(self) -> int:
        lat = sorted(r.latency_ms for r in self.results)
        return lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))] if lat else 0

    def summary(self) -> dict:
        return {
            "topic": self.topic, "cases": self.cases, "pass_rate": self.pass_rate,
            "mean_judge_score": self.mean_judge_score, "mean_cost_usd": self.mean_cost_usd,
            "p95_latency_ms": self.p95_latency_ms, "git_sha": self.git_sha, "label": self.label,
            "created_at": self.created_at,
        }

    def to_json(self) -> dict:
        return {"summary": self.summary(), "results": [asdict(r) for r in self.results]}


async def evaluate_case(case: EvalCase, run: RunFn, judge: JudgeFn) -> CaseResult:
    out = await run(case)
    failures = run_checks(case.checks, out)
    verdict: JudgeVerdict | None = None
    if not case.checks.expect_refusal and out.status == "success" and out.answer:
        try:
            verdict = await judge(case, out)
        except AppError as exc:
            failures.append(f"judge unavailable: {exc.message}")
    if verdict is not None and verdict.overall < case.checks.min_judge_score:
        failures.append(f"judge score {verdict.overall} < {case.checks.min_judge_score}")
    return CaseResult(
        case_id=case.id,
        passed=not failures,
        judge_score=verdict.overall if verdict else None,
        scores=verdict.scores() if verdict else {},
        failures=failures,
        rationale=verdict.rationale if verdict else "",
        cost_usd=out.cost_usd,
        latency_ms=int(out.latency_s * 1000),
        request_id=out.request_id,
    )


async def run_suite(
    topic: str, cases: list[EvalCase], run: RunFn, judge: JudgeFn, *, concurrency: int,
    git_sha: str | None = None, label: str | None = None,
) -> Report:
    sem = asyncio.Semaphore(concurrency)

    async def one(c: EvalCase) -> CaseResult:
        async with sem:
            result = await evaluate_case(c, run, judge)
            logger.info("eval case done", extra={"case": c.id, "passed": result.passed, "score": result.judge_score})
            return result

    report = Report(topic=topic, created_at=datetime.now(UTC).isoformat(), git_sha=git_sha, label=label)
    report.results = list(await asyncio.gather(*(one(c) for c in cases)))
    return report


def write_report(report: Report, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.created_at.replace(":", "").replace("-", "")[:15]
    path = reports_dir / f"{report.topic}-{stamp}.json"
    path.write_text(json.dumps(report.to_json(), indent=2))
    return path


async def persist(repo: EvalRepository, report: Report, gate_passed: bool | None) -> int:
    return await repo.save_offline_run(report.summary(), gate_passed, [asdict(r) for r in report.results])
