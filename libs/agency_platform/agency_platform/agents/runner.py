"""TopicRunner: the uniform LLMOps envelope around every agent run.

    preflight()  -> entitlement (subscription), rate limit, daily token/cost
                    quota, per-topic request and token budgets. Runs BEFORE the
                    response starts so failures map to proper HTTP statuses.
    stream()     -> response cache (identical first-turn questions), else runs
                    the agent in a producer task with a hard timeout, relaying
                    events (with SSE heartbeats); then records the run
                    (agent_runs, PII-redacted), real usage (aggregated by the
                    model gateway per request id), per-user/per-topic daily
                    rollups, metrics, online-eval sampling and session memory --
                    even if the client disconnects.
"""
import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

import httpx

from agency_platform.agents.base import AgentEvent, RunContext, TopicAgent
from agency_platform.agents.response_cache import CachedAnswer, ResponseCache
from agency_platform.agents.sessions import SessionStore, messages_to_fold, turns_from_runs
from agency_platform.auth.deps import AuthenticatedUser
from agency_platform.billing.usage import RateLimiter, UsageMeter
from agency_platform.config.prompts import PromptStore
from agency_platform.config.registry import evals, privacy, topics
from agency_platform.config.schemas import TopicConfig
from agency_platform.config.settings import PlatformSettings
from agency_platform.errors import AppError, RateLimitedError, ServiceUnavailableError
from agency_platform.llm.call_token import CallClaims, mint_call_token
from agency_platform.llm.client import ModelGatewayClient
from agency_platform.observability.metrics import AGENT_RUN_LATENCY, AGENT_RUNS, CACHE_EVENTS, QUOTA_REJECTIONS
from agency_platform.repositories import ConversationRepository, RunRepository, RunResult
from agency_platform.security.content import clean_user_prompt
from agency_platform.security.pii import redact

logger = logging.getLogger(__name__)

HEARTBEAT = AgentEvent(type="status", content="", data={"heartbeat": True})
_END = object()
_MAX_STORED_ANSWER = 20_000


@dataclass(frozen=True)
class RunRequest:
    user: AuthenticatedUser
    prompt: str
    session_id: str
    request_id: str


@dataclass
class _Outcome:
    status: str = "success"
    error_code: str | None = None
    cache_hit: bool = False


