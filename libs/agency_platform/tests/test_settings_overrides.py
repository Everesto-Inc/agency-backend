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
    for section in ov.SECTIONS.values():
        assert section.file().get() is not None, section.name
