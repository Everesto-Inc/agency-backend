"""Admin → Settings: edit config/*.yaml values without a code change — validated, versioned, live in ~10 s.

    GET  /api/v1/admin/settings                      every section: schema, file values, override, live values
    PUT  /api/v1/admin/settings/{name}               save the values you want (stored: only what differs)
    GET  /api/v1/admin/settings/{name}/history       who changed what, when, why
    POST /api/v1/admin/settings/{name}/rollback/{id} make an old version live again (as a NEW version)
    GET  /api/v1/admin/settings/export               every section's live values as ONE JSON file
    POST /api/v1/admin/settings/import               such a file → a DRAFT (nothing goes live) + what changes
    POST /api/v1/admin/settings/drafts               edited values → a draft (to test it first)
    GET  /api/v1/admin/settings/drafts[/{id}]        drafts with their changes vs live
    POST /api/v1/admin/settings/drafts/{id}/apply    draft → live (one new version per changed section)
    POST /api/v1/admin/settings/drafts/{id}/discard

The YAML files stay the defaults (and the source in git); a version with {} = "use the file".
"""
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, Field, ValidationError

from agency_platform.auth.deps import AdminUser
from agency_platform.cache import get_redis
from agency_platform.config.overrides import apply_live, bump, sections
from agency_platform.config.params import deep_merge, diff
from agency_platform.db import get_db
from agency_platform.errors import AppError, ConflictError, NotFoundError
from agency_platform.repositories import SettingsRepository
from agency_platform.service import openapi

settings_router = APIRouter(tags=["Admin: settings"], responses=openapi.ADMIN)
SectionName = Path(pattern=r"^[a-z_]{1,32}$")


class InvalidSettingsError(AppError):
    status_code = 422
    code = "INVALID_SETTINGS"


class Version(BaseModel):
    id: int
    author: str
    comment: str
    created_at: datetime
    content: dict[str, Any] = Field(description="What differed from the file in this version ({} = the file as is)")


class SettingsSection(BaseModel):
    name: str
    title: str
    description: str
    json_schema: dict[str, Any] = Field(description="JSON Schema of the values (for the form)")
    file_values: dict[str, Any] = Field(description="config/<name>.yaml as written — the defaults")
    override: dict[str, Any] = Field(description="What Admin → Settings changed ({} = nothing)")
    values: dict[str, Any] = Field(description="What the services use now: file + override")
    version: Version | None = None
    override_rejected: str | None = Field(None, description="Set if the override doesn't validate with the file")


class SettingsUpdate(BaseModel):
    values: dict[str, Any] = Field(..., description="All values as you want them (only differences are stored)")
    comment: str = Field("", max_length=300)


def _section(name: str):
    all_ = sections()
    if name not in all_:
        raise NotFoundError(f"Unknown settings section '{name}'", available=list(all_))
    return all_[name]


def _view(name: str, version: dict[str, Any] | None) -> SettingsSection:
    s = _section(name)
    f = s.file()
    value = f.get()
    return SettingsSection(
        name=name, title=s.title, description=s.description, json_schema=f.model.model_json_schema(),
        file_values=f.file_values(), override=f.override, values=value.model_dump(mode="json"),
        version=Version(**version) if version else None, override_rejected=f.override_rejected,
    )


async def _save(name: str, override: dict[str, Any], author: str, comment: str) -> SettingsSection:
    f = _section(name).file()
    try:
        f.validate_with(override)
    except ValidationError as exc:
        raise InvalidSettingsError("These values are not valid", errors=[
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]) from exc
    repo = SettingsRepository(get_db())
    await repo.add(name, override, author, comment)
    await bump(get_redis())                      # every other service applies it within ~10 s
    await apply_live(get_db())                   # this one: now
    return _view(name, (await repo.live()).get(name))


@settings_router.get("", summary="Every editable settings section")
async def list_settings(_: AdminUser) -> list[SettingsSection]:
    live = await SettingsRepository(get_db()).live()
    await apply_live(get_db())
    return [_view(name, live.get(name)) for name in sections()]


# =========================================================== export / import / drafts
FORMAT = "agency-config"


