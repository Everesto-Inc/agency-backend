"""Chat history endpoints + /chat remembering the conversation — handlers with fakes, no DB."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agency_platform.errors import NotFoundError
from edge_gateway import routes_chat
from edge_gateway import routes_conversations as rc
from edge_gateway.schemas import ConversationUpdate

NOW = datetime.now(UTC)
CONV = {"id": "abc", "title": "Range of the Ioniq 5?", "pinned": False, "turns": 2, "last_topic": "ev_research",
        "created_at": NOW, "updated_at": NOW}
USER = SimpleNamespace(id="u1")


class Repo:
    def __init__(self):
        self.convs = {"abc": dict(CONV)}
        self.touched = []

    async def recent(self, user_id, *, limit, query=None):
        return [c for c in self.convs.values() if not query or query in c["title"]][:limit]

    async def get(self, user_id, cid):
        return self.convs.get(cid)

    async def messages(self, user_id, cid, limit=200):
        return [{"request_id": "r1", "topic": "ev_research", "prompt": "Range?", "answer": "570 km", "status": "success",
                 "created_at": NOW, "total_tokens": 900, "cost_usd": 0.0012}]

    async def update(self, user_id, cid, *, title, pinned):
        if cid not in self.convs:
            return False
        if title:
            self.convs[cid]["title"] = title
        if pinned is not None:
            self.convs[cid]["pinned"] = pinned
        return True

    async def delete(self, user_id, cid):
        return self.convs.pop(cid, None) is not None

    async def touch(self, **kw):
        self.touched.append(kw)


class Sessions:
    forgotten: list = []

    def __init__(self, _r):
        pass

    async def forget_session(self, user_id, cid):
        Sessions.forgotten.append((user_id, cid))
        return 1


@pytest.fixture
def repo(monkeypatch):
    r = Repo()
    Sessions.forgotten = []
    monkeypatch.setattr(rc, "_repo", lambda: r)
    monkeypatch.setattr(rc, "SessionStore", Sessions)
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    return r


async def test_list_get_rename_pin_delete(repo):
    assert [c.id for c in await rc.list_conversations(USER, limit=50, q=None)] == ["abc"]
    assert await rc.list_conversations(USER, limit=50, q="nothing") == []

    conv = await rc.get_conversation("abc", USER)
    assert conv.messages[0].answer == "570 km" and conv.messages[0].cost_usd == pytest.approx(0.0012)

    renamed = await rc.update_conversation("abc", ConversationUpdate(title="  Ioniq range  ", pinned=True), USER)
    assert renamed.title == "Ioniq range" and renamed.pinned is True

    await rc.delete_conversation("abc", USER)
    assert Sessions.forgotten == [("u1", "abc")]
    with pytest.raises(NotFoundError):
        await rc.get_conversation("abc", USER)


async def test_unknown_conversation_is_404(repo):
    with pytest.raises(NotFoundError):
        await rc.update_conversation("nope", ConversationUpdate(title="x"), USER)
    with pytest.raises(NotFoundError):
        await rc.delete_conversation("nope", USER)


async def test_chat_remembers_with_a_redacted_title(monkeypatch):
    r = Repo()
    monkeypatch.setattr(routes_chat, "ConversationRepository", lambda _db: r)
    monkeypatch.setattr(routes_chat, "get_db", lambda: None)
    await routes_chat._remember(USER, "s1", "Mail me at jane.doe@example.com about the Ioniq 5", "ev_research")
    t = r.touched[0]
    assert t["conversation_id"] == "s1" and t["topic"] == "ev_research"
    assert "jane.doe@example.com" not in t["title"] and "Ioniq 5" in t["title"]
