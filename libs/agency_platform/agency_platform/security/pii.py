"""PII detection and redaction (regex + checksum validation, no external calls).

Applied in two places:
  * model gateway: before ANY message leaves for an LLM provider (all frameworks)
  * storage: prompts/answers persisted in agent_runs and eval cases

Placeholders are typed (``[EMAIL]``, ``[CARD]``...) so the model still knows
something was there. Detectors are deliberately conservative (checksums for
cards/IBANs) to avoid mangling prices, model numbers and specs.
"""
import re
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# International / local phone numbers: needs a + or a separator pattern so plain
# numbers like prices, years or kWh values are never matched.
_PHONE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?|\d{2,4}[\s.\-])\d{3,4}[\s.\-]\d{3,4}(?![\w.])"
)
_CARD = re.compile(r"(?<!\d)\d(?:[ \-]?\d){12,18}(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    rearranged = s[4:] + s[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return int(numeric) % 97 == 1


def _card(m: re.Match) -> str | None:
    digits = re.sub(r"\D", "", m.group())
    return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else None


def _iban(m: re.Match) -> str | None:
    return "[IBAN]" if _iban_ok(m.group()) else None


_DETECTORS: dict[str, tuple[re.Pattern, Callable[[re.Match], str | None]]] = {
    "email": (_EMAIL, lambda m: "[EMAIL]"),
    "iban": (_IBAN, _iban),
    "credit_card": (_CARD, _card),
    "ssn": (_SSN, lambda m: "[SSN]"),
    "phone": (_PHONE, lambda m: "[PHONE]"),
    "ip_address": (_IPV4, lambda m: "[IP]"),
}
_ORDER = ["email", "iban", "credit_card", "ssn", "phone", "ip_address"]


def redact(text: str, entities: Iterable[str]) -> tuple[str, Counter]:
    """Return (redacted_text, counts per entity type)."""
    counts: Counter = Counter()
    wanted = set(entities)
    for name in _ORDER:
        if name not in wanted:
            continue
        pattern, replacer = _DETECTORS[name]

        def _sub(m: re.Match, _name: str = name, _replacer=replacer) -> str:
            out = _replacer(m)
            if out is None:
                return m.group()
            counts[_name] += 1
            return out

        text = pattern.sub(_sub, text)
    return text, counts


def redact_messages(messages: list[dict[str, Any]], entities: Iterable[str]) -> tuple[list[dict[str, Any]], Counter]:
    """Redact string and text-part contents of OpenAI-format messages (copy, not in place)."""
    total: Counter = Counter()
    entities = list(entities)
    out: list[dict[str, Any]] = []
    for msg in messages:
        new = dict(msg)
        content = msg.get("content")
        if isinstance(content, str):
            new["content"], c = redact(content, entities)
            total += c
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text, c = redact(part["text"], entities)
                    total += c
                    part = {**part, "text": text}
                parts.append(part)
            new["content"] = parts
        out.append(new)
    return out, total
