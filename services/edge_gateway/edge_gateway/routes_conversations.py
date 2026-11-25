"""Chat history: my conversations (a conversation = the session_id of POST /chat), their messages, rename/pin, delete."""
from typing import Annotated

from fastapi import APIRouter, Path, Query

from agency_platform.agents.schemas import SESSION_ID_PATTERN
from agency_platform.agents.sessions import SessionStore
from agency_platform.auth.deps import CurrentUser
from agency_platform.cache import get_redis
from agency_platform.db import get_db
from agency_platform.errors import NotFoundError
from agency_platform.repositories import ConversationRepository
from agency_platform.service import openapi
from edge_gateway.schemas import Conversation, ConversationMessage, ConversationSummary, ConversationUpdate

conversations_router = APIRouter(tags=["Conversations"], responses=openapi.AUTHED)

ConversationId = Annotated[str, Path(pattern=SESSION_ID_PATTERN, description="The conversation (= chat session_id)")]


def _repo() -> ConversationRepository:
    return ConversationRepository(get_db())


@conversations_router.get("", summary="My conversations (pinned first, then most recent)")
async def list_conversations(
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
    q: str | None = Query(default=None, max_length=100, description="Only titles containing this text"),
) -> list[ConversationSummary]:
    return [ConversationSummary(**r) for r in await _repo().recent(user.id, limit=limit, query=q or None)]


@conversations_router.get("/{conversation_id}", summary="One conversation with its questions and answers",
                          responses=openapi.errors(404))
async def get_conversation(conversation_id: ConversationId, user: CurrentUser) -> Conversation:
    """Continue it by sending POST /chat with `session_id` = this id."""
    repo = _repo()
    conv = await repo.get(user.id, conversation_id)
    if conv is None:
        raise NotFoundError("Conversation not found")
    messages = [ConversationMessage(**{**m, "cost_usd": float(m["cost_usd"] or 0), "total_tokens": int(m["total_tokens"] or 0),
                                       "models": [x for x in (m.get("models") or "").split(",") if x]})
                for m in await repo.messages(user.id, conversation_id)]
    return Conversation(**conv, messages=messages)


@conversations_router.patch("/{conversation_id}", summary="Rename or pin a conversation", responses=openapi.errors(404, 422))
async def update_conversation(conversation_id: ConversationId, body: ConversationUpdate, user: CurrentUser) -> ConversationSummary:
    repo = _repo()
    title = body.title.strip() if body.title is not None else None
    if not await repo.update(user.id, conversation_id, title=title or None, pinned=body.pinned):
        raise NotFoundError("Conversation not found")
    conv = await repo.get(user.id, conversation_id)
    assert conv is not None
    return ConversationSummary(**conv)


@conversations_router.delete("/{conversation_id}", status_code=204, summary="Delete a conversation (its text is erased)",
                             responses=openapi.errors(404))
async def delete_conversation(conversation_id: ConversationId, user: CurrentUser) -> None:
    """Gone from the history, questions/answers erased, assistant memory forgotten. Usage (tokens, cost) is kept."""
    if not await _repo().delete(user.id, conversation_id):
        raise NotFoundError("Conversation not found")
    await SessionStore(get_redis()).forget_session(user.id, conversation_id)
