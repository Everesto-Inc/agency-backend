"""Pictures shown in answers: GET /api/v1/images/<id> serves ONLY pictures the assistants stored (agency_platform
.tools.images) — not an open proxy. Public (an <img> tag can't send a token); ids are unguessable sha256 hashes and
the pictures are public web images anyway. Not in the OpenAPI spec (binary, for the browser only).
"""
from fastapi import APIRouter, Path, Request, Response

from agency_platform.db import get_db
from agency_platform.errors import NotFoundError
from agency_platform.tools.images import ImageStore

images_router = APIRouter(include_in_schema=False)


@images_router.get("/{image_id}")
async def get_image(request: Request, image_id: str = Path(pattern=r"^[0-9a-f]{64}$")) -> Response:
    row = await ImageStore(get_db(), request.app.state.http).get(image_id)
    if row is None:
        raise NotFoundError("Image not found")
    return Response(
        content=bytes(row["bytes"]),
        media_type=row["content_type"],
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",   # id = hash of the source: never changes
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        },
    )
