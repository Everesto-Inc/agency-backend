"""Pictures: upload (a user's own, sent with a question) and read (answers' web pictures + the user's uploads).

    POST /api/v1/uploads?session_id=<chat>   body = the picture bytes (Content-Type image/jpeg|png|webp)
                                              → {"id", "path"}; then POST /chat with image_ids=[id, …]
    GET  /api/v1/images/<id>                  web pictures: public · uploads: only their owner (Bearer token)

Pictures are uploaded when the user presses Send (the browser shrinks them first), never on paste — nothing is
stored for a picture that isn't sent. The bytes live in OCI Object Storage; MySQL says who may see them.
GET is not in the OpenAPI spec (binary, for the browser only).
"""
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel

from agency_platform.agents.schemas import SESSION_ID_PATTERN
from agency_platform.auth.deps import CurrentUser, authenticate_token, get_account_directory, get_token_service
from agency_platform.cache import get_redis, rkey
from agency_platform.config.registry import guardrails
from agency_platform.config.settings import get_settings
from agency_platform.db import get_db
from agency_platform.errors import AppError, BadRequestError, NotFoundError, RateLimitedError, SubscriptionRequiredError
from agency_platform.security.guardrails import guardrail
from agency_platform.service import openapi
from agency_platform.tools.images import MAX_UPLOAD_BYTES, ImageStore
from agency_platform.tools.objectstore import ObjectStore

images_router = APIRouter(include_in_schema=False)
uploads_router = APIRouter(tags=["Chat"], responses=openapi.AUTHED)

UPLOAD_BODY_LIMIT = MAX_UPLOAD_BYTES + 64 * 1024


def image_store(request: Request) -> ImageStore:
    http = request.app.state.http
    return ImageStore(get_db(), http, ObjectStore.from_settings(get_settings(), http))


class Uploaded(BaseModel):
    id: str
    path: str
    content_type: str
    size_bytes: int


@uploads_router.post("", status_code=201, summary="Send a picture (with the next chat message)",
                     responses=openapi.errors(400, 403, 413, 429, 503))
async def upload(request: Request, user: CurrentUser,
                 session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)]) -> Uploaded:
    """Body = the picture itself (JPEG, PNG or WebP, ≤ 2 MB). Returns an id to pass in `image_ids` of POST /chat.
    The type is checked from the file's bytes. Private: only you can load it; deleted with the chat."""
    try:
        if not user.plan_config.topics:
            raise SubscriptionRequiredError("Your account is waiting for activation")
        per_day = guardrails().pictures.per_day
        redis = get_redis()
        day_key = rkey(f"uploads:{user.id}:{datetime.now(UTC).date().isoformat()}")
        count = await redis.incr(day_key)
        if count == 1:
            await redis.expire(day_key, 86_400)
        if count > per_day:
            raise RateLimitedError(f"At most {per_day} pictures per day", retry_after_s=3600)
        img = await image_store(request).save_upload(user.id, session_id, await request.body())
    except (BadRequestError, RateLimitedError, SubscriptionRequiredError) as exc:
        await guardrail("upload_refused", "blocked", detail=f"{exc.code}: {exc.message}"[:200], user_id=user.id)
        raise
    return Uploaded(id=img.id, path=img.path, content_type=img.content_type, size_bytes=img.size_bytes)


async def _viewer_id(request: Request) -> str | None:
    """The signed-in user, if a valid Bearer token was sent (a plain <img> tag sends none → None)."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    try:
        return (await authenticate_token(header[7:], get_token_service(), get_account_directory())).id
    except AppError:
        return None


@images_router.get("/{image_id}")
async def get_image(request: Request, image_id: str = Path(pattern=r"^[0-9a-f]{64}$")) -> Response:
    meta, data = await image_store(request).read(image_id)
    private = meta.kind == "upload"
    if private and meta.user_id != await _viewer_id(request):
        raise NotFoundError("Picture not found")   # same answer as "doesn't exist": an id reveals nothing
    return Response(
        content=data,
        media_type=meta.content_type,
        headers={
            # web pictures never change (id = hash of the source); uploads: this browser only, no shared caches
            "Cache-Control": "private, max-age=86400" if private else "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
            "Cross-Origin-Resource-Policy": "same-site",
        },
    )
