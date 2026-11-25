"""Admin → Settings routes: only differences stored, validation, history, rollback."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agency_platform.errors import NotFoundError
from edge_gateway import routes_settings as rs

ADMIN = SimpleNamespace(id="a1", email="admin@example.com")


class Repo:
    rows: list = []

    def __init__(self, _db):
        pass

    async def add(self, name, content, author, comment):
        Repo.rows.append({"id": len(Repo.rows) + 1, "name": name, "content": content, "author": author,
                          "comment": comment, "created_at": datetime.now(UTC)})
        return len(Repo.rows)

    async def live(self):
        out = {}
        for r in Repo.rows:
            out[r["name"]] = r
        return out

    async def history(self, name):
        return [r for r in reversed(Repo.rows) if r["name"] == name]

    async def get(self, vid):
        return next((r for r in Repo.rows if r["id"] == vid), None)

    drafts_: list = []

    async def add_draft(self, content, based_on, author, comment, source):
        Repo.drafts_.append({"id": len(Repo.drafts_) + 1, "content": content, "based_on": based_on, "author": author,
                             "comment": comment, "source": source, "status": "draft", "created_at": datetime.now(UTC),
                             "decided_at": None, "decided_by": None})
        return len(Repo.drafts_)

    async def draft(self, did):
        return next((d for d in Repo.drafts_ if d["id"] == did), None)

    async def drafts(self):
        return list(reversed(Repo.drafts_))

    async def decide_draft(self, did, status, by):
        d = await self.draft(did)
        if d is None or d["status"] != "draft":
            return False
        d.update(status=status, decided_by=by, decided_at=datetime.now(UTC))
        return True


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    Repo.rows = []
    Repo.drafts_ = []
    bumps = []

    async def bump(_redis):
        bumps.append(1)

    async def apply_live(_db):
        live = await Repo(None).live()
        for name, s in rs.sections().items():
            s.file().set_override(live.get(name, {}).get("content") or {})
        return 0
    monkeypatch.setattr(rs, "SettingsRepository", Repo)
    monkeypatch.setattr(rs, "get_db", lambda: None)
    monkeypatch.setattr(rs, "get_redis", lambda: None)
    monkeypatch.setattr(rs, "bump", bump)
    monkeypatch.setattr(rs, "apply_live", apply_live)
    yield bumps
    for s in rs.sections().values():
        s.file().set_override({})


async def test_save_stores_only_the_difference_and_goes_live(fakes):
    sections = {s.name: s for s in await rs.list_settings(ADMIN)}
    values = sections["memory"].values | {"recent": 6}
    out = await rs.update_settings(rs.SettingsUpdate(values=values, comment="longer memory"), ADMIN, "memory")
    assert Repo.rows[0]["content"] == {"recent": 6} and Repo.rows[0]["author"] == "admin@example.com"
    assert out.values["recent"] == 6 and out.override == {"recent": 6} and fakes == [1]
    assert "properties" in out.json_schema


async def test_invalid_values_are_refused_with_field_errors():
    with pytest.raises(rs.InvalidSettingsError) as err:
        await rs.update_settings(rs.SettingsUpdate(values={"recent": 999}), ADMIN, "memory")
    assert err.value.details["errors"][0]["field"] == "recent" and Repo.rows == []


async def test_history_and_rollback_make_a_new_version():
    await rs.update_settings(rs.SettingsUpdate(values={"recent": 6}), ADMIN, "memory")
    await rs.update_settings(rs.SettingsUpdate(values={"recent": 2}), ADMIN, "memory")
    out = await rs.rollback_settings(ADMIN, 1, "memory")
    assert out.values["recent"] == 6 and len(Repo.rows) == 3 and Repo.rows[-1]["comment"] == "rollback to version #1"
    assert [v.id for v in await rs.settings_history(ADMIN, "memory")] == [3, 2, 1]
    with pytest.raises(NotFoundError):
        await rs.rollback_settings(ADMIN, 1, "plans")            # version of another section


async def test_unknown_section():
    with pytest.raises(NotFoundError):
        await rs.settings_history(ADMIN, "nope")



async def test_export_then_import_makes_a_draft_not_a_change():
    exported = json.loads((await rs.export_settings(ADMIN)).body)
    assert exported["format"] == "agency-config" and {"plans", "models", "prompts_ev_research"} <= set(exported["sections"])
    exported["sections"]["memory"]["recent"] = 7
    exported["sections"]["guardrails"]["injection"]["mode"] = "block"
    view = await rs.import_settings(rs.ConfigFile(**exported), ADMIN)
    assert view.status == "draft" and Repo.rows == []                      # nothing live yet
    changes = {(s.name, c.path): (c.live, c.draft) for s in view.sections for c in s.changes}
    assert changes == {("memory", "recent"): (4, 7), ("guardrails", "injection.mode"): ("flag", "block")}
    assert rs.sections()["memory"].file().get().recent == 4


async def test_invalid_import_is_refused_with_section_and_field():
    with pytest.raises(rs.InvalidSettingsError) as err:
        await rs.import_settings(rs.ConfigFile(sections={"memory": {"recent": 999}, "nope": {}}), ADMIN)
    fields = {e["field"] for e in err.value.details["errors"]}
    assert fields == {"memory.recent", "nope"}
    with pytest.raises(rs.InvalidSettingsError):
        await rs.import_settings(rs.ConfigFile(format="something-else", sections={}), ADMIN)


async def test_apply_writes_versions_for_changed_sections_only(fakes):
    view = await rs.create_draft(rs.DraftCreate(sections={"memory": {"recent": 6}}, comment="try 6"), ADMIN)
    applied = await rs.apply_draft(view.id, ADMIN)
    assert applied.status == "applied" and [(r["name"], r["content"]) for r in Repo.rows] == [("memory", {"recent": 6})]
    assert Repo.rows[0]["comment"] == f"draft #{view.id}: try 6" and fakes == [1]
    assert rs.sections()["memory"].file().get().recent == 6
    with pytest.raises(rs.ConflictError):
        await rs.apply_draft(view.id, ADMIN)                                # only once


async def test_discard_and_stale_drafts():
    d1 = await rs.create_draft(rs.DraftCreate(sections={"memory": {"recent": 6}}), ADMIN)
    await rs.update_settings(rs.SettingsUpdate(values={"recent": 2}), ADMIN, "memory")   # live changed after the draft
    assert (await rs.get_draft(d1.id, ADMIN)).stale == ["memory"]
    assert (await rs.discard_draft(d1.id, ADMIN)).status == "discarded"
