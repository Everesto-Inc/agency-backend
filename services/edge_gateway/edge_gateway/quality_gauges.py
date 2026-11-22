"""Exports quality state from the database as Prometheus gauges every few minutes,
so alerting rules cover quality (online judge score, drift, offline eval pass
rate) and not just latency/errors. Runs on every edge replica; read-only."""
import asyncio
import contextlib
import logging

from fastapi import FastAPI

from agency_platform.db import get_db
from agency_platform.observability.metrics import DRIFT_FLAG, EVAL_PASS_RATE, QUALITY_SCORE
from agency_platform.repositories import EvalRepository

logger = logging.getLogger(__name__)
_INTERVAL_S = 300


async def refresh_once() -> None:
    repo = EvalRepository(get_db())
    for r in await repo.online_scores():
        if r["score_24h"] is not None:
            QUALITY_SCORE.labels(r["topic"]).set(float(r["score_24h"]))
    for r in await repo.latest_offline():
        if r["pass_rate"] is not None:
            EVAL_PASS_RATE.labels(r["topic"]).set(float(r["pass_rate"]))
    for r in await repo.latest_drift():
        for metric in (r["metrics"] or {}):
            DRIFT_FLAG.labels(r["topic"], metric).set(1 if metric in (r["flagged"] or []) else 0)


async def _loop() -> None:
    while True:
        try:
            await refresh_once()
        except Exception:
            logger.warning("quality gauge refresh failed", exc_info=True)
        await asyncio.sleep(_INTERVAL_S)


async def start_quality_gauges(app: FastAPI) -> None:
    app.state.quality_task = asyncio.create_task(_loop(), name="quality-gauges")


async def stop_quality_gauges(app: FastAPI) -> None:
    task = getattr(app.state, "quality_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
