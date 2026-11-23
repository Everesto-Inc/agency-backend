"""Shared async Redis client (quotas, rate limits, caches, session memory)."""
import redis.asyncio as redis

from agency_platform.config.settings import PlatformSettings

_client: redis.Redis | None = None
_prefix = ""


def rkey(key: str) -> str:
    """Every Redis key goes through here. In the shared platform Redis the app's ACL user may only
    touch keys starting with REDIS_KEY_PREFIX (set by Backstage's Redis switch); empty locally."""
    return f"{_prefix}{key}"


def init_redis(settings: PlatformSettings) -> redis.Redis:
    global _client, _prefix
    _prefix = settings.redis_key_prefix
    if _client is None:
        _client = redis.from_url(
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_timeout=5,
            health_check_interval=30,
        )
    return _client


def get_redis() -> redis.Redis:
    if _client is None:
        raise RuntimeError("Redis not initialised; call init_redis() in the app lifespan")
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
