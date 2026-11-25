"""/chat: one entrypoint that picks the right topic agent and streams its answer.

Topic services are also reachable directly (each has its own API under
/api/v1/<topic>); /chat is the convenience front door for free-form prompts.
"""
import json
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agency_platform.agents.schemas import SESSION_ID_PATTERN, SSE_EXAMPLE, RunResponse
from agency_platform.auth.deps import AuthenticatedUser, CurrentUser
from agency_platform.config.registry import privacy, prompt_store, topics
from agency_platform.config.settings import get_settings
from agency_platform.db import get_db
from agency_platform.errors import BadRequestError, ServiceUnavailableError, SubscriptionRequiredError
from agency_platform.llm.call_token import CallClaims, mint_call_token
from agency_platform.llm.client import ModelGatewayClient
from agency_platform.repositories import ConversationRepository, title_from_prompt
from agency_platform.security.content import clean_user_prompt
from agency_platform.security.pii import redact
from agency_platform.service import openapi
from agency_platform.tools.images import MAX_PER_MESSAGE
from agency_platform.tools.vision import describe, prompt_with_pictures, question_only
from edge_gateway.routes_images import image_store
from edge_gateway.schemas import TopicSummary
from edge_gateway.topic_router import TopicChoice, choose_topic

router = APIRouter(tags=["Chat"], responses=openapi.AUTHED)
topics_router = APIRouter(tags=["Topics"], responses=openapi.AUTHED)


class ChatBody(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20_000, description="Any question; the right assistant is picked",
                        examples=["Cheapest new Sony WH-1000XM5 headphones in the US"])
    session_id: str = Field(default="default", pattern=SESSION_ID_PATTERN,
                            description="Conversation id: same id = follow-up questions share memory")
    topic: str | None = Field(default=None, description="Skip auto-routing and use this topic (see GET /topics)",
                              examples=[None])
    stream: bool = Field(True, description="true: Server-Sent Events (first event `routed`); false: one JSON document")
    image_ids: list[str] = Field(default_factory=list, max_length=MAX_PER_MESSAGE,
                                 description="Pictures sent with this question (ids from POST /uploads, same chat)")


async def _remember(user: AuthenticatedUser, session_id: str, prompt: str, topic: str) -> None:
    """Chat history: create the conversation (title = first question, PII-redacted like stored runs) or bump it."""
    pii = privacy().pii
    prompt = question_only(prompt) or prompt
    text = redact(prompt, pii.entities)[0] if pii.redact_in_storage else prompt
    await ConversationRepository(get_db()).touch(
        user_id=user.id, conversation_id=session_id, title=title_from_prompt(text), topic=topic
    )


async def _with_pictures(body: ChatBody, prompt: str, request: Request, user: AuthenticatedUser) -> str:
    """Pictures → one line each (cheap vision model) appended to the question: routing, every assistant and the
    chat memory work on text; the pictures themselves stay in the bucket."""
    if not user.plan_config.topics:
        raise SubscriptionRequiredError("Your account is waiting for activation")
    store = image_store(request)
    images = await store.attach(user.id, body.session_id, body.image_ids, request.state.request_id)
    llm = _gateway_client(user, request)
    if llm is None:
        raise ServiceUnavailableError("Pictures need the model gateway (MODEL_GATEWAY_CLIENT_SECRET)")
    pictures = [(await store.read(img.id))[1] for img in images]
    descriptions = await describe(llm, prompt_store(), user.id, body.prompt,
                                  [(data, img.content_type) for data, img in zip(pictures, images, strict=True)])
    for img, text in zip(images, descriptions, strict=True):
        await store.set_description(img.id, text)
    return prompt_with_pictures(prompt, descriptions)


def _gateway_client(user: AuthenticatedUser, request: Request) -> ModelGatewayClient | None:
    settings = get_settings()
    if settings.model_gateway_client_secret is None:
        return None
    token = mint_call_token(
        CallClaims(client_id="edge", user_id=user.id, plan=user.plan, request_id=request.state.request_id),
        settings.model_gateway_client_secret.get_secret_value(),
        ttl_s=60,
    )
    return ModelGatewayClient(settings.model_gateway_url, token, request.app.state.http)


