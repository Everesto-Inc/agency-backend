"""Install a skill from GitHub: one folder with a SKILL.md, pinned to the exact commit.

    https://github.com/<owner>/<repo>                         SKILL.md at the repo root
    https://github.com/<owner>/<repo>/tree/<ref>/<folder>     a folder (the usual case)
    https://github.com/<owner>/<repo>/blob/<ref>/<folder>/SKILL.md

Text only: SKILL.md + references (skills.yaml text_extensions). Scripts and binaries are listed as skipped and
never stored or run. Checks: licence allow-list, file count/size limits, the injection screen on every file
(findings are shown to the admin as warnings). Uses GitHub's public API without a token (60 calls/hour/IP —
an install needs 4).
"""
import asyncio
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from agency_platform.config.registry import guardrails, skills_config
from agency_platform.errors import AppError
from agency_platform.security.injection import check
from agency_platform.skills import SkillDoc, SkillFormatError, parse_skill_md

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SCRIPT_EXT = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".ps1", ".bat",
               ".cmd", ".go", ".rs", ".java", ".exe", ".jar", ".wasm", ".so", ".dll", ".bin"}
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "agency-skills-installer"}


class SkillInstallError(AppError):
    status_code = 422
    code = "SKILL_INSTALL_FAILED"


@dataclass(frozen=True)
class SkillSource:
    owner: str
    repo: str
    ref: str | None
    path: str          # folder inside the repo, "" = root

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def url(self, ref: str) -> str:
        return f"https://github.com/{self.full_name}/tree/{ref}/{self.path}".rstrip("/")


def parse_github_url(url: str) -> SkillSource:
    """Pure. The ref is one path segment (a branch, tag or commit without '/')."""
    u = urlsplit(url.strip())
    if u.scheme != "https" or u.hostname not in ("github.com", "www.github.com"):
        raise SkillInstallError("Paste a https://github.com/… link to a skill folder")
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2 or not all(_PART.match(p) for p in parts[:2]):
        raise SkillInstallError("The link needs at least github.com/<owner>/<repo>")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if len(parts) == 2:
        return SkillSource(owner, repo, None, "")
    if parts[2] not in ("tree", "blob") or len(parts) < 4:
        raise SkillInstallError("Use a folder link like github.com/<owner>/<repo>/tree/<branch>/<folder>")
    ref, rest = parts[3], parts[4:]
    if rest and rest[-1] == "SKILL.md":
        rest = rest[:-1]
    path = posixpath.normpath("/".join(rest)) if rest else ""
    if path in (".", ""):
        path = ""
    if path.startswith("/") or ".." in path.split("/"):
        raise SkillInstallError("That folder path is not valid")
    return SkillSource(owner, repo, ref, path)


@dataclass
class FetchedSkill:
    source: SkillSource
    ref: str
    commit_sha: str
    licence: str
    doc: SkillDoc
    files: dict[str, str]                                   # relative path → text
    skipped: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


async def get_json(http: httpx.AsyncClient, path: str, *, allow_404: bool = False) -> Any:
    try:
        r = await http.get(f"{API}{path}", headers=_HEADERS, timeout=15)
    except httpx.HTTPError as exc:
        raise SkillInstallError(f"GitHub could not be reached ({type(exc).__name__})") from exc
    if r.status_code == 404 and allow_404:
        return None
    if r.status_code == 404:
        raise SkillInstallError("Not found on GitHub (private repo, wrong branch or folder?)")
    if r.status_code in (403, 429):
        raise SkillInstallError("GitHub's rate limit was reached — try again in an hour")
    if r.status_code != 200:
        raise SkillInstallError(f"GitHub answered {r.status_code}")
    return r.json()


def classify(rel: str, size: int) -> str | None:
    """Pure: why a file is skipped (None = keep)."""
    cfg = skills_config()
    ext = posixpath.splitext(rel)[1].lower()
    if ext in _SCRIPT_EXT:
        return "script — never stored or run"
    if ext not in cfg.text_extensions and posixpath.basename(rel) != "SKILL.md":
        return "not a text file we use"
    if size > cfg.max_file_bytes:
        return f"too big ({size:,} bytes, limit {cfg.max_file_bytes:,})"
    return None


