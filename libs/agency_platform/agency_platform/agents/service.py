"""Builds a complete topic microservice around a TopicAgent.

Every topic service exposes the same API under /api/v1/<topic>:
    POST /run                       run (SSE stream or single JSON response)
    WS   /ws                        same over WebSocket (auth = first message)
    POST /runs/{request_id}/feedback  thumbs up/down -> per-model/prompt quality
    GET  /info                      framework + entitlement info
    GET  /docs, /openapi.json       this topic's API docs (under the prefix, so the ingress reaches them)
plus any topic-specific routers the service adds (tagged "<Topic>: tools").
"""
import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from agency_platform.agents.base import AgentEvent, TopicAgent
from agency_platform.agents.response_cache import ResponseCache
from agency_platform.agents.runner import HEARTBEAT, RunRequest, TopicRunner
from agency_platform.agents.schemas import SSE_EXAMPLE, FeedbackBody, RunBody, RunResponse, TopicInfo
from agency_platform.agents.sessions import SessionStore
from agency_platform.auth.deps import (
    CurrentUser,
    authenticate_token,
    get_account_directory,
    get_token_service,
)
from agency_platform.billing.usage import RateLimiter, UsageMeter
from agency_platform.cache import get_redis
from agency_platform.config.registry import prompt_store, topics
from agency_platform.config.settings import PlatformSettings
from agency_platform.db import get_db
from agency_platform.errors import AppError, NotFoundError
from agency_platform.observability.metrics import FEEDBACK
from agency_platform.repositories import ConversationRepository, RunRepository
from agency_platform.service import openapi
from agency_platform.service.app import ServiceSpec, create_service_app

_WS_AUTH_TIMEOUT_S = 10


def sse(event: AgentEvent) -> str:
    if event is HEARTBEAT:
        return ": ping\n\n"
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def _sse_stream(events: AsyncIterator[AgentEvent]) -> AsyncIterator[str]:
    async for ev in events:
        yield sse(ev)


def build_topic_router(get_runner, tag: str) -> APIRouter:
    router = APIRouter(tags=[tag], responses=openapi.AUTHED)

    @router.post(
        "/run",
        summary="Ask the agent",
        response_model=RunResponse,
        responses={
            **openapi.sse_and_json(RunResponse, "SSE stream of agent events (stream=true) or the full result (stream=false)",
                                   SSE_EXAMPLE),
            **openapi.errors(400, 403, 413, 422, 503),
        },
    )
    async def run(body: RunBody, request: Request, user: CurrentUser):
        """Runs the topic agent on one question. Checks the plan, rate limit and daily
        token/cost quota first; tokens and cost of every LLM call are recorded per topic."""
        runner: TopicRunner = get_runner()
        prompt = await runner.preflight(user, body.prompt)
        req = RunRequest(user=user, prompt=prompt, session_id=body.session_id, request_id=request.state.request_id,
                         direct=body.direct, out_of_scope=body.out_of_scope, skills=body.skills)
        if body.stream:
            return StreamingResponse(
                _sse_stream(runner.stream(req)),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
            )
        return await runner.collect(req)

    @router.post("/runs/{request_id}/feedback", status_code=204, summary="Rate an answer",
                 responses=openapi.errors(404, 422))
    async def feedback(request_id: str, body: FeedbackBody, user: CurrentUser) -> None:
        runner: TopicRunner = get_runner()
        runs = RunRepository(get_db())
        found = await runs.set_feedback(
            request_id=request_id, user_id=user.id, topic=runner.topic, score=body.score, comment=body.comment
        )
        if not found:
            raise NotFoundError("Run not found")
        FEEDBACK.labels(runner.topic, str(body.score)).inc()
        if body.score != 0:
            # Feedback -> dataset: every rated run becomes an eval-case candidate
            # for an admin to curate into evals/datasets (see quality worker).
            await runs.propose_eval_case(
                request_id=request_id, label="negative" if body.score < 0 else "positive", comment=body.comment
            )

    @router.get("/info", summary="What this assistant does, and whether my plan includes it")
    async def info(user: CurrentUser) -> TopicInfo:
        runner: TopicRunner = get_runner()
        cfg = runner.topic_config()
        return TopicInfo(
            topic=runner.topic,
            display_name=cfg.display_name,
            framework=runner.agent.framework,
            description=cfg.description,
            entitled=user.plan_config.allows_topic(runner.topic),
            plan=user.plan,
        )

    @router.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        runner: TopicRunner = get_runner()
        try:
            first = await asyncio.wait_for(websocket.receive_json(), timeout=_WS_AUTH_TIMEOUT_S)
            if not isinstance(first, dict) or first.get("type") != "auth" or not first.get("token"):
                await websocket.close(code=4401, reason="auth required")
                return
            user = await authenticate_token(str(first["token"]), get_token_service(), get_account_directory())
        except (TimeoutError, AppError, ValueError, json.JSONDecodeError):
            await websocket.close(code=4401, reason="auth failed")
            return
        await websocket.send_json({"type": "ready", "topic": runner.topic})
        try:
            while True:
                try:
                    body = RunBody.model_validate(await websocket.receive_json())
                except (ValidationError, ValueError, json.JSONDecodeError):
                    await websocket.send_json({"type": "error", "data": {"code": "BAD_REQUEST"}, "content": "Invalid message"})
                    continue
                try:
                    prompt = await runner.preflight(user, body.prompt)
                except AppError as exc:
                    await websocket.send_json({"type": "error", "content": exc.message, "data": {"code": exc.code}})
                    continue
                req = RunRequest(user=user, prompt=prompt, session_id=body.session_id, request_id=uuid.uuid4().hex)
                async for ev in runner.stream(req):
                    if ev is not HEARTBEAT:
                        await websocket.send_text(ev.model_dump_json())
        except WebSocketDisconnect:
            return

    return router


