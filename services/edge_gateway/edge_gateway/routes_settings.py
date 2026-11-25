"""Admin → Settings: edit config/*.yaml values without a code change — validated, versioned, live in ~10 s.

    GET  /api/v1/admin/settings                      every section: schema, file values, override, live values
    PUT  /api/v1/admin/settings/{name}               save the values you want (stored: only what differs)
    GET  /api/v1/admin/settings/{name}/history       who changed what, when, why
    POST /api/v1/admin/settings/{name}/rollback/{id} make an old version live again (as a NEW version)

The YAML files stay the defaults (and the source in git); a version with {} = "use the file".
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field, ValidationError

from agency_platform.auth.deps import AdminUser
from agency_platform.cache import get_redis
from agency_platform.config.overrides import SECTIONS, apply_live, bump
from agency_platform.config.params import diff
from agency_platform.db import get_db
from agency_platform.errors import AppError, NotFoundError
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
    if name not in SECTIONS:
        raise NotFoundError(f"Unknown settings section '{name}'", available=list(SECTIONS))
    return SECTIONS[name]


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
    return [_view(name, live.get(name)) for name in SECTIONS]


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
