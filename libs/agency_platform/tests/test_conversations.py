"""Chat history: titles, repository SQL (fake DB), forgetting one conversation's memory."""
import os
from pathlib import Path

import fakeredis.aioredis

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from agency_platform.agents.sessions import SessionStore
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
    assert db.calls[0][1] == ("New", True, U, "a")
    assert await repo.update(U, "a", title=None, pinned=None)  # nothing to change, but it exists
    db = FakeDb()
    assert await ConversationRepository(db).delete(U, "a")
    assert db.calls[0][0].startswith("DELETE FROM conversations")
    assert "SET prompt = NULL, answer = NULL" in db.calls[1][0]  # text erased, usage kept


async def test_forget_session_only_touches_that_conversation():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SessionStore(r)
    await store.append(U, "finance", "keep", {"role": "user", "content": "a"})
    await store.append(U, "finance", "drop", {"role": "user", "content": "b"})
    await store.append(U, "ev_research", "drop", {"role": "user", "content": "c"})
    assert await store.forget_session(U, "drop") == 2
    assert await r.keys() == [f"sess:{U}:finance:keep"]
