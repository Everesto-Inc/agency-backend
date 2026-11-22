"""LLM-as-judge through the model gateway (task 'eval_judge' -> high tier).

The judge sees the question, the grounded data the agent had (artifacts), an
optional rubric and reference, and the answer -- all fenced as untrusted data,
so an answer can't instruct the judge to give it a 5.
"""
import json
from typing import Any

from pydantic import BaseModel, Field

from agency_platform.config.prompts import PromptStore
from agency_platform.llm.client import ModelGatewayClient
from agency_platform.security.content import fence_untrusted

CRITERIA = ("groundedness", "relevance", "completeness", "safety")


class JudgeVerdict(BaseModel):
    groundedness: int = Field(..., ge=1, le=5)
    relevance: int = Field(..., ge=1, le=5)
    completeness: int = Field(..., ge=1, le=5)
    safety: int = Field(..., ge=1, le=5)
    rationale: str = Field("", max_length=2000)

    @property
    def overall(self) -> float:
        # Groundedness weighs double: an ungrounded answer is the costliest failure here.
        return round((2 * self.groundedness + self.relevance + self.completeness + self.safety) / 5, 2)

    def scores(self) -> dict[str, int]:
        return {c: getattr(self, c) for c in CRITERIA}


class Judge:
    def __init__(self, llm: ModelGatewayClient, prompts: PromptStore, task: str = "eval_judge") -> None:
        self._llm = llm
        self._prompts = prompts
        self._task = task

    async def judge(
        self, *, topic: str, question: str, answer: str, artifacts: dict[str, Any] | None = None,
        rubric: str | None = None, reference: str | None = None,
    ) -> JudgeVerdict:
        p = self._prompts.get("quality", "judge")
        default_rubric = self._prompts.get("quality", f"rubric_{topic}").system if _has(self._prompts, topic) else ""
        body = "\n\n".join(
            part for part in [
                f"QUESTION:\n{question}",
                f"RUBRIC:\n{rubric or default_rubric or 'Answer the question accurately and helpfully.'}",
                f"REFERENCE (known-good facts):\n{reference}" if reference else "",
                "GROUNDED DATA the assistant had:\n"
                + fence_untrusted(json.dumps(artifacts or {}, default=str)[:12000], "agent artifacts"),
                "ANSWER TO EVALUATE:\n" + fence_untrusted(answer[:12000], "agent answer"),
            ] if part
        )
        return await self._llm.structured(
            [{"role": "system", "content": p.system}, {"role": "user", "content": body}],
            JudgeVerdict,
            task=self._task,
            prompt=p,
        )


def _has(store: PromptStore, topic: str) -> bool:
    try:
        store.get("quality", f"rubric_{topic}")
        return True
    except KeyError:
        return False
