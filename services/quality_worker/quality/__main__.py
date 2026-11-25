"""Quality jobs CLI.

    python -m quality offline [--topic T ...] [--tags smoke] [--label pr-123] [--gate] [--update-baseline]
    python -m quality online          # judge sampled production runs        (CronJob, hourly)
    python -m quality drift           # drift report + webhook alert          (CronJob, hourly)
    python -m quality export-cases    # approved feedback cases -> datasets   (manual / CI)
    python -m quality retention       # apply config/privacy.yaml retention   (CronJob, daily)
    python -m quality smoke           # live end-to-end check of the stack    (after deploy)
"""
import argparse
import asyncio
import os
import sys

import httpx

from agency_platform.config.registry import evals, privacy, prompt_store, topics
from agency_platform.db import close_db, init_db
from agency_platform.observability.logging import configure_logging
from agency_platform.repositories import ConfigTestRepository, DatasetRepository, EvalRepository, RetentionRepository
from agency_platform.tools.images import ImageStore
from agency_platform.tools.objectstore import ObjectStore
from quality import drift, experiments, maintenance, offline, online
from quality.client import AgencyClient
from quality.datasets import load_cases
from quality.gate import evaluate_gate, load_baseline, markdown, save_baseline
from quality.judge import Judge
from quality.settings import settings
from quality.smoke import run_smoke


async def _offline(args: argparse.Namespace, http: httpx.AsyncClient) -> int:
    s = settings()
    cfg = evals()
    client = AgencyClient(s, http)
    judge = Judge(await client.gateway(), prompt_store(), cfg.offline.judge_task)
    names = args.topic or list(topics().enabled())
    repo = EvalRepository(await init_db(s)) if args.persist else None
    gate_results, summaries, exit_code = [], {}, 0

    for topic in names:
        cases = load_cases(s.evals_dir, topic, args.tags)
        if not cases:
            print(f"[{topic}] no cases in {s.evals_dir}/datasets/{topic}.jsonl -- skipped")
            continue

        async def run(case, _t=topic):
            return await client.run_topic(_t, case.prompt, cfg.offline.request_timeout_s)

        async def score(case, out, _t=topic):
            return await judge.judge(topic=_t, question=case.prompt, answer=out.answer, artifacts=out.artifacts,
                                     rubric=case.rubric, reference=case.reference)

        report = await offline.run_suite(topic, cases, run, score, concurrency=cfg.offline.concurrency,
                                         git_sha=s.git_sha, label=args.label)
        path = offline.write_report(report, s.evals_dir / "reports")
        summary = report.summary()
        summaries[topic] = summary
        gate = evaluate_gate(summary, load_baseline(s.evals_dir, topic), cfg.gate) if args.gate else None
        if gate:
            gate_results.append(gate)
            exit_code |= 0 if gate.passed else 1
        if repo is not None:
            await offline.persist(repo, report, gate.passed if gate else None)
        if args.update_baseline:
            save_baseline(s.evals_dir, summary)
        print(f"[{topic}] pass {summary['pass_rate']:.0%}  judge {summary['mean_judge_score']}  "
              f"${summary['mean_cost_usd']:.4f}/case  p95 {summary['p95_latency_ms']}ms  -> {path}")

    if gate_results:
        md = markdown(gate_results, summaries)
        print("\n" + md)
        if out := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(out, "a") as f:  # noqa: ASYNC230 - tiny one-shot CI write
                f.write(md + "\n")
    return exit_code


async def _online(http: httpx.AsyncClient) -> int:
    s = settings()
    repo = EvalRepository(await init_db(s))
    client = AgencyClient(s, http)
    judge = Judge(await client.gateway(), prompt_store(), evals().offline.judge_task)

    async def score(row: dict):
        return await judge.judge(topic=row["topic"], question=row["prompt"], answer=row["answer"])

    print(await online.judge_pending(repo, score, evals().online.max_judged_per_job))
    return 0


async def _drift(http: httpx.AsyncClient) -> int:
    s = settings()
    cfg = evals().drift
    reports = await drift.detect(EvalRepository(await init_db(s)), list(topics().enabled()), cfg)
    for r in reports:
        state = "FLAGGED " + ",".join(r.flagged) if r.flagged else ("ok" if r.enough_data else "insufficient data")
        print(f"[{r.topic}] {state}")
    url = s.alert_webhook_url.get_secret_value() if s.alert_webhook_url else os.environ.get(cfg.alert_webhook_env)
    await drift.alert(http, url, reports)
    return 0


async def _experiments(http: httpx.AsyncClient) -> int:
    """Admin → Experiments: run queued config tests (dataset × live vs draft)."""
    s = settings()
    cfg = evals()
    db = await init_db(s)
    store = ObjectStore.from_settings(s, http)
    if store is None:
        print("experiments: no bucket configured (S3_*) — nothing to do")
        return 0
    client = AgencyClient(s, http)
    judge = Judge(await client.gateway(), prompt_store(), cfg.offline.judge_task)

    async def run(case, draft_id):
        return await client.run_topic(case.topic, case.prompt, cfg.offline.request_timeout_s, draft_id=draft_id)

    async def score(case, out):
        return await judge.judge(topic=case.topic, question=case.prompt, answer=out.answer, artifacts=out.artifacts,
                                 rubric=case.rubric, reference=case.reference)

    done = await experiments.run_pending(ConfigTestRepository(db), DatasetRepository(db), store, run, score,
                                         concurrency=cfg.offline.concurrency)
    print(f"experiments: ran {len(done)} test(s) {done}")
    return 0


async def _main(args: argparse.Namespace) -> int:
    s = settings()
    configure_logging(s.service_name, s.log_level, s.log_json)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as http:
        try:
            if args.cmd == "offline":
                return await _offline(args, http)
            if args.cmd == "online":
                return await _online(http)
            if args.cmd == "drift":
                return await _drift(http)
            if args.cmd == "export-cases":
                print(await maintenance.export_approved_cases(EvalRepository(await init_db(s)), s.evals_dir))
                return 0
            if args.cmd == "retention":
                db = await init_db(s)
                result = await RetentionRepository(db).apply(privacy().retention)
                # pictures users sent: same lifetime as the question text (bucket object + row)
                result["uploaded_pictures"] = await ImageStore(db, http, ObjectStore.from_settings(s, http)) \
                    .delete_uploads_older_than(privacy().retention.run_text_days)
                print(result)
                return 0
            if args.cmd == "smoke":
                return await run_smoke(s, http)
            if args.cmd == "experiments":
                return await _experiments(http)
        finally:
            await close_db()
    return 2


def main() -> None:
    p = argparse.ArgumentParser(prog="quality", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    off = sub.add_parser("offline", help="run dataset evals against the live services")
    off.add_argument("--topic", action="append")
    off.add_argument("--tags", nargs="*")
    off.add_argument("--label")
    off.add_argument("--gate", action="store_true", help="fail (exit 1) on regression vs evals/baselines")
    off.add_argument("--update-baseline", action="store_true")
    off.add_argument("--no-persist", dest="persist", action="store_false", help="don't write eval_runs to Postgres")
    for name in ("online", "drift", "export-cases", "retention", "smoke", "experiments"):
        sub.add_parser(name)
    sys.exit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    main()
