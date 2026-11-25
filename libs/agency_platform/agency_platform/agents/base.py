"""The contract every topic agent implements, whatever framework it uses.

A topic agent only does domain work. Everything else (auth, entitlement,
quotas, timeouts, usage accounting, run records, streaming transport,
session memory) is handled around it by the TopicRunner -- so LangGraph,
CrewAI and AutoGen topics all behave identically from the outside.
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

from agency_platform.auth.deps import AuthenticatedUser
from agency_platform.config.prompts import Prompt, PromptStore
from agency_platform.llm.client import ModelGatewayClient

EventType = Literal["status", "tool_call", "tool_result", "artifact", "token", "error", "usage", "done"]


class AgentEvent(BaseModel):
    """Normalised streaming event (SSE `event:` = type, `data:` = this JSON)."""

    type: EventType
    content: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def status(cls, text: str, **data: Any) -> "AgentEvent":
        return cls(type="status", content=text, data=data)

    @classmethod
    def answer(cls, text: str, **data: Any) -> "AgentEvent":
        return cls(type="token", content=text, data=data)

    @classmethod
    def artifact(cls, name: str, payload: Any) -> "AgentEvent":
        return cls(type="artifact", content=name, data={"payload": payload})

    @classmethod
    def error(cls, message: str, code: str = "AGENT_ERROR", **data: Any) -> "AgentEvent":
        return cls(type="error", content=message, data={"code": code, **data})


@dataclass
class RunContext:
    request_id: str
    session_id: str
    topic: str
    user: AuthenticatedUser
    llm: ModelGatewayClient
    http: httpx.AsyncClient
    prompts: PromptStore
    history: list[dict[str, str]] = field(default_factory=list)
    summary: str | None = None   # running summary of the chat's older messages (memory layer 2)

    def prompt(self, name: str) -> Prompt:
        """Resolve a prompt for this user (sticky A/B experiment arm if one is running)."""
        return self.prompts.get(self.topic, name, subject=self.user.id)


class TopicAgent(ABC):
    topic: ClassVar[str]
    framework: ClassVar[str]

    async def startup(self, app: FastAPI) -> None:  # noqa: B027 - optional hook
        """Warm caches, build graphs, open clients."""

    async def shutdown(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""

    @abstractmethod
    def run(self, prompt: str, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        """Stream events for one user request. Must emit at most one final answer
        (type='token'); may emit status/tool/artifact events along the way."""
