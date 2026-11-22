"""Hot-reloadable, schema-validated parameter files (config/*.yaml).

A "parameter" is anything an operator should be able to tune without a code
change or redeploy (model tiers, plan limits, staleness windows, ...). Files
are mounted read-only (docker volume locally, ConfigMap in Kubernetes) and
re-read when their mtime changes.

Safety: a reload that fails validation is rejected and the *last known good*
version keeps serving, so a typo in a ConfigMap can't take the fleet down.
"""
import logging
import threading
from pathlib import Path
from typing import Generic, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


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

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> M:
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
                value = self._model.model_validate(raw)
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
