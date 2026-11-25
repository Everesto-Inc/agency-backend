"""Executes a routed request: candidate models of the chosen tier in order,
then (optionally) escalation to higher tiers -- never above the plan ceiling.

Errors that are the caller's fault (malformed request) fail immediately;
provider-side failures (rate limits, timeouts, outages, auth/misconfig,
context window) fall through to the next candidate. Every attempt, failed or
not, is recorded for performance tracking.

Also: canary models (a sticky % of a tier's runs tries the canary first) and an
exact-match cache for deterministic calls.
"""
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import litellm

from agency_platform.config.prompts import bucket
from agency_platform.errors import BadRequestError, ServiceUnavailableError
from agency_platform.llm.tiers import Tier
from model_gateway.catalog import ModelsFile, ModelSpec
from model_gateway.llm_cache import LLMCache, cache_key, cacheable
from model_gateway.policy import RouteDecision
from model_gateway.recorder import CallRecord, CallRecorder
from model_gateway.settings import GatewaySettings

logger = logging.getLogger(__name__)

litellm.drop_params = True  # tolerate params a given provider doesn't support
litellm.telemetry = False

# Caller errors: retrying on another model won't help.
_FATAL = (litellm.exceptions.BadRequestError, litellm.exceptions.UnprocessableEntityError)
# ...except these BadRequest subclasses, which another model may well handle.
_RETRYABLE_BAD_REQUEST = (
    litellm.exceptions.ContextWindowExceededError,
    litellm.exceptions.ContentPolicyViolationError,
    litellm.exceptions.UnsupportedParamsError,
)

PASSTHROUGH_PARAMS = {
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "tools", "tool_choice",
    "response_format", "seed", "presence_penalty", "frequency_penalty", "parallel_tool_calls", "reasoning_effort",
}


@dataclass(frozen=True)
class CallMeta:
    request_id: str
    user_id: str | None
    client_id: str
    prompt_ref: str | None
    pii_redactions: int = 0


@dataclass
class Outcome:
    model_id: str
    spec: ModelSpec
    tier: Tier
    attempts: int
    canary: bool = False
    cached: bool = False


Candidate = tuple[Tier, str, ModelSpec, bool]  # (tier, model id, spec, is_canary)


