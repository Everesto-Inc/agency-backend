"""Shared platform Redis: the app's ACL user may only touch keys starting with REDIS_KEY_PREFIX."""
import os
from pathlib import Path

import fakeredis.aioredis
import pytest

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from agency_platform import cache
from agency_platform.agents.sessions import SessionStore
from agency_platform.auth.accounts import AccountDirectory
from agency_platform.billing.usage import RateLimiter
from agency_platform.config.settings import PlatformSettings

PREFIX = "orchid-agency-backend:"


@pytest.fixture
def prefixed(monkeypatch):
    monkeypatch.setattr(cache, "_prefix", PREFIX)


def test_rkey_is_a_noop_without_prefix(monkeypatch):
    monkeypatch.setattr(cache, "_prefix", "")
    assert cache.rkey("usage:tok:u1") == "usage:tok:u1"


async def test_init_redis_takes_the_prefix_from_settings(monkeypatch):
    monkeypatch.setattr(cache, "_client", None)
    monkeypatch.setattr(cache, "_prefix", "")
    settings = PlatformSettings(model_gateway_client_secret="c" * 40, redis_key_prefix=PREFIX)
    client = cache.init_redis(settings)
    try:
        assert cache.get_redis() is client
        assert cache.rkey("x") == f"{PREFIX}x"
    finally:
        await cache.close_redis()


def test_account_cache_key_is_prefixed(prefixed):
    assert AccountDirectory._key("u1") == f"{PREFIX}acct:u1"


async def test_session_store_writes_and_forgets_only_prefixed_keys(prefixed):
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SessionStore(r)
    await store.append("u1", "s1", {"role": "user", "content": "hi"})
    assert await r.keys() == [f"{PREFIX}sess:u1:s1"]
    assert await store.forget_user("u1") == 1
    assert await r.keys() == []


async def test_rate_limiter_keys_are_prefixed(prefixed):
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = RateLimiter(r)
    await limiter.hit("login:u1", limit=5, window_s=60)
    keys = await r.keys()
    assert len(keys) == 1 and keys[0].startswith(f"{PREFIX}rl:login:u1:")
    await limiter.reset("login:u1", window_s=60)
    assert await r.keys() == []