@router.post(
    "",
    summary="Ask anything (auto-routed to the right assistant)",
    response_model=RunResponse,
    responses={
        **openapi.sse_and_json(RunResponse, "SSE stream (stream=true) or the full result incl. `routing` (stream=false)",
                               "event: routed\ndata: {\"topic\": \"product_finder\", \"confidence\": 0.92, "
                               "\"method\": \"keywords\"}\n\n" + SSE_EXAMPLE),
        **openapi.errors(400, 403, 422, 502, 503),
    },
)
async def chat(body: ChatBody, request: Request, user: CurrentUser):
    """Picks the topic (keywords first, a cheap LLM classification only when unclear), checks
    the plan includes it, then runs that topic's agent. Same result as calling the topic directly."""
    prompt = clean_user_prompt(body.prompt, 20_000)
    enabled = topics().enabled()
    if body.image_ids:
        prompt = await _with_pictures(body, prompt, request, user)

    if body.topic:
        if body.topic not in enabled:
            raise BadRequestError(f"Unknown topic '{body.topic}'", available=list(enabled))
        choice = TopicChoice(body.topic, 1.0, "explicit", {})
    else:
        # Only spend an LLM classification call if the user can run *something*.
        llm = _gateway_client(user, request) if user.plan_config.topics else None
        choice = await choose_topic(prompt, llm)
        if choice.topic is None:
            raise BadRequestError(
                "Couldn't tell which assistant fits this request. Pick one explicitly via 'topic'.",
                available=list(enabled),
            )

    user.require_topic(choice.topic)  # fast upsell answer before any proxying
    target = f"{str(enabled[choice.topic].service_url).rstrip('/')}/api/v1/{choice.topic}/run"
    headers = {
        "Authorization": request.headers["authorization"],
        "X-Request-ID": request.state.request_id,
        "Content-Type": "application/json",
    }
    payload = {"prompt": prompt, "session_id": body.session_id, "stream": body.stream}
    routed = {"topic": choice.topic, "confidence": choice.confidence, "method": choice.method}
    http: httpx.AsyncClient = request.app.state.http
    timeout = httpx.Timeout(enabled[choice.topic].run_timeout_s + 30, connect=5)

    if not body.stream:
        try:
            resp = await http.post(target, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(f"Topic '{choice.topic}' unavailable") from exc
        data = resp.json()
        if resp.status_code == 200:
            data["routing"] = routed
            await _remember(user, body.session_id, prompt, choice.topic)
        return JSONResponse(data, status_code=resp.status_code, headers=_retry_after(resp))

    upstream_req = http.build_request("POST", target, json=payload, headers=headers, timeout=timeout)
    try:
        upstream = await http.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        raise ServiceUnavailableError(f"Topic '{choice.topic}' unavailable") from exc

    if upstream.status_code != 200:  # preflight refusal (quota, subscription...): relay as-is
        content = await upstream.aread()
        await upstream.aclose()
        return JSONResponse(json.loads(content or b"{}"), status_code=upstream.status_code, headers=_retry_after(upstream))

    await _remember(user, body.session_id, prompt, choice.topic)  # accepted by the assistant → in the history

    async def relay() -> AsyncIterator[bytes]:
        try:
            yield f"event: routed\ndata: {json.dumps(routed)}\n\n".encode()
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        relay(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"}
    )


def _retry_after(resp: httpx.Response) -> dict[str, str]:
    return {"Retry-After": resp.headers["retry-after"]} if "retry-after" in resp.headers else {}


@topics_router.get("", summary="Available assistants and whether my plan includes them")
async def list_topics(user: CurrentUser) -> list[TopicSummary]:
    plan = user.plan_config
    return [
        TopicSummary(
            topic=name,
            display_name=t.display_name,
            framework=t.framework,
            description=t.description,
            entitled=plan.allows_topic(name),
            daily_request_limit=plan.topic_daily_limit(name),
            run_path=f"/api/v1/{name}/run",
            docs_path=f"/api/v1/{name}/docs",
        )
        for name, t in topics().enabled().items()
    ]