class ConfigFile(BaseModel):
    """The JSON file you download / upload: every section's values (all of them, not only changes)."""
    format: str = Field(FORMAT, description='Always "agency-config"')
    format_version: int = 1
    exported_at: datetime | None = None
    exported_by: str | None = None
    sections: dict[str, dict[str, Any]] = Field(..., description="section name → its values")


class Change(BaseModel):
    path: str
    live: Any = None
    draft: Any = None


class DraftSection(BaseModel):
    name: str
    title: str
    changes: list[Change]
    errors: list[dict[str, str]] = Field(default_factory=list)


class DraftView(BaseModel):
    id: int
    status: str
    source: str
    author: str
    comment: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    sections: list[DraftSection] = Field(description="Only sections the draft changes vs what is live NOW")
    stale: list[str] = Field(default_factory=list,
                             description="Sections changed live after the draft was made (applying overwrites them)")


class DraftCreate(BaseModel):
    sections: dict[str, dict[str, Any]] = Field(..., description="section name → values as you want them")
    comment: str = Field("", max_length=300)


def _leaves(base: Any, new: Any, prefix: str = "") -> list[Change]:
    """Pure: changed leaf paths between two value trees (for the preview)."""
    if isinstance(base, dict) and isinstance(new, dict):
        out: list[Change] = []
        for k in sorted(set(base) | set(new)):
            out += _leaves(base.get(k), new.get(k), f"{prefix}.{k}" if prefix else str(k))
        return out
    return [] if base == new else [Change(path=prefix, live=base, draft=new)]