class TopicRunner:
    def __init__(
        self,
        agent: TopicAgent,
        settings: PlatformSettings,
        *,
        runs: RunRepository,
        meter: UsageMeter,
        limiter: RateLimiter,
        sessions: SessionStore,
        prompts: PromptStore,
        http: httpx.AsyncClient,
        cache: ResponseCache | None = None,
        heartbeat_s: float = 15.0,
        conversations: ConversationRepository | None = None,
    ) -> None:
        self.agent = agent
        self._settings = settings
        self._runs = runs
        self._meter = meter
        self._limiter = limiter
        self._sessions = sessions
        self._prompts = prompts
        self._http = http
        self._cache = cache
        self._heartbeat_s = heartbeat_s
        self._conversations = conversations   # running chat summary (memory layer 2); None = off
        self._background: set[asyncio.Task] = set()

    @property
    def topic(self) -> str:
        return self.agent.topic

    def topic_config(self) -> TopicConfig:
        cfg = topics().topics.get(self.topic)
        if cfg is None or not cfg.enabled:
            raise ServiceUnavailableError(f"Topic '{self.topic}' is disabled")
        return cfg

    # ------------------------------------------------------------------ preflight
    async def preflight(self, user: AuthenticatedUser, prompt: str) -> str:
        cfg = self.topic_config()
        cleaned = clean_user_prompt(prompt, cfg.max_prompt_chars)
        try:
            user.require_topic(self.topic)
            plan = user.plan_config
            await self._limiter.hit(f"user:{user.id}", plan.requests_per_minute, 60)
            await self._meter.check_daily_quota(user.id, user.plan, plan)
            await self._meter.check_topic_quota(user.id, self.topic, plan)
            await self._meter.check_topic_tokens(user.id, self.topic, plan)
        except (RateLimitedError, AppError) as exc:
            QUOTA_REJECTIONS.labels(self.topic, exc.code).inc()
            raise
        return cleaned

    # ------------------------------------------------------------------ stream
    async def stream(self, req: RunRequest) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        producer = asyncio.create_task(self._produce(req, queue))
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=self._heartbeat_s)
                except TimeoutError:
                    yield HEARTBEAT
                    continue
                if item is _END:
                    break
                yield item
        finally:
            if not producer.done():
                producer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer

    async def collect(self, req: RunRequest) -> dict:
        """Non-streaming mode: run to completion and return one JSON document."""
        answer: list[str] = []
        artifacts: dict[str, object] = {}
        steps: list[str] = []
        errors: list[dict] = []
        usage: dict = {}
        status, cache_hit = "success", False
        async for ev in self.stream(req):
            if ev.type == "token":
                answer.append(ev.content)
            elif ev.type == "artifact":
                artifacts[ev.content] = ev.data.get("payload")
            elif ev.type == "status" and ev.content:
                steps.append(ev.content)
            elif ev.type == "error":
                errors.append({"code": ev.data.get("code"), "message": ev.content})
            elif ev.type == "usage":
                usage = ev.data
            elif ev.type == "done":
                status = ev.data.get("status", status)
                cache_hit = ev.data.get("cache_hit", False)
        return {
            "request_id": req.request_id,
            "session_id": req.session_id,
            "topic": self.topic,
            "status": status,
            "cache_hit": cache_hit,
            "answer": "".join(answer),
            "artifacts": artifacts,
            "steps": steps,
            "errors": errors,
            "usage": usage,
        }

    async def _produce(self, req: RunRequest, queue: asyncio.Queue) -> None:
        cfg = self.topic_config()
        started = time.perf_counter()
        outcome = _Outcome()
        answer_parts: list[str] = []
        artifacts: dict[str, object] = {}
        steps: list[str] = []
        saw_error = False
        fingerprint = self._prompts.fingerprint(self.topic, req.user.id)

        async def emit(ev: AgentEvent) -> None:
            await queue.put(ev)

        ctx: RunContext | None = None
        summary: str | None = None
        summary_upto: datetime | None = None
        try:
            await self._record_start(req, fingerprint)
            history = await self._memory(req)
            summary, summary_upto = await self._summary(req)
            cacheable = self._cache is not None and cfg.response_cache_ttl_s > 0 and not history
            cache_key = (
                self._cache.key(self.topic, fingerprint, req.user.plan_config.max_tier, req.prompt) if cacheable else None
            )

            if cache_key and (hit := await self._cache.get(cache_key)):  # type: ignore[union-attr]
                outcome.cache_hit = True
                CACHE_EVENTS.labels("response", self.topic, "hit").inc()
                await emit(AgentEvent.status("Answered from a recent identical request", cache_hit=True))
                for name, payload in hit.artifacts.items():
                    await emit(AgentEvent.artifact(name, payload))
                answer_parts.append(hit.answer)
                await emit(AgentEvent.answer(hit.answer, cache_hit=True))
                return
            if cache_key:
                CACHE_EVENTS.labels("response", self.topic, "miss").inc()

            ctx = self._build_context(req, cfg, history, summary)
            async with asyncio.timeout(cfg.run_timeout_s):
                async for ev in self.agent.run(req.prompt, ctx):
                    if ev.type == "token":
                        answer_parts.append(ev.content)
                    elif ev.type == "artifact":
                        artifacts[ev.content] = ev.data.get("payload")
                    elif ev.type == "status" and ev.content:
                        steps.append(ev.content)
                    elif ev.type == "error":
                        saw_error = True
                        outcome.error_code = ev.data.get("code", "AGENT_ERROR")
                    await emit(ev)
            if saw_error and not answer_parts:
                outcome.status = "error"
            elif cache_key and not saw_error and answer_parts:
                await self._cache.put(  # type: ignore[union-attr]
                    cache_key, CachedAnswer("".join(answer_parts), artifacts), cfg.response_cache_ttl_s
                )
        except TimeoutError:
            outcome.status, outcome.error_code = "timeout", "TIMEOUT"
            await emit(AgentEvent.error(f"Run exceeded {cfg.run_timeout_s}s and was stopped", code="TIMEOUT"))
        except asyncio.CancelledError:
            outcome.status, outcome.error_code = "cancelled", "CLIENT_DISCONNECTED"
            raise
        except AppError as exc:
            outcome.status, outcome.error_code = "error", exc.code
            await emit(AgentEvent.error(exc.message, code=exc.code))
        except Exception:
            logger.exception("agent run failed", extra={"topic": self.topic})
            outcome.status, outcome.error_code = "error", "AGENT_ERROR"
            await emit(AgentEvent.error("The agent failed unexpectedly. Please try again."))
        finally:
            latency_s = time.perf_counter() - started
            usage: dict = {}
            try:
                # Shield bookkeeping so a client disconnect can't skip accounting.
                usage = await asyncio.shield(self._finalise(req, outcome, latency_s, "".join(answer_parts)))
            except Exception:
                logger.exception("run finalisation failed", extra={"topic": self.topic})
            if outcome.status != "cancelled":
                await emit(AgentEvent(type="usage", data=usage))
                await emit(AgentEvent(type="done", data={
                    "status": outcome.status, "latency_ms": int(latency_s * 1000), "cache_hit": outcome.cache_hit,
                    "request_id": req.request_id,
                }))
                await queue.put(_END)
            if outcome.status == "success" and ctx is not None and self._conversations is not None:
                # after the answer, never before it: fold older messages into the chat's summary (cheap model)
                task = asyncio.create_task(self._update_summary(req, ctx, summary, summary_upto))
                self._background.add(task)
                task.add_done_callback(self._background.discard)

    async def _memory(self, req: RunRequest) -> list[dict[str, str]]:
        """The chat's memory: Redis (fast, 24 h); when empty, the chat's stored turns from MySQL, put back into Redis."""
        history = await self._sessions.history(req.user.id, req.session_id)
        if history:
            return history
        try:
            runs = await self._runs.chat_runs(user_id=req.user.id, session_id=req.session_id,
                                              exclude_request_id=req.request_id, limit=self._sessions.max_turns // 2)
        except Exception:
            logger.warning("chat memory reload failed", extra={"topic": self.topic}, exc_info=True)
            return []
        history = turns_from_runs(runs)
        if history:
            await self._sessions.append(req.user.id, req.session_id, *history)
        return history

    async def _summary(self, req: RunRequest) -> tuple[str | None, datetime | None]:
        if self._conversations is None:
            return None, None
        try:
            return await self._conversations.summary(req.user.id, req.session_id)
        except Exception:
            logger.warning("chat summary read failed", extra={"topic": self.topic}, exc_info=True)
            return None, None

    async def _update_summary(self, req: RunRequest, ctx: RunContext, previous: str | None,
                              upto: datetime | None) -> None:
        """Fold the older messages not yet summarized into the running summary (low tier, ≤ 300 tokens)."""
        try:
            fold = messages_to_fold(await self._sessions.history(req.user.id, req.session_id), upto)
            if not fold:
                return
            text = await self._write_summary(ctx, previous, fold)
            if text:
                await self._conversations.set_summary(  # type: ignore[union-attr]
                    req.user.id, req.session_id, text, datetime.fromisoformat(fold[-1]["at"]))
        except Exception:
            logger.warning("chat summary update failed", extra={"topic": self.topic}, exc_info=True)

    async def _write_summary(self, ctx: RunContext, previous: str | None, fold: list[dict[str, str]]) -> str:
        p = self._prompts.get("memory", "conversation_summary", subject=ctx.user.id)
        lines = "\n".join(f"{m['role']} ({m.get('topic') or 'assistant'}): {m['content'][:1500]}" for m in fold)
        result = await ctx.llm.chat(
            [{"role": "system", "content": p.system},
             {"role": "user", "content": f"Current summary:\n{previous or '(none yet)'}\n\nNew messages:\n{lines}"}],
            task="summarization", prompt=p,
        )
        return result.content.strip()

    def _build_context(self, req: RunRequest, cfg: TopicConfig, history: list[dict[str, str]],
                       summary: str | None = None) -> RunContext:
        secret = self._settings.model_gateway_client_secret
        if secret is None:
            raise ServiceUnavailableError("MODEL_GATEWAY_CLIENT_SECRET is not configured")
        token = mint_call_token(
            CallClaims(
                client_id=self.topic,
                user_id=req.user.id,
                plan=req.user.plan,
                request_id=req.request_id,
                session_id=req.session_id,
            ),
            secret.get_secret_value(),
            ttl_s=cfg.run_timeout_s + 60,
        )
        return RunContext(
            request_id=req.request_id,
            session_id=req.session_id,
            topic=self.topic,
            user=req.user,
            llm=ModelGatewayClient(self._settings.model_gateway_url, token, self._http),
            http=self._http,
            prompts=self._prompts,
            history=history,
            summary=summary,
        )

    @staticmethod
    def _store_text(text: str) -> str:
        pii = privacy().pii
        return redact(text, pii.entities)[0] if pii.redact_in_storage else text

    async def _record_start(self, req: RunRequest, fingerprint: str) -> None:
        await self._runs.start(
            request_id=req.request_id,
            user_id=req.user.id,
            topic=self.topic,
            session_id=req.session_id,
            prompt=self._store_text(req.prompt),
            prompt_fingerprint=fingerprint,
        )

    async def _finalise(self, req: RunRequest, outcome: _Outcome, latency_s: float, answer: str) -> dict:
        AGENT_RUNS.labels(self.topic, outcome.status).inc()
        AGENT_RUN_LATENCY.labels(self.topic).observe(latency_s)
        usage = await self._meter.run_usage(req.request_id)
        success = outcome.status == "success"
        sampled = success and not outcome.cache_hit and random.random() < evals().online.sample_rate  # noqa: S311
        try:
            await self._runs.finish(req.request_id, RunResult(
                status=outcome.status,
                error_code=outcome.error_code,
                latency_ms=int(latency_s * 1000),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=usage.cost_usd,
                llm_calls=usage.llm_calls,
                answer_chars=len(answer),
                answer=self._store_text(answer[:_MAX_STORED_ANSWER]) if answer else None,
                cache_hit=outcome.cache_hit,
                eval_sampled=sampled,
            ))
            await self._runs.count_daily_run(
                user_id=req.user.id, topic=self.topic, success=success, cached=outcome.cache_hit
            )
            if success:
                await self._meter.count_topic_request(req.user.id, self.topic)
                await self._sessions.append(  # one memory per chat, shared by every assistant
                    req.user.id,
                    req.session_id,
                    {"role": "user", "content": req.prompt, "topic": self.topic},
                    {"role": "assistant", "content": answer[:8000], "topic": self.topic},
                )
        except Exception:
            logger.exception("failed to finalise run record", extra={"topic": self.topic})
        return {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": round(usage.cost_usd, 6),
            "llm_calls": usage.llm_calls,
        }
