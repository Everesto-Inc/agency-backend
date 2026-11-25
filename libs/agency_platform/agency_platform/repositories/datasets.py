"""eval_datasets: versioned test datasets (files in the bucket, metadata here)."""
import json
from typing import Any

from agency_platform.db import Database


def _row(r: dict[str, Any]) -> dict[str, Any]:
    t = r["topics"]
    return {**r, "topics": json.loads(t) if isinstance(t, str | bytes) else (t or {})}


class DatasetRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def next_version(self, name: str) -> int:
        return int(await self._db.fetchval("SELECT COALESCE(MAX(version), 0) + 1 FROM eval_datasets WHERE name = %s",
                                           (name,)) or 1)

    async def add(self, *, name: str, version: int, cases: int, topics: dict[str, int], size_bytes: int,
                  object_key: str, author: str, comment: str) -> int:
        return await self._db.insert(
            """INSERT INTO eval_datasets (name, version, cases, topics, size_bytes, object_key, author, comment)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (name, version, cases, json.dumps(topics), size_bytes, object_key, author[:255], comment[:300]))

    async def get(self, dataset_id: int) -> dict[str, Any] | None:
        r = await self._db.fetchrow(
            """SELECT id, name, version, cases, topics, size_bytes, object_key, author, comment, created_at
               FROM eval_datasets WHERE id = %s""", (dataset_id,))
        return _row(r) if r else None

    async def all(self) -> list[dict[str, Any]]:
        """Every version, newest first (the UI groups them by name)."""
        rows = await self._db.fetch(
            """SELECT id, name, version, cases, topics, size_bytes, object_key, author, comment, created_at
               FROM eval_datasets ORDER BY name, version DESC""")
        return [_row(r) for r in rows]
