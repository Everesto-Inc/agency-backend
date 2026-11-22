"""Online evaluation: LLM-as-judge on a random sample of real production runs.

Runners flag ~sample_rate of successful runs (agent_runs.eval_sampled). This
job judges those not yet judged and stores the scores (eval_results with no
eval_run_id), which feed quality dashboards, drift detection, experiment and
canary comparisons.
"""
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agency_platform.errors import AppError
from agency_platform.repositories import EvalRepository
from quality.judge import JudgeVerdict

logger = logging.getLogger(__name__)

JudgeRun = Callable[[dict[str, Any]], Awaitable[JudgeVerdict]]
PASS_SCORE = 3.5


async def judge_pending(repo: EvalRepository, judge: JudgeRun, limit: int) -> dict[str, int]:
    rows = await repo.pending_online(limit)
    done = failed = 0
    for row in rows:
        try:
            verdict = await judge(row)
        except AppError as exc:
            failed += 1
            logger.warning("online judge failed", extra={"request_id": row["request_id"], "error": exc.message})
            continue
        await repo.save_online_result(
            topic=row["topic"], request_id=row["request_id"], passed=verdict.overall >= PASS_SCORE,
            judge_score=verdict.overall, scores=verdict.scores(), rationale=verdict.rationale,
            cost_usd=row["cost_usd"], latency_ms=row["latency_ms"],
        )
        done += 1
    return {"pending": len(rows), "judged": done, "failed": failed}
