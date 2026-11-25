"""GET /api/v1/images/<id>: serves stored pictures only, cacheable, safe headers — handler with a fake store."""
from types import SimpleNamespace

import pytest

from agency_platform.errors import NotFoundError
from edge_gateway import routes_images

IID = "a" * 64
REQ = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http=None)))


class Store:
    def __init__(self, *_a):
        pass

    async def get(self, iid):
        return {"content_type": "image/png", "bytes": b"\x89PNG"} if iid == IID else None


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    monkeypatch.setattr(routes_images, "ImageStore", Store)
    monkeypatch.setattr(routes_images, "get_db", lambda: None)


async def test_serves_a_stored_picture():
    r = await routes_images.get_image(REQ, IID)
    assert r.status_code == 200 and r.body == b"\x89PNG" and r.media_type == "image/png"
    assert "immutable" in r.headers["cache-control"] and r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


async def test_unknown_picture_is_404():
    with pytest.raises(NotFoundError):
        await routes_images.get_image(REQ, "b" * 64)


def test_only_sha256_ids_are_routed():
    route = next(r for r in routes_images.images_router.routes if r.path == "/{image_id}")
    param = route.dependant.path_params[0]
    pattern = next(m.pattern for m in param.field_info.metadata if hasattr(m, "pattern"))
    assert pattern == r"^[0-9a-f]{64}$"
