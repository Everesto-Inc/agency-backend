"""Test doubles shared by every service's test suite (no network, no DB)."""
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from agency_platform.agents.base import RunContext
from agency_platform.auth.deps import AuthenticatedUser
from agency_platform.config.prompts import PromptStore
from agency_platform.llm.client import ChatResult, OpenAICompatConfig

Responder = Callable[[str, list[dict[str, Any]]], str | dict]


class FakeLLM:
    """Scripted stand-in for ModelGatewayClient, keyed by task name."""

    def __init__(self, responses: dict[str, Responder | str | dict]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []
        self.call_token = "fake-call-token"  # noqa: S105

    def openai_compat(self, *, task: str | None = None, tier: Any = None) -> OpenAICompatConfig:
        return OpenAICompatConfig("http://fake-gateway/v1", self.call_token, f"task-{task}" if task else "auto")

    def _answer(self, task: str | None, messages: list[dict[str, Any]]) -> str:
        key = task or "auto"
        if key not in self._responses:
            raise AssertionError(f"FakeLLM: no scripted response for task '{key}'")
        r = self._responses[key]
        r = r(key, messages) if callable(r) else r
        return json.dumps(r) if isinstance(r, dict) else r

    async def chat(self, messages: list[dict[str, Any]], *, task: str | None = None, **kw: Any) -> ChatResult:
        self.calls.append({"task": task, "messages": messages, **kw})
        content = self._answer(task, messages)
        return ChatResult(content, "fake-model", "low", "scripted", {}, {})

    async def structured(self, messages: list[dict[str, Any]], schema: type[BaseModel], *, task: str | None = None, **kw: Any):
        self.calls.append({"task": task, "messages": messages, "schema": schema.__name__})
        return schema.model_validate(json.loads(self._answer(task, messages)))


def fake_context(
    topic: str,
    llm: FakeLLM,
    prompts_dir: Path,
    *,
    plan: str = "pro",
    history: list[dict[str, str]] | None = None,
) -> RunContext:
    return RunContext(
        request_id="test-request-0001",
        session_id="s1",
        topic=topic,
        user=AuthenticatedUser(id="00000000-0000-0000-0000-000000000001", email="t@example.com", role="user", plan=plan),
        llm=llm,  # type: ignore[arg-type]
        http=httpx.AsyncClient(),
        prompts=PromptStore(prompts_dir),
        history=history or [],
    )
