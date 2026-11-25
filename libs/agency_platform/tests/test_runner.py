import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("PROMPTS_DIR", str(ROOT / "prompts"))

from agency_platform.agents.base import AgentEvent, TopicAgent
from agency_platform.agents.response_cache import ResponseCache
from agency_platform.agents.runner import HEARTBEAT, RunRequest, TopicRunner
from agency_platform.agents.sessions import SessionStore
from agency_platform.auth.deps import AuthenticatedUser
from agency_platform.billing.usage import RateLimiter, UsageMeter
from agency_platform.config.prompts import PromptStore
from agency_platform.config.settings import PlatformSettings
from agency_platform.errors import BadRequestError, SubscriptionRequiredError

UID = "00000000-0000-0000-0000-0000000000aa"


class FakeRuns:
    """In-memory RunRepository: records calls in order."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def start(self, **kw):
        self.calls.append(("start", kw))

    async def finish(self, request_id, result):
        self.calls.append(("finish", {"request_id": request_id, "result": result}))

    async def count_daily_run(self, **kw):
        self.calls.append(("count_daily_run", kw))

    stored: list[dict] = []   # the chat's earlier runs in MySQL (memory reload)
    fail_reload = False

    async def chat_runs(self, **kw):
        if self.fail_reload:
            raise RuntimeError("db down")
        self.calls.append(("chat_runs", kw))
        return list(self.stored)


class ScriptedAgent(TopicAgent):
    topic = "ev_research"
    framework = "test"

    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay, self.fail = delay, fail

    async def run(self, prompt, ctx):
        yield AgentEvent.status("working")
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom with secret internals")
        assert ctx.llm.call_token  # ctx (incl. signed call token) was built
        yield AgentEvent.answer(f"answer to {prompt}")


def make_runner(agent, redis, runs, heartbeat=15.0, cache=False, conversations=None):
    settings = PlatformSettings(model_gateway_client_secret="c" * 40)
    return TopicRunner(
        agent, settings, runs=runs, meter=UsageMeter(redis), limiter=RateLimiter(redis),
        sessions=SessionStore(redis), prompts=PromptStore(ROOT / "prompts"), http=httpx.AsyncClient(),
        cache=ResponseCache(redis) if cache else None, heartbeat_s=heartbeat, conversations=conversations,
    )


def user(plan="pro"):
    return AuthenticatedUser(id=UID, email="u@example.com", role="user", plan=plan)


async def collect(runner, req):
    return [e async for e in runner.stream(req)]


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def test_preflight_enforces_subscription_and_input(redis):
    runner = make_runner(ScriptedAgent(), redis, FakeRuns())
    with pytest.raises(SubscriptionRequiredError):
        await runner.preflight(user("none"), "hello")
    with pytest.raises(BadRequestError):
        await runner.preflight(user(), "x" * 5000)  # over topics.yaml max_prompt_chars
    assert await runner.preflight(user(), "  Tesla range? ") == "Tesla range?"


async def test_successful_run_records_usage_session_and_quota(redis):
    runs = FakeRuns()
    runner = make_runner(ScriptedAgent(), redis, runs)
    meter = UsageMeter(redis)
    await meter.record_llm_usage(user_id=UID, topic="ev_research", request_id="req-ok-000001", prompt_tokens=7, completion_tokens=3, cost_usd=0.001,
                                 model="gemini-2.5-flash")
    await meter.record_llm_usage(user_id=UID, topic="ev_research", request_id="req-ok-000001", prompt_tokens=0, completion_tokens=0, cost_usd=0,
                                 model="gemini-2.5-flash-lite")
    await meter.record_llm_usage(user_id=UID, topic="ev_research", request_id="req-ok-000001", prompt_tokens=0, completion_tokens=0, cost_usd=0,
                                 model="gemini-2.5-flash")
    events = await collect(runner, RunRequest(user(), "q", "s1", "req-ok-000001"))
    types = [e.type for e in events]
    assert types == ["status", "token", "usage", "done"]
    assert events[2].data["total_tokens"] == 10
    assert events[2].data["models"] == [{"model": "gemini-2.5-flash", "calls": 2}, {"model": "gemini-2.5-flash-lite", "calls": 1}]
    assert events[3].data["status"] == "success"
    assert [c[0] for c in runs.calls] == ["start", "chat_runs", "finish", "count_daily_run"]
    assert runs.calls[3][1] == {"user_id": UID, "topic": "ev_research", "success": True, "cached": False}
    assert await meter.topic_requests_today(UID, "ev_research") == 1
    assert len(await SessionStore(redis).history(UID, "s1")) == 2


async def test_agent_crash_is_sanitised_and_not_counted(redis):
    runner = make_runner(ScriptedAgent(fail=True), redis, FakeRuns())
    events = await collect(runner, RunRequest(user(), "q", "s1", "req-fail-00001"))
    err = next(e for e in events if e.type == "error")
    assert "secret internals" not in err.content
    assert events[-1].data["status"] == "error"
    assert await UsageMeter(redis).topic_requests_today(UID, "ev_research") == 0


async def test_heartbeats_while_agent_is_silent(redis):
    runner = make_runner(ScriptedAgent(delay=0.35), redis, FakeRuns(), heartbeat=0.1)
    events = await collect(runner, RunRequest(user(), "q", "s1", "req-hb-000001"))
    assert sum(1 for e in events if e is HEARTBEAT) >= 2
    assert events[-1].data["status"] == "success"


async def test_client_disconnect_still_finalises(redis):
    runs = FakeRuns()
    runner = make_runner(ScriptedAgent(delay=5), redis, runs)
    stream = runner.stream(RunRequest(user(), "q", "s1", "req-dc-000001"))
    first = await stream.__anext__()
    assert first.type == "status"
    await stream.aclose()  # client went away mid-run
    await asyncio.sleep(0.05)
    finished = [kw["result"] for kind, kw in runs.calls if kind == "finish"]
    assert finished and finished[0].status == "cancelled"


class CountingAgent(ScriptedAgent):
    def __init__(self):
        super().__init__()
        self.runs = 0

    async def run(self, prompt, ctx):
        self.runs += 1
        yield AgentEvent.artifact("data", {"n": 1})
        yield AgentEvent.answer(f"answer to {prompt}")


async def test_identical_first_turn_question_served_from_cache(redis):
    agent = CountingAgent()
    runner = make_runner(agent, redis, FakeRuns(), cache=True)
    await collect(runner, RunRequest(user(), "Tesla Model 3 range?", "s1", "req-c-0000001"))
    second = await runner.collect(RunRequest(user(), "  tesla model 3 RANGE ", "s2", "req-c-0000002"))
    assert agent.runs == 1
    assert second["cache_hit"] is True and second["answer"] == "answer to Tesla Model 3 range?"
    assert second["artifacts"] == {"data": {"n": 1}}
    # different plan ceiling -> different cache entry (answers can differ by model tier)
    await runner.collect(RunRequest(user("free"), "Tesla Model 3 range?", "s3", "req-c-0000003"))
    assert agent.runs == 2
    # follow-ups in a conversation are never cached
    await runner.collect(RunRequest(user(), "Tesla Model 3 range?", "s1", "req-c-0000004"))
    assert agent.runs == 3


async def test_pii_redacted_in_stored_prompt(redis):
    runs = FakeRuns()
    runner = make_runner(ScriptedAgent(), redis, runs)
    await collect(runner, RunRequest(user(), "call me on +44 20 7946 0958 about the Ioniq", "s1", "req-p-0000001"))
    stored_prompt = runs.calls[0][1]["prompt"]
    assert "7946" not in stored_prompt and "[PHONE]" in stored_prompt


def test_prompt_ab_assignment_is_sticky_and_tagged(tmp_path):
    (tmp_path / "t.yaml").write_text(
        "version: '1.0'\n"
        "prompts:\n  p: {system: base}\n"
        "experiments:\n  p:\n    variants:\n      control: {weight: 50}\n      short: {weight: 50, system: brief}\n"
    )
    store = PromptStore(tmp_path)
    arms = {u: store.get("t", "p", subject=u) for u in (f"user-{i}" for i in range(200))}
    assert all(store.get("t", "p", subject=u).variant == a.variant for u, a in arms.items())  # sticky
    short = [a for a in arms.values() if a.variant == "short"]
    assert 60 < len(short) < 140 and short[0].system == "brief" and short[0].ref == "t/p@1.0~short"
    assert store.get("t", "p").variant is None  # no subject -> control prompt, untagged
    fp_before = store.fingerprint("t", "user-1")
    import os
    import time
    (tmp_path / "t.yaml").write_text((tmp_path / "t.yaml").read_text().replace("base", "base v2"))
    os.utime(tmp_path / "t.yaml", (time.time() + 5, time.time() + 5))
    assert store.fingerprint("t", "user-1") != fp_before  # edits invalidate response cache keys


class HistoryAgent(ScriptedAgent):
    """Answers with what it remembers, so tests can see the memory it was given."""

    def __init__(self, topic="ev_research"):
        super().__init__()
        self.topic = topic
        self.seen: list[list[dict]] = []

    async def run(self, prompt, ctx):
        self.seen.append(list(ctx.history))
        yield AgentEvent.answer(f"answer to {prompt}")


async def test_memory_is_reloaded_from_mysql_when_redis_forgot(redis):
    runs = FakeRuns()
    runs.stored = [{"topic": "ev_research", "prompt": "Range of the Model Y?", "answer": "533 km"},
                   {"topic": "ev_research", "prompt": "no answer stored", "answer": None}]
    agent = HistoryAgent()
    await collect(make_runner(agent, redis, runs), RunRequest(user(), "And in winter?", "chat-1", "req-mem-00001"))
    assert [m["content"] for m in agent.seen[0]] == ["Range of the Model Y?", "533 km"]   # reloaded (unanswered skipped)
    reload = next(kw for name, kw in runs.calls if name == "chat_runs")
    assert reload["exclude_request_id"] == "req-mem-00001" and reload["session_id"] == "chat-1"
    # put back into Redis + the new turn appended: the next run reads Redis, no second reload
    assert len(await SessionStore(redis).history(UID, "chat-1")) == 4
    await collect(make_runner(agent, redis, runs), RunRequest(user(), "Thanks", "chat-1", "req-mem-00002"))
    assert [n for n, _ in runs.calls].count("chat_runs") == 1


async def test_one_memory_per_chat_shared_by_assistants(redis):
    ev, finance = HistoryAgent("ev_research"), HistoryAgent("finance")
    await collect(make_runner(ev, redis, FakeRuns()), RunRequest(user(), "Model Y price?", "chat-2", "req-mem-00003"))
    await collect(make_runner(finance, redis, FakeRuns()), RunRequest(user(), "Is Tesla stock cheap?", "chat-2", "req-mem-00004"))
    remembered = finance.seen[0]
    assert [m["content"] for m in remembered] == ["Model Y price?", "answer to Model Y price?"]
    assert {m["topic"] for m in remembered} == {"ev_research"}          # it knows who said it


async def test_memory_reload_failure_never_breaks_the_answer(redis):
    runs = FakeRuns()
    runs.fail_reload = True
    events = await collect(make_runner(HistoryAgent(), redis, runs), RunRequest(user(), "q", "chat-3", "req-mem-00005"))
    assert events[-1].data["status"] == "success"


class FakeConversations:
    def __init__(self, summary=None, upto=None):
        self.value, self.upto, self.saved = summary, upto, []

    async def summary(self, user_id, conversation_id):
        return self.value, self.upto

    async def set_summary(self, user_id, conversation_id, summary, upto):
        self.saved.append((summary, upto))


class SummaryAgent(HistoryAgent):
    async def run(self, prompt, ctx):
        self.seen.append([{"summary": ctx.summary}])
        yield AgentEvent.answer(f"answer to {prompt}")


async def test_summary_reaches_the_agent_and_is_updated_after_the_answer(redis, monkeypatch):
    convs = FakeConversations(summary="- user lives in Germany")
    agent = SummaryAgent()
    runner = make_runner(agent, redis, FakeRuns(), conversations=convs)
    written = []

    async def fake_write(ctx, previous, fold):
        written.append((previous, [m["content"] for m in fold]))
        return "- user lives in Germany\n- compared Model Y and Ioniq 5"

    monkeypatch.setattr(runner, "_write_summary", fake_write)
    for i in range(5):  # 5 questions → 10 messages → 6 older than the last 4
        await collect(runner, RunRequest(user(), f"q{i}", "chat-s", f"req-sum-0000{i}"))
        await asyncio.gather(*runner._background)
    assert agent.seen[0] == [{"summary": "- user lives in Germany"}]
    assert len(written) == 1 and written[0][0] == "- user lives in Germany"
    assert written[0][1] == ["q0", "answer to q0", "q1", "answer to q1", "q2", "answer to q2"]
    assert convs.saved[0][0].endswith("Ioniq 5") and convs.saved[0][1] is not None


async def test_no_summary_without_conversations_or_on_failure(redis, monkeypatch):
    runner = make_runner(SummaryAgent(), redis, FakeRuns())   # conversations=None → feature off
    await collect(runner, RunRequest(user(), "q", "chat-t", "req-sum-00010"))
    assert runner._background == set()
    convs = FakeConversations()
    broken = make_runner(SummaryAgent(), redis, FakeRuns(), conversations=convs)

    async def boom(*a):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(broken, "_write_summary", boom)
    await redis.delete("sess:" + UID + ":chat-u")
    for i in range(5):
        events = await collect(broken, RunRequest(user(), f"q{i}", "chat-u", f"req-sum-0010{i}"))
        await asyncio.gather(*broken._background)
    assert events[-1].data["status"] == "success" and convs.saved == []


async def test_direct_answer_is_one_short_call_without_the_agent(monkeypatch):
    """Triage said direct: no agent workflow, one 'direct' call, still remembered like any answer."""
    from agency_platform.agents import runner as runner_mod
    calls = []

    class LLM:
        async def chat(self, messages, **kw):
            calls.append(kw["task"])
            assert "EV Helper" in messages[0]["content"]            # tells what the team can do
            return SimpleNamespace(content="Hi! I'm the agency team.")

    r = runner_mod.TopicRunner.__new__(runner_mod.TopicRunner)
    r._prompts = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(system="desk {assistants}", ref="edge/direct@2.0"))
    user = SimpleNamespace(id="u1", plan_config=SimpleNamespace(allows_topic=lambda t: True))
    ctx = SimpleNamespace(user=user, llm=LLM(), history=[{"role": "user", "content": "earlier"}])
    req = SimpleNamespace(prompt="who are you?", user=user)
    assert await r._direct(ctx, req) == "Hi! I'm the agency team." and calls == ["direct"]
