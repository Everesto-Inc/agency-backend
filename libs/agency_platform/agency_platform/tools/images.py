"""Pictures in chats: the bytes live in OCI Object Storage, MySQL `images` says what they are and who may see them.

    store = ImageStore(get_db(), http, ObjectStore.from_settings(settings, http))
    path = await store.save_from_url("https://…/model-y.jpg", alt="2024 Tesla Model Y")   # → "/api/v1/images/<id>"
    img = await store.save_upload(user_id, session_id, data)                                # a user's picture

Two kinds:
  web     found on the web for an answer. Downloaded once (public address only, real image, ≤ MAX_WEB_BYTES, no
          redirects), shared by everyone, id = sha256 of the source address → bucket key web/<id>.
  upload  sent by a user with a question (the browser shrinks it first). Private to that user: served only to them,
          deleted with the chat, the account, or after the retention period → key uploads/<user>/<id>.

The browser only ever loads pictures from our API (GET /api/v1/images/<id>), never from the bucket or a third
party: strict CSP, no tracking, and pictures stay visible even if the source page changes.
The type is decided from the file's first bytes, never from what the sender claims.
"""
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

from agency_platform.db import Database
from agency_platform.errors import BadRequestError, NotFoundError, ServiceUnavailableError, UpstreamError
from agency_platform.security.ssrf import UnsafeURLError, assert_public_url
from agency_platform.tools.objectstore import ObjectStore

logger = logging.getLogger(__name__)

MAX_WEB_BYTES = 1_500_000
MAX_UPLOAD_BYTES = 2_000_000          # the browser sends ~300 KB (≤ 1600 px); this is the hard server limit
MAX_PER_MESSAGE = 4
PATH_PREFIX = "/api/v1/images/"
UPLOAD_TYPES = ("image/jpeg", "image/png", "image/webp")
WEB_TYPES = (*UPLOAD_TYPES, "image/gif", "image/avif")
_ID_HEX = 64


def image_id(url: str) -> str:
    """Pure: a web picture's id = sha256 of its source address (same picture → stored once)."""
    return hashlib.sha256(url.strip().encode()).hexdigest()


def is_image_id(value: str) -> bool:
    return len(value) == _ID_HEX and all(c in "0123456789abcdef" for c in value)