def _check(name: str, values: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """values as wanted → (override vs the file, field errors)."""
    f = _section(name).file()
    override = diff(f.file_values(), values)
    try:
        f.validate_with(override)
        return override, []
    except ValidationError as exc:
        return override, [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]


def _draft_view(d: dict[str, Any], live: dict[str, dict[str, Any]]) -> DraftView:
    out, stale = [], []
    all_ = sections()
    for name, override in sorted(d["content"].items()):
        if name not in all_:
            out.append(DraftSection(name=name, title=name, changes=[], errors=[{"field": "", "message": "unknown section"}]))
            continue
        f = all_[name].file()
        live_values = f._live().model_dump(mode="json")
        try:
            draft_values = f.validate_with(override).model_dump(mode="json")
            errors: list[dict[str, str]] = []
        except ValidationError as exc:
            draft_values = deep_merge(f.file_values(), override)
            errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        changes = _leaves(live_values, draft_values)
        if changes or errors:
            out.append(DraftSection(name=name, title=all_[name].title, changes=changes, errors=errors))
        if (live.get(name) or {}).get("id") != d["based_on"].get(name):
            stale.append(name)
    return DraftView(**{k: d[k] for k in ("id", "status", "source", "author", "comment", "created_at", "decided_at",
                                          "decided_by")}, sections=out, stale=[s for s in stale if s in d["content"]])


async def _new_draft(values_by_section: dict[str, dict[str, Any]], author: str, comment: str, source: str) -> DraftView:
    content, errors = {}, []
    for name, values in values_by_section.items():
        if name not in sections():
            errors.append({"field": name, "message": "unknown section"})
            continue
        override, errs = _check(name, values)
        errors += [{"field": f"{name}.{e['field']}".rstrip("."), "message": e["message"]} for e in errs]
        content[name] = override
    if errors:
        raise InvalidSettingsError("These values are not valid", errors=errors)
    repo = SettingsRepository(get_db())
    live = await repo.live()
    based_on = {name: (live.get(name) or {}).get("id") for name in content}
    draft_id = await repo.add_draft(content, based_on, author, comment, source)
    return _draft_view(await repo.draft(draft_id), live)  # type: ignore[arg-type]


@settings_router.get("/export", summary="Download every section's live values as one JSON file",
                     response_model=ConfigFile)
async def export_settings(admin: AdminUser) -> Response:
    await apply_live(get_db())
    body = ConfigFile(exported_at=datetime.now(UTC), exported_by=admin.email, sections={
        name: s.file()._live().model_dump(mode="json") for name, s in sections().items()})
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return Response(body.model_dump_json(indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="agency-config-{stamp}.json"'})


@settings_router.post("/import", summary="Upload a config file → a draft (nothing goes live yet)", status_code=201,
                      responses=openapi.errors(422))
async def import_settings(body: ConfigFile, admin: AdminUser, comment: str = "") -> DraftView:
    """Sections you leave out stay as they are. Check the changes, test the draft (Admin → Experiments), then apply."""
    if body.format != FORMAT:
        raise InvalidSettingsError(f'Not an agency config file (format must be "{FORMAT}")')
    return await _new_draft(body.sections, admin.email, comment or "imported file", "import")


@settings_router.post("/drafts", summary="Save edited values as a draft (to test before applying)", status_code=201,
                      responses=openapi.errors(422))
async def create_draft(body: DraftCreate, admin: AdminUser) -> DraftView:
    return await _new_draft(body.sections, admin.email, body.comment, "edit")


@settings_router.get("/drafts", summary="Drafts, newest first")
async def list_drafts(_: AdminUser) -> list[DraftView]:
    repo = SettingsRepository(get_db())
    live = await repo.live()
    return [_draft_view(d, live) for d in await repo.drafts()]


@settings_router.get("/drafts/{draft_id}", summary="One draft with its changes vs live", responses=openapi.errors(404))
async def get_draft(draft_id: int, _: AdminUser) -> DraftView:
    repo = SettingsRepository(get_db())
    d = await repo.draft(draft_id)
    if d is None:
        raise NotFoundError("Draft not found")
    return _draft_view(d, await repo.live())


@settings_router.post("/drafts/{draft_id}/apply", summary="Make a draft live", responses=openapi.errors(404, 409, 422))
async def apply_draft(draft_id: int, admin: AdminUser) -> DraftView:
    repo = SettingsRepository(get_db())
    d = await repo.draft(draft_id)
    if d is None:
        raise NotFoundError("Draft not found")
    view = _draft_view(d, await repo.live())
    if d["status"] != "draft":
        raise ConflictError(f"Draft is already {d['status']}")
    if any(s.errors for s in view.sections):
        raise InvalidSettingsError("The draft is not valid with the current files", sections=[s.name for s in view.sections if s.errors])
    if not await repo.decide_draft(draft_id, "applied", admin.email):
        raise ConflictError("Draft was decided meanwhile")
    for s in view.sections:
        await repo.add(s.name, d["content"][s.name], admin.email, f"draft #{draft_id}: {d['comment']}"[:300])
    await bump(get_redis())
    await apply_live(get_db())
    return _draft_view(await repo.draft(draft_id), await repo.live())  # type: ignore[arg-type]


@settings_router.post("/drafts/{draft_id}/discard", summary="Throw a draft away", responses=openapi.errors(404, 409))
async def discard_draft(draft_id: int, admin: AdminUser) -> DraftView:
    repo = SettingsRepository(get_db())
    if await repo.draft(draft_id) is None:
        raise NotFoundError("Draft not found")
    if not await repo.decide_draft(draft_id, "discarded", admin.email):
        raise ConflictError("Draft is not open any more")
    return _draft_view(await repo.draft(draft_id), await repo.live())  # type: ignore[arg-type]


@settings_router.put("/{name}", summary="Save a section (validated; live in ~10 s)", responses=openapi.errors(404, 422))
async def update_settings(body: SettingsUpdate, admin: AdminUser, name: str = SectionName) -> SettingsSection:
    """Send all values as you want them; only what differs from the file is stored. Sending the file values
    (or {} → see reset) means "use the file"."""
    f = _section(name).file()
    return await _save(name, diff(f.file_values(), body.values), admin.email, body.comment)


@settings_router.get("/{name}/history", summary="Versions of a section, newest first", responses=openapi.errors(404))
async def settings_history(admin: AdminUser, name: str = SectionName) -> list[Version]:
    _section(name)
    return [Version(**v) for v in await SettingsRepository(get_db()).history(name)]


@settings_router.post("/{name}/rollback/{version_id}", summary="Make an old version live again (as a new version)",
                      responses=openapi.errors(404, 422))
async def rollback_settings(admin: AdminUser, version_id: int, name: str = SectionName) -> SettingsSection:
    old = await SettingsRepository(get_db()).get(version_id)
    if old is None or old["name"] != name:
        raise NotFoundError("Version not found")
    return await _save(name, old["content"], admin.email, f"rollback to version #{version_id}")
