"""Conversation memory in Redis, shared by all replicas.

Keys are scoped by user id, so a guessed/leaked session id can never expose
another user's conversation.
"""
import json

from redis.asyncio import Redis

_MAX_TURNS = 20
_TTL_S = 24 * 3600


class SessionStore:
    def __init__(self, redis: Redis, max_turns: int = _MAX_TURNS, ttl_s: int = _TTL_S) -> None:
        self._r = redis
        self._max = max_turns
        self._ttl = ttl_s

    @staticmethod
    def _key(user_id: str, topic: str, session_id: str) -> str:
        return f"sess:{user_id}:{topic}:{session_id}"

    async def history(self, user_id: str, topic: str, session_id: str) -> list[dict[str, str]]:
        items = await self._r.lrange(self._key(user_id, topic, session_id), 0, -1)
        return [json.loads(i) for i in items]

    async def append(self, user_id: str, topic: str, session_id: str, *turns: dict[str, str]) -> None:
        key = self._key(user_id, topic, session_id)
        async with self._r.pipeline(transaction=True) as p:
            p.rpush(key, *[json.dumps(t) for t in turns])
            p.ltrim(key, -self._max, -1)
            p.expire(key, self._ttl)
            await p.execute()

    async def forget_user(self, user_id: str) -> int:
        """Delete every session of a user (account erasure)."""
        deleted = 0
        async for key in self._r.scan_iter(match=f"sess:{user_id}:*", count=500):
            deleted += await self._r.delete(key)
        return deleted
