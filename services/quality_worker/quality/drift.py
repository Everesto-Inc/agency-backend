"""Drift detection: compare the latest window against a trailing baseline, per topic.

Signals (all thresholds in config/evals.yaml):
  judge_score              mean online LLM-judge score drops
  negative_feedback_rate   share of rated runs that got a thumbs-down rises
  error_rate               share of runs that failed / timed out rises
  cost_per_run             mean $ per run rises (e.g. a prompt got longer, routing shifted up)
  tier_distribution        the mix of model tiers used shifts (Jensen-Shannon divergence)
"""
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from agency_platform.config.schemas import DriftConfig
from agency_platform.repositories import EvalRepository

logger = logging.getLogger(__name__)


def jensen_shannon(p: dict[str, float], q: dict[str, float]) -> float:
    """JS divergence (base 2, in [0, 1]) between two categorical distributions given as counts."""
    keys = set(p) | set(q)
    sp, sq = sum(p.values()) or 1, sum(q.values()) or 1
    pn = {k: p.get(k, 0) / sp for k in keys}
    qn = {k: q.get(k, 0) / sq for k in keys}
    m = {k: (pn[k] + qn[k]) / 2 for k in keys}

    def kl(a: dict[str, float]) -> float:
        return sum(a[k] * math.log2(a[k] / m[k]) for k in keys if a[k] > 0)

    return round((kl(pn) + kl(qn)) / 2, 4)


@dataclass
class WindowStats:
    runs: int = 0
    error_rate: float = 0.0
    cost_per_run: float = 0.0
    negative_feedback_rate: float | None = None
    judge_score: float | None = None
    tiers: dict[str, float] = field(default_factory=dict)


@dataclass
class DriftReport:
    topic: str
    window_start: datetime
    window_end: datetime
    metrics: dict[str, dict]
    flagged: list[str]
    enough_data: bool


def compare(topic: str, cur: WindowStats, base: WindowStats, cfg: DriftConfig,
            start: datetime, end: datetime) -> DriftReport:
    t = cfg.thresholds
    metrics: dict[str, dict] = {}
    flagged: list[str] = []
    enough = cur.runs >= cfg.min_runs and base.runs >= cfg.min_runs

    def add(name: str, current, baseline, delta, is_flag: bool) -> None:
        metrics[name] = {"current": current, "baseline": baseline, "delta": delta, "flagged": bool(is_flag and enough)}
        if is_flag and enough:
            flagged.append(name)

    if cur.judge_score is not None and base.judge_score is not None:
        d = round(cur.judge_score - base.judge_score, 3)
        add("judge_score", cur.judge_score, base.judge_score, d, -d > t.judge_score_drop)
    if cur.negative_feedback_rate is not None and base.negative_feedback_rate is not None:
        d = round(cur.negative_feedback_rate - base.negative_feedback_rate, 4)
        add("negative_feedback_rate", cur.negative_feedback_rate, base.negative_feedback_rate, d,
            d > t.negative_feedback_rate_increase)
    d = round(cur.error_rate - base.error_rate, 4)
    add("error_rate", cur.error_rate, base.error_rate, d, d > t.error_rate_increase)
    pct = round((cur.cost_per_run - base.cost_per_run) / base.cost_per_run * 100, 1) if base.cost_per_run else 0.0
    add("cost_per_run", cur.cost_per_run, base.cost_per_run, pct, pct > t.cost_per_run_increase_pct)
    if cur.tiers and base.tiers:
        jsd = jensen_shannon(cur.tiers, base.tiers)
        add("tier_distribution", cur.tiers, base.tiers, jsd, jsd > t.tier_distribution_jsd)
    return DriftReport(topic, start, end, metrics, flagged, enough)


def to_window_stats(raw: dict) -> WindowStats:
    neg, judge = raw.get("neg_rate"), raw.get("judge_score")
    return WindowStats(
        runs=int(raw.get("runs") or 0),
        error_rate=round(float(raw.get("error_rate") or 0), 4),
        cost_per_run=round(float(raw.get("cost_per_run") or 0), 6),
        negative_feedback_rate=round(float(neg), 4) if neg is not None else None,
        judge_score=round(float(judge), 3) if judge is not None else None,
        tiers=raw.get("tiers") or {},
    )


async def detect(repo: EvalRepository, topics: list[str], cfg: DriftConfig,
                 now: datetime | None = None) -> list[DriftReport]:
    now = now or datetime.now(UTC)
    w_start = now - timedelta(hours=cfg.window_hours)
    b_start = w_start - timedelta(days=cfg.baseline_days)
    reports = []
    for topic in topics:
        cur = to_window_stats(await repo.window_stats(topic, w_start, now))
        base = to_window_stats(await repo.window_stats(topic, b_start, w_start))
        rep = compare(topic, cur, base, cfg, w_start, now)
        await repo.save_drift(topic=topic, window_start=w_start, window_end=now,
                              metrics=json.loads(json.dumps(rep.metrics, default=str)), flagged=rep.flagged)
        reports.append(rep)
    return reports


async def alert(http: httpx.AsyncClient, webhook_url: str | None, reports: list[DriftReport]) -> bool:
    flagged = [r for r in reports if r.flagged]
    if not flagged or not webhook_url:
        return False
    lines = [f"*LLM drift detected* ({len(flagged)} topic(s))"]
    for r in flagged:
        for m in r.flagged:
            v = r.metrics[m]
            lines.append(f"- `{r.topic}` {m}: {v['baseline']} -> {v['current']} (delta {v['delta']})")
    try:
        await http.post(webhook_url, json={"text": "\n".join(lines)}, timeout=10)
        return True
    except httpx.HTTPError:
        logger.warning("drift alert webhook failed")
        return False
