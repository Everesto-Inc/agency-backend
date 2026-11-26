import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("PROMPTS_DIR", str(ROOT / "prompts"))

from agency_platform.skills import (
    SkillFormatError,
    SkillLibrary,
    parse_skill_md,
    render_instructions,
    reset_skill_library,
)

SKILL_MD = """---
name: stock-analysis
description: "Analyse or value a listed stock. Use when asked if a stock is a buy."
---

# Stock Analysis

Never invent a number. Compare with sector peers.
"""


class FakeSkillRepo:
    def __init__(self):
        self.files = {7: {"SKILL.md": SKILL_MD, "references/06-valuation.md": "V" * 30_000},
                      8: {"SKILL.md": SKILL_MD.replace("stock-analysis", "fx-notes")}}
        self.rows = [
            {"name": "stock-analysis", "assistants": ["finance", "ev_research"], "version_id": 7, "version": 2,
             "description": "Analyse a stock", "repo": "o/r", "commit_sha": "a" * 40},
            {"name": "fx-notes", "assistants": ["product_finder"], "version_id": 8, "version": 1,
             "description": "FX", "repo": "o/r", "commit_sha": "b" * 40},
        ]

    async def enabled(self):
        return self.rows

    async def file(self, version_id, path):
        return self.files.get(version_id, {}).get(path)

    async def file_list(self, version_id):
        return [{"path": p, "size_bytes": len(c)} for p, c in sorted(self.files.get(version_id, {}).items())]


def test_parse_skill_md():
    doc = parse_skill_md(SKILL_MD)
    assert doc.name == "stock-analysis" and doc.description.startswith("Analyse") and doc.body.startswith("# Stock")
    assert parse_skill_md(SKILL_MD.replace("name: stock-analysis\n", ""), "from-folder").name == "from-folder"
    for bad, why in [("# no front matter", "front matter"),
                     (SKILL_MD.replace('description: "Analyse or value a listed stock. Use when asked if a stock is a buy."', "x: 1"), "description"),
                     (SKILL_MD.replace("stock-analysis", "Bad Name!"), "lowercase"),
                     ("---\nname: a-b\ndescription: d\n---\n", "no instructions")]:
        with pytest.raises(SkillFormatError, match=why):
            parse_skill_md(bad)


async def test_library_loads_only_enabled_skills_of_the_assistant():
    lib = SkillLibrary(FakeSkillRepo())
    assert [s.name for s in await lib.for_assistant("finance")] == ["stock-analysis"]
    assert await lib.for_assistant("product_finder") == []        # not a skills-capable assistant (skills.yaml)
    loaded = await lib.load("finance", ["stock-analysis", "fx-notes", "made-up"])
    assert [s.ref.label for s in loaded] == ["stock-analysis@v2"]
    assert loaded[0].instructions.startswith("# Stock Analysis") and loaded[0].files == ["references/06-valuation.md"]


async def test_reference_files_are_read_in_chunks_and_only_from_skills_in_use():
    lib = SkillLibrary(FakeSkillRepo())
    loaded = await lib.load("finance", ["stock-analysis"])
    part1 = await lib.read(loaded, "stock-analysis", "references/06-valuation.md")
    assert len(part1["content"]) == 12_000 and part1["next_offset"] == 12_000 and part1["total_chars"] == 30_000
    last = await lib.read(loaded, "stock-analysis", "./references/06-valuation.md", offset=24_000)
    assert len(last["content"]) == 6_000 and last["next_offset"] is None
    assert "error" in await lib.read(loaded, "stock-analysis", "../../etc/passwd")
    assert "error" in await lib.read(loaded, "fx-notes", "SKILL.md")     # not loaded for this run


async def test_instructions_are_framed_as_guidance_under_the_assistants_rules():
    lib = SkillLibrary(FakeSkillRepo())
    text = render_instructions(await lib.load("finance", ["stock-analysis"]))
    assert "# Skills for this question" in text and "never override your own rules" in text
    assert "Never invent a number" in text and "read_skill_file" in text and "o/r @ aaaaaaa" in text
    assert render_instructions([]) == ""


async def test_a_run_gets_the_skills_triage_picked(monkeypatch):
    import fakeredis.aioredis
    from test_runner import FakeRuns, collect, make_runner, user

    from agency_platform.agents.base import AgentEvent, TopicAgent
    from agency_platform.agents.runner import RunRequest

    seen = {}

    class Agent(TopicAgent):
        topic, framework = "ev_research", "test"

        async def run(self, prompt, ctx):
            seen["skills"] = [s.ref.name for s in ctx.skills]
            seen["prompt"] = ctx.skill_instructions()
            seen["file"] = await ctx.read_skill_file("stock-analysis", "references/06-valuation.md")
            yield AgentEvent.answer("ok")

    reset_skill_library(SkillLibrary(FakeSkillRepo()))
    try:
        runs = FakeRuns()
        events = await collect(make_runner(Agent(), fakeredis.aioredis.FakeRedis(decode_responses=True), runs),
                               RunRequest(user(), "is X a buy?", "s1", "req-skill-01", skills=["stock-analysis"]))
    finally:
        reset_skill_library(None)
    assert seen["skills"] == ["stock-analysis"] and "Never invent a number" in seen["prompt"]
    assert seen["file"]["content"].startswith("VVV")
    assert any(e.type == "status" and e.content == "Using skill stock-analysis@v2" for e in events)
    assert runs.calls[0][1]["skills"] == "stock-analysis@v2"


async def test_a_broken_skill_store_never_blocks_the_answer():
    import fakeredis.aioredis
    from test_runner import FakeRuns, ScriptedAgent, collect, make_runner, user

    from agency_platform.agents.runner import RunRequest

    class Broken(FakeSkillRepo):
        async def enabled(self):
            raise RuntimeError("db down")

    reset_skill_library(SkillLibrary(Broken()))
    try:
        events = await collect(make_runner(ScriptedAgent(), fakeredis.aioredis.FakeRedis(decode_responses=True), FakeRuns()),
                               RunRequest(user(), "q", "s1", "req-skill-02", skills=["stock-analysis"]))
    finally:
        reset_skill_library(None)
    assert any(e.type == "token" for e in events)
