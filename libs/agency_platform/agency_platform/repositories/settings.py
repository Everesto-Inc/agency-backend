"""settings_versions: Admin → Settings history. Newest row per name = live override."""
import json
from typing import Any

from agency_platform.db import Database


def _row(r: dict[str, Any]) -> dict[str, Any]:
    content = r["content"]
    return {**r, "content": json.loads(content) if isinstance(content, str | bytes) else (content or {})}


class SettingsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def live(self) -> dict[str, dict[str, Any]]:
        """name → newest version."""
        rows = await self._db.fetch(
            """SELECT v.id, v.name, v.content, v.author, v.comment, v.created_at FROM settings_versions v
               JOIN (SELECT name, MAX(id) AS id FROM settings_versions GROUP BY name) last ON last.id = v.id""")
        return {r["name"]: _row(r) for r in rows}

    async def history(self, name: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """SELECT id, name, content, author, comment, created_at FROM settings_versions
               WHERE name = %s ORDER BY id DESC LIMIT %s""", (name, limit))
        return [_row(r) for r in rows]

    async def get(self, version_id: int) -> dict[str, Any] | None:
        r = await self._db.fetchrow(
            "SELECT id, name, content, author, comment, created_at FROM settings_versions WHERE id = %s", (version_id,))
        return _row(r) if r else None

    async def add(self, name: str, content: dict[str, Any], author: str, comment: str) -> int:
        return await self._db.insert(
            "INSERT INTO settings_versions (name, content, author, comment) VALUES (%s, %s, %s, %s)",
            (name, json.dumps(content, sort_keys=True), author[:255], comment[:300]))

    # ---- drafts --------------------------------------------------------------------------------
    async def add_draft(self, content: dict[str, Any], based_on: dict[str, int | None], author: str, comment: str,
                        source: str) -> int:
        return await self._db.insert(
            """INSERT INTO settings_drafts (content, based_on, author, comment, source) VALUES (%s, %s, %s, %s, %s)""",
            (json.dumps(content, sort_keys=True), json.dumps(based_on), author[:255], comment[:300], source[:16]))

    async def draft(self, draft_id: int) -> dict[str, Any] | None:
        r = await self._db.fetchrow(
            """SELECT id, content, based_on, author, comment, source, status, created_at, decided_at, decided_by
               FROM settings_drafts WHERE id = %s""", (draft_id,))
        return _draft(r) if r else None

    async def drafts(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """SELECT id, content, based_on, author, comment, source, status, created_at, decided_at, decided_by
               FROM settings_drafts ORDER BY id DESC LIMIT %s""", (limit,))
        return [_draft(r) for r in rows]

    async def decide_draft(self, draft_id: int, status: str, by: str) -> bool:
        """draft → applied | discarded (only once)."""
        n = await self._db.execute(
            """UPDATE settings_drafts SET status = %s, decided_at = UTC_TIMESTAMP(6), decided_by = %s
               WHERE id = %s AND status = 'draft'""", (status, by[:255], draft_id))
        return n > 0


def _draft(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    for k in ("content", "based_on"):
        v = out[k]
        out[k] = json.loads(v) if isinstance(v, str | bytes) else (v or {})
    return out
