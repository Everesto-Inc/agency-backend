"""Structured JSON logging with request correlation and secret redaction."""
import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

# Defence in depth: even if someone logs a header dict or a DSN by mistake,
# credentials don't reach the log pipeline.
_REDACTIONS = [
    (re.compile(r"(?i)(bearer\s+)[a-z0-9\-_\.=]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"), r"\1[REDACTED]"),
    (re.compile(r"(://[^:/\s]+:)[^@\s]+(@)"), r"\1[REDACTED]\2"),
    (re.compile(r"\b(sk|pk|AIza)[A-Za-z0-9_\-]{16,}\b"), "[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if rid := request_id_var.get():
            payload["request_id"] = rid
        if uid := user_id_var.get():
            payload["user_id"] = uid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value if isinstance(value, int | float | bool | None) else redact(str(value))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(service: str, level: str = "INFO", as_json: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if as_json:
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Third-party chatter that would otherwise log prompts/headers verbatim.
    for noisy in ("httpx", "httpcore", "LiteLLM", "litellm", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
