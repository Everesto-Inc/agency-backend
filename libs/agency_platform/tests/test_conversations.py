"""Chat history: titles, repository SQL (fake DB), forgetting one conversation's memory."""
import os
from pathlib import Path

import fakeredis.aioredis

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from datetime import UTC, datetime

from agency_platform.agents.sessions import SessionStore, messages_to_fold, turns_from_runs, with_history
from agency_platform.repositories import ConversationRepository, title_from_prompt

U = "6f1c2c1e-3b9d-4a51-9a52-0b5a4c1d2e3f"


def test_title_is_one_line_and_cut_at_a_word():
    assert title_from_prompt("  Range of the\n\nIoniq 5?  ") == "Range of the Ioniq 5?"
    long = "What is the cheapest way to charge an electric car at home in Germany during the winter months"
    t = title_from_prompt(long, limit=40)
    assert t.endswith("…") and len(t) <= 41 and not t[:-1].endswith(" ")
    assert title_from_prompt("   ") == "New conversation"
    assert title_from_prompt("x" * 100, limit=10) == "x" * 10 + "…"


class FakeDb:
    def __init__(self, rows=None, matched=1):
        self.calls, self.rows, self.matched = [], rows or [], matched

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self.matched

    async def fetch(self, sql, params=None):
        self.calls.append((sql, params))
        return self.rows

    async def fetchrow(self, sql, params=None):
        self.calls.append((sql, params))
        return self.rows[0] if self.rows else None


async def test_touch_creates_or_bumps():
    db = FakeDb()
    await ConversationRepository(db).touch(user_id=U, conversation_id="abc", title="t" * 300, topic="finance")
    sql, args = db.calls[0]
    assert "ON DUPLICATE KEY UPDATE turns = turns + 1" in sql
    assert args[0] == "abc" and args[1] == U and len(args[2]) == 200 and args[3] == "finance"


async def test_list_escapes_the_search_and_puts_pinned_first():
    db = FakeDb(rows=[{"id": "a", "title": "x", "pinned": 1, "turns": 1, "last_topic": None, "created_at": 0, "updated_at": 0}])
    rows = await ConversationRepository(db).recent(U, limit=20, query="50%_off")
    sql, args = db.calls[0]
    assert "ORDER BY pinned DESC, updated_at DESC" in sql and args == (U, "%50\\%\\_off%", 20)
    assert rows[0]["pinned"] is True


async def test_update_and_delete():
    db = FakeDb(rows=[{"id": "a", "title": "x", "pinned": 0, "turns": 1, "last_topic": None, "created_at": 0, "updated_at": 0}])
    repo = ConversationRepository(db)
    assert await repo.update(U, "a", title="New", pinned=True)
    assert "COALESCE" in db.calls[0][0] and db.calls[0][1] == ("New", True, U, "a")
    assert await repo.update(U, "a", title=None, pinned=None)  # nothing to change, but it exists
    assert await repo.update(U, "a", title=None, pinned=False)  # only pin changes, title stays (NULL -> COALESCE)
    assert db.calls[-1][1] == (None, False, U, "a")
    assert not await ConversationRepository(FakeDb(matched=0)).update(U, "gone", title="x", pinned=None)
    db = FakeDb()
    assert await ConversationRepository(db).delete(U, "a")
    assert db.calls[0][0].startswith("DELETE FROM conversations")
    assert "SET prompt = NULL, answer = NULL" in db.calls[1][0]  # text erased, usage kept


async def test_forget_session_only_touches_that_conversation():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SessionStore(r)
    await store.append(U, "keep", {"role": "user", "content": "a"})
    await store.append(U, "drop", {"role": "user", "content": "b"})
    await r.rpush(f"sess:{U}:ev_research:drop", "old per-assistant key")   # from before memory was per chat
    assert await store.forget_session(U, "drop") == 2
    assert await r.keys() == [f"sess:{U}:keep"]


def test_turns_from_runs_and_with_history():
    at = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    turns = turns_from_runs([{"topic": "finance", "prompt": "Q1", "answer": "A1", "created_at": at},
                             {"topic": "ev", "prompt": "Q2", "answer": None}])
    assert turns == [{"role": "user", "content": "Q1", "topic": "finance", "at": at.isoformat()},
                     {"role": "assistant", "content": "A1", "topic": "finance", "at": at.isoformat()}]
    assert with_history("new?", []) == "new?"
    text = with_history("and in France?", [*turns, {"role": "system", "content": "x"}])
    assert text.startswith("Earlier conversation:\nuser: Q1\nassistant: A1") and text.endswith("User question: and in France?")



def _msgs(n):  # n messages, one minute apart
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}",
             "at": datetime(2026, 11, 25, 10, i, tzinfo=UTC).isoformat()} for i in range(n)]


def test_fold_only_older_messages_not_yet_summarized_and_in_batches():
    assert messages_to_fold(_msgs(9), None) == []                       # 5 older than the last 4 → < 6, wait
    fold = messages_to_fold(_msgs(10), None)
    assert [m["content"] for m in fold] == ["m0", "m1", "m2", "m3", "m4", "m5"]   # never the last 4
    upto = datetime(2026, 11, 25, 10, 5)                                # MySQL gives naive UTC
    assert messages_to_fold(_msgs(14), upto) == []                      # m6..m9 only → < 6
    assert [m["content"] for m in messages_to_fold(_msgs(16), upto)] == [f"m{i}" for i in range(6, 12)]
    assert messages_to_fold([{"role": "user", "content": "no time"}] * 10, None) == []


def test_with_history_puts_the_summary_first():
    text = with_history("and the price?", _msgs(2), summary="- user in Germany")
    assert text.index("Summary of the earlier conversation") < text.index("Earlier conversation:") < text.index("User question")
    assert with_history("q", [], summary="- s").startswith("Summary of the earlier conversation:\n- s")


async def test_append_stamps_messages():
    import fakeredis.aioredis
    store = SessionStore(fakeredis.aioredis.FakeRedis(decode_responses=True))
    await store.append(U, "c1", {"role": "user", "content": "x"})
    assert (await store.history(U, "c1"))[0]["at"].startswith("20")


async def test_summary_sql():
    db = FakeDb(rows=[{"summary": "- s", "summary_upto": datetime(2026, 11, 25, 10, 5)}])
    repo = ConversationRepository(db)
    assert await repo.summary(U, "c1") == ("- s", datetime(2026, 11, 25, 10, 5))
    await repo.set_summary(U, "c1", "x" * 3000, datetime(2026, 11, 25, 11, 0))
    sql, args = db.calls[-1]
    assert sql.startswith("UPDATE conversations SET summary") and len(args[0]) == 2000 and args[2:] == (U, "c1")
    assert await ConversationRepository(FakeDb()).summary(U, "none") == (None, None)
