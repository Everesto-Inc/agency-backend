"""Admin → Experiments: test datasets (upload, versions, preview) and config tests (dataset × live vs draft).

    POST /api/v1/admin/experiments/datasets?name=ev-basics   body = JSONL (one eval case per line, ≤ 2 MB)
    GET  /api/v1/admin/experiments/datasets                  every version, newest first
    GET  /api/v1/admin/experiments/datasets/{id}/preview     the cases (first `limit`) + topics
    POST /api/v1/admin/experiments/tests                     queue: dataset × (live, draft) — cost estimate back
    GET  /api/v1/admin/experiments/tests[/{id}]              status, progress, side-by-side results
    POST /api/v1/admin/experiments/tests/{id}/cancel

Files live in the bucket (agency/datasets/<name>/v<n>.jsonl); the quality worker (CronJob agency-experiments,
every 2 min) runs queued tests as the eval service account, with the draft sent per request.
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, Field

from agency_platform.auth.deps import AdminUser
from agency_platform.config.registry import topics
from agency_platform.config.settings import get_settings
from agency_platform.db import get_db
from agency_platform.errors import AppError, BadRequestError, NotFoundError, ServiceUnavailableError
from agency_platform.evals import EvalCase, parse_jsonl, to_jsonl
from agency_platform.repositories import ConfigTestRepository, DatasetRepository, SettingsRepository
from agency_platform.service import openapi
from agency_platform.tools.objectstore import ObjectStore

experiments_router = APIRouter(tags=["Admin: experiments"], responses=openapi.ADMIN)

MAX_TEST_CASES = 50
DATASET_NAME = r"^[a-z0-9][a-z0-9-]{1,62}$"


class InvalidDatasetError(AppError):
    status_code = 422
    code = "INVALID_DATASET"


class Dataset(BaseModel):
    id: int
    name: str
    version: int
    cases: int
    topics: dict[str, int]
    size_bytes: int
    author: str
    comment: str
    created_at: datetime


class DatasetPreview(BaseModel):
    dataset: Dataset
    total: int
    rows: list[EvalCase]


class TestCreate(BaseModel):
    dataset_id: int
    draft_id: int | None = Field(None, description="None = run the live config only (a benchmark)")
    max_cases: int = Field(20, ge=1, le=MAX_TEST_CASES)


class ConfigTest(BaseModel):
    id: int
    dataset_id: int
    dataset_name: str
    dataset_version: int
    draft_id: int | None
    max_cases: int
    status: str = Field(description="queued · running · cancelling · done · failed · cancelled")
    created_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    done_cases: int
    total_cases: int
    estimated_cost_usd: float | None = None
    summary: dict[str, Any] | None = Field(None, description="per variant (live / draft): pass rate, judge, cost, p95, models")
    results: list[dict[str, Any]] | None = Field(None, description="per case: both variants side by side")
    error: str | None


def _store(request: Request) -> ObjectStore:
    store = ObjectStore.from_settings(get_settings(), request.app.state.http)
    if store is None:
        raise ServiceUnavailableError("Datasets need the bucket (S3_* settings)")
    return store


# =========================================================== datasets
@experiments_router.post("/datasets", status_code=201, summary="Upload a test dataset (JSONL) — a new version",
                         responses=openapi.errors(413, 422, 503))
async def upload_dataset(request: Request, admin: AdminUser,
                         name: str = Query(..., pattern=DATASET_NAME, description="e.g. ev-basics"),
                         comment: str = Query("", max_length=300)) -> Dataset:
    """One JSON object per line: {"id", "topic", "prompt", "checks": {...}, "rubric", "reference", "tags"} —
    same format as evals/datasets/*.jsonl. Errors come back per line; nothing is stored unless all lines are valid."""
    parsed = parse_jsonl(await request.body(), set(topics().enabled()))
    if parsed.errors:
        raise InvalidDatasetError("The file has problems", errors=parsed.errors)
    repo = DatasetRepository(get_db())
    version = await repo.next_version(name)
    key = f"datasets/{name}/v{version}.jsonl"
    data = to_jsonl(parsed.cases)               # normalised: what runs is exactly what the preview shows
    await _store(request).put(key, data, "application/x-ndjson")
    dataset_id = await repo.add(name=name, version=version, cases=len(parsed.cases), topics=parsed.topics,
                                size_bytes=len(data), object_key=key, author=admin.email, comment=comment)
    return Dataset(**(await repo.get(dataset_id)))  # type: ignore[arg-type]


@experiments_router.get("/datasets", summary="Test datasets, every version")
async def list_datasets(_: AdminUser) -> list[Dataset]:
    return [Dataset(**r) for r in await DatasetRepository(get_db()).all()]


@experiments_router.get("/datasets/{dataset_id}/preview", summary="A dataset's cases", responses=openapi.errors(404))
async def preview_dataset(request: Request, _: AdminUser, dataset_id: int = Path(...),
                          limit: int = Query(20, ge=1, le=500)) -> DatasetPreview:
    row = await DatasetRepository(get_db()).get(dataset_id)
    if row is None:
        raise NotFoundError("Dataset not found")
    data, _ctype = await _store(request).get(row["object_key"])
    parsed = parse_jsonl(data)
    return DatasetPreview(dataset=Dataset(**row), total=len(parsed.cases), rows=parsed.cases[:limit])


# =========================================================== config tests
def _test(r: dict[str, Any], estimate: float | None = None) -> ConfigTest:
    return ConfigTest(**{k: r.get(k) for k in ConfigTest.model_fields if k != "estimated_cost_usd"},
                      estimated_cost_usd=estimate)


@experiments_router.post("/tests", status_code=201, summary="Test a draft against live on a dataset (queued)",
                         responses=openapi.errors(400, 404))
async def create_test(body: TestCreate, admin: AdminUser) -> ConfigTest:
    """Each case runs once with the live config and once with the draft (as the eval service account). Starts
    within ~2 min; results stream in. The estimate uses each assistant's recent average cost per question."""
    db = get_db()
    ds = await DatasetRepository(db).get(body.dataset_id)
    if ds is None:
        raise NotFoundError("Dataset not found")
    if body.draft_id is not None:
        draft = await SettingsRepository(db).draft(body.draft_id)
        if draft is None:
            raise NotFoundError("Draft not found")
        if draft["status"] != "draft":
            raise BadRequestError(f"Draft is already {draft['status']}")
    total = min(body.max_cases, ds["cases"])
    tests = ConfigTestRepository(db)
    avg = await tests.avg_cost_per_question()
    variants = 2 if body.draft_id else 1
    per_case = sum(avg.get(t, 0.01) * n for t, n in ds["topics"].items()) / max(ds["cases"], 1)
    test_id = await tests.queue(dataset_id=body.dataset_id, draft_id=body.draft_id, max_cases=body.max_cases,
                                total=total, by=admin.email)
    return _test(await tests.get(test_id), round(per_case * total * variants * 1.2, 4))  # type: ignore[arg-type]  # +20 % judge


@experiments_router.get("/tests", summary="Config tests, newest first (no per-case results)")
async def list_tests(_: AdminUser) -> list[ConfigTest]:
    return [_test(r) for r in await ConfigTestRepository(get_db()).recent()]


@experiments_router.get("/tests/{test_id}", summary="One config test with per-case results", responses=openapi.errors(404))
async def get_test(test_id: int, _: AdminUser) -> ConfigTest:
    r = await ConfigTestRepository(get_db()).get(test_id)
    if r is None:
        raise NotFoundError("Test not found")
    return _test(r)


@experiments_router.post("/tests/{test_id}/cancel", summary="Stop a queued or running test", responses=openapi.errors(404))
async def cancel_test(test_id: int, _: AdminUser) -> ConfigTest:
    repo = ConfigTestRepository(get_db())
    if not await repo.cancel(test_id):
        raise NotFoundError("No queued or running test with this id")
    return _test(await repo.get(test_id, with_results=False))  # type: ignore[arg-type]
