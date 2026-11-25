"""Chat history endpoints + /chat remembering the conversation — handlers with fakes, no DB."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agency_platform.errors import NotFoundError
from agency_platform.tools.images import StoredImage
from agency_platform.tools.vision import MARKER
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
        return [{"request_id": "r1", "topic": "ev_research", "prompt": "Range?" + MARKER + "\nPicture 1: white Model Y", "answer": "570 km", "status": "success",
                 "created_at": NOW, "total_tokens": 900, "cost_usd": 0.0012, "models": "gemini-2.5-flash,gemini-2.5-flash-lite"}]

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


class Pictures:
    deleted: list = []

    async def for_session(self, user_id, cid):
        return {"r1": [StoredImage("f" * 64, "upload", "image/jpeg", 1000, user_id, cid, "r1", "white Model Y")]}

    async def delete_session(self, user_id, cid):
        Pictures.deleted.append((user_id, cid))
        return 1


@pytest.fixture
def repo(monkeypatch):
    r = Repo()
    Sessions.forgotten = []
    Pictures.deleted = []
    monkeypatch.setattr(rc, "image_store", lambda _req: Pictures())
    monkeypatch.setattr(rc, "_repo", lambda: r)
    monkeypatch.setattr(rc, "SessionStore", Sessions)
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    return r


async def test_list_get_rename_pin_delete(repo):
    assert [c.id for c in await rc.list_conversations(USER, limit=50, q=None)] == ["abc"]
    assert await rc.list_conversations(USER, limit=50, q="nothing") == []

    conv = await rc.get_conversation("abc", USER, None)
    assert conv.messages[0].answer == "570 km" and conv.messages[0].cost_usd == pytest.approx(0.0012)
    assert conv.messages[0].models == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    assert conv.messages[0].prompt == "Range?"                     # the user's words, not the picture text
    [pic] = conv.messages[0].images
    assert pic.path == "/api/v1/images/" + "f" * 64 and pic.description == "white Model Y"

    renamed = await rc.update_conversation("abc", ConversationUpdate(title="  Ioniq range  ", pinned=True), USER)
    assert renamed.title == "Ioniq range" and renamed.pinned is True

    await rc.delete_conversation("abc", USER, None)
    assert Sessions.forgotten == [("u1", "abc")] and Pictures.deleted == [("u1", "abc")]
    with pytest.raises(NotFoundError):
        await rc.get_conversation("abc", USER, None)


async def test_unknown_conversation_is_404(repo):
    with pytest.raises(NotFoundError):
        await rc.update_conversation("nope", ConversationUpdate(title="x"), USER)
    with pytest.raises(NotFoundError):
        await rc.delete_conversation("nope", USER, None)


async def test_chat_remembers_with_a_redacted_title(monkeypatch):
    r = Repo()
    monkeypatch.setattr(routes_chat, "ConversationRepository", lambda _db: r)
    monkeypatch.setattr(routes_chat, "get_db", lambda: None)
    await routes_chat._remember(USER, "s1", "Mail me at jane.doe@example.com about the Ioniq 5", "ev_research")
    t = r.touched[0]
    assert t["conversation_id"] == "s1" and t["topic"] == "ev_research"
    assert "jane.doe@example.com" not in t["title"] and "Ioniq 5" in t["title"]



async def test_chat_title_never_contains_the_picture_descriptions(monkeypatch):
    r = Repo()
    monkeypatch.setattr(routes_chat, "ConversationRepository", lambda _db: r)
    monkeypatch.setattr(routes_chat, "get_db", lambda: None)
    await routes_chat._remember(USER, "s1", "Is this a Tesla?" + MARKER + "\nPicture 1: white car", "ev_research")
    assert r.touched[0]["title"] == "Is this a Tesla?"


async def test_chat_with_pictures_describes_them_for_the_assistants(monkeypatch):
    attached = []

    class Store:
        async def attach(self, user_id, session_id, ids, request_id):
            attached.append((user_id, session_id, ids, request_id))
            return [StoredImage(i, "upload", "image/png", 10, user_id, session_id, request_id) for i in ids]

        async def read(self, iid):
            return None, b"png-bytes"

        async def set_description(self, iid, text):
            attached.append(("described", iid, text))

    async def describe(llm, prompts, user_id, question, pictures):
        assert question == "Is this a Tesla?" and pictures == [(b"png-bytes", "image/png")]
        return ["white Tesla Model Y"]

    monkeypatch.setattr(routes_chat, "image_store", lambda _req: Store())
    monkeypatch.setattr(routes_chat, "describe", describe)
    monkeypatch.setattr(routes_chat, "_gateway_client", lambda user, req: object())
    monkeypatch.setattr(routes_chat, "prompt_store", lambda: None)
    user = SimpleNamespace(id="u1", plan_config=SimpleNamespace(topics=["ev_research"]))
    request = SimpleNamespace(state=SimpleNamespace(request_id="req-9"))
    body = routes_chat.ChatBody(prompt="Is this a Tesla?", session_id="s1", image_ids=["a" * 64])
    prompt = await routes_chat._with_pictures(body, "Is this a Tesla?", request, user)
    assert prompt == "Is this a Tesla?" + MARKER + "\nPicture 1: white Tesla Model Y"
    assert attached == [("u1", "s1", ["a" * 64], "req-9"), ("described", "a" * 64, "white Tesla Model Y")]


def test_hard_ceiling_of_pictures_per_message():   # the working limit (4) is config, checked in attach()
    with pytest.raises(ValueError):
        routes_chat.ChatBody(prompt="x", image_ids=["a" * 64] * 11)



async def test_rate_an_answer_from_the_chat(monkeypatch):
    calls = []

    class Runs:
        def __init__(self, _db):
            pass

        async def topic_in_session(self, *, request_id, user_id, session_id):
            return "ev_research" if (request_id, user_id, session_id) == ("r1", "u1", "abc") else None

        async def set_feedback(self, **kw):
            calls.append(("set", kw["score"], kw["topic"]))
            return True

        async def propose_eval_case(self, **kw):
            calls.append(("case", kw["label"]))

    from agency_platform.agents.schemas import FeedbackBody
    monkeypatch.setattr(rc, "RunRepository", Runs)
    monkeypatch.setattr(rc, "get_db", lambda: None)
    await rc.rate_answer("abc", "r1", FeedbackBody(score=-1, comment="wrong range"), USER)
    assert calls == [("set", -1, "ev_research"), ("case", "negative")]
    await rc.rate_answer("abc", "r1", FeedbackBody(score=0), USER)          # taken back: no eval case
    assert calls[-1] == ("set", 0, "ev_research")
    with pytest.raises(NotFoundError):                                     # other chat / other user
        await rc.rate_answer("other", "r1", FeedbackBody(score=1), USER)