def screen(files: dict[str, str]) -> list[dict[str, Any]]:
    """Pure: injection patterns per file, and scripts the instructions expect (they won't run here)."""
    out: list[dict[str, Any]] = []
    threshold = guardrails().injection.threshold
    for rel, text in files.items():
        found = check(text)
        if found.is_attempt(threshold):
            out.append({"path": rel, "kind": "injection", "detail": f"{found.detail} (score {found.score})"})
    body = files.get("SKILL.md", "")
    if re.search(r"\b(python3?|bash|node|npx|sh)\s+[\w\"'<>/.-]*\.(py|sh|js|ts)\b", body):
        out.append({"path": "SKILL.md", "kind": "scripts",
                    "detail": "the instructions mention scripts — they will not run here; assistants use their own tools"})
    return out


async def fetch_skill(http: httpx.AsyncClient, url: str) -> FetchedSkill:
    cfg = skills_config()
    src = parse_github_url(url)
    repo_path = f"/repos/{src.owner}/{src.repo}"
    ref = src.ref or (await get_json(http, repo_path))["default_branch"]
    commit = await get_json(http, f"{repo_path}/commits/{quote(ref, safe='')}")
    sha = commit["sha"]
    lic = await get_json(http, f"{repo_path}/license", allow_404=True)
    licence = ((lic or {}).get("license") or {}).get("spdx_id") or "none"
    if licence not in cfg.allowed_licences:
        raise SkillInstallError(f"Licence {licence!r} is not on the allowed list "
                                f"({', '.join(cfg.allowed_licences)}) — config/skills.yaml")
    tree = await get_json(http, f"{repo_path}/git/trees/{sha}?recursive=1")
    prefix = f"{src.path}/" if src.path else ""
    blobs = [(t["path"][len(prefix):], int(t.get("size") or 0)) for t in tree.get("tree", [])
             if t.get("type") == "blob" and t["path"].startswith(prefix)]
    if not any(rel == "SKILL.md" for rel, _ in blobs):
        raise SkillInstallError(f"No SKILL.md in {src.path or 'the repo root'} — link the skill's own folder")

    keep: list[tuple[str, int]] = []
    skipped: list[dict[str, str]] = []
    total = 0
    for rel, size in sorted(blobs, key=lambda b: (b[0] != "SKILL.md", b[0])):
        reason = classify(rel, size)
        if reason is None and len(keep) >= cfg.max_files:
            reason = f"over the limit of {cfg.max_files} files"
        if reason is None and total + size > cfg.max_total_bytes:
            reason = f"over the total size limit ({cfg.max_total_bytes:,} bytes)"
        if reason:
            skipped.append({"path": rel, "reason": reason})
        else:
            keep.append((rel, size))
            total += size

    sem = asyncio.Semaphore(8)

    async def download(rel: str) -> tuple[str, str | None]:
        async with sem:
            try:
                r = await http.get(f"{RAW}/{src.owner}/{src.repo}/{sha}/{quote(prefix + rel)}", timeout=20,
                                   headers={"User-Agent": _HEADERS["User-Agent"]})
                if r.status_code != 200:
                    return rel, None
                return rel, r.content.decode("utf-8")
            except (httpx.HTTPError, UnicodeDecodeError):
                return rel, None

    files: dict[str, str] = {}
    for rel, text in await asyncio.gather(*(download(rel) for rel, _ in keep)):
        if text is None:
            skipped.append({"path": rel, "reason": "could not be read as UTF-8 text"})
        else:
            files[rel] = text
    if "SKILL.md" not in files:
        raise SkillInstallError("SKILL.md could not be downloaded")
    try:
        doc = parse_skill_md(files["SKILL.md"], posixpath.basename(src.path) or src.repo.lower())
    except SkillFormatError as exc:
        raise SkillInstallError(str(exc)) from exc
    return FetchedSkill(src, ref, sha, licence, doc, files, skipped, screen(files))
