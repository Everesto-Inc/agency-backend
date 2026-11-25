"""Conversation memory, shared by all replicas and by all assistants of one chat.

    Redis   sess:<user>:<chat>     the last turns, for 24 h — the fast copy every run reads
    MySQL   agent_runs (by chat)   the durable copy (PII-redacted) — reloaded into Redis when Redis has nothing
    MySQL   conversations.summary  a short running summary of the OLDER messages (cheap model, every ~3 questions)

ONE memory per chat (not per assistant): switching from EV Helper to Finance keeps the context. Each turn
records which assistant wrote it. Keys are scoped by user id, so a guessed/leaked chat id can never expose
another user's conversation.
"""
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from agency_platform.cache import rkey

_MAX_TURNS = 20
_TTL_S = 24 * 3600
RECENT = 4          # messages the assistants read word for word
FOLD_AT_LEAST = 6   # summarize once this many older messages are not in the summary yet (≈ every 3 questions)


def turns_from_runs(runs: Iterable[dict[str, Any]], max_chars: int = 8000) -> list[dict[str, str]]:
    """Pure: stored runs (oldest first, with prompt + answer) → memory turns (user, assistant, …)."""
    turns: list[dict[str, str]] = []
    for r in runs:
        if r.get("prompt") and r.get("answer"):
            at = _iso(r.get("created_at"))
            turns.append({"role": "user", "content": r["prompt"], "topic": r.get("topic") or "", "at": at})
            turns.append({"role": "assistant", "content": str(r["answer"])[:max_chars], "topic": r.get("topic") or "", "at": at})
    return turns


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return str(value or datetime.now(UTC).isoformat())


def messages_to_fold(history: list[dict[str, str]], upto: datetime | str | None,
                     keep_recent: int = RECENT, at_least: int = FOLD_AT_LEAST) -> list[dict[str, str]]:
    """Pure: the older messages (outside the last `keep_recent`) not yet in the summary — or [] if fewer than
    `at_least`, so the cheap model runs about every 3 questions, not every time."""
    older = history[:-keep_recent] if len(history) > keep_recent else []
    cutoff = _iso(upto) if upto else ""
    new = [m for m in older if m.get("at") and m["at"] > cutoff]
    return new if len(new) >= at_least else []


def with_history(prompt: str, history: list[dict[str, str]], last: int = RECENT, max_chars: int = 500,
                 summary: str | None = None) -> str:
    """Pure: the question with the chat's summary + last messages in front (for agents that take one task text)."""
    recent = [m for m in history if m.get("role") in ("user", "assistant")][-last:]
    if not recent and not summary:
        return prompt
    parts = []
    if summary:
        parts.append(f"Summary of the earlier conversation:\n{summary}")
    if recent:
        parts.append("Earlier conversation:\n" + "\n".join(f"{m['role']}: {m['content'][:max_chars]}" for m in recent))
    return "\n\n".join(parts) + f"\n\nUser question: {prompt}"


class SessionStore:
    def __init__(self, redis: Redis, max_turns: int = _MAX_TURNS, ttl_s: int = _TTL_S) -> None:
        self._r = redis
        self._max = max_turns
        self._ttl = ttl_s

    @property
    def max_turns(self) -> int:
        return self._max

    @staticmethod
    def _key(user_id: str, session_id: str) -> str:
        return rkey(f"sess:{user_id}:{session_id}")

    async def history(self, user_id: str, session_id: str) -> list[dict[str, str]]:
        items = await self._r.lrange(self._key(user_id, session_id), 0, -1)
        return [json.loads(i) for i in items]

    async def append(self, user_id: str, session_id: str, *turns: dict[str, str]) -> None:
        if not turns:
            return
        key = self._key(user_id, session_id)
        now = datetime.now(UTC).isoformat()
        async with self._r.pipeline(transaction=True) as p:
            p.rpush(key, *[json.dumps({**t, "at": t.get("at") or now}) for t in turns])
            p.ltrim(key, -self._max, -1)
            p.expire(key, self._ttl)
            await p.execute()

    async def forget_session(self, user_id: str, session_id: str) -> int:
        """Delete one chat's memory (also the old per-assistant keys sess:<user>:<topic>:<chat>)."""
        deleted = await self._r.delete(self._key(user_id, session_id))
        async for key in self._r.scan_iter(match=rkey(f"sess:{user_id}:*:{session_id}"), count=500):
            deleted += await self._r.delete(key)
        return deleted

    async def forget_user(self, user_id: str) -> int:
        """Delete every chat memory of a user (account erasure)."""
        deleted = 0
        async for key in self._r.scan_iter(match=rkey(f"sess:{user_id}:*"), count=500):
            deleted += await self._r.delete(key)
        return deleted
