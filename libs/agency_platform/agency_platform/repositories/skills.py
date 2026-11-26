"""skills / skill_versions / skill_files: installed instruction packs (Admin → Skills)."""
import json
from typing import Any

from agency_platform.db import Database

_VERSION_COLS = """v.id, v.name, v.version, v.description, v.source_url, v.repo, v.ref, v.commit_sha, v.path, v.licence,
                   v.files, v.total_bytes, v.skipped, v.warnings, v.installed_by, v.comment, v.created_at"""


class SkillRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ install
    async def add_version(self, *, name: str, description: str, source_url: str, repo: str, ref: str,
                          commit_sha: str, path: str, licence: str, files: dict[str, str],
                          skipped: list[dict[str, str]], warnings: list[dict[str, Any]], installed_by: str,
                          comment: str) -> tuple[int, int, bool]:
        """Store a new immutable version. The first version of a skill also creates the skill (off, no assistants);
        later versions wait until the admin activates them. → (version_id, version, is_new_skill)."""
        async with self._db.transaction() as tx:
            version = int(await tx.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM skill_versions WHERE name = %s FOR UPDATE", (name,)) or 1)
            vid = await tx.insert(
                f"""INSERT INTO skill_versions (name, version, description, source_url, repo, ref, commit_sha, path,
                       licence, files, total_bytes, skipped, warnings, installed_by, comment)
                    VALUES ({", ".join(["%s"] * 15)})""",
                (name, version, description[:1024], source_url[:500], repo[:200], ref[:100], commit_sha, path[:300],
                 licence[:64], len(files), sum(len(c.encode()) for c in files.values()), json.dumps(skipped),
                 json.dumps(warnings), installed_by[:255], comment[:300]))
            await tx.executemany(
                "INSERT INTO skill_files (version_id, path, size_bytes, content) VALUES (%s, %s, %s, %s)",
                [(vid, p, len(c.encode()), c) for p, c in files.items()])
            is_new = await tx.execute(
                """INSERT IGNORE INTO skills (name, active_version_id, enabled, assistants, updated_by)
                   VALUES (%s, %s, FALSE, '[]', %s)""", (name, vid, installed_by[:255])) == 1
        return vid, version, is_new

    # ------------------------------------------------------------------ admin
    async def update(self, name: str, *, enabled: bool | None, assistants: list[str] | None,
                     active_version_id: int | None, by: str) -> int:
        sets, args = ["updated_by = %s"], [by[:255]]
        if enabled is not None:
            sets.append("enabled = %s")
            args.append(enabled)
        if assistants is not None:
            sets.append("assistants = %s")
            args.append(json.dumps(sorted(set(assistants))))
        if active_version_id is not None:
            sets.append("active_version_id = %s")
            args.append(active_version_id)
        return await self._db.execute(f"UPDATE skills SET {', '.join(sets)} WHERE name = %s", (*args, name))

    async def delete(self, name: str) -> None:
        """The skill and every version (files go with their version)."""
        async with self._db.transaction() as tx:
            await tx.execute("DELETE FROM skills WHERE name = %s", (name,))
            await tx.execute("DELETE FROM skill_versions WHERE name = %s", (name,))

    async def skills(self) -> list[dict[str, Any]]:
        """Every skill with its active version and the newest version number."""
        return await self._db.fetch(
            f"""SELECT s.name, s.enabled, s.assistants, s.updated_by, s.updated_at, {_VERSION_COLS},
                       (SELECT MAX(x.version) FROM skill_versions x WHERE x.name = s.name) AS latest_version
                FROM skills s JOIN skill_versions v ON v.id = s.active_version_id ORDER BY s.name""")

    async def skill(self, name: str) -> dict[str, Any] | None:
        return next((r for r in await self.skills() if r["name"] == name), None)

    async def versions(self, name: str) -> list[dict[str, Any]]:
        return await self._db.fetch(
            f"SELECT {_VERSION_COLS} FROM skill_versions v WHERE v.name = %s ORDER BY v.version DESC", (name,))

    async def version(self, version_id: int) -> dict[str, Any] | None:
        return await self._db.fetchrow(f"SELECT {_VERSION_COLS} FROM skill_versions v WHERE v.id = %s", (version_id,))

    async def file_list(self, version_id: int) -> list[dict[str, Any]]:
        return await self._db.fetch(
            "SELECT path, size_bytes FROM skill_files WHERE version_id = %s ORDER BY path", (version_id,))

    # ------------------------------------------------------------------ assistants
    async def enabled(self) -> list[dict[str, Any]]:
        """What the assistants may use: enabled skills, active version."""
        return await self._db.fetch(
            """SELECT s.name, s.assistants, v.id AS version_id, v.version, v.description, v.repo, v.commit_sha
               FROM skills s JOIN skill_versions v ON v.id = s.active_version_id
               WHERE s.enabled ORDER BY s.name""")

    async def file(self, version_id: int, path: str) -> str | None:
        return await self._db.fetchval(
            "SELECT content FROM skill_files WHERE version_id = %s AND path = %s", (version_id, path))
