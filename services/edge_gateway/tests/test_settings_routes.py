"""Admin → Settings routes: only differences stored, validation, history, rollback."""
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


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    Repo.rows = []
    bumps = []

    async def bump(_redis):
        bumps.append(1)

    async def apply_live(_db):
        live = await Repo(None).live()
        for name, s in rs.SECTIONS.items():
            s.file().set_override(live.get(name, {}).get("content") or {})
        return 0
    monkeypatch.setattr(rs, "SettingsRepository", Repo)
    monkeypatch.setattr(rs, "get_db", lambda: None)
    monkeypatch.setattr(rs, "get_redis", lambda: None)
    monkeypatch.setattr(rs, "bump", bump)
    monkeypatch.setattr(rs, "apply_live", apply_live)
    yield bumps
    for s in rs.SECTIONS.values():
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
