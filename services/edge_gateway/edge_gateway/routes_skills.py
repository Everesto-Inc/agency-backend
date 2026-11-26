"""Admin → Skills: install instruction packs (SKILL.md folders) from GitHub, review, switch on per assistant.

    GET    /api/v1/admin/skills                            skills + which assistants can use them
    POST   /api/v1/admin/skills/install                    {url} → a new version (a new skill starts OFF)
    GET    /api/v1/admin/skills/{name}                     the skill + every version
    PATCH  /api/v1/admin/skills/{name}                     on/off, assistants, active version (rollback)
    POST   /api/v1/admin/skills/{name}/check-update        newer commit on GitHub?
    DELETE /api/v1/admin/skills/{name}                     the skill and all its versions
    GET    /api/v1/admin/skills/versions/{id}/files[/content?path=]   preview

Nothing is live until the admin switches it on; assistants pick it up within skills.yaml cache_s.
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel, Field

from agency_platform.auth.deps import AdminUser
from agency_platform.config.registry import skills_config, topics
from agency_platform.db import get_db
from agency_platform.errors import BadRequestError, NotFoundError
from agency_platform.repositories import SkillRepository
from agency_platform.service import openapi
from edge_gateway.skills_github import fetch_skill, get_json, parse_github_url

skills_router = APIRouter(tags=["Admin: skills"], responses=openapi.ADMIN)
SkillName = Path(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")


class SkillInstall(BaseModel):
    url: str = Field(..., max_length=500, examples=[
        "https://github.com/alirezarezvani/claude-skills/tree/main/finance/skills/stock-analysis"])
    comment: str = Field("", max_length=300)


class SkippedFile(BaseModel):
    path: str
    reason: str


class SkillWarning(BaseModel):
    path: str
    kind: str = Field(description="injection · scripts")
    detail: str


class SkillVersion(BaseModel):
    id: int
    name: str
    version: int
    description: str
    source_url: str
    repo: str
    ref: str
    commit_sha: str
    path: str
    licence: str
    files: int
    total_bytes: int
    skipped: list[SkippedFile]
    warnings: list[SkillWarning]
    installed_by: str
    comment: str
    created_at: datetime


class Skill(BaseModel):
    name: str
    enabled: bool
    assistants: list[str]
    active: SkillVersion
    latest_version: int
    update_waiting: bool = Field(description="A newer installed version is not active yet")
    updated_by: str
    updated_at: datetime


class AssistantRef(BaseModel):
    id: str
    name: str


class SkillsOverview(BaseModel):
    skills: list[Skill]
    assistants: list[AssistantRef] = Field(description="Assistants that can load skills (config/skills.yaml)")
    max_skills_per_run: int
    allowed_licences: list[str]


class SkillDetail(BaseModel):
    skill: Skill
    versions: list[SkillVersion]


class SkillInstallResult(BaseModel):
    skill: Skill
    installed: SkillVersion
    new_skill: bool
    message: str


class SkillUpdate(BaseModel):
    enabled: bool | None = None
    assistants: list[str] | None = Field(None, max_length=10)
    active_version_id: int | None = Field(None, description="Switch to (or roll back to) this installed version")


class SkillFile(BaseModel):
    path: str
    size_bytes: int


class SkillFileContent(BaseModel):
    path: str
    content: str


class UpdateCheck(BaseModel):
    ref: str
    installed_sha: str
    latest_sha: str
    update_available: bool


def _version(r: dict[str, Any]) -> SkillVersion:
    return SkillVersion(**{k: r[k] for k in SkillVersion.model_fields})


def _skill(r: dict[str, Any]) -> Skill:
    return Skill(name=r["name"], enabled=bool(r["enabled"]), assistants=list(r["assistants"] or []),
                 active=_version(r), latest_version=int(r["latest_version"]),
                 update_waiting=int(r["latest_version"]) > int(r["version"]),
                 updated_by=r["updated_by"], updated_at=r["updated_at"])


async def _get(repo: SkillRepository, name: str) -> dict[str, Any]:
    row = await repo.skill(name)
    if row is None:
        raise NotFoundError("Skill not found")
    return row


def _assistants() -> list[AssistantRef]:
    enabled = topics().enabled()
    return [AssistantRef(id=t, name=enabled[t].display_name)
            for t in skills_config().supported_assistants if t in enabled]


@skills_router.get("", summary="Installed skills")
async def list_skills(_: AdminUser) -> SkillsOverview:
    cfg = skills_config()
    rows = await SkillRepository(get_db()).skills()
    return SkillsOverview(skills=[_skill(r) for r in rows], assistants=_assistants(),
                          max_skills_per_run=cfg.max_skills_per_run, allowed_licences=cfg.allowed_licences)


@skills_router.post("/install", status_code=201, summary="Install a skill from GitHub (a new version)",
                    responses=openapi.errors(422))
async def install_skill(body: SkillInstall, request: Request, admin: AdminUser) -> SkillInstallResult:
    """Downloads the folder's text files at the current commit (scripts are skipped, never run), checks the
    licence and size limits, screens every file for injection patterns. A new skill starts OFF with no assistant;
    a new version of an existing skill is stored but not activated — switch to it on the skill's page."""
    fetched = await fetch_skill(request.app.state.http, body.url)
    repo = SkillRepository(get_db())
    vid, version, is_new = await repo.add_version(
        name=fetched.doc.name, description=fetched.doc.description, source_url=fetched.source.url(fetched.ref),
        repo=fetched.source.full_name, ref=fetched.ref, commit_sha=fetched.commit_sha, path=fetched.source.path,
        licence=fetched.licence, files=fetched.files, skipped=fetched.skipped, warnings=fetched.warnings,
        installed_by=admin.email, comment=body.comment)
    row = await _get(repo, fetched.doc.name)
    installed = _version(await repo.version(vid) or {})
    msg = (f"Installed {fetched.doc.name} v{version} — OFF until you choose assistants and switch it on."
           if is_new else f"Stored {fetched.doc.name} v{version} — not active yet; activate it to use it.")
    return SkillInstallResult(skill=_skill(row), installed=installed, new_skill=is_new, message=msg)


