"""Admin → Experiments routes: dataset upload (versioned, line errors), preview, queue a test with an estimate."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agency_platform.errors import BadRequestError, NotFoundError
from edge_gateway import routes_experiments as rx

ADMIN = SimpleNamespace(id="a1", email="admin@example.com")
NOW = datetime.now(UTC)
CASE = {"id": "ev-1", "topic": "ev_research", "prompt": "Range of the Ioniq 5?"}


class Store:
    objects: dict = {}

    async def put(self, key, data, ctype):
        Store.objects[key] = data

    async def get(self, key):
        return Store.objects[key], "application/x-ndjson"


class Datasets:
    rows: list = []

    def __init__(self, _db):
        pass

    async def next_version(self, name):
        return 1 + sum(1 for r in Datasets.rows if r["name"] == name)

    async def add(self, **kw):
        Datasets.rows.append({**kw, "id": len(Datasets.rows) + 1, "created_at": NOW})
        return len(Datasets.rows)

    async def get(self, i):
        return next((r for r in Datasets.rows if r["id"] == i), None)

    async def all(self):
        return list(reversed(Datasets.rows))


class Tests:
    queued: list = []

    def __init__(self, _db):
        pass

    async def avg_cost_per_question(self):
        return {"ev_research": 0.01}

    async def queue(self, **kw):
        Tests.queued.append(kw)
        return 1

    async def get(self, i, with_results=True):
        q = Tests.queued[0]
        return {"id": 1, "dataset_id": q["dataset_id"], "dataset_name": "ev-basics", "dataset_version": 1,
                "draft_id": q["draft_id"], "max_cases": q["max_cases"], "status": "queued", "created_by": q["by"],
                "created_at": NOW, "started_at": None, "finished_at": None, "done_cases": 0, "total_cases": q["total"],
                "summary": None, "results": None, "error": None}


class Settings:
    def __init__(self, _db):
        pass

    async def draft(self, i):
        return {"id": i, "status": "draft"} if i == 12 else ({"id": i, "status": "applied"} if i == 13 else None)


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    Store.objects, Datasets.rows, Tests.queued = {}, [], []
    monkeypatch.setattr(rx, "_store", lambda _req: Store())
    monkeypatch.setattr(rx, "DatasetRepository", Datasets)
    monkeypatch.setattr(rx, "ConfigTestRepository", Tests)
    monkeypatch.setattr(rx, "SettingsRepository", Settings)
    monkeypatch.setattr(rx, "get_db", lambda: None)


def req(body: bytes):
    async def read():
        return body
    return SimpleNamespace(body=read)


async def test_upload_is_versioned_and_normalised():
    body = (json.dumps(CASE) + "\n" + json.dumps({**CASE, "id": "ev-2"}) + "\n").encode()
    first = await rx.upload_dataset(req(body), ADMIN, "ev-basics", "first")
    second = await rx.upload_dataset(req(body), ADMIN, "ev-basics", "")
    assert (first.version, second.version, first.cases, first.topics) == (1, 2, 2, {"ev_research": 2})
    assert set(Store.objects) == {"datasets/ev-basics/v1.jsonl", "datasets/ev-basics/v2.jsonl"}
    preview = await rx.preview_dataset(None, ADMIN, first.id, 1)
    assert preview.total == 2 and [c.id for c in preview.rows] == ["ev-1"]


async def test_bad_file_stores_nothing_and_says_which_line():
    with pytest.raises(rx.InvalidDatasetError) as err:
        await rx.upload_dataset(req(b'{"id": "x"}\n' + json.dumps(CASE).encode()), ADMIN, "bad", "")
    assert err.value.details["errors"][0]["line"] == 1 and Store.objects == {}


async def test_queue_a_test_with_estimate_and_checks():
    await rx.upload_dataset(req(json.dumps(CASE).encode()), ADMIN, "ev-basics", "")
    t = await rx.create_test(rx.TestCreate(dataset_id=1, draft_id=12, max_cases=20), ADMIN)
    assert t.status == "queued" and t.total_cases == 1 and t.estimated_cost_usd == pytest.approx(0.024)  # 1 × 2 × 0.01 × 1.2
    with pytest.raises(NotFoundError):
        await rx.create_test(rx.TestCreate(dataset_id=1, draft_id=99), ADMIN)
    with pytest.raises(BadRequestError):
        await rx.create_test(rx.TestCreate(dataset_id=1, draft_id=13), ADMIN)   # already applied
    with pytest.raises(ValueError):
        rx.TestCreate(dataset_id=1, max_cases=51)
