"""Pictures: S3 signing + client, the picture store (web + uploads), vision text, per-path body limits."""
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from agency_platform.errors import BadRequestError, NotFoundError, ServiceUnavailableError, UpstreamError
from agency_platform.security.ssrf import UnsafeURLError
from agency_platform.service.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from agency_platform.tools import images as images_mod
from agency_platform.tools import vision
from agency_platform.tools.images import MAX_UPLOAD_BYTES, ImageStore, image_id, markdown, sniff
from agency_platform.tools.objectstore import ObjectStore, ObjectStoreConfig, sign_v4
from agency_platform.tools.search import WebSearch

JPEG = b"\xff\xd8\xff" + b"0" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50
HOST = "ns123.compat.objectstorage.eu-paris-1.oraclecloud.com"


# ---- S3 signing (vector produced by botocore's S3SigV4Auth for the same request) -------------------------
def test_sigv4_matches_botocore():
    import hashlib
    h = sign_v4(method="PUT", url=f"https://{HOST}/dev-agency-media/agency/web/abc",
                headers={"host": HOST, "content-type": "image/jpeg"},
                payload_sha256=hashlib.sha256(b"hello picture").hexdigest(), access_key="AKID", secret_key="SECRET",
                region="eu-paris-1", now=datetime(2026, 11, 25, 15, 52, 29, tzinfo=UTC))
    assert h["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKID/20261125/eu-paris-1/s3/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, "
        "Signature=2c394bc2bb95451fcb4b6af319204f2eb25d91acd343e67ba812c9f7b8ccb3ad")
    assert h["x-amz-date"] == "20261125T155229Z"


class FakeBucket:
    """In-memory bucket behind httpx.MockTransport (checks that every call is signed, path-style, prefixed)."""
    def __init__(self, fail=False):
        self.objects, self.fail = {}, fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
        assert request.url.host == HOST and request.url.path.startswith("/dev-agency-media/agency/")
        key = request.url.path.removeprefix("/dev-agency-media/")
        if self.fail:
            return httpx.Response(500)
        if request.method == "PUT":
            self.objects[key] = (request.content, request.headers["content-type"])
            return httpx.Response(200)
        if request.method == "GET":
            if key not in self.objects:
                return httpx.Response(404)
            data, ctype = self.objects[key]
            return httpx.Response(200, content=data, headers={"content-type": ctype})
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)


def bucket_store(bucket: FakeBucket) -> ObjectStore:
    http = httpx.AsyncClient(transport=httpx.MockTransport(bucket.handler))
    return ObjectStore(ObjectStoreConfig(f"https://{HOST}", "eu-paris-1", "dev-agency-media", "agency/", "AKID", "SECRET"), http)


async def test_object_store_put_get_delete():
    bucket = FakeBucket()
    store = bucket_store(bucket)
    await store.put("web/abc", JPEG, "image/jpeg")
    assert await store.get("web/abc") == (JPEG, "image/jpeg")
    await store.delete("web/abc")
    await store.delete("web/abc")                       # already gone = fine
    with pytest.raises(NotFoundError):
        await store.get("web/abc")
    with pytest.raises(ValueError):
        await store.put("../escape", JPEG, "image/jpeg")
    with pytest.raises(UpstreamError):
        await bucket_store(FakeBucket(fail=True)).put("web/x", JPEG, "image/jpeg")


def test_object_store_is_off_without_settings():
    off = SimpleNamespace(s3_endpoint=None, s3_bucket="b", s3_access_key_id=None, s3_secret_access_key=None,
                          s3_region="r", s3_prefix="p/")
    assert ObjectStore.from_settings(off, None) is None
    on = SimpleNamespace(s3_endpoint=SecretStr(f"https://{HOST}/"), s3_bucket="dev-agency-media", s3_region="eu-paris-1",
                         s3_prefix="agency/", s3_access_key_id=SecretStr("AKID"), s3_secret_access_key=SecretStr("SECRET"))
    assert ObjectStore.from_settings(on, None) is not None


