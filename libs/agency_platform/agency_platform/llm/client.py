"""Client for the model gateway (OpenAI-compatible API + routing extensions).

Agents describe *what kind of work* a call is and the gateway picks the model:

    await llm.chat(messages, task="intent_parsing")     # -> low tier
    await llm.chat(messages, task="reasoning")          # -> high tier
    await llm.chat(messages)                            # -> "auto": classifier decides
    await llm.structured(messages, MySchema, task="extraction")  # validated JSON,
                                                         #    escalates a tier on bad output

Framework-native clients (CrewAI, AutoGen, crawl4ai) get the same routing via
`openai_compat()`, which returns base_url / api_key / model alias.
"""
import json
import logging
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from agency_platform.config.prompts import Prompt
from agency_platform.errors import QuotaExceededError, ServiceUnavailableError, UpstreamError
from agency_platform.llm.tiers import AUTO_ALIAS, Tier, task_alias, tier_alias

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Extension headers understood by the gateway.
H_MIN_TIER = "X-Agency-Min-Tier"
H_PROMPT_REF = "X-Agency-Prompt"
H_TIER = "X-Agency-Tier"
H_MODEL = "X-Agency-Model"
H_ROUTE_REASON = "X-Agency-Route-Reason"


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    tier: str
    route_reason: str
    usage: dict[str, int]
    raw: dict[str, Any]


@dataclass(frozen=True)
class OpenAICompatConfig:
    base_url: str
    api_key: str
    model: str


def resolve_alias(task: str | None, tier: Tier | None) -> str:
    if task and tier:
        raise ValueError("pass either task or tier, not both")
    if task:
        return task_alias(task)
    if tier:
        return tier_alias(tier)
    return AUTO_ALIAS


class ModelGatewayClient:
    """Bound to one agent run (one call token)."""

    def __init__(self, base_url: str, call_token: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._token = call_token
        self._http = http

    @property
    def call_token(self) -> str:
        return self._token

    def openai_compat(self, *, task: str | None = None, tier: Tier | None = None) -> OpenAICompatConfig:
        return OpenAICompatConfig(base_url=f"{self._base}/v1", api_key=self._token, model=resolve_alias(task, tier))

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        task: str | None = None,
        tier: Tier | None = None,
        min_tier: Tier | None = None,
        prompt: Prompt | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        timeout_s: float = 120.0,
    ) -> ChatResult:
        body: dict[str, Any] = {"model": resolve_alias(task, tier), "messages": messages}
        if temperature is None and prompt is not None:
            temperature = prompt.spec.temperature
        if max_tokens is None and prompt is not None:
            max_tokens = prompt.spec.max_tokens
        for key, value in (("temperature", temperature), ("max_tokens", max_tokens), ("response_format", response_format)):
            if value is not None:
                body[key] = value

        headers = {"Authorization": f"Bearer {self._token}"}
        if min_tier:
            headers[H_MIN_TIER] = min_tier.value
        if prompt:
            headers[H_PROMPT_REF] = prompt.ref

        try:
            resp = await self._http.post(f"{self._base}/v1/chat/completions", json=body, headers=headers, timeout=timeout_s)
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(f"Model gateway unreachable: {type(exc).__name__}") from exc

        if resp.status_code == 429:
            detail = _error_message(resp)
            raise QuotaExceededError(detail, retry_after_s=int(resp.headers.get("Retry-After", "60")))
        if resp.status_code >= 400:
            raise UpstreamError(f"Model gateway error {resp.status_code}: {_error_message(resp)}")

        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        return ChatResult(
            content=content,
            model=resp.headers.get(H_MODEL, data.get("model", "")),
            tier=resp.headers.get(H_TIER, ""),
            route_reason=resp.headers.get(H_ROUTE_REASON, ""),
            usage=data.get("usage") or {},
            raw=data,
        )

    async def structured(
        self,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        task: str | None = None,
        tier: Tier | None = None,
        prompt: Prompt | None = None,
        max_escalations: int = 1,
        temperature: float | None = 0.0,
    ) -> T:
        """JSON-mode call validated against `schema`.

        If the answer doesn't validate, retry with the minimum tier raised by one
        (cheap model first, stronger model only when it actually failed)."""
        min_tier: Tier | None = None
        last_error: Exception | None = None
        for attempt in range(max_escalations + 1):
            result = await self.chat(
                messages,
                task=task,
                tier=tier,
                min_tier=min_tier,
                prompt=prompt,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            try:
                return schema.model_validate(json.loads(_strip_fences(result.content)))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                current = Tier(result.tier) if result.tier in Tier.__members__.values() else Tier.LOW
                if current == Tier.HIGH:
                    break
                min_tier = current.escalate()
                logger.warning(
                    "structured output invalid; escalating",
                    extra={"attempt": attempt, "from_tier": current.value, "to_tier": min_tier.value},
                )
        raise UpstreamError(f"Model returned invalid structured output: {last_error}")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message", err))
        return str(err or body.get("detail") or body)
    except ValueError:
        return resp.text[:300]
