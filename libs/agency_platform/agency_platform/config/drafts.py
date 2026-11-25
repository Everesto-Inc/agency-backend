"""Per-request config drafts: test a config before it goes live (Admin → Experiments).

A draft = {section name: override} stored in MySQL settings_drafts. A request carrying a draft makes every
ParamFile of those sections return file + draft values — for that request only (a ContextVar):

  quality worker ──X-Agency-Config-Draft: 12──▶ topic service (honoured for admin / service accounts only)
  topic service ──call token claim "cfg": 12──▶ model gateway (signed: can't be forged by a caller)

Asyncio tasks copy the context when created, so the agent's background work keeps the draft too.
"""
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

HEADER = "X-Agency-Config-Draft"
_CACHE_S = 60


@dataclass(frozen=True)
class Draft:
    id: int
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)


_current: ContextVar[Draft | None] = ContextVar("config_draft", default=None)
_cache: dict[int, tuple[float, Draft]] = {}


def current_draft() -> Draft | None:
    return _current.get()


def use_draft(draft: Draft | None) -> None:
    _current.set(draft)


def parse_header(value: str | None) -> int | None:
    """Pure: header value → draft id (None if absent or not a positive integer)."""
    if not value or not value.strip().isdigit():
        return None
    n = int(value.strip())
    return n if n > 0 else None


async def load_draft(draft_id: int) -> Draft | None:
    """From MySQL, cached a minute per process (a draft's content never changes once created)."""
    hit = _cache.get(draft_id)
    if hit and time.monotonic() - hit[0] < _CACHE_S:
        return hit[1]
    from agency_platform.db import get_db
    from agency_platform.repositories.settings import SettingsRepository
    row = await SettingsRepository(get_db()).draft(draft_id)
    if row is None:
        return None
    draft = Draft(id=draft_id, sections=row["content"])
    _cache[draft_id] = (time.monotonic(), draft)
    return draft


async def activate(draft_id: int | None) -> Draft | None:
    """Make a draft current for this request (no-op for None / unknown ids)."""
    if draft_id is None:
        return None
    draft = await load_draft(draft_id)
    use_draft(draft)
    return draft