def sniff(data: bytes) -> str | None:
    """Pure: the real image type from the first bytes (None = not a picture we accept)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return None


def markdown(pictures: list[dict[str, str]]) -> str:
    """Pure: pictures → markdown appended under an answer (alt text + where it came from)."""
    lines = []
    for p in pictures:
        alt = p.get("alt", "").replace("[", "").replace("]", "")
        lines.append(f"![{alt}]({p['path']})")
        if p.get("source"):
            lines.append(f"*Picture: {p['source']}*")
    return "\n\n".join(lines)


@dataclass(frozen=True)
class StoredImage:
    id: str
    kind: str
    content_type: str
    size_bytes: int
    user_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    description: str | None = None

    @property
    def path(self) -> str:
        return PATH_PREFIX + self.id


def _row(r: dict[str, Any]) -> StoredImage:
    return StoredImage(**{k: r[k] for k in StoredImage.__dataclass_fields__})


class ImageStore:
    def __init__(self, db: Database, http: httpx.AsyncClient, objects: ObjectStore | None) -> None:
        self._db = db
        self._http = http
        self._objects = objects

    @property
    def enabled(self) -> bool:
        return self._objects is not None

    def _require(self) -> ObjectStore:
        if self._objects is None:
            raise ServiceUnavailableError("Pictures are not configured (S3_* settings missing)")
        return self._objects

    # ---- web pictures (answers) ------------------------------------------------
    async def save_from_url(self, url: str, alt: str = "") -> str | None:
        """Store a web picture (if usable) and return its path on our API; None if it can't be used."""
        if self._objects is None:
            return None
        iid = image_id(url)
        if await self._db.fetchval("SELECT 1 FROM images WHERE id = %s", (iid,)):
            return PATH_PREFIX + iid
        try:
            await assert_public_url(url)
            data = await self._download(url)
            content_type = sniff(data)
            if content_type not in WEB_TYPES:
                return None
            await self._objects.put(f"web/{iid}", data, content_type)
        except (UnsafeURLError, httpx.HTTPError, ValueError, UpstreamError):
            return None
        await self._db.execute(
            """INSERT IGNORE INTO images (id, kind, object_key, content_type, size_bytes, source_url, alt)
               VALUES (%s, 'web', %s, %s, %s, %s, %s)""",
            (iid, f"web/{iid}", content_type, len(data), url[:2000], alt[:200]),
        )
        return PATH_PREFIX + iid

    async def _download(self, url: str) -> bytes:
        async with self._http.stream("GET", url, timeout=10, follow_redirects=False,
                                     headers={"accept": ",".join(WEB_TYPES)}) as resp:
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            if int(resp.headers.get("content-length") or 0) > MAX_WEB_BYTES:
                raise ValueError("too large")
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_WEB_BYTES:
                    raise ValueError("too large")
        if not data:
            raise ValueError("empty")
        return bytes(data)

    # ---- uploads (a user's pictures) --------------------------------------------
    async def save_upload(self, user_id: str, session_id: str, data: bytes) -> StoredImage:
        objects = self._require()
        if len(data) > MAX_UPLOAD_BYTES:
            raise BadRequestError(f"Picture too large (max {MAX_UPLOAD_BYTES // 1_000_000} MB)")
        content_type = sniff(data)
        if content_type not in UPLOAD_TYPES:
            raise BadRequestError("Only JPEG, PNG or WebP pictures can be sent")
        iid = secrets.token_hex(32)
        key = f"uploads/{user_id}/{iid}"
        await objects.put(key, data, content_type)
        await self._db.execute(
            """INSERT INTO images (id, kind, object_key, content_type, size_bytes, user_id, session_id)
               VALUES (%s, 'upload', %s, %s, %s, %s, %s)""",
            (iid, key, content_type, len(data), user_id, session_id),
        )
        return StoredImage(iid, "upload", content_type, len(data), user_id, session_id)

    async def attach(self, user_id: str, session_id: str, ids: list[str], request_id: str) -> list[StoredImage]:
        """The user's own, not-yet-sent uploads of this chat → linked to the question. Anything else is refused."""
        ids = list(dict.fromkeys(ids))
        if len(ids) > MAX_PER_MESSAGE:
            raise BadRequestError(f"At most {MAX_PER_MESSAGE} pictures per message")
        if not all(is_image_id(i) for i in ids):
            raise BadRequestError("Unknown picture")
        out = []
        for iid in ids:   # at most 4
            row = await self._db.fetchrow(
                """SELECT id, kind, content_type, size_bytes, user_id, session_id, request_id, description FROM images
                   WHERE id = %s AND kind = 'upload' AND user_id = %s AND session_id = %s AND request_id IS NULL""",
                (iid, user_id, session_id),
            )
            if row is None:
                raise BadRequestError("Unknown picture (not yours, other chat, or already sent)")
            out.append(_row({**row, "request_id": request_id}))
        for img in out:
            await self._db.execute("UPDATE images SET request_id = %s WHERE id = %s AND user_id = %s",
                                   (request_id, img.id, user_id))
        return out

    async def set_description(self, iid: str, text: str) -> None:
        await self._db.execute("UPDATE images SET description = %s WHERE id = %s", (text[:1000], iid))

    async def for_session(self, user_id: str, session_id: str) -> dict[str, list[StoredImage]]:
        """Sent uploads of one chat, per question (chat history)."""
        rows = await self._db.fetch(
            """SELECT id, kind, content_type, size_bytes, user_id, session_id, request_id, description FROM images
               WHERE kind = 'upload' AND user_id = %s AND session_id = %s AND request_id IS NOT NULL
               ORDER BY created_at""",
            (user_id, session_id),
        )
        out: dict[str, list[StoredImage]] = {}
        for r in rows:
            out.setdefault(r["request_id"], []).append(_row(r))
        return out

    # ---- reading ------------------------------------------------------------------
    async def read(self, iid: str) -> tuple[StoredImage, bytes]:
        row = await self._db.fetchrow(
            """SELECT id, kind, content_type, size_bytes, user_id, session_id, request_id, description, object_key
               FROM images WHERE id = %s""",
            (iid,),
        )
        if row is None:
            raise NotFoundError("Picture not found")
        data, _ = await self._require().get(row["object_key"])
        return _row(row), data

    # ---- deleting (bucket first, then the row: a failed bucket delete keeps the row → retried next time) -------
    async def _delete(self, rows: list[dict[str, Any]]) -> int:
        done = 0
        for r in rows:
            if self._objects is not None:
                try:
                    await self._objects.delete(r["object_key"])
                except UpstreamError:
                    logger.warning("picture delete failed; will retry", extra={"image_id": r["id"]})
                    continue
            await self._db.execute("DELETE FROM images WHERE id = %s", (r["id"],))
            done += 1
        return done

    async def delete_session(self, user_id: str, session_id: str) -> int:
        return await self._delete(await self._db.fetch(
            "SELECT id, object_key FROM images WHERE kind = 'upload' AND user_id = %s AND session_id = %s",
            (user_id, session_id)))

    async def delete_user(self, user_id: str) -> int:
        return await self._delete(await self._db.fetch(
            "SELECT id, object_key FROM images WHERE kind = 'upload' AND user_id = %s", (user_id,)))

    async def delete_uploads_older_than(self, days: int) -> int:
        return await self._delete(await self._db.fetch(
            """SELECT id, object_key FROM images
               WHERE kind = 'upload' AND created_at < UTC_TIMESTAMP(6) - INTERVAL %s DAY LIMIT 1000""",
            (days,)))
