"""Admin → Settings, live in every service: overrides from MySQL merged over config/*.yaml.

    save (edge)  → settings_versions row + INCR <prefix>settings:rev in Redis
    every service: a background loop checks that counter every POLL_S seconds; when it changed, it loads the
                   newest version per name and hands it to the matching ParamFile (validated there).

So a change is live everywhere within ~POLL_S seconds, survives restarts (it's in MySQL), and a bad value can't
take a service down (ParamFile keeps serving the file alone if file + override doesn't validate).
"""
import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from agency_platform.config.params import ParamFile
from agency_platform.config.registry import (
    evals_file,
    guardrails_file,
    memory_file,
    models_file,
    plans_file,
    privacy_file,
    prompt_store,
    scope_file,
    topics_file,
)

logger = logging.getLogger(__name__)

POLL_S = 10
REV_KEY = "settings:rev"


@dataclass(frozen=True)
class Section:
    name: str
    title: str
    description: str
    file: Callable[[], ParamFile]


_BASE = [
    Section("plans", "Plans & limits", "Daily tokens / cost, requests per minute, which assistants each plan has, "
                                       "the plan new accounts get.", plans_file),
    Section("models", "Models & routing", "Which models exist (cost, timeout), which model serves each tier, "
                                          "which tier each task gets, canaries.", models_file),
    Section("topics", "Assistants & routing", "Which assistants exist and are on, timeouts, keywords, small-talk "
                                              "phrases, whether follow-ups stay with the chat's assistant.", topics_file),
    Section("scope", "Scope (what we talk about)", "The allowed domains, what is not allowed, and how an "
                                                   "out-of-scope reply sounds — triage is built from it.", scope_file),
    Section("guardrails", "Guardrails", "Injection check (off / flag / block), picture limits.", guardrails_file),
    Section("memory", "Chat memory", "How many messages the assistants read word for word, when older ones are "
                                     "folded into the summary, the fast copy's size and lifetime.", memory_file),
    Section("privacy", "Privacy & retention", "PII masking, how long texts, pictures and logs are kept.", privacy_file),
    Section("evals", "Quality checks", "Online judge sampling, drift and offline eval settings.", evals_file),
]


def sections() -> dict[str, Section]:
    """Every editable section: the config files + one per prompt file (prompts_<topic>)."""
    store = prompt_store()
    out = {s.name: s for s in _BASE}
    for topic in store.topics():
        out[f"prompts_{topic}"] = Section(
            f"prompts_{topic}", f"Prompts · {topic}",
            f"The instructions the assistants get (prompts/{topic}.yaml): system prompts, temperature, "
            "max tokens, A/B experiments.", lambda t=topic: store.file(t))
    return out


async def apply_live(db) -> int:
    """Load the newest version of every section into its ParamFile. Returns how many sections have an override."""
    from agency_platform.repositories.settings import SettingsRepository
    live = await SettingsRepository(db).live()
    for name, section in sections().items():
        section.file().set_override(live.get(name, {}).get("content") or {})
    return sum(1 for v in live.values() if v.get("content"))


async def bump(redis) -> None:
    from agency_platform.cache import rkey
    await redis.incr(rkey(REV_KEY))


class SettingsSync:
    """One per service process."""

    def __init__(self, db, redis, poll_s: float = POLL_S) -> None:
        self._db, self._redis, self._poll = db, redis, poll_s
        self._rev: str | None = "unset"
        self._task: asyncio.Task | None = None

    async def check(self) -> bool:
        from agency_platform.cache import rkey
        rev = await self._redis.get(rkey(REV_KEY))
        if rev == self._rev:
            return False
        n = await apply_live(self._db)
        self._rev = rev
        logger.info("settings overrides applied", extra={"rev": rev, "sections_overridden": n})
        return True

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                logger.warning("settings sync failed; retrying", exc_info=True)
            await asyncio.sleep(self._poll)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="settings-sync")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