def create_topic_service(
    agent: TopicAgent,
    settings: PlatformSettings,
    *,
    extra_routers: Sequence[tuple[APIRouter, str]] = (),
) -> FastAPI:
    state: dict[str, TopicRunner] = {}

    def get_runner() -> TopicRunner:
        return state["runner"]

    async def startup(app: FastAPI) -> None:
        topics()  # fail fast on invalid topics.yaml
        state["runner"] = TopicRunner(
            agent,
            settings,
            runs=RunRepository(get_db()),
            meter=UsageMeter(get_redis()),
            limiter=RateLimiter(get_redis()),
            sessions=SessionStore(get_redis()),
            prompts=prompt_store(),
            http=app.state.http,
            cache=ResponseCache(get_redis()),
            conversations=ConversationRepository(get_db()),
        )
        await agent.startup(app)

    async def shutdown(_: FastAPI) -> None:
        await agent.shutdown()

    base = f"/api/v1/{agent.topic}"
    cfg = topics().topics.get(agent.topic)
    name = cfg.display_name if cfg else agent.topic
    agent_tag, tools_tag = name, f"{name}: tools"
    tags = [{"name": agent_tag, "description": (cfg.description if cfg else "") + f" Framework: {agent.framework}."}]
    if extra_routers:
        tags.append({"name": tools_tag, "description": "Deterministic helpers (no LLM calls)."})
        for r, _ in extra_routers:  # route tags are fixed at decoration time: set them per route
            for route in r.routes:
                if isinstance(route, APIRoute) and not route.tags:
                    route.tags = [tools_tag]
    spec = ServiceSpec(
        title=f"{name} API",
        description=(
            f"The **{name}** assistant ({agent.framework}). Log in at the edge API "
            "(`POST /api/v1/auth/login`), click **Authorize**, then `POST {base}/run`.\n\n"
            "Streaming (`stream: true`) returns Server-Sent Events: `status`, `tool_call`, `tool_result`, "
            "`artifact`, `token` (the answer), `error`, `usage` (tokens + cost), `done`. "
            f"The same API is available over WebSocket at `{base}/ws` (first message: "
            '`{"type": "auth", "token": "..."}`).'
        ).replace("{base}", base),
        tags=tags,
        docs_prefix=base,
        routers=[(build_topic_router(get_runner, agent_tag), base), *[(r, base + p) for r, p in extra_routers]],
        on_startup=[startup],
        on_shutdown=[shutdown],
    )
    return create_service_app(settings, spec)