# ---- the picture store ---------------------------------------------------------------------------------------
class FakeDb:
    """Just enough of MySQL `images` for ImageStore (matches on the SQL's shape)."""
    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def fetchval(self, sql, params=None):
        return 1 if params[0] in self.rows else None

    async def fetchrow(self, sql, params=None):
        r = self.rows.get(params[0])
        if r is None:
            return None
        if "request_id IS NULL" in sql:
            _, user, session = params
            ok = r["kind"] == "upload" and r["user_id"] == user and r["session_id"] == session and r["request_id"] is None
            return dict(r) if ok else None
        return dict(r)

    async def fetch(self, sql, params=None):
        rows = list(self.rows.values())
        if "request_id IS NOT NULL" in sql:
            return [r for r in rows if r["user_id"] == params[0] and r["session_id"] == params[1] and r["request_id"]]
        if "session_id = %s" in sql:
            return [r for r in rows if r["kind"] == "upload" and r["user_id"] == params[0] and r["session_id"] == params[1]]
        if "INTERVAL" in sql:
            return [r for r in rows if r["kind"] == "upload"]
        return [r for r in rows if r["kind"] == "upload" and r["user_id"] == params[0]]

    async def execute(self, sql, params=None):
        if sql.lstrip().startswith("INSERT"):
            if "'web'" in sql:
                iid, key, ctype, size, _src, _alt = params
                self.rows[iid] = dict(id=iid, kind="web", object_key=key, content_type=ctype, size_bytes=size,
                                      user_id=None, session_id=None, request_id=None, description=None)
            else:
                iid, key, ctype, size, user, session = params
                self.rows[iid] = dict(id=iid, kind="upload", object_key=key, content_type=ctype, size_bytes=size,
                                      user_id=user, session_id=session, request_id=None, description=None)
        elif sql.startswith("UPDATE images SET request_id"):
            self.rows[params[1]]["request_id"] = params[0]
        elif sql.startswith("UPDATE images SET description"):
            self.rows[params[1]]["description"] = params[0]
        elif sql.startswith("DELETE"):
            self.rows.pop(params[0], None)
        return 1


@pytest.fixture(autouse=True)
def public_only(monkeypatch):
    async def check(url):
        if "10.0.0.1" in url:
            raise UnsafeURLError("private address")
    monkeypatch.setattr(images_mod, "assert_public_url", check)


