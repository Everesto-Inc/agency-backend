"""Pictures shown in answers: downloaded ONCE from the web, checked, stored in MySQL, served by our own API.

    store = ImageStore(get_db(), http)
    path = await store.save_from_url("https://…/model-y.jpg", alt="2024 Tesla Model Y")   # → "/api/v1/images/<id>"

Why a copy and not a link: the browser only ever loads images from our API (strict CSP, no third-party
requests, no tracking), and a picture stays visible in the chat history even if the source page changes.
GET /api/v1/images/<id> serves ONLY what was stored here — it is not an open proxy.
Checks before storing: public address only (SSRF guard), real image type, at most MAX_BYTES, no redirects
to anywhere unchecked.
"""
import hashlib
from typing import Any

import httpx

from agency_platform.db import Database
from agency_platform.security.ssrf import UnsafeURLError, assert_public_url

MAX_BYTES = 1_500_000
ALLOWED_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/avif")
PATH_PREFIX = "/api/v1/images/"


def image_id(url: str) -> str:
    """Pure: the stored picture's id = sha256 of its source address (same picture → stored once)."""
    return hashlib.sha256(url.strip().encode()).hexdigest()


def markdown(pictures: list[dict[str, str]]) -> str:
    """Pure: pictures → markdown appended under an answer (alt text + where it came from)."""
    lines = []
    for p in pictures:
        alt = p.get("alt", "").replace("[", "").replace("]", "")
        lines.append(f"![{alt}]({p['path']})")
        if p.get("source"):
            lines.append(f"*Picture: {p['source']}*")
    return "\n\n".join(lines)


class ImageStore:
    def __init__(self, db: Database, http: httpx.AsyncClient) -> None:
        self._db = db
        self._http = http

    async def save_from_url(self, url: str, alt: str = "") -> str | None:
        """Store the picture (if usable) and return its path on our API; None if it can't be used."""
        iid = image_id(url)
        if await self._db.fetchval("SELECT 1 FROM images WHERE id = %s", (iid,)):
            return PATH_PREFIX + iid
        try:
            await assert_public_url(url)
            content_type, data = await self._download(url)
        except (UnsafeURLError, httpx.HTTPError, ValueError):
            return None
        await self._db.execute(
            """INSERT IGNORE INTO images (id, content_type, bytes, source_url, alt)
               VALUES (%s, %s, %s, %s, %s)""",
            (iid, content_type, data, url[:2000], alt[:200]),
        )
        return PATH_PREFIX + iid

    async def _download(self, url: str) -> tuple[str, bytes]:
        async with self._http.stream("GET", url, timeout=10, follow_redirects=False,
                                     headers={"accept": ",".join(ALLOWED_TYPES)}) as resp:
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in ALLOWED_TYPES:
                raise ValueError(f"not an image: {content_type or 'unknown'}")
            if int(resp.headers.get("content-length") or 0) > MAX_BYTES:
                raise ValueError("too large")
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("too large")
        if not data:
            raise ValueError("empty")
        return content_type, bytes(data)

    async def get(self, iid: str) -> dict[str, Any] | None:
        return await self._db.fetchrow("SELECT content_type, bytes FROM images WHERE id = %s", (iid,))
