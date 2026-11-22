"""Feedback -> dataset export (approved eval_cases -> evals/datasets/*.jsonl)."""
import logging
from pathlib import Path

from agency_platform.repositories import EvalRepository
from quality.datasets import Checks, EvalCase, append_cases

logger = logging.getLogger(__name__)


async def export_approved_cases(repo: EvalRepository, evals_dir: Path) -> dict[str, int]:
    """Approved eval_cases -> evals/datasets/<topic>.jsonl (dedup by id), then mark exported.

    Negative-feedback cases become regression tests: by default the answer must
    pass the judge; curators add must_contain / rubric etc. via `checks`."""
    rows = await repo.approved_cases()
    by_topic: dict[str, list[EvalCase]] = {}
    for r in rows:
        checks = dict(r["checks"] or {})
        rubric = checks.pop("rubric", None)
        reference = checks.pop("reference", None)
        by_topic.setdefault(r["topic"], []).append(EvalCase(
            id=f"fb-{r['id']}", topic=r["topic"], prompt=r["prompt"], checks=Checks.model_validate(checks),
            rubric=rubric or (f"User complaint to fix: {r['comment']}" if r["label"] == "negative" and r["comment"] else None),
            reference=reference, tags=["feedback", r["label"]],
        ))
    written: dict[str, int] = {}
    for topic, cases in by_topic.items():
        written[topic] = append_cases(evals_dir, topic, cases)
    await repo.mark_exported([r["id"] for r in rows])
    return written