def web_client(routes):
    def handler(request):
        status, body = routes[str(request.url)]
        return httpx.Response(status, content=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_sniff_decides_the_type_from_the_bytes():
    assert sniff(JPEG) == "image/jpeg" and sniff(PNG) == "image/png"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff(b"<svg xmlns=...>") is None and sniff(b"%PDF-1.7") is None


async def test_web_picture_stored_once_in_the_bucket():
    url = "https://cdn.example.com/model-y.jpg"
    db, bucket = FakeDb(), FakeBucket()
    store = ImageStore(db, web_client({url: (200, JPEG)}), bucket_store(bucket))
    path = await store.save_from_url(url, alt="2024 Tesla Model Y")
    assert path == f"/api/v1/images/{image_id(url)}"
    assert bucket.objects[f"agency/web/{image_id(url)}"] == (JPEG, "image/jpeg")
    assert db.rows[image_id(url)]["kind"] == "web"
    again = ImageStore(db, web_client({}), bucket_store(bucket))      # already stored → no download
    assert await again.save_from_url(url) == path


@pytest.mark.parametrize("url,route", [
    ("https://x.example/page", (200, b"<html>")),                       # not a picture (bytes decide)
    ("https://x.example/huge.jpg", (200, JPEG + b"0" * 1_500_000)),     # too large
    ("https://x.example/moved.jpg", (302, b"")),                        # redirects are not followed
    ("https://10.0.0.1/x.jpg", (200, JPEG)),                            # internal address: never fetched
])
async def test_unusable_web_pictures_are_refused(url, route):
    db, bucket = FakeDb(), FakeBucket()
    assert await ImageStore(db, web_client({url: route}), bucket_store(bucket)).save_from_url(url) is None
    assert db.rows == {} and bucket.objects == {}


async def test_without_a_bucket_web_pictures_are_skipped_and_uploads_refused():
    store = ImageStore(FakeDb(), web_client({}), None)
    assert not store.enabled and await store.save_from_url("https://x.example/a.jpg") is None
    with pytest.raises(ServiceUnavailableError):
        await store.save_upload("u1", "chat1", JPEG)


async def test_upload_attach_read_and_delete():
    db, bucket = FakeDb(), FakeBucket()
    store = ImageStore(db, web_client({}), bucket_store(bucket))
    img = await store.save_upload("u1", "chat1", PNG)
    assert img.content_type == "image/png" and len(img.id) == 64
    assert f"agency/uploads/u1/{img.id}" in bucket.objects

    with pytest.raises(BadRequestError):                       # someone else's / other chat
        await store.attach("u2", "chat1", [img.id], "req-1")
    with pytest.raises(BadRequestError):
        await store.attach("u1", "chat2", [img.id], "req-1")
    [attached] = await store.attach("u1", "chat1", [img.id], "req-1")
    assert attached.request_id == "req-1"
    with pytest.raises(BadRequestError):                       # can't be sent twice
        await store.attach("u1", "chat1", [img.id], "req-2")

    await store.set_description(img.id, "white Tesla Model Y")
    assert (await store.for_session("u1", "chat1"))["req-1"][0].description == "white Tesla Model Y"
    meta, data = await store.read(img.id)
    assert data == PNG and meta.user_id == "u1"

    assert await store.delete_session("u1", "chat1") == 1
    assert bucket.objects == {} and db.rows == {}


async def test_upload_checks():
    store = ImageStore(FakeDb(), web_client({}), bucket_store(FakeBucket()))
    with pytest.raises(BadRequestError):
        await store.save_upload("u1", "c", b"<svg onload=alert(1)>")
    with pytest.raises(BadRequestError):
        await store.save_upload("u1", "c", JPEG + b"0" * MAX_UPLOAD_BYTES)
    with pytest.raises(BadRequestError):
        await store.attach("u1", "c", ["a" * 64] * 1 + ["b" * 64, "c" * 64, "d" * 64, "e" * 64], "r")
    with pytest.raises(BadRequestError):
        await store.attach("u1", "c", ["not-an-id"], "r")


async def test_failed_bucket_delete_keeps_the_row_for_a_retry():
    db, bucket = FakeDb(), FakeBucket()
    store = ImageStore(db, web_client({}), bucket_store(bucket))
    await store.save_upload("u1", "c", JPEG)
    bucket.fail = True
    assert await store.delete_user("u1") == 0 and len(db.rows) == 1


def test_markdown():
    md = markdown([{"path": "/api/v1/images/abc", "alt": "Model [Y]", "source": "ev-database.org"}])
    assert md == "![Model Y](/api/v1/images/abc)\n\n*Picture: ev-database.org*"


async def test_image_search_keeps_https_addresses_only():
    def handler(request):
        assert b'"include_images":true' in request.content.replace(b" ", b"")
        return httpx.Response(200, json={"images": ["https://a.example/1.jpg", {"url": "https://b.example/2.png"}, "http://c/3.jpg", 5]})
    search = WebSearch("key", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await search.images("Model Y") == ["https://a.example/1.jpg", "https://b.example/2.png"]


# ---- vision: pictures → text ---------------------------------------------------------------------------------
def test_prompt_with_pictures_and_back():
    full = vision.prompt_with_pictures("Is this a Tesla?", ["white Tesla Model Y", "a wallbox"])
    assert full.startswith("Is this a Tesla?" + vision.MARKER)
    assert "Picture 1: white Tesla Model Y\nPicture 2: a wallbox" in full
    assert vision.question_only(full) == "Is this a Tesla?"
    assert vision.prompt_with_pictures("hi", []) == "hi" and vision.question_only(None) is None
    assert len(vision.prompt_with_pictures("x" * 20_000, ["a car"])) == 20_000


async def test_describe_sends_the_picture_and_survives_a_failure():
    calls = []

    class LLM:
        async def chat(self, messages, **kw):
            calls.append((messages, kw))
            if len(calls) == 2:
                raise UpstreamError("gateway busy")
            return SimpleNamespace(content="  white Tesla\n Model Y  ")

    prompts = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(system="describe", ref="vision/describe_picture"))
    out = await vision.describe(LLM(), prompts, "u1", "Is this a Tesla?", [(JPEG, "image/jpeg"), (PNG, "image/png")])
    assert out == ["white Tesla Model Y", "(the picture could not be described)"]
    parts = calls[0][0][1]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,") and calls[0][1]["task"] == "vision"


# ---- middleware --------------------------------------------------------------------------------------------
async def _call(mw, path, size):
    sent = []

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive():
        return {"type": "http.request", "body": b"0" * size}

    async def send(msg):
        sent.append(msg)
    await mw(app)({"type": "http", "path": path, "headers": [(b"content-length", str(size).encode())]}, receive, send)
    return sent[0]["status"]


async def test_body_limit_per_path():
    def mw(app):
        return BodySizeLimitMiddleware(app, max_bytes=100, overrides={"/api/v1/uploads": 5000})
    assert await _call(mw, "/api/v1/chat", 200) == 413
    assert await _call(mw, "/api/v1/uploads", 2000) == 200
    assert await _call(mw, "/api/v1/uploads", 6000) == 413


async def test_security_headers_keep_a_routes_own_cache_header():
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
