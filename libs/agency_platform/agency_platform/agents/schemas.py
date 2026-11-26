"""Public request/response models shared by every topic service's API."""
from typing import Any, Literal

from pydantic import BaseModel, Field

SESSION_ID_PATTERN = r"^[A-Za-z0-9\-_]{1,64}$"

RunStatus = Literal["success", "error", "timeout", "cancelled"]


class RunBody(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20_000, description="The user's question",
                        examples=["Tesla Model 3 2024 vs diesel in France, 15000 km/year"])
    session_id: str = Field("default", pattern=SESSION_ID_PATTERN,
                            description="Conversation id: runs with the same id share memory (follow-ups)")
    stream: bool = Field(True, description="true: Server-Sent Events stream; false: one JSON document (RunResponse)")
    out_of_scope: bool = Field(False, description="Triage: outside config/scope.yaml → a short polite refusal saying "
                                                  "what the agency can do; logged as a guardrail event")
    direct: bool = Field(False, description="Triage decided a short answer is enough (greeting, 'who are you', a "
                                            "simple general question): one short call, no agent workflow")
    skills: list[str] = Field(default_factory=list, max_length=5,
                              description="Triage: enabled skills (Admin → Skills) this question needs — their "
                                          "instructions are added to the assistant's prompt; unknown names are ignored")


class FeedbackBody(BaseModel):
    score: Literal[-1, 0, 1] = Field(..., description="-1 bad, 0 neutral, 1 good")
    comment: str | None = Field(None, max_length=2000, description="What was wrong or right (feeds the eval datasets)")


class RunUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = Field(0.0, description="Provider cost of every LLM call this run made")
    llm_calls: int = 0


class RunError(BaseModel):
    code: str | None = None
    message: str


class RunResponse(BaseModel):
    """Result of one agent run (stream=false). With stream=true the same information
    arrives as SSE events: status, tool_call, tool_result, artifact, token, error, usage, done."""

    request_id: str
    session_id: str
    topic: str
    status: RunStatus
    cache_hit: bool = Field(False, description="Answered from the response cache (identical recent question)")
    answer: str = Field("", description="Markdown answer")
    artifacts: dict[str, Any] = Field(default_factory=dict, description="Structured data behind the answer (sources, numbers)")
    steps: list[str] = Field(default_factory=list, description="What the agent did, in order")
    errors: list[RunError] = Field(default_factory=list)
    usage: RunUsage = Field(default_factory=RunUsage)
    routing: dict[str, Any] | None = Field(None, description="Only via /chat: which topic was chosen and how")


class TopicInfo(BaseModel):
    topic: str
    display_name: str
    framework: str = Field(..., description="Agent framework behind this topic (langgraph, crewai, autogen)")
    description: str
    entitled: bool = Field(..., description="Whether the caller's plan includes this topic")
    plan: str


SSE_EXAMPLE = (
    "event: status\ndata: {\"type\":\"status\",\"content\":\"Parsing the question\",\"data\":{}}\n\n"
    "event: token\ndata: {\"type\":\"token\",\"content\":\"**Answer** ...\",\"data\":{}}\n\n"
    "event: usage\ndata: {\"type\":\"usage\",\"content\":\"\",\"data\":{\"total_tokens\":9087,\"cost_usd\":0.0035}}\n\n"
    "event: done\ndata: {\"type\":\"done\",\"content\":\"\",\"data\":{\"status\":\"success\",\"request_id\":\"...\"}}\n\n"
)
