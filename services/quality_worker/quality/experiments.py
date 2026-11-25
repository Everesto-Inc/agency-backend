"""Config tests (Admin → Experiments): a dataset × the LIVE config and a DRAFT, side by side.

The edge queues a test (config_tests); this job (CronJob agency-experiments, every 2 min) claims queued tests one
by one and runs every case through the assistants as the eval service account — once normally, once with the
draft header — checks + judges both answers (the judge never uses the draft: same yardstick for both), saves
progress after each case and stops if an admin cancels.
"""
import asyncio
import logging
import statistics
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from agency_platform.evals import EvalCase, parse_jsonl
from quality.checks import RunOutput
from quality.offline import CaseResult, evaluate_case

logger = logging.getLogger(__name__)

RunFn = Callable[[EvalCase, int | None], Awaitable[RunOutput]]
JudgeFn = Callable[[EvalCase, RunOutput], Awaitable[Any]]
ANSWER_EXCERPT = 1500


def summarize(entries: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    """Pure: per-variant summary over the finished cases."""
    rows = [e["variants"][variant] for e in entries if variant in e["variants"]]
    if not rows:
        return {"cases": 0}
    judged = [r["judge_score"] for r in rows if r["judge_score"] is not None]
    lat = sorted(r["latency_ms"] for r in rows)
    models: Counter[str] = Counter(m for r in rows for m in r["models"])
    return {
        "cases": len(rows),
        "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 4),
        "mean_judge_score": round(statistics.fmean(judged), 3) if judged else None,
        "mean_cost_usd": round(statistics.fmean(r["cost_usd"] for r in rows), 6),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "p95_latency_ms": lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))],
        "models": dict(models.most_common()),
    }


def _variant(result: CaseResult, out: RunOutput) -> dict[str, Any]:
    return {"passed": result.passed, "judge_score": result.judge_score, "failures": result.failures,
            "rationale": result.rationale[:500], "cost_usd": result.cost_usd, "latency_ms": result.latency_ms,
            "models": out.models, "status": out.status, "answer": out.answer[:ANSWER_EXCERPT]}


async def run_test(test: dict[str, Any], cases: list[EvalCase], run: RunFn, judge: JudgeFn, repo, *,
                   concurrency: int = 3) -> str:
    """Runs one claimed test; returns its final status (done / cancelled)."""
    variants = [("live", None)] + ([("draft", test["draft_id"])] if test["draft_id"] else [])
    entries: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(concurrency)
    cancelled = asyncio.Event()
    lock = asyncio.Lock()

    async def one(case: EvalCase) -> None:
        async with sem:
            if cancelled.is_set():
                return
            entry: dict[str, Any] = {"case_id": case.id, "topic": case.topic, "prompt": case.prompt[:500], "variants": {}}
            for name, draft_id in variants:            # one after the other: same moment, fair comparison
                captured: dict[str, RunOutput] = {}

                async def run_case(c: EvalCase, _d=draft_id, _cap=captured) -> RunOutput:
                    _cap["out"] = await run(c, _d)
                    return _cap["out"]
                result = await evaluate_case(case, run_case, judge)
                entry["variants"][name] = _variant(result, captured["out"])
            async with lock:
                entries.append(entry)
                status = await repo.progress(test["id"], len(entries), entries)
                if status == "cancelling":
                    cancelled.set()

    await asyncio.gather(*(one(c) for c in cases))
    summary = {name: summarize(entries, name) for name, _ in variants}
    final = "cancelled" if cancelled.is_set() else "done"
    await repo.finish(test["id"], status=final, summary=summary, results=entries)
    return final


async def run_pending(repo, datasets, store, run: RunFn, judge: JudgeFn, *, concurrency: int, max_tests: int = 3) -> list[int]:
    """Claim and run queued tests (at most max_tests per job run)."""
    done = []
    for _ in range(max_tests):
        test_id = await repo.claim_next()
        if test_id is None:
            break
        test = await repo.get(test_id, with_results=False)
        try:
            ds = await datasets.get(test["dataset_id"])
            data, _ = await store.get(ds["object_key"])
            cases = parse_jsonl(data).cases[: test["max_cases"]]
            status = await run_test(test, cases, run, judge, repo, concurrency=concurrency)
            logger.info("config test finished", extra={"test_id": test_id, "status": status, "cases": len(cases)})
        except Exception as exc:
            logger.exception("config test failed", extra={"test_id": test_id})
            await repo.finish(test_id, status="failed", summary=None, results=None, error=f"{type(exc).__name__}: {exc}")
        done.append(test_id)
    return done
