"""Admin → Settings: overrides merged over the YAML files, validated, synced live via a Redis counter."""
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from agency_platform.agents import sessions
from agency_platform.config import overrides as ov
from agency_platform.config.params import ParamFile, deep_merge, diff
from agency_platform.config.schemas import MemoryFile


def test_deep_merge_and_diff_are_inverse():
    base = {"a": 1, "b": {"c": 2, "d": [1, 2]}, "e": "x"}
    desired = {"a": 1, "b": {"c": 3, "d": [1, 2]}, "e": "y"}
    assert diff(base, desired) == {"b": {"c": 3}, "e": "y"}
    assert deep_merge(base, diff(base, desired)) == desired
    assert diff(base, base) == {}


@pytest.fixture
def memfile(tmp_path):
    path = tmp_path / "memory.yaml"
    path.write_text("recent: 4\nfold_at_least: 6\nmax_turns: 20\nttl_hours: 24\n")
    return ParamFile(path, MemoryFile)


def test_override_on_top_of_the_file(memfile):
    assert memfile.get().recent == 4
    memfile.set_override({"recent": 6})
    assert memfile.get().recent == 6 and memfile.get().max_turns == 20
    assert memfile.file_values()["recent"] == 4                 # the file stays the default
    memfile.set_override({})
    assert memfile.get().recent == 4


def test_invalid_override_is_ignored_not_fatal(memfile):
    memfile.set_override({"recent": 999})                       # le=20
    assert memfile.get().recent == 4 and memfile.override_rejected
    with pytest.raises(ValidationError):
        memfile.validate_with({"recent": 999})


def test_memory_knobs_are_read_live(monkeypatch, memfile):
    monkeypatch.setattr(sessions, "memory", memfile.get)
    history = [{"role": "user", "content": str(i), "at": f"2026-11-25T10:{i:02d}"} for i in range(10)]
    assert len(sessions.messages_to_fold(history, None)) == 6     # 10 − recent 4
    memfile.set_override({"recent": 2, "fold_at_least": 2})
    assert len(sessions.messages_to_fold(history, None)) == 8
    assert "User question: q" in sessions.with_history("q", history) \
        and sessions.with_history("q", history).count("user: ") == 2


async def test_sync_applies_only_when_the_counter_changes(monkeypatch):
    applied = []

    async def apply_live(db):
        applied.append(db)
        return 1
    monkeypatch.setattr(ov, "apply_live", apply_live)

    class Redis:
        rev = None

        async def get(self, key):
            return self.rev
    redis = Redis()
    sync = ov.SettingsSync("db", redis)
    assert await sync.check() is True                           # first time: always load
    assert await sync.check() is False                          # nothing changed
    redis.rev = "7"
    assert await sync.check() is True and len(applied) == 2


def test_every_section_has_a_valid_file():
    for section in ov.sections().values():
        assert section.file().get() is not None, section.name


# ---- drafts: a request can test a config without anyone else seeing it ------------------------------------
from agency_platform.config import drafts as dr
from agency_platform.llm.call_token import CallClaims, mint_call_token, verify_call_token


def test_header_parsing():
    assert dr.parse_header("12") == 12 and dr.parse_header(" 7 ") == 7
    assert dr.parse_header(None) is None and dr.parse_header("abc") is None and dr.parse_header("0") is None


async def test_a_draft_applies_only_inside_its_request(memfile):
    import asyncio
    import contextvars
    memfile.section = "memory"

    async def in_request():
        dr.use_draft(dr.Draft(id=1, sections={"memory": {"recent": 8}}))
        await asyncio.sleep(0)
        return memfile.get().recent

    inside = await asyncio.create_task(in_request(), context=contextvars.copy_context())
    assert inside == 8 and memfile.get().recent == 4               # outside the request: live


def test_invalid_draft_falls_back_to_live(memfile):
    memfile.section = "memory"
    token = dr._current.set(dr.Draft(id=2, sections={"memory": {"recent": 999}}))
    try:
        assert memfile.get().recent == 4
    finally:
        dr._current.reset(token)


def test_call_token_carries_the_draft_signed():
    secret = "s" * 40
    t = mint_call_token(CallClaims("ev_research", "u1", "pro", "r1", config_draft=12), secret, 60)
    assert verify_call_token(t, secret).config_draft == 12
    assert verify_call_token(mint_call_token(CallClaims("ev_research", "u1", "pro", "r1"), secret, 60), secret).config_draft is None


async def test_only_admins_and_service_accounts_may_send_a_draft(monkeypatch):
    from types import SimpleNamespace

    from agency_platform.auth import deps
    from agency_platform.errors import ForbiddenError

    async def auth(token, tokens, directory):
        return deps.AuthenticatedUser(id="u1", email="x@example.com", role=token, plan="pro")

    async def activate(draft_id):
        return dr.Draft(id=draft_id) if draft_id == 5 else None
    monkeypatch.setattr(deps, "authenticate_token", auth)
    monkeypatch.setattr(deps, "activate", activate)
    req = SimpleNamespace(headers={dr.HEADER: "5"})
    creds = lambda role: SimpleNamespace(scheme="Bearer", credentials=role)  # noqa: E731
    with pytest.raises(ForbiddenError):
        await deps.get_current_user(req, creds("user"), None, None)
    assert (await deps.get_current_user(req, creds("service"), None, None)).role == "service"
    with pytest.raises(ForbiddenError):
        await deps.get_current_user(SimpleNamespace(headers={dr.HEADER: "6"}), creds("admin"), None, None)


def test_sections_include_models_and_every_prompt_file():
    names = set(ov.sections())
    assert {"plans", "models", "guardrails", "memory", "privacy", "evals"} <= names
    assert {"prompts_ev_research", "prompts_memory", "prompts_vision"} <= names
    for s in ov.sections().values():
        assert s.file().section == s.name                                  # drafts can find every file
