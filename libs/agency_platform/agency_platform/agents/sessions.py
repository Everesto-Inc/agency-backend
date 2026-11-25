"""Conversation memory, shared by all replicas and by all assistants of one chat.

    Redis   sess:<user>:<chat>     the last turns, for 24 h — the fast copy every run reads
    MySQL   agent_runs (by chat)   the durable copy (PII-redacted) — reloaded into Redis when Redis has nothing

ONE memory per chat (not per assistant): switching from EV Helper to Finance keeps the context. Each turn
records which assistant wrote it. Keys are scoped by user id, so a guessed/leaked chat id can never expose
another user's conversation.
"""
import json
from collections.abc import Iterable
from typing import Any

from redis.asyncio import Redis

from agency_platform.cache import rkey

_MAX_TURNS = 20
_TTL_S = 24 * 3600


def turns_from_runs(runs: Iterable[dict[str, Any]], max_chars: int = 8000) -> list[dict[str, str]]:
    """Pure: stored runs (oldest first, with prompt + answer) → memory turns (user, assistant, …)."""
    turns: list[dict[str, str]] = []
    for r in runs:
        if r.get("prompt") and r.get("answer"):
            turns.append({"role": "user", "content": r["prompt"], "topic": r.get("topic") or ""})
            turns.append({"role": "assistant", "content": str(r["answer"])[:max_chars], "topic": r.get("topic") or ""})
    return turns


def with_history(prompt: str, history: list[dict[str, str]], last: int = 4, max_chars: int = 500) -> str:
    """Pure: the question with the chat's last messages in front (for agents that take one task text)."""
    recent = [m for m in history if m.get("role") in ("user", "assistant")][-last:]
    if not recent:
        return prompt
    lines = "\n".join(f"{m['role']}: {m['content'][:max_chars]}" for m in recent)
    return f"Earlier conversation:\n{lines}\n\nUser question: {prompt}"


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
        async with self._r.pipeline(transaction=True) as p:
            p.rpush(key, *[json.dumps(t) for t in turns])
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
