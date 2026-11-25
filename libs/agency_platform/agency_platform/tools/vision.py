"""A user's picture → one line of text (cheap vision model), which is all the assistants and the chat memory get.

Why text and not the picture itself: every assistant (LangGraph, CrewAI, AutoGen) works unchanged, follow-up
questions don't pay for the picture again, and the memory stays small. The picture stays in the bucket and is
shown again in the chat history.
"""
import asyncio
import base64

from agency_platform.config.prompts import PromptStore
from agency_platform.llm.client import ModelGatewayClient

MARKER = "\n\n[Pictures the user sent — described by a vision model]"


def prompt_with_pictures(prompt: str, descriptions: list[str], max_chars: int = 20_000) -> str:
    """Pure: the question as the assistants see it (never over max_chars: the question is shortened, the picture
    lines are kept). The history shows only the part before MARKER."""
    if not descriptions:
        return prompt
    lines = "\n".join(f"Picture {i}: {d}" for i, d in enumerate(descriptions, 1))
    suffix = f"{MARKER}\n{lines}"
    return prompt[: max_chars - len(suffix)] + suffix


def question_only(stored_prompt: str | None) -> str | None:
    """Pure: the user's own words (drops the picture descriptions added above)."""
    return stored_prompt.split(MARKER, 1)[0] if stored_prompt else stored_prompt


async def describe(llm: ModelGatewayClient, prompts: PromptStore, user_id: str, question: str,
                   pictures: list[tuple[bytes, str]]) -> list[str]:
    """One vision call per picture, in parallel. A failed one becomes an honest placeholder, never a guess."""
    p = prompts.get("vision", "describe_picture", subject=user_id)

    async def one(data: bytes, content_type: str) -> str:
        url = f"data:{content_type};base64,{base64.b64encode(data).decode()}"
        try:
            result = await llm.chat(
                [{"role": "system", "content": p.system},
                 {"role": "user", "content": [
                     {"type": "text", "text": f"The user's question: {question[:500]}"},
                     {"type": "image_url", "image_url": {"url": url}},
                 ]}],
                task="vision", prompt=p, timeout_s=45,
            )
            text = " ".join(result.content.split())
            return text[:600] or "(the picture could not be described)"
        except Exception:
            return "(the picture could not be described)"

    return list(await asyncio.gather(*(one(d, t) for d, t in pictures)))
