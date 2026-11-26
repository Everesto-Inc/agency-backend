"""Guardrail events: every time a guardrail fires, count it (Prometheus) and log it (MySQL guardrail_events).

    await guardrail("injection", "flagged", topic="ev_research", detail="patterns: ignore_instructions")

Never pass the user's text as detail — only what fired (pattern names, counts, a domain, a field name).
Best-effort: logging a guardrail must never break the request it protects. User and request ids default to
the current request's context.
"""
import logging
from typing import Literal

from prometheus_client import Counter

from agency_platform.config.settings import get_settings
from agency_platform.observability.logging import request_id_var, user_id_var

logger = logging.getLogger(__name__)

Kind = Literal[
    "quota",            # daily tokens / cost / topic quota reached
    "rate_limit",       # too many requests per minute
    "plan",             # assistant not in the user's plan / account not activated
    "input_rejected",   # empty, too long
    "injection",        # text trying to override the assistant's instructions (question or picture)
    "pii_masked",       # personal data masked before an LLM call
    "grounding",        # a number the model extracted is not on the source page → dropped
    "unsafe_url",       # internal / non-public address refused
    "upload_refused",   # picture not accepted (type, size, count, daily cap)
    "scope",            # outside what the agency talks about (config/scope.yaml) → polite refusal
]
Action = Literal["blocked", "flagged", "masked", "dropped"]

GUARDRAIL_EVENTS = Counter("agency_guardrail_events_total", "Guardrails that fired", ["service", "kind", "action"])


async def guardrail(kind: Kind, action: Action, *, topic: str | None = None, detail: str = "",
                    user_id: str | None = None, request_id: str | None = None) -> None:
    service = get_settings().service_name
    GUARDRAIL_EVENTS.labels(service, kind, action).inc()
    try:
        from agency_platform.db import get_db  # lazy: no import cycle, and services without a DB just count
        from agency_platform.repositories.guardrails import GuardrailRepository
        await GuardrailRepository(get_db()).record(
            service=service, kind=kind, action=action, topic=topic, user_id=user_id or user_id_var.get(),
            request_id=request_id or request_id_var.get(), detail=detail)
    except Exception:
        logger.warning("guardrail event not stored", extra={"kind": kind, "action": action}, exc_info=True)
