"""Test datasets: JSONL parsing with per-line errors."""
import json

from agency_platform.evals import MAX_DATASET_BYTES, parse_jsonl, to_jsonl

OK = {"id": "ev-1", "topic": "ev_research", "prompt": "Range of the Ioniq 5?", "checks": {"must_contain_any": ["km"]}}


def jl(*rows) -> bytes:
    return "\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows).encode()


def test_valid_file_and_round_trip():
    p = parse_jsonl(jl(OK, "", {**OK, "id": "ev-2"}), {"ev_research"})
    assert p.errors == [] and [c.id for c in p.cases] == ["ev-1", "ev-2"] and p.topics == {"ev_research": 2}
    assert [c.id for c in parse_jsonl(to_jsonl(p.cases)).cases] == ["ev-1", "ev-2"]


def test_every_problem_is_reported_with_its_line():
    p = parse_jsonl(jl(OK, "{not json", {"id": "x", "topic": "ev_research"}, {**OK, "topic": "weather"}, OK),
                    {"ev_research"})
    assert [e["line"] for e in p.errors] == [2, 3, 4, 5]
    msgs = " | ".join(e["message"] for e in p.errors)
    assert "not JSON" in msgs and "prompt" in msgs and "unknown topic 'weather'" in msgs and "duplicate id 'ev-1'" in msgs


def test_empty_too_large_not_utf8():
    assert parse_jsonl(b"\n\n").errors[0]["message"] == "no cases in the file"
    assert "larger than" in parse_jsonl(b"x" * (MAX_DATASET_BYTES + 1)).errors[0]["message"]
    assert parse_jsonl(b"\xff\xfe").errors[0]["message"] == "not UTF-8 text"
