"""OpenAI-compatible API with tier routing.

    POST /v1/chat/completions   model = auto | tier-<t> | task-<name>
    GET  /v1/models             the aliases callers may use
    POST /v1/route              dry-run routing decision (debugging, evals)

Auth: `Authorization: Bearer <call token>` minted per agent run by the calling
service (see agency_platform.llm.call_token).
"""
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, Header, Request, Security
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from agency_platform.billing.usage import UsageMeter
from agency_platform.config.drafts import activate
from agency_platform.config.params import ParamFile
from agency_platform.config.prompts import PromptStore
from agency_platform.config.registry import plans, privacy
from agency_platform.errors import UnauthorizedError
from agency_platform.llm.call_token import CallClaims, unverified_client_id, verify_call_token
from agency_platform.llm.client import H_MIN_TIER, H_MODEL, H_PROMPT_REF, H_ROUTE_REASON, H_TIER
from agency_platform.llm.tiers import AUTO_ALIAS, Tier, task_alias, tier_alias
from agency_platform.observability.logging import user_id_var
from agency_platform.observability.metrics import PII_REDACTIONS, ROUTING_DECISIONS
from agency_platform.security.guardrails import guardrail
from agency_platform.security.pii import redact_messages
from agency_platform.service import openapi
from model_gateway.catalog import ModelsFile
from model_gateway.executor import CallMeta, Executor
from model_gateway.policy import RouteDecision, RoutingPolicy
from model_gateway.recorder import usage_topic

logger = logging.getLogger(__name__)
router = APIRouter(responses=openapi.errors(401))
OPENAI, ROUTING = ["OpenAI-compatible"], ["Routing"]

call_token_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="CallToken",
    description="Per-run call token minted by the calling service (agency_platform.llm.call_token). "
                "Internal only: this service is never exposed publicly.",
)


@dataclass
class GatewayState:
    models: ParamFile[ModelsFile]
    policy: RoutingPolicy
    executor: Executor
    meter: UsageMeter
    prompts: PromptStore
    client_secrets: dict[str, str]


def get_state(request: Request) -> GatewayState:
    return request.app.state.gateway


State = Annotated[GatewayState, Depends(get_state)]


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = AUTO_ALIAS
    messages: list[dict[str, Any]] = Field(..., min_length=1, max_length=500)
    stream: bool = False


async def authenticate(
    state: State, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(call_token_scheme)]
) -> CallClaims:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Missing call token")
    token = credentials.credentials
    client_id = unverified_client_id(token)
    secret = state.client_secrets.get(client_id or "")
    if secret is None:
        raise UnauthorizedError("Unknown client")
    try:
        claims = verify_call_token(token, secret)
    except jwt.PyJWTError:
        raise UnauthorizedError("Invalid or expired call token") from None
    if claims.client_id != client_id:
        raise UnauthorizedError("Client mismatch")
    user_id_var.set(claims.user_id)
    await activate(claims.config_draft)   # testing a config draft: models / plans / privacy from the draft
    return claims


Claims = Annotated[CallClaims, Depends(authenticate)]


def _min_tier(raw: str | None) -> Tier | None:
    try:
        return Tier(raw) if raw else None
    except ValueError:
        return None


async def _route(
    state: State, body: ChatCompletionRequest, claims: CallClaims, min_tier: Tier | None, meta: CallMeta
) -> tuple[RouteDecision, ModelsFile]:
    models = state.models.get()
    extras = body.model_extra or {}

    async def judge(text: str) -> Tier | None:
        """Low-tier LLM verdict for ambiguous requests (only called in the middle band)."""
        prompt = state.prompts.get("model_gateway", "complexity_judge")
        decision = RouteDecision(requested="judge", tier=Tier.LOW, ceiling=Tier.LOW, source="judge",
                                 reason="complexity judge", task="router_judge")
        try:
            resp, _ = await state.executor.complete(
                decision, models,
                [{"role": "system", "content": prompt.system}, {"role": "user", "content": text}],
                {"temperature": 0, "max_tokens": 60, "response_format": {"type": "json_object"}},
                CallMeta(meta.request_id, meta.user_id, meta.client_id, prompt.ref),
            )
            verdict = json.loads(resp.choices[0].message.content or "{}").get("tier")
            return Tier(verdict)
        except Exception:
            logger.warning("complexity judge failed; falling back to default tier")
            return None

    decision = await state.policy.decide(
        requested_model=body.model,
        messages=body.messages,
        has_tools=bool(extras.get("tools")),
        client_id=claims.client_id,
        plan=claims.plan,
        min_tier=min_tier,
        models=models,
        judge=judge,
    )
    ROUTING_DECISIONS.labels(decision.source, decision.tier.value).inc()
    return decision, models


class ChatCompletion(BaseModel):
    """OpenAI chat.completion object (documentation; the provider response is passed through)."""

    model_config = ConfigDict(extra="allow")
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: dict[str, Any] | None = None


