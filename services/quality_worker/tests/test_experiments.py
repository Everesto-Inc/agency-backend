"""Config tests: each case runs live and with the draft, both judged the same way; progress, cancel, failure."""
from types import SimpleNamespace

from quality import experiments
from quality.checks import RunOutput

from agency_platform.evals import EvalCase, to_jsonl

CASES = [EvalCase(id=f"c{i}", topic="ev_research", prompt=f"q{i}", checks={"must_contain": ["km"]}) for i in range(3)]


class Repo:
    def __init__(self, cancel_after=None):
        self.saved, self.final, self.cancel_after = [], None, cancel_after
        self.queue = [7]

    async def progress(self, test_id, done, entries):
        self.saved.append(done)
        return "cancelling" if self.cancel_after and done >= self.cancel_after else "running"

    async def finish(self, test_id, *, status, summary, results, error=None):
        self.final = SimpleNamespace(status=status, summary=summary, results=results, error=error)

    async def claim_next(self):
        return self.queue.pop() if self.queue else None

    async def get(self, test_id, with_results=True):
        return {"id": test_id, "draft_id": 12, "dataset_id": 1, "max_cases": 2}


async def run(case, draft_id):
    # the draft answers better (mentions km) and uses another model
    answer = "570 km" if draft_id else "about 570"
    return RunOutput(status="success", answer=answer, latency_s=1.0 if draft_id else 2.0, cost_usd=0.002,
                     models=["gemini-flash" if draft_id else "gemini-flash-lite"])


async def judge(case, out):
    return SimpleNamespace(overall=4.5 if "km" in out.answer else 3.0, scores=lambda: {}, rationale="ok")


async def test_live_vs_draft_side_by_side():
    repo = Repo()
    status = await experiments.run_test({"id": 1, "draft_id": 12}, CASES, run, judge, repo, concurrency=2)
    assert status == "done" and repo.final.status == "done" and sorted(repo.saved) == [1, 2, 3]
    live, draft = repo.final.summary["live"], repo.final.summary["draft"]
    assert (live["pass_rate"], draft["pass_rate"]) == (0.0, 1.0)
    assert draft["mean_judge_score"] == 4.5 and draft["models"] == {"gemini-flash": 3}
    assert live["p95_latency_ms"] == 2000 and draft["p95_latency_ms"] == 1000
    entry = repo.final.results[0]
    assert set(entry["variants"]) == {"live", "draft"} and entry["variants"]["draft"]["answer"] == "570 km"


async def test_benchmark_without_a_draft_runs_live_only():
    repo = Repo()
    await experiments.run_test({"id": 1, "draft_id": None}, CASES[:1], run, judge, repo)
    assert set(repo.final.summary) == {"live"}


async def test_cancel_stops_after_the_current_case():
    repo = Repo(cancel_after=1)
    status = await experiments.run_test({"id": 1, "draft_id": 12}, CASES, run, judge, repo, concurrency=1)
    assert status == "cancelled" and len(repo.final.results) == 1


async def test_run_pending_loads_the_dataset_and_records_failures():
    repo = Repo()
    datasets = SimpleNamespace(get=lambda _id: _async({"object_key": "datasets/x/v1.jsonl"}))
    store = SimpleNamespace(get=lambda key: _async((to_jsonl(CASES), "application/x-ndjson")))
    assert await experiments.run_pending(repo, datasets, store, run, judge, concurrency=2) == [7]
    assert repo.final.status == "done" and len(repo.final.results) == 2              # max_cases = 2

    repo = Repo()
    broken = SimpleNamespace(get=lambda key: _async_raise(RuntimeError("bucket down")))
    await experiments.run_pending(repo, datasets, broken, run, judge, concurrency=2)
    assert repo.final.status == "failed" and "bucket down" in repo.final.error


async def _async(v):
    return v


async def _async_raise(exc):
    raise exc
