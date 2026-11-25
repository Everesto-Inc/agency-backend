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
