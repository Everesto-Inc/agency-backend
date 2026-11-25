"""Hot-reloadable, schema-validated parameter files (config/*.yaml).

A "parameter" is anything an operator should be able to tune without a code
change or redeploy (model tiers, plan limits, staleness windows, ...). Files
are mounted read-only (docker volume locally, ConfigMap in Kubernetes) and
re-read when their mtime changes.

Safety: a reload that fails validation is rejected and the *last known good*
version keeps serving, so a typo in a ConfigMap can't take the fleet down.

Overrides (Admin → Settings): a dict merged over the file (config/overrides.py keeps it in sync from MySQL).
An override that doesn't validate together with the file is ignored — the file alone keeps serving.

Drafts (testing a config before it goes live): a request may carry a draft (config.drafts.current_draft) — then
get() returns file + that draft's values for THIS request only. Live traffic never sees a draft.
"""
import logging
import threading
from pathlib import Path
from typing import Any, Generic, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


def deep_merge(base: Any, over: Any) -> Any:
    """Pure: `over` on top of `base` — dicts merge key by key, anything else is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = deep_merge(base.get(k), v) if k in base else v
        return out
    return over


def diff(base: Any, desired: Any) -> Any:
    """Pure: the smallest override that turns `base` into `desired` (dicts: only changed keys; else whole value).
    Returns {} when nothing differs (at the top level)."""
    if isinstance(base, dict) and isinstance(desired, dict):
        out = {}
        for k, v in desired.items():
            if k not in base:
                out[k] = v
            elif base[k] != v:
                out[k] = diff(base[k], v)
        return out
    return desired


class ParamFileError(RuntimeError):
    """Raised when a parameter file can't be loaded and no good version exists."""


class ParamFile(Generic[M]):
    """One YAML file bound to a pydantic model, reloaded when it changes on disk."""

    def __init__(self, path: Path, model: type[M]) -> None:
        self._path = path
        self._model = model
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._value: M | None = None
        self._raw: dict[str, Any] = {}
        self._override: dict[str, Any] = {}
        self.override_rejected: str | None = None   # why the current override is not applied (None = applied)
        self.section: str | None = None              # Admin → Settings section name (set by config.overrides)
        self._draft_cache: dict[int, M] = {}

    @property
    def path(self) -> Path:
        return self._path

    @property
    def model(self) -> type[M]:
        return self._model

    def file_values(self) -> dict[str, Any]:
        """The file as written (the defaults Admin → Settings edits on top of)."""
        self._live()
        return self._raw

    @property
    def override(self) -> dict[str, Any]:
        return self._override

    def set_override(self, override: dict[str, Any] | None) -> None:
        with self._lock:
            if (override or {}) == self._override:
                return
            self._override = dict(override or {})
            self._mtime = None          # recompute on the next get()

    def validate_with(self, override: dict[str, Any]) -> M:
        """Raises ValidationError if file + override isn't valid (used before saving an override)."""
        return self._model.model_validate(deep_merge(self.file_values(), override))

    def get(self) -> M:
        if self.section is not None:
            from agency_platform.config.drafts import current_draft  # lazy: no import cycle
            draft = current_draft()
            if draft is not None and self.section in draft.sections:
                return self._draft_value(draft.id, draft.sections[self.section])
        return self._live()

    def _draft_value(self, draft_id: int, override: dict[str, Any]) -> M:
        if draft_id not in self._draft_cache:
            try:
                self._draft_cache[draft_id] = self.validate_with(override)
            except ValidationError:
                logger.error("draft not valid for this file; serving live", extra={"path": str(self._path), "draft": draft_id})
                return self._live()
            if len(self._draft_cache) > 20:
                self._draft_cache.pop(next(iter(self._draft_cache)))
        return self._draft_cache[draft_id]

    def _live(self) -> M:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError as exc:
            if self._value is not None:
                logger.error("param file vanished, serving last good", extra={"path": str(self._path)})
                return self._value
            raise ParamFileError(f"Parameter file not found: {self._path}") from exc

        if self._value is not None and mtime == self._mtime:
            return self._value

        with self._lock:
            if self._value is not None and mtime == self._mtime:
                return self._value
            try:
                raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
                value = self._model.model_validate(raw)       # the file alone must always be valid
                self._raw = raw
                self.override_rejected = None
                if self._override:
                    try:
                        value = self._model.model_validate(deep_merge(raw, self._override))
                    except ValidationError as exc:
                        self.override_rejected = str(exc)[:500]
                        logger.error("settings override rejected; serving the file alone",
                                     extra={"path": str(self._path), "error": self.override_rejected})
            except (yaml.YAMLError, ValidationError) as exc:
                if self._value is not None:
                    logger.error(
                        "invalid param reload rejected; keeping last good",
                        extra={"path": str(self._path), "error": str(exc)},
                    )
                    self._mtime = mtime  # don't re-parse the same broken file every call
                    return self._value
                raise ParamFileError(f"Invalid parameter file {self._path}: {exc}") from exc
            if self._value is not None:
                logger.info("param file reloaded", extra={"path": str(self._path)})
            self._value, self._mtime = value, mtime
            return value
