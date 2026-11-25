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
from agency_platform.config.registry import memory

# Defaults of config/memory.yaml (Admin → Settings changes them live; these names stay for tests/docs).
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
                     keep_recent: int | None = None, at_least: int | None = None) -> list[dict[str, str]]:
    """Pure: the older messages (outside the last `keep_recent`) not yet in the summary — or [] if fewer than
    `at_least`, so the cheap model runs about every 3 questions, not every time."""
    cfg = memory() if keep_recent is None or at_least is None else None
    keep_recent = cfg.recent if keep_recent is None else keep_recent  # type: ignore[union-attr]
    at_least = cfg.fold_at_least if at_least is None else at_least    # type: ignore[union-attr]
    older = history[:-keep_recent] if keep_recent and len(history) > keep_recent else ([] if keep_recent else history)
    cutoff = _iso(upto) if upto else ""
    new = [m for m in older if m.get("at") and m["at"] > cutoff]
    return new if len(new) >= at_least else []


def with_history(prompt: str, history: list[dict[str, str]], last: int | None = None, max_chars: int = 500,
                 summary: str | None = None) -> str:
    """Pure: the question with the chat's summary + last messages in front (for agents that take one task text)."""
    last = memory().recent if last is None else last
    recent = [m for m in history if m.get("role") in ("user", "assistant")][-last:] if last else []
    if not recent and not summary:
        return prompt
    parts = []
    if summary:
        parts.append(f"Summary of the earlier conversation:\n{summary}")
    if recent:
        parts.append("Earlier conversation:\n" + "\n".join(f"{m['role']}: {m['content'][:max_chars]}" for m in recent))
    return "\n\n".join(parts) + f"\n\nUser question: {prompt}"


class SessionStore:
    def __init__(self, redis: Redis, max_turns: int | None = None, ttl_s: int | None = None) -> None:
        self._r = redis
        self._max_fixed = max_turns   # None = config/memory.yaml (live)
        self._ttl_fixed = ttl_s

    @property
    def max_turns(self) -> int:
        return self._max_fixed if self._max_fixed is not None else memory().max_turns

    @property
    def _max(self) -> int:
        return self.max_turns

    @property
    def _ttl(self) -> int:
        return self._ttl_fixed if self._ttl_fixed is not None else memory().ttl_hours * 3600

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
