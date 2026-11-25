"""Pictures in answers: download checks, stored once, markdown, image search."""
import os
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from agency_platform.security.ssrf import UnsafeURLError
from agency_platform.tools import images as images_mod
from agency_platform.tools.images import MAX_BYTES, ImageStore, image_id, markdown
from agency_platform.tools.search import WebSearch

JPEG = b"\xff\xd8\xff" + b"0" * 100


class FakeDb:
    def __init__(self, existing=()):
        self.rows = {i: True for i in existing}
        self.inserted = []

    async def fetchval(self, sql, params=None):
        return 1 if params[0] in self.rows else None

    async def execute(self, sql, params=None):
        self.inserted.append(params)
        return 1


def client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        status, ctype, body = routes[str(request.url)]
        return httpx.Response(status, headers={"content-type": ctype}, content=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def public_only(monkeypatch):
    async def check(url):
        if "10.0.0.1" in url:
            raise UnsafeURLError("private address")
    monkeypatch.setattr(images_mod, "assert_public_url", check)


async def test_stores_a_real_image_once_and_returns_our_path():
    url = "https://cdn.example.com/model-y.jpg"
    db = FakeDb()
    store = ImageStore(db, client({url: (200, "image/jpeg", JPEG)}))
    path = await store.save_from_url(url, alt="2024 Tesla Model Y")
    assert path == f"/api/v1/images/{image_id(url)}" and len(image_id(url)) == 64
    _iid, ctype, data, source, alt = db.inserted[0]
    assert (ctype, data, source, alt) == ("image/jpeg", JPEG, url, "2024 Tesla Model Y")
    stored = ImageStore(FakeDb(existing=[image_id(url)]), client({}))          # already stored → no download
    assert await stored.save_from_url(url) == path


@pytest.mark.parametrize("url,route", [
    ("https://x.example/page", (200, "text/html", b"<html>")),                 # not an image
    ("https://x.example/huge.jpg", (200, "image/jpeg", b"0" * (MAX_BYTES + 1))),  # too large
    ("https://x.example/moved.jpg", (302, "image/jpeg", b"")),                 # redirects are not followed
    ("https://x.example/gone.jpg", (404, "image/jpeg", b"")),
])
async def test_unusable_pictures_are_refused(url, route):
    db = FakeDb()
    assert await ImageStore(db, client({url: route})).save_from_url(url) is None
    assert db.inserted == []


async def test_internal_addresses_are_never_fetched():
    db = FakeDb()
    assert await ImageStore(db, client({})).save_from_url("https://10.0.0.1/x.jpg") is None


def test_markdown():
    md = markdown([{"path": "/api/v1/images/abc", "alt": "Model [Y]", "source": "ev-database.org"}])
    assert md == "![Model Y](/api/v1/images/abc)\n\n*Picture: ev-database.org*"


async def test_image_search_keeps_https_addresses_only():
    def handler(request):
        assert b'"include_images":true' in request.content.replace(b" ", b"")
        return httpx.Response(200, json={"images": ["https://a.example/1.jpg", {"url": "https://b.example/2.png"}, "http://c/3.jpg", 5]})
    search = WebSearch("key", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await search.images("Model Y") == ["https://a.example/1.jpg", "https://b.example/2.png"]


async def test_security_headers_keep_a_routes_own_cache_header():
    from agency_platform.service.middleware import SecurityHeadersMiddleware
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"cache-control", b"public, max-age=31536000, immutable")]})

    async def send(msg):
        sent.append(msg)
    await SecurityHeadersMiddleware(app, hsts=False)({"type": "http", "path": "/api/v1/images/x"}, None, send)
    headers = sent[0]["headers"]
    assert [v for k, v in headers if k == b"cache-control"] == [b"public, max-age=31536000, immutable"]
    assert (b"x-frame-options", b"DENY") in headers and any(k == b"content-security-policy" for k, _ in headers)