@router.post(
    "/chat/completions",
    tags=OPENAI,
    summary="Chat completion, routed to a model tier",
    responses={
        **openapi.sse_and_json(ChatCompletion, "chat.completion (stream=false) or chat.completion.chunk SSE (stream=true). "
                               "Headers X-Agency-Tier / X-Agency-Model say what was used.",
                               "data: {\"object\": \"chat.completion.chunk\", ...}\n\ndata: [DONE]\n\n"),
        **openapi.errors(400, 422, 429, 503),
    },
)
async def chat_completions(
    body: ChatCompletionRequest,
    state: State,
    claims: Claims,
    x_agency_min_tier: Annotated[str | None, Header(alias=H_MIN_TIER)] = None,
    x_agency_prompt: Annotated[str | None, Header(alias=H_PROMPT_REF)] = None,
):
    # Hard stop mid-run: a long agent loop can't blow through the daily budgets.
    plan_name = claims.plan
    plan = plans().get(plan_name)
    await state.meter.check_daily_quota(claims.user_id, plan_name, plan)
    await state.meter.check_topic_tokens(claims.user_id, usage_topic(claims.client_id), plan)

    # PII never leaves for a provider, whichever framework built the messages.
    pii = privacy().pii
    redactions = 0
    if pii.redact_before_llm:
        body.messages, counts = redact_messages(body.messages, pii.entities)
        for entity, n in counts.items():
            PII_REDACTIONS.labels(entity).inc(n)
        redactions = sum(counts.values())
        if redactions:
            await guardrail("pii_masked", "masked", topic=usage_topic(claims.client_id), user_id=claims.user_id,
                            request_id=claims.request_id,
                            detail=", ".join(f"{e} ×{n}" for e, n in sorted(counts.items())))

    meta = CallMeta(
        request_id=claims.request_id,
        user_id=claims.user_id,
        client_id=claims.client_id,
        prompt_ref=(x_agency_prompt or "")[:120] or None,
        pii_redactions=redactions,
    )
    decision, models = await _route(state, body, claims, _min_tier(x_agency_min_tier), meta)
    params = dict(body.model_extra or {})

    if body.stream:
        chunks, outcome = await state.executor.stream(decision, models, body.messages, params, meta)
        headers = _headers(decision, outcome.model_id, outcome.tier)

        async def sse() -> AsyncIterator[str]:
            async for chunk in chunks:
                payload = chunk.model_dump(exclude_none=True)
                payload["model"] = outcome.model_id
                yield f"data: {json.dumps(payload, default=str)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream", headers=headers)

    resp, outcome = await state.executor.complete(decision, models, body.messages, params, meta)
    payload = resp.model_dump(exclude_none=True)
    payload["model"] = outcome.model_id
    return JSONResponse(
        payload, headers=_headers(decision, outcome.model_id, outcome.tier, cached=outcome.cached, canary=outcome.canary)
    )


def _headers(decision: RouteDecision, model_id: str, tier: Tier, *, cached: bool = False, canary: bool = False) -> dict[str, str]:
    h = {H_TIER: tier.value, H_MODEL: model_id, H_ROUTE_REASON: decision.reason[:200]}
    if cached:
        h["X-Agency-Cache"] = "hit"
    if canary:
        h["X-Agency-Canary"] = "1"
    return h


class ModelAlias(BaseModel):
    id: str = Field(..., examples=["task-synthesis"])
    object: str = "model"
    owned_by: str = "agency"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelAlias]


@router.get("/models", tags=OPENAI, summary="Model aliases callers may request (auto, tier-*, task-*)")
async def list_models(state: State, _: Claims) -> ModelList:
    models = state.models.get()
    aliases = [AUTO_ALIAS, *[tier_alias(t) for t in Tier], *[task_alias(t) for t in models.routing.tasks]]
    return ModelList(data=[ModelAlias(id=a) for a in aliases])


class RouteProbe(BaseModel):
    model: str = AUTO_ALIAS
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    has_tools: bool = False


class RouteDecisionOut(BaseModel):
    tier: str
    source: str = Field(..., description="task | tier | classifier | judge | default")
    reason: str
    ceiling: str = Field(..., description="Highest tier the caller's plan allows")
    candidates: list[str] = Field(..., description="Models tried in order (fallbacks after the first)")


@router.post("/route", tags=ROUTING, summary="Dry-run: which tier and models would this request get?",
             responses=openapi.errors(422))
async def route_dry_run(probe: RouteProbe, state: State, claims: Claims) -> RouteDecisionOut:
    """Heuristic-only dry run (no judge call, no cost): what tier would this get?"""
    models = state.models.get()
    decision = await state.policy.decide(
        requested_model=probe.model, messages=probe.messages, has_tools=probe.has_tools,
        client_id=claims.client_id, plan=claims.plan, min_tier=None, models=models, judge=None,
    )
    return RouteDecisionOut(
        tier=decision.tier, source=decision.source, reason=decision.reason,
        ceiling=decision.ceiling, candidates=list(models.tiers[decision.tier]),
    )