class Executor:
    def __init__(self, settings: GatewaySettings, recorder: CallRecorder, cache: LLMCache | None = None) -> None:
        self._settings = settings
        self._recorder = recorder
        self._cache = cache

    # ---------------------------------------------------------------- planning
    def _usable(self, spec: ModelSpec, needs: set[str]) -> bool:
        return spec.enabled and spec.supports(needs) and bool(self._settings.provider_key(spec.provider))

    def plan(
        self, decision: RouteDecision, models: ModelsFile, needs: set[str], canary_key: str = ""
    ) -> list[Candidate]:
        tiers = [decision.tier]
        if models.routing.escalate_on_error:
            t = decision.tier
            for _ in range(models.routing.max_escalations):
                nxt = t.escalate()
                if nxt == t or nxt.rank > decision.ceiling.rank:
                    break
                tiers.append(nxt)
                t = nxt
        seen: set[str] = set()
        out: list[Candidate] = []
        canary = models.canary_for(decision.tier)
        if canary and bucket("canary", canary_key, decision.tier.value) < canary.percent:
            spec = models.models[canary.model]
            if self._usable(spec, needs):
                seen.add(canary.model)
                out.append((decision.tier, canary.model, spec, True))
        for tier in tiers:
            for model_id in models.tiers[tier]:
                spec = models.models[model_id]
                if model_id in seen or not self._usable(spec, needs):
                    continue
                seen.add(model_id)
                out.append((tier, model_id, spec, False))
        if not out:
            raise ServiceUnavailableError(
                f"No enabled model with a configured API key for tier '{decision.tier}' (needs: {sorted(needs)})"
            )
        return out

    def _cost(self, spec: ModelSpec, response: Any, pt: int, ct: int) -> float:
        explicit = spec.cost(pt, ct)
        if explicit is not None:
            return explicit
        try:
            return float(litellm.completion_cost(completion_response=response, model=spec.litellm_model) or 0.0)
        except Exception:
            return 0.0

    def _record(self, meta: CallMeta, d: RouteDecision, tier: Tier, model_id: str, attempt: int, status: str,
                error: str | None, pt: int, ct: int, cost: float, started: float, streamed: bool,
                *, canary: bool = False, cached: bool = False) -> CallRecord:
        return CallRecord(
            request_id=meta.request_id, user_id=meta.user_id, client_id=meta.client_id, task=d.task,
            prompt_ref=meta.prompt_ref, requested_model=d.requested, route_source=d.source,
            route_reason=d.reason, tier=tier.value, model=model_id, attempt=attempt, status=status,
            error_type=error, prompt_tokens=pt, completion_tokens=ct, cost_usd=cost,
            latency_ms=int((time.perf_counter() - started) * 1000), streamed=streamed,
            canary=canary, cached=cached, pii_redactions=meta.pii_redactions,
        )

    def _kwargs(self, spec: ModelSpec, messages: list[dict], params: dict[str, Any], meta: CallMeta, d: RouteDecision,
                models: ModelsFile | None = None) -> dict:
        # thinking: models.yaml routing.thinking (per task, else per the model's tier) unless the caller set it
        effort = None if "reasoning_effort" in params or models is None else \
            models.routing.thinking.effort(d.task, spec.tier)
        return {
            **({"reasoning_effort": effort} if effort else {}),
            "model": spec.litellm_model,
            "messages": messages,
            "api_key": self._settings.provider_key(spec.provider),
            "timeout": spec.timeout_s,
            "num_retries": 0,  # fallback across models is our job, explicitly
            "metadata": {  # surfaces in LangSmith via litellm callback
                "run_name": f"{meta.client_id}:{d.task or d.source}",
                "request_id": meta.request_id,
                "topic": meta.client_id,
                "tier": d.tier.value,
                "route_reason": d.reason,
                "prompt_ref": meta.prompt_ref,
            },
            **{k: v for k, v in params.items() if k in PASSTHROUGH_PARAMS},
        }

    @staticmethod
    def _is_fatal(exc: Exception) -> bool:
        return isinstance(exc, _FATAL) and not isinstance(exc, _RETRYABLE_BAD_REQUEST)

    # ---------------------------------------------------------------- non-streaming
    async def complete(
        self, decision: RouteDecision, models: ModelsFile, messages: list[dict], params: dict[str, Any], meta: CallMeta
    ) -> tuple[Any, Outcome]:
        needs = _needs(params)
        last: Exception | None = None
        candidates = self.plan(decision, models, needs, meta.request_id)

        key = None
        if self._cache is not None and cacheable(params, models.cache):
            tier, model_id, spec, is_canary = candidates[0]
            key = cache_key(model_id, messages, {k: v for k, v in params.items() if k in PASSTHROUGH_PARAMS})
            started = time.perf_counter()
            if (hit := await self._cache.get(key)) is not None:
                await self._recorder.record(self._record(
                    meta, decision, tier, model_id, 0, "success", None, 0, 0, 0.0, started, False,
                    canary=is_canary, cached=True,
                ))
                return litellm.ModelResponse(**hit), Outcome(model_id, spec, tier, 0, canary=is_canary, cached=True)

        for attempt, (tier, model_id, spec, is_canary) in enumerate(candidates, start=1):
            started = time.perf_counter()
            try:
                resp = await litellm.acompletion(**self._kwargs(spec, messages, params, meta, decision, models))
            except Exception as exc:
                await self._recorder.record(
                    self._record(meta, decision, tier, model_id, attempt, "error", type(exc).__name__, 0, 0, 0.0,
                                 started, False, canary=is_canary)
                )
                if self._is_fatal(exc):
                    raise BadRequestError(f"Request rejected by model provider: {_short(exc)}") from exc
                logger.warning("model attempt failed", extra={"model": model_id, "error": type(exc).__name__})
                last = exc
                continue
            usage = getattr(resp, "usage", None)
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            await self._recorder.record(
                self._record(meta, decision, tier, model_id, attempt, "success", None, pt, ct,
                             self._cost(spec, resp, pt, ct), started, False, canary=is_canary)
            )
            if key is not None and attempt == 1:  # cache only the planned model's answer
                await self._cache.put(key, resp.model_dump(exclude_none=True), models.cache)  # type: ignore[union-attr]
            return resp, Outcome(model_id=model_id, spec=spec, tier=tier, attempts=attempt, canary=is_canary)
        raise ServiceUnavailableError(f"All candidate models failed ({type(last).__name__ if last else 'none'})")

    # ---------------------------------------------------------------- streaming
    async def stream(
        self, decision: RouteDecision, models: ModelsFile, messages: list[dict], params: dict[str, Any], meta: CallMeta
    ) -> tuple[AsyncIterator[Any], Outcome]:
        """Fallback is only possible before the first chunk; after that we're committed."""
        needs = _needs(params)
        last: Exception | None = None
        for attempt, (tier, model_id, spec, is_canary) in enumerate(self.plan(decision, models, needs, meta.request_id), start=1):
            started = time.perf_counter()
            try:
                kwargs = self._kwargs(spec, messages, params, meta, decision, models)
                stream = await litellm.acompletion(**kwargs, stream=True, stream_options={"include_usage": True})
                iterator = stream.__aiter__()
                first = await iterator.__anext__()
            except StopAsyncIteration:
                last = RuntimeError("empty stream")
                continue
            except Exception as exc:
                await self._recorder.record(
                    self._record(meta, decision, tier, model_id, attempt, "error", type(exc).__name__, 0, 0, 0.0,
                                 started, True, canary=is_canary)
                )
                if self._is_fatal(exc):
                    raise BadRequestError(f"Request rejected by model provider: {_short(exc)}") from exc
                last = exc
                continue
            outcome = Outcome(model_id=model_id, spec=spec, tier=tier, attempts=attempt, canary=is_canary)
            return self._relay(first, iterator, outcome, decision, messages, meta, started), outcome
        raise ServiceUnavailableError(f"All candidate models failed ({type(last).__name__ if last else 'none'})")

    async def _relay(self, first: Any, iterator: AsyncIterator[Any], outcome: Outcome, decision: RouteDecision,
                     messages: list[dict], meta: CallMeta, started: float) -> AsyncIterator[Any]:
        pt = ct = 0
        text: list[str] = []
        status, error = "success", None
        try:
            chunk = first
            while True:
                usage = getattr(chunk, "usage", None)
                if usage:
                    pt = int(getattr(usage, "prompt_tokens", 0) or pt)
                    ct = int(getattr(usage, "completion_tokens", 0) or ct)
                for choice in getattr(chunk, "choices", None) or []:
                    delta = getattr(choice, "delta", None)
                    if delta is not None and getattr(delta, "content", None):
                        text.append(delta.content)
                yield chunk
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
        except Exception as exc:
            status, error = "error", type(exc).__name__
            raise
        finally:
            if pt == 0:  # provider didn't report usage: estimate rather than bill zero
                pt = _estimate(outcome.spec.litellm_model, messages=messages)
                ct = _estimate(outcome.spec.litellm_model, text="".join(text))
            cost = outcome.spec.cost(pt, ct)
            if cost is None:
                try:
                    in_cost, out_cost = litellm.cost_per_token(
                        model=outcome.spec.litellm_model, prompt_tokens=pt, completion_tokens=ct
                    )
                    cost = float(in_cost + out_cost)
                except Exception:
                    cost = 0.0
            await self._recorder.record(
                self._record(meta, decision, outcome.tier, outcome.model_id, outcome.attempts, status,
                             error, pt, ct, cost, started, True, canary=outcome.canary)
            )


def _needs(params: dict[str, Any]) -> set[str]:
    needs: set[str] = set()
    if params.get("tools"):
        needs.add("tools")
    rf = params.get("response_format")
    if isinstance(rf, dict) and rf.get("type") in {"json_object", "json_schema"}:
        needs.add("json")
    return needs


def _estimate(model: str, *, messages: list[dict] | None = None, text: str | None = None) -> int:
    try:
        if messages is not None:
            return int(litellm.token_counter(model=model, messages=messages))
        return int(litellm.token_counter(model=model, text=text or ""))
    except Exception:
        raw = text if text is not None else str(messages)
        return max(1, len(raw) // 4)


def _short(exc: Exception) -> str:
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
