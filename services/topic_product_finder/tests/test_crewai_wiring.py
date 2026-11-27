"""Real CrewAI against a fake OpenAI-compatible model server: the agent calls our tool (CrewAI's worker thread →
the service's event loop and back), sees its result, and answers. No real model, no network beyond localhost."""
import json
import os
import socket
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("PROMPTS_DIR", str(ROOT / "prompts"))
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import uvicorn
from fastapi import FastAPI, Request
from test_research import FakeFX, FakeSearch

from agency_platform.llm.client import OpenAICompatConfig
from agency_platform.testing import FakeLLM, fake_context
from product_finder.crews import CrewAICrews
from product_finder.research import Research

SEEN: list[dict] = []


def fake_model() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        SEEN.append(body)
        tool_results = [m for m in body["messages"] if m.get("role") == "tool"]
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        base = {"id": f"c{len(SEEN)}", "object": "chat.completion", "created": 0, "model": body["model"], "usage": usage}
        if not tool_results:
            call = {"id": "call_1", "type": "function", "function": {
                "name": "search_web", "arguments": json.dumps({"query": "nomad leather folio ipad pro 13"})}}
            return {**base, "choices": [{"index": 0, "finish_reason": "tool_calls",
                                         "message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
        assert "Nomad Leather Folio 119,95" in tool_results[-1]["content"]      # our tool's result reached the model
        return {**base, "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "Nomad Leather Folio: 119,95 € ~ (search snippet)."}}]}
    return app


def serve() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(fake_model(), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/v1"


class LocalLLM(FakeLLM):
    def __init__(self, base_url: str):
        super().__init__({})
        self._base = base_url

    def openai_compat(self, *, task=None, tier=None):
        return OpenAICompatConfig(self._base, "test-token", f"task-{task}")


async def test_crewai_agent_calls_our_tools_and_answers():
    ctx = fake_context("product_finder", LocalLLM(serve()), ROOT / "prompts")
    research = Research(ctx, FakeSearch(), FakeFX(), "FR")
    answer = await CrewAICrews().research("premium leather folio for iPad Pro 13 M4", research, ctx)
    assert answer == "Nomad Leather Folio: 119,95 € ~ (search snippet)."
    assert research.searches == 1 and research.search.calls[0]["query"] == "nomad leather folio ipad pro 13"
    assert any(t["function"]["name"] == "read_page" for t in SEEN[0].get("tools", []))   # native function calling
    assert SEEN[0]["model"] == "task-tool_use"
