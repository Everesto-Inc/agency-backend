"""Chat history. A conversation's id is the chat session_id (per user); its messages are that session's agent_runs."""
import re
from typing import Any

from agency_platform.db import Database

_COLS = "id, title, pinned, turns, last_topic, created_at, updated_at"
TITLE_MAX = 200


def title_from_prompt(prompt: str, limit: int = 80) -> str:
    """Pure: one line, shortened at a word boundary (callers redact PII first)."""
    text = re.sub(r"\s+", " ", prompt).strip() or "New conversation"
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip(" ,.;:-") + "…"


def _row(r: dict[str, Any]) -> dict[str, Any]:
    return {**r, "pinned": bool(r["pinned"])}


class ConversationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def touch(self, *, user_id: str, conversation_id: str, title: str, topic: str) -> None:
        """A question was asked: create the conversation (title = first question) or bump it."""
        await self._db.execute(
            """INSERT INTO conversations (id, user_id, title, turns, last_topic, updated_at)
               VALUES (%s, %s, %s, 1, %s, UTC_TIMESTAMP(6))
               ON DUPLICATE KEY UPDATE turns = turns + 1, last_topic = VALUES(last_topic), updated_at = UTC_TIMESTAMP(6)""",
            (conversation_id, user_id, title[:TITLE_MAX], topic),
        )

    async def recent(self, user_id: str, *, limit: int, query: str | None = None) -> list[dict[str, Any]]:
        """Pinned first, then most recent."""
        where, args = "WHERE user_id = %s", [user_id]
        if query:
            where += " AND title LIKE %s"
            args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        rows = await self._db.fetch(
            f"SELECT {_COLS} FROM conversations {where} ORDER BY pinned DESC, updated_at DESC LIMIT %s", (*args, limit)
        )
        return [_row(r) for r in rows]

    async def get(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            f"SELECT {_COLS} FROM conversations WHERE user_id = %s AND id = %s", (user_id, conversation_id)
        )
        return _row(row) if row else None

    async def messages(self, user_id: str, conversation_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """The questions + answers of a conversation, oldest first (text is null after the retention period)."""
        return await self._db.fetch(
            """SELECT request_id, topic, prompt, answer, status, created_at,
                      prompt_tokens + completion_tokens AS total_tokens, cost_usd
               FROM agent_runs WHERE user_id = %s AND session_id = %s
               ORDER BY created_at LIMIT %s""",
            (user_id, conversation_id, limit),
        )

    async def update(self, user_id: str, conversation_id: str, *, title: str | None, pinned: bool | None) -> bool:
        """Rename and/or pin; a None field stays as it is. False = no such conversation."""
        if title is not None or pinned is not None:
            changed = await self._db.execute(
                """UPDATE conversations SET title = COALESCE(%s, title), pinned = COALESCE(%s, pinned)
                   WHERE user_id = %s AND id = %s""",
                (title[:TITLE_MAX] if title is not None else None, pinned, user_id, conversation_id),
            )
            if changed:
                return True
        # nothing to change, or the same values again (MySQL counts changed rows): does it exist?
        return await self.get(user_id, conversation_id) is not None

    async def delete(self, user_id: str, conversation_id: str) -> bool:
        """Remove it from the history and erase its text. Tokens/cost of the runs stay (usage + billing)."""
        deleted = await self._db.execute(
            "DELETE FROM conversations WHERE user_id = %s AND id = %s", (user_id, conversation_id)
        )
        await self._db.execute(
            "UPDATE agent_runs SET prompt = NULL, answer = NULL WHERE user_id = %s AND session_id = %s",
            (user_id, conversation_id),
        )
        return bool(deleted)
