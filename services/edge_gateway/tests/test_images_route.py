"""Pictures over HTTP: upload (plan, daily cap) and read (web public, uploads only for their owner)."""
from types import SimpleNamespace

import pytest

from agency_platform.errors import NotFoundError, RateLimitedError, SubscriptionRequiredError
from agency_platform.tools.images import StoredImage
from edge_gateway import routes_images

WEB, UPLOAD = "a" * 64, "b" * 64
REQ = SimpleNamespace(headers={})


class Store:
    saved = []

    async def read(self, iid):
        if iid == WEB:
            return StoredImage(WEB, "web", "image/png", 4), b"\x89PNG"
        if iid == UPLOAD:
            return StoredImage(UPLOAD, "upload", "image/jpeg", 4, "owner"), b"\xff\xd8\xff0"
        raise NotFoundError("Picture not found")

    async def save_upload(self, user_id, session_id, data):
        Store.saved.append((user_id, session_id, data))
        return StoredImage(UPLOAD, "upload", "image/jpeg", len(data), user_id, session_id)


class Redis:
    def __init__(self):
        self.n = 0

    async def incr(self, key):
        self.n += 1
        return self.n

    async def expire(self, key, ttl):
        pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    Store.saved = []
    monkeypatch.setattr(routes_images, "image_store", lambda _req: Store())


def viewer(monkeypatch, user_id):
    async def who(_request):
        return user_id
    monkeypatch.setattr(routes_images, "_viewer_id", who)


async def test_web_pictures_are_public_and_cached_for_long(monkeypatch):
    viewer(monkeypatch, None)
    r = await routes_images.get_image(REQ, WEB)
    assert r.body == b"\x89PNG" and r.media_type == "image/png"
    assert "immutable" in r.headers["cache-control"] and "sandbox" in r.headers["content-security-policy"]


async def test_uploads_only_for_their_owner(monkeypatch):
    viewer(monkeypatch, "owner")
    r = await routes_images.get_image(REQ, UPLOAD)
    assert r.headers["cache-control"].startswith("private")
    for someone in (None, "someone-else"):
        viewer(monkeypatch, someone)
        with pytest.raises(NotFoundError):
            await routes_images.get_image(REQ, UPLOAD)


async def test_upload_needs_an_active_plan_and_has_a_daily_cap(monkeypatch):
    redis = Redis()
    monkeypatch.setattr(routes_images, "get_redis", lambda: redis)

    async def body():
        return b"\xff\xd8\xffdata"
    request = SimpleNamespace(body=body)
    user = SimpleNamespace(id="u1", plan_config=SimpleNamespace(topics=["ev_research"]))
    out = await routes_images.upload(request, user, "chat1")
    assert out.id == UPLOAD and out.path == f"/api/v1/images/{UPLOAD}" and Store.saved == [("u1", "chat1", b"\xff\xd8\xffdata")]

    with pytest.raises(SubscriptionRequiredError):
        await routes_images.upload(request, SimpleNamespace(id="u2", plan_config=SimpleNamespace(topics=[])), "chat1")
    redis.n = routes_images.UPLOADS_PER_DAY
    with pytest.raises(RateLimitedError):
        await routes_images.upload(request, user, "chat1")


def test_only_sha256_ids_are_routed():
    route = next(r for r in routes_images.images_router.routes if r.path == "/{image_id}")
    param = route.dependant.path_params[0]
    assert next(m.pattern for m in param.field_info.metadata if hasattr(m, "pattern")) == r"^[0-9a-f]{64}$"