@skills_router.get("/versions/{version_id}/files", summary="Files of a skill version", responses=openapi.errors(404))
async def version_files(_: AdminUser, version_id: int = Path(...)) -> list[SkillFile]:
    repo = SkillRepository(get_db())
    if await repo.version(version_id) is None:
        raise NotFoundError("Version not found")
    return [SkillFile(**f) for f in await repo.file_list(version_id)]


@skills_router.get("/versions/{version_id}/content", summary="One file of a skill version (preview)",
                   responses=openapi.errors(404))
async def version_file(_: AdminUser, version_id: int = Path(...),
                       path: str = Query(..., max_length=300)) -> SkillFileContent:
    text = await SkillRepository(get_db()).file(version_id, path)
    if text is None:
        raise NotFoundError("File not found")
    return SkillFileContent(path=path, content=text)


@skills_router.get("/{name}", summary="A skill and its versions", responses=openapi.errors(404))
async def get_skill(_: AdminUser, name: str = SkillName) -> SkillDetail:
    repo = SkillRepository(get_db())
    row = await _get(repo, name)
    return SkillDetail(skill=_skill(row), versions=[_version(v) for v in await repo.versions(name)])


@skills_router.patch("/{name}", summary="Switch on/off, choose assistants, activate a version",
                     responses=openapi.errors(400, 404))
async def update_skill(body: SkillUpdate, admin: AdminUser, name: str = SkillName) -> Skill:
    repo = SkillRepository(get_db())
    await _get(repo, name)
    if body.assistants is not None:
        allowed = {a.id for a in _assistants()}
        if bad := sorted(set(body.assistants) - allowed):
            raise BadRequestError(f"These assistants can't use skills: {', '.join(bad)}", allowed=sorted(allowed))
    if body.active_version_id is not None:
        v = await repo.version(body.active_version_id)
        if v is None or v["name"] != name:
            raise BadRequestError("That version doesn't belong to this skill")
    await repo.update(name, enabled=body.enabled, assistants=body.assistants,
                      active_version_id=body.active_version_id, by=admin.email)
    return _skill(await _get(repo, name))


@skills_router.post("/{name}/check-update", summary="Is there a newer commit on GitHub?",
                    responses=openapi.errors(404, 422))
async def check_update(request: Request, _: AdminUser, name: str = SkillName) -> UpdateCheck:
    """Compares the installed commit with the branch/tag it came from. Installing the newer one is a separate,
    explicit step (POST /install with the same link) — nothing changes behind the admin's back."""
    row = await _get(SkillRepository(get_db()), name)
    src = parse_github_url(row["source_url"])
    latest = (await get_json(request.app.state.http, f"/repos/{src.full_name}/commits/{row['ref']}"))["sha"]
    return UpdateCheck(ref=row["ref"], installed_sha=row["commit_sha"], latest_sha=latest,
                       update_available=latest != row["commit_sha"])


@skills_router.delete("/{name}", status_code=204, summary="Delete a skill and all its versions",
                      responses=openapi.errors(404))
async def delete_skill(_: AdminUser, name: str = SkillName) -> Response:
    repo = SkillRepository(get_db())
    await _get(repo, name)
    await repo.delete(name)
    return Response(status_code=204)
