"""Runs the real migrations and every repository query against a real MySQL.

Skipped unless AGENCY_TEST_DATABASE_URL points at a disposable database, e.g.

    docker compose up -d --wait mysql
    cd libs/agency_platform && AGENCY_TEST_DATABASE_URL=mysql://agency:localdev@127.0.0.1:33069/agency_test \
        uv run --extra test pytest tests/test_mysql_integration.py

Never point it at a database you care about: it creates and deletes data.
"""
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agency_platform.auth.accounts import AccountRepository, RefreshTokenStore
from agency_platform.config.schemas import RetentionConfig
from agency_platform.db.database import connect
from agency_platform.db.migrate import migrate
from agency_platform.errors import ConflictError, UnauthorizedError
from agency_platform.repositories import (
    CALL_COLUMNS,
    AdminMetrics,
    EvalRepository,
    LlmCallRepository,
    RetentionRepository,
    RunRepository,
    RunResult,
    UsageRepository,
)
from agency_platform.repositories.usage import since

URL = os.environ.get("AGENCY_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(not URL, reason="AGENCY_TEST_DATABASE_URL not set (see module docstring)")


@pytest.fixture
async def db():
    await migrate(ROOT / "migrations", URL)
    assert await migrate(ROOT / "migrations", URL) == []  # idempotent
    database = await connect(URL, max_size=4)
    yield database
    await database.close()


def _call(request_id: str, user_id: str | None, client: str = "ev_research", **over) -> tuple:
    row = {
        "request_id": request_id, "user_id": user_id, "client_id": client, "task": "synthesis",
        "prompt_ref": "ev_research/synthesize@2.0~b", "requested_model": "task-synthesis", "route_source": "task",
        "route_reason": "task synthesis", "tier": "mid", "model": "gemini-flash", "attempt": 1, "status": "success",
        "error_type": None, "prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.0012, "latency_ms": 800,
        "streamed": False, "cached": False, "canary": False, "pii_redactions": 0,
    } | over
    return tuple(row[c] for c in CALL_COLUMNS)


async def test_full_lifecycle(db):
    accounts = AccountRepository(db)
    email = f"it-{uuid.uuid4().hex[:8]}@example.test"
    acct = await accounts.create(email, "correct horse battery", plan="pro")
    with pytest.raises(ConflictError):
        await accounts.create(email.upper(), "correct horse battery", plan="pro")
    assert (await accounts.authenticate(email, "correct horse battery")).id == acct.id
    expires = datetime.now(UTC) + timedelta(days=30)
    updated = await accounts.set_plan(acct.id, "free", expires)
    assert updated and updated.plan == "free" and abs((updated.plan_expires_at - expires).total_seconds()) < 1
    assert await accounts.set_plan(str(uuid.uuid4()), "free", None) is None

    # refresh tokens: rotation + reuse detection that sticks
    tokens = RefreshTokenStore(db, ttl_days=1)
    t1 = await tokens.issue(acct.id)
    uid, t2 = await tokens.rotate(t1)
    assert uid == acct.id
    with pytest.raises(UnauthorizedError, match="reuse"):
        await tokens.rotate(t1)
    with pytest.raises(UnauthorizedError):  # family revoked -> the fresh token is dead too
        await tokens.rotate(t2)

    # a run, its usage and feedback
    runs = RunRepository(db)
    rid = f"it-{uuid.uuid4().hex}"
    await runs.start(request_id=rid, user_id=acct.id, topic="ev_research", session_id="s1", prompt="q?",
                     prompt_fingerprint="fp")
    await runs.start(request_id=rid, user_id=acct.id, topic="ev_research", session_id="s1", prompt="q?",
                     prompt_fingerprint="fp")  # idempotent
    await runs.finish(rid, RunResult("success", None, 900, 100, 50, 0.0012, 1, 3, "ans", False, True))
    await runs.count_daily_run(user_id=acct.id, topic="ev_research", success=True, cached=False)
    await runs.count_daily_run(user_id=acct.id, topic="ev_research", success=True, cached=True)
    await LlmCallRepository(db).insert_batch(
        [_call(rid, acct.id), _call(rid, acct.id, attempt=2, model="gemini-flash-lite", canary=True)],
        [(acct.id, "ev_research", datetime.now(UTC).date(), 2, 200, 100, 0.0024)],
    )
    assert await runs.set_feedback(request_id=rid, user_id=acct.id, topic="ev_research", score=-1, comment="wrong")
    assert await runs.set_feedback(request_id=rid, user_id=acct.id, topic="ev_research", score=-1, comment="wrong")  # same value still "found"
    assert not await runs.set_feedback(request_id=rid, user_id=str(uuid.uuid4()), topic="ev_research", score=1, comment=None)
    await runs.propose_eval_case(request_id=rid, label="negative", comment="wrong")
    await runs.propose_eval_case(request_id=rid, label="positive", comment="ok now")  # candidate -> updated
    assert len(await runs.list_for_user(acct.id, topic=None, limit=10)) == 1
    assert (await runs.recent(topic="ev_research", status="success", limit=5))
    assert len(await runs.llm_calls(rid)) == 2

    usage = UsageRepository(db)
    per_topic = await usage.by_topic(from_day=since(1), user_id=acct.id)
    assert per_topic["ev_research"]["runs"] == 2 and per_topic["ev_research"]["cached_runs"] == 1
    assert per_topic["ev_research"]["total_tokens"] == 300 and per_topic["ev_research"]["llm_calls"] == 2
    assert (await usage.daily_series(from_day=since(1), user_id=acct.id, topic="ev_research"))[0]["runs"] == 2
    top = await usage.top_users(from_day=since(1), limit=500)
    assert next(u for u in top if u["user_id"] == acct.id)["topics"]["ev_research"]["runs"] == 2

    # dashboard queries (calendar ranges)
    today = datetime.now(UTC).date()
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    assert today.strftime("%Y-%m") in await usage.months(acct.id)
    in_month = await usage.by_topic(from_day=month_start, to_day=next_month, user_id=acct.id)
    assert in_month["ev_research"]["runs"] == 2
    assert await usage.by_topic(from_day=next_month, to_day=next_month, user_id=acct.id) == {}
    assert await usage.active_users(from_day=month_start, to_day=next_month) >= 1
    start = datetime.combine(month_start, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(next_month, datetime.min.time(), tzinfo=UTC)
    stats = await runs.stats(start=start, end=end, user_id=acct.id)
    assert stats["runs"] == 1 and stats["successful"] == 1 and stats["negative_feedback"] == 1
    assert len(await runs.list_for_user(acct.id, topic=None, limit=10, start=start, end=end)) == 1
    models = await AdminMetrics(db).models_between(start, end)
    assert next(m for m in models if m["model"] == "gemini-flash")["total_tokens"] >= 150
    counts = await AdminMetrics(db).user_counts(month_start, next_month)
    assert counts["total"] >= 1 and counts["new_signups"] >= 1 and counts["by_plan"]

    # quality
    evals = EvalRepository(db)
    pending = await evals.pending_online(1000)
    assert any(p["request_id"] == rid for p in pending)
    assert await evals.save_online_result(topic="ev_research", request_id=rid, passed=True, judge_score=4.25,
                                          scores={"groundedness": 4}, rationale="ok", cost_usd=0.0012, latency_ms=900)
    assert not await evals.save_online_result(topic="ev_research", request_id=rid, passed=False, judge_score=1,
                                              scores={}, rationale="dup", cost_usd=0, latency_ms=1)
    assert all(p["request_id"] != rid for p in await evals.pending_online(1000))
    run_id = await evals.save_offline_run(
        {"topic": "ev_research", "cases": 1, "pass_rate": 1.0, "mean_judge_score": 4.5, "mean_cost_usd": 0.001,
         "p95_latency_ms": 900, "git_sha": "abc", "label": "it"},
        True,
        [{"case_id": "c1", "request_id": None, "passed": True, "judge_score": 4.5, "scores": {"relevance": 5},
          "failures": [], "rationale": "fine", "cost_usd": 0.001, "latency_ms": 900}],
    )
    assert run_id > 0
    assert any(r["id"] == run_id for r in await evals.latest_offline("ev_research"))
    assert (await evals.online_scores("ev_research"))[0]["score_24h"] is not None
    now = datetime.now(UTC)
    stats = await evals.window_stats("ev_research", now - timedelta(hours=1), now + timedelta(minutes=1))
    assert stats["runs"] >= 1 and stats["tiers"].get("mid")
    await evals.save_drift(topic="ev_research", window_start=now - timedelta(hours=24), window_end=now,
                           metrics={"error_rate": {"current": 0.1}}, flagged=["error_rate"])
    assert (await evals.latest_drift("ev_research"))[0]["flagged"] == ["error_rate"]
    cases = await evals.list_cases(topic="ev_research", status="candidate", label="positive", limit=100)
    case = next(c for c in cases if c["source_request_id"] == rid)
    assert case["comment"] == "ok now"
    assert (await evals.update_case(case["id"], status="approved", checks={"must_contain": ["kWh"]}))["checks"] == {"must_contain": ["kWh"]}
    assert any(c["id"] == case["id"] for c in await evals.approved_cases())
    assert await evals.mark_exported([case["id"]]) == 1

    # admin analytics (exercise every query shape incl. percentiles)
    metrics = AdminMetrics(db)
    models = await metrics.models("24h")
    flash = next(m for m in models if m["model"] == "gemini-flash")
    assert flash["p95_ms"] == 800 and flash["attempts"] >= 1
    assert await metrics.routing("24h")
    assert next(t for t in await metrics.topics("24h") if t["topic"] == "ev_research")["runs"] >= 1
    assert any(e["prompt_ref"].endswith("~b") for e in await metrics.experiments("ev_research", "24h"))
    assert any(c["canary"] for c in await metrics.canaries("24h"))

    # retention with tiny windows touches nothing recent; export + erase
    out = await RetentionRepository(db, chunk=2).apply(RetentionConfig(
        run_text_days=30, llm_calls_days=180, eval_results_days=365, expired_refresh_tokens_days=7))
    assert set(out) == {"agent_runs_text", "eval_cases_text", "llm_calls", "eval_results", "refresh_tokens"}
    exported = await accounts.export(acct.id)
    assert exported["account"]["email"] == email and len(exported["runs"]) == 1
    await accounts.erase(acct.id)
    assert await accounts.get(acct.id) is None
    assert len(await runs.llm_calls(rid)) == 2  # ledger kept...
    assert await db.fetchval("SELECT COUNT(*) AS n FROM llm_calls WHERE request_id = %s AND user_id IS NOT NULL",
                             (rid,)) == 0  # ...but anonymised
